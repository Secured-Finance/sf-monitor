#!/usr/bin/env python3
"""Shared alert formatting and Slack delivery for every sf-monitor script trigger.

Every alert this repo sends is rendered here, so all of them look the same:

    [<SF_ENV>:<network name>] <Title>
    <optional one-line summary>
    <Label>: <human-readable value>
    ...
    Tx: <explorer link>

That uniformity is the reason the declarative Slack triggers
(config/triggers/slack.json, now removed) were replaced by script triggers for *all*
concerns, not just the two needing computation: OZ Monitor's notification templating is
plain `${...}` substitution over the decoded log, so a declarative trigger can only ever
print a raw event arg. It cannot turn a bytes32 currency into "USDFC", a uint maturity
into a date, or a uint256 amount into a token-decimal figure, and it has no access to
the per-network explorer base URL - all of which need either a lookup table or a live
contract call. See config/triggers/scripts/event_alert.py for the formatting of the
concerns that need nothing but this module.

This module is imported by the trigger scripts rather than duplicated into them. OZ
Monitor runs a trigger script as `python3 -c <file contents>`, so `__file__` is not
defined and the module is not importable by path - but cwd is /app (WORKDIR in the
upstream image) and the whole of config/ is mounted/copied there, so every trigger
script starts by putting this directory on sys.path:

    import os, sys
    sys.path.insert(0, os.path.join("config", "triggers", "scripts"))
    import sf_alerts

Amount formatting needs each currency's ERC20 decimals, which is deliberately resolved
live per match (TokenInfo below) rather than from a generate-time map, for the same
reason total_deposits_check.py resolves the token address live: a currency whose token
was re-registered would make a cached figure silently wrong - off by orders of
magnitude - rather than visibly absent. Unlike the token address, decimals come from a
contract that cannot change them, so they are cached for the life of the process (one
`eth_call` per currency per match, which matters on Filecoin - see the trigger
timeout_ms note in CLAUDE.md).

Everything here is pure formatting except TokenInfo (RPC) and post_slack (HTTP), so the
message layout is unit-testable without either - see tests/test_sf_alerts.py.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, localcontext

# Paths are relative to the container's cwd (/app), as described above - never to
# __file__, which does not exist under `python3 -c`.
NETWORKS_DIR = os.path.join("config", "networks")
SCRIPTS_DIR = os.path.join("config", "triggers", "scripts")
EXPLORERS_PATH = os.path.join(SCRIPTS_DIR, "network_explorers.json")
# Written by `npm run generate` (tools/generate-monitors.ts) for the active SF_ENV, same
# as config/monitors/*.json: scripts needing a contract this monitor does not itself
# watch (TokenVault, for currency -> token resolution, when the matched address is a
# LendingMarket proxy) can't get it from the match payload.
CONTRACTS_PATH = os.path.join(SCRIPTS_DIR, "contract_addresses.json")

# Constants.sol PRICE_DIGIT: unit prices are integers out of 10000, where 10000 is par -
# so 9550 means 95.50% of face value.
PRICE_DIGIT_DECIMALS = 2

SLACK_ATTEMPTS = 3
SLACK_TIMEOUT_SECONDS = 5

TOKEN_VAULT_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_ccy", "type": "bytes32"}],
        "name": "getTokenAddress",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def decode_bytes32_string(hex_str):
    """bytes32 currency code -> ticker, e.g. 0x5553444643...00 -> "USDFC"."""
    raw = bytes.fromhex(hex_str[2:] if hex_str.startswith("0x") else hex_str)
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def to_bytes32(hex_str):
    return bytes.fromhex(hex_str[2:] if hex_str.startswith("0x") else hex_str)


def to_int(value):
    """Match payloads carry numbers as decimal strings, hex strings, or ints."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def format_units(raw, decimals):
    """Scales an integer base-unit amount by `decimals`, for humans.

    Trailing zeros are dropped and thousands separated, so 100000000000000000 at 18
    decimals reads "0.1" and 1250500000 at 6 reads "1,250.5". Exact, never rounded: a
    rounded amount in a safety alert is a misreported amount.
    """
    raw = to_int(raw)
    if decimals == 0:
        return f"{raw:,}"
    with localcontext() as ctx:
        # Enough precision for any uint256 (78 digits) plus its fractional part.
        ctx.prec = 120
        value = Decimal(raw).scaleb(-decimals)
    text = format(value, "f")
    integer_part, _, fraction = text.partition(".")
    fraction = fraction.rstrip("0")
    formatted = f"{int(integer_part):,}"
    return f"{formatted}.{fraction}" if fraction else formatted


def format_timestamp(timestamp):
    """Maturity/epoch seconds -> "December 1, 2026 12:00 AM UTC"."""
    moment = datetime.fromtimestamp(to_int(timestamp), tz=timezone.utc)
    # %-d/%-I (no zero padding) are glibc/musl extensions, as used by the original
    # Defender handler's output - fine on the Linux image this runs on.
    return moment.strftime("%B %-d, %Y %-I:%M %p UTC")


def format_unit_price(raw):
    """Unit price out of Constants.PRICE_DIGIT -> percent of face value."""
    return f"{format_units(raw, PRICE_DIGIT_DECIMALS)}%"


def shorten_hash(value):
    return f"{value[:10]}…{value[-8:]}" if len(value) > 20 else value


def matched_events(match_):
    """Every matched event in this transaction, each as a {name: value} dict.

    matched_on_args.events is a list, one entry per matching log OZ Monitor found in the
    transaction - not a single event. A transaction can carry several matching logs (two
    currencies auto-rolled together, a withdrawal in more than one currency), so callers
    must handle every entry, not just the first.
    """
    events = (match_.get("matched_on_args") or {}).get("events") or []
    return [{arg["name"]: arg["value"] for arg in event["args"]} for event in events]


def read_payload(stream=None):
    """OZ Monitor's script-trigger stdin contract.

    The process is spawned as `python3 -c <script>` with no extra argv - any configured
    trigger "arguments" arrive bundled into the JSON on stdin as `args`, not as
    sys.argv (see services/trigger/script/executor.rs in the OpenZeppelin Monitor
    source). Returns (match, args); the script exits 0 on success, non-zero + stderr on
    failure.
    """
    payload = json.load(stream or sys.stdin)
    return payload["monitor_match"]["EVM"], payload.get("args") or []


class TokenInfo:
    """Resolves a currency's ERC20 token address and decimals, live, cached per process.

    `token_vault_address` is TokenVault: the only contract mapping a protocol currency
    code to its ERC20 (getTokenAddress) - CurrencyController.getDecimals is NOT an
    alternative, it returns the *price feed's* aggregated decimals, not the token's.
    """

    def __init__(self, w3, token_vault_address, block_identifier="latest"):
        self._w3 = w3
        self._vault = w3.eth.contract(
            address=w3.to_checksum_address(token_vault_address), abi=TOKEN_VAULT_ABI
        )
        self._block_identifier = block_identifier
        self._addresses = {}
        self._decimals = {}

    def token_address(self, ccy_hex):
        """The currency's ERC20 address, or None if TokenVault has none registered."""
        if ccy_hex not in self._addresses:
            address = self._vault.functions.getTokenAddress(to_bytes32(ccy_hex)).call(
                block_identifier=self._block_identifier
            )
            self._addresses[ccy_hex] = None if int(address, 16) == 0 else address
        return self._addresses[ccy_hex]

    def decimals(self, ccy_hex):
        """The currency's token decimals, or None if they can't be resolved.

        Returns None rather than guessing 18: an alert that says "amount unscaled" is
        recoverable, one that silently reports a USDFC amount as if it had 18 decimals
        is off by 10^12 and reads as a non-event.
        """
        if ccy_hex not in self._decimals:
            self._decimals[ccy_hex] = self._read_decimals(ccy_hex)
        return self._decimals[ccy_hex]

    def _read_decimals(self, ccy_hex):
        try:
            token_address = self.token_address(ccy_hex)
            if token_address is None:
                return None
            token = self._w3.eth.contract(
                address=self._w3.to_checksum_address(token_address), abi=ERC20_ABI
            )
            # Always at head, not at the matched block: decimals are immutable, and a
            # historic call is one more way for this to fail on a slow RPC.
            return int(token.functions.decimals().call())
        except Exception as error:  # noqa: BLE001 - formatting must survive a bad RPC
            print(
                f"could not resolve decimals for {decode_bytes32_string(ccy_hex)} ({ccy_hex}): {error}",
                file=sys.stderr,
            )
            return None

    def format_amount(self, ccy_hex, raw):
        symbol = decode_bytes32_string(ccy_hex)
        decimals = self.decimals(ccy_hex)
        if decimals is None:
            return format_raw_amount(ccy_hex, raw)
        return f"{format_units(raw, decimals)} {symbol}"


def format_raw_amount(ccy_hex, raw):
    """Fallback when decimals can't be resolved: labelled base units, never a guess."""
    return (
        f"{to_int(raw):,} {decode_bytes32_string(ccy_hex)} "
        "(raw base units - decimals unavailable)"
    )


class UnscaledTokenInfo:
    """Stand-in for TokenInfo when there is no TokenVault address to resolve with.

    Only reachable if contract_addresses.json is missing or doesn't cover this network -
    a stale image, or a config/ tree that wasn't generated. An alert carrying raw base
    units is worth sending; failing the whole trigger over a formatting input is not.
    """

    def format_amount(self, ccy_hex, raw):
        return format_raw_amount(ccy_hex, raw)


class AlertContext:
    """Per-match context: where the alert goes and how to label it.

    Built from the match payload plus the config mounted alongside this script -
    nothing here is hardcoded per network:
      - network slug and the watched addresses: the match payload itself.
      - human-readable network name and RPC env var name: config/networks/<slug>.json,
        the same file OZ Monitor reads.
      - explorer base URL: network_explorers.json next to this script (it can't live in
        config/networks/*.json - OZ Monitor's own Network struct rejects unknown fields).
      - contract addresses this monitor doesn't watch: contract_addresses.json, written
        by the generator for the active SF_ENV.
      - the environment label: SF_ENV, present via docker-compose's `env_file: .env`.
    """

    def __init__(
        self,
        network_slug,
        network_name,
        explorer,
        env_label,
        rpc_url_env=None,
        contracts=None,
    ):
        self.network_slug = network_slug
        self.network_name = network_name
        self.explorer = explorer
        self.env_label = env_label
        self.rpc_url_env = rpc_url_env
        self.contracts = contracts or {}
        self._w3 = None
        self._token_info = None

    @classmethod
    def from_match(cls, match_):
        network_slug = match_["network_slug"]
        with open(os.path.join(NETWORKS_DIR, f"{network_slug}.json")) as f:
            network_config = json.load(f)
        with open(EXPLORERS_PATH) as f:
            explorer = json.load(f)[network_slug]
        contracts = {}
        if os.path.exists(CONTRACTS_PATH):
            with open(CONTRACTS_PATH) as f:
                contracts = json.load(f).get(network_slug, {})
        return cls(
            network_slug=network_slug,
            network_name=network_config["name"],
            explorer=explorer,
            env_label=os.environ.get("SF_ENV", ""),
            rpc_url_env=network_config["rpc_urls"][0]["url"]["value"],
            contracts=contracts,
        )

    @property
    def heading_prefix(self):
        return f"[{self.env_label}:{self.network_name}]"

    def tx_link(self, tx_hash):
        return f"<{self.explorer}/tx/{tx_hash}|{shorten_hash(tx_hash)}>"

    def address_link(self, address):
        return f"<{self.explorer}/address/{address}|{address}>"

    def w3(self):
        """A web3 client for this network, built on first use.

        Imported lazily and built lazily so a concern needing no contract call (auto
        rolls, block unit price) makes no RPC connection at all, and so the pure
        formatting above is importable without web3 installed.
        """
        if self._w3 is None:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(os.environ[self.rpc_url_env].strip()))
        return self._w3

    def token_info(self, token_vault_address=None, block_identifier="latest"):
        """TokenInfo for this network, defaulting to the generated TokenVault address.

        Callers already holding the TokenVault address (total_deposits_check.py, whose
        monitor watches TokenVault itself) pass it in rather than reading it from the
        generated file, so the alert describes exactly the contract that matched.
        """
        if self._token_info is None:
            address = token_vault_address or self.contracts.get("TokenVault")
            if address is None:
                print(
                    f"no TokenVault address for {self.network_slug} in {CONTRACTS_PATH}"
                    " - amounts will be reported in raw base units",
                    file=sys.stderr,
                )
                self._token_info = UnscaledTokenInfo()
            else:
                self._token_info = TokenInfo(self.w3(), address, block_identifier)
        return self._token_info


def render(prefix, title, fields, summary=None):
    """The one message layout every alert from this repo uses."""
    lines = [f"*{prefix} {title}*"]
    if summary:
        lines.append(summary)
    lines.extend(f"{label}: {value}" for label, value in fields)
    return "\n".join(lines)


def post_slack(text):
    """Posts to SLACK_WEBHOOK_URL, retrying a failed delivery.

    The declarative Slack triggers this replaced had upstream's retry policy behind
    them; a bare one-shot POST here would make a dropped connection a silently missed
    alert. Raises on final failure so the trigger exits non-zero and OZ Monitor logs it.
    """
    import requests

    url = os.environ["SLACK_WEBHOOK_URL"].strip()
    last_error = None
    for attempt in range(SLACK_ATTEMPTS):
        try:
            response = requests.post(
                url, json={"text": text}, timeout=SLACK_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            return
        except Exception as error:  # noqa: BLE001 - every failure mode is retryable
            last_error = error
            if attempt + 1 < SLACK_ATTEMPTS:
                time.sleep(1)
    raise RuntimeError(f"failed to deliver Slack alert: {last_error}")


def send_alert(ctx, title, fields, summary=None):
    text = render(ctx.heading_prefix, title, fields, summary)
    print(text)
    post_slack(text)
