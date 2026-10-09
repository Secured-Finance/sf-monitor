"""Shared protocol invariant checks (see invariant-monitoring-spec.md).

Each invariant in the spec is evaluated from two places: an OZ Monitor script trigger,
which runs it on the event that could have broken it, and the periodic sweep, which runs
it over every currency/maturity on a timer as a backstop for anything the event path
misses (a cursor gap, a restart, a dropped block). Writing a check twice is how a monitor
starts lying, so the checks live here once and both callers import them.

Import mechanism: OZ Monitor execs a trigger as `python3 -c <content>` with cwd /app, so
`__file__` is undefined and this directory is not on sys.path automatically. Callers do:

    import sys
    sys.path.insert(0, "config/triggers/scripts")
    import invariants

`./config` is mounted read-only into the container at /app/config (docker-compose.yml),
so this file is present at runtime alongside the trigger that imports it.

Block pinning: every read in a single check must come from the same block, or two reads
landing at different heights produce a phantom breach. `block_ref` below resolves to a
block hash where the match payload carries one, falling back to the block number.
"""
import json
import os
from dataclasses import dataclass

import requests
from web3 import Web3

NETWORKS_DIR = os.path.join("config", "networks")
EXPLORERS_PATH = os.path.join("config", "triggers", "scripts", "network_explorers.json")

TOKEN_VAULT_ABI = [
    {
        "inputs": [{"name": "_ccy", "type": "bytes32"}],
        "name": "getTokenAddress",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "_ccy", "type": "bytes32"}],
        "name": "getTotalDepositAmount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

CURRENCY_CONTROLLER_ABI = [
    {
        "inputs": [],
        "name": "getCurrencies",
        "outputs": [{"name": "", "type": "bytes32[]"}],
        "stateMutability": "view",
        "type": "function",
    }
]

FUTURE_VALUE_VAULT_ABI = [
    {
        "inputs": [{"name": "maturity", "type": "uint256"}],
        "name": "getTotalLendingSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "maturity", "type": "uint256"}],
        "name": "getTotalBorrowingSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

LENDING_MARKET_CONTROLLER_ABI = [
    {
        "inputs": [
            {"name": "_ccy", "type": "bytes32"},
            {"name": "_maturity", "type": "uint256"},
        ],
        "name": "getPendingOrderAmount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "_ccy", "type": "bytes32"}],
        "name": "getMaturities",
        "outputs": [{"name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]

SWEEP_TARGETS_PATH = os.path.join("config", "sweep-targets.json")

# How much future value one unit of pending (present-value) settlement is allowed to
# explain. pendingOrderAmount is denominated in present value while the supply gap is in
# future value, so a multiplier bridges the two; 2 tolerates settlement down to unit
# price 5,000 (half of par). See invariant-monitoring-spec.md §02 for the exposure this
# leaves and §08 for how tracking pending future value exactly would remove it.
FV_MULTIPLIER = 2

# Integer-rounding residue allowed on top of the pending term, in the currency's base
# units.
#
# The spec claims the check is exact when pendingOrderAmount == 0. Live testnet data says
# otherwise: Arbitrum Sepolia USDC @1791504000 sits at lend=303927520, borrow=303927519,
# pending=0 - a gap of exactly 1 base unit (0.000001 USDC). Maker-side and taker-side
# future value are computed by separate divisions (amount * PRICE_DIGIT / unitPrice and
# friends, via RoundingUint256), so the two legs can land one unit apart. Checked back
# through history: the residue appeared when that market first traded and has been
# exactly 1 ever since - it is per-fill rounding, not a drift that grows with time.
#
# 1000 base units is ~0.001 USDC / ~1e-15 ETH, which leaves room for a market with many
# independent fills while staying roughly nine orders of magnitude below the scale of the
# September incident (~2.5e12 base units of unbacked USDC). The gap between "rounding" and
# "theft" is enormous, so this costs essentially no detection power.
#
# OPEN: this bound is empirical, not derived. The real per-maturity worst case should come
# from the number of rounding sites per fill - or the residue should be eliminated in the
# contracts. Until then do not arm an auto-pause on I1 without revisiting this.
FV_DUST = 1000


def load_sweep_targets():
    """Per-network contract addresses, written by tools/generate-monitors.ts.

    The sweep has no match payload to read an address from and can't call deployments.ts
    itself, so the generator resolves these from the contracts package and bakes them
    into the image next to the monitors.
    """
    with open(SWEEP_TARGETS_PATH) as f:
        return json.load(f)


def get_currencies(w3, currency_controller_address, block_identifier):
    """Currencies currently registered in the protocol, as 0x-prefixed bytes32 hex.

    Note this is CurrencyController.getCurrencies() - the registry of listed currencies -
    not LendingMarketController.getUsedCurrencies(), which is per-user and mutable.
    """
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(currency_controller_address),
        abi=CURRENCY_CONTROLLER_ABI,
    )
    raw = contract.functions.getCurrencies().call(block_identifier=block_identifier)
    return ["0x" + value.hex().removeprefix("0x") for value in raw]


def decode_bytes32_string(hex_str):
    raw = bytes.fromhex(hex_str[2:] if hex_str.startswith("0x") else hex_str)
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def to_bytes32(hex_str):
    return bytes.fromhex(hex_str[2:] if hex_str.startswith("0x") else hex_str)


def load_network(network_slug):
    """Returns (rpc_url_env_var_name, human readable network name)."""
    with open(os.path.join(NETWORKS_DIR, f"{network_slug}.json")) as f:
        config = json.load(f)
    return config["rpc_urls"][0]["url"]["value"], config["name"]


def load_explorer(network_slug):
    # Can't live in config/networks/*.json - OZ Monitor's own Network struct rejects
    # unknown fields.
    with open(EXPLORERS_PATH) as f:
        return json.load(f)[network_slug]


def web3_for(network_slug):
    rpc_url_env, _ = load_network(network_slug)
    return Web3(Web3.HTTPProvider(os.environ[rpc_url_env].strip()))


def block_ref(match_evm):
    """The block identifier every read in one check should be pinned to.

    Prefers the block hash, which is reorg-safe: a hash that no longer exists makes the
    call fail loudly rather than silently returning state from a different chain branch.
    Falls back to the block number when the match payload carries no hash.

    Verified 2026-10-02 against a real OZ Monitor v1.6.0 match payload (a live Sepolia
    TokenVault.Withdraw, captured by temporarily dumping stdin to a file) and against
    openzeppelin-monitor's own source at that tag (script executor, EVM monitor-match
    models): the payload shape this module assumes - matched_on_args.events[].args with
    name/value/indexed/kind, monitor.addresses[0].address, transaction.hash/blockHash/
    blockNumber - matches exactly. blockHash was present in the captured payload. Source
    confirms it's `Option<B256>`, "None when pending" - i.e. only unset for a pending
    transaction, which a confirmed monitor match never is. The fallback to blockNumber
    below is therefore defensive, not a path real traffic is expected to exercise.

    Correction, found during the September replay: matched_on_args.events is a LIST, not
    a single entry. OZ Monitor's filter_block (src/services/filter/filters/evm/filter.rs)
    emits one MonitorMatch per (monitor, transaction), but pushes every matching log in
    that transaction into events - confirmed live when a single replayed attack tx
    (0x6fdb1869...) carried 13 FutureValueVault.Transfer logs from one call to
    cleanUpFunds, which loops over every maturity a user holds a position in
    (FundManagementLogic.sol:819). Reading only events[0], as every script here
    originally did, silently drops every event after the first - see
    all_matched_events() below, which all three checks now use instead.
    """
    tx = match_evm["transaction"]
    block_hash = tx.get("blockHash")
    if block_hash:
        return block_hash

    raw = tx["blockNumber"]
    return int(raw, 16) if isinstance(raw, str) else int(raw)


def all_matched_events(match_evm):
    """Every matched event in this transaction, each as a {name: value} dict.

    matched_on_args.events is a list, one entry per matching log OZ Monitor found in the
    transaction - not a single event. A transaction that calls cleanUpFunds for a user
    with positions at several maturities emits one FutureValueVault.Transfer per
    maturity, all in the same tx; a multi-currency withdrawal does the same for
    TokenVault.Withdraw. Callers must handle every entry, not just the first - see
    block_ref's docstring for how this was found.
    """
    events = (match_evm.get("matched_on_args") or {}).get("events") or []
    return [{arg["name"]: arg["value"] for arg in ev["args"]} for ev in events]


@dataclass
class SolvencyResult:
    ccy_name: str
    token_address: str
    held: int
    owed: int

    @property
    def breached(self):
        return self.held < self.owed

    @property
    def shortfall(self):
        return max(self.owed - self.held, 0)


def check_solvency(w3, token_vault_address, ccy_hex, block_identifier):
    """I2 - custody solvency, per currency.

        IERC20(token).balanceOf(TokenVault) >= TokenVault.getTotalDepositAmount(ccy)

    The vault physically holds what it claims to hold. This is the chokepoint: nothing is
    extracted without crossing it, so it catches any value-extracting attack regardless of
    mechanism - but it is the last to fire, when money is already moving.

    No tolerance, no state, nothing to tune. A breach means the protocol's internal
    accounting has diverged from the tokens actually in custody.

    Native currency needs no special case: TransferHelper wraps on deposit and unwraps on
    withdrawal (TransferHelper.sol:45-46, :54), so the vault only ever custodies the
    wrapped token and getTokenAddress returns it.
    """
    token_vault = w3.eth.contract(
        address=Web3.to_checksum_address(token_vault_address), abi=TOKEN_VAULT_ABI
    )
    ccy_bytes32 = to_bytes32(ccy_hex)

    token_address = token_vault.functions.getTokenAddress(ccy_bytes32).call(
        block_identifier=block_identifier
    )
    if int(token_address, 16) == 0:
        raise ValueError(
            f"TokenVault has no token registered for currency {ccy_hex}"
        )

    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
    )

    held = token.functions.balanceOf(
        Web3.to_checksum_address(token_vault_address)
    ).call(block_identifier=block_identifier)
    owed = token_vault.functions.getTotalDepositAmount(ccy_bytes32).call(
        block_identifier=block_identifier
    )

    return SolvencyResult(
        ccy_name=decode_bytes32_string(ccy_hex),
        token_address=token_address,
        held=held,
        owed=owed,
    )


@dataclass
class ConservationResult:
    ccy_name: str
    maturity: int
    lend_supply: int
    borrow_supply: int
    pending_amount: int

    @property
    def gap(self):
        return abs(self.lend_supply - self.borrow_supply)

    @property
    def explainable(self):
        return self.pending_amount * FV_MULTIPLIER + FV_DUST

    @property
    def breached(self):
        return self.gap > self.explainable

    @property
    def unexplained(self):
        return max(self.gap - self.explainable, 0)


def check_fv_conservation(
    w3,
    future_value_vault_address,
    lending_market_controller_address,
    ccy_hex,
    maturity,
    block_identifier,
):
    """I1 - future value conservation, per currency per maturity.

        | lendSupply - borrowSupply | <= pendingOrderAmount * FV_MULTIPLIER

    Every filled order records a lender claim and an equal borrower obligation, so the
    two supplies would be identical were settlement immediate. It isn't: only the
    partially filled maker settles synchronously (updateFundsForMaker), while fully
    filled makers settle lazily at their own cleanup. The outstanding amount is carried
    in the protocol's own pendingOrderAmount counter, which is why there is a tolerance
    term rather than a flat equality.

    When pendingOrderAmount == 0 the tolerance is zero and ANY nonzero gap is a breach.
    That is the important property of this check: it is exact whenever the market is
    quiescent, and fuzzy only while settlement is genuinely outstanding - from state
    alone, with no log bookkeeping. Do not "simplify" the multiplication away; it is what
    makes the quiescent case exact.

    Note the reserve fund is INSIDE the balancing set. The order fee is charged
    taker-only (updateFundsForMaker passes _feeInFV = 0, FundManagementLogic.sol:279) and
    the reserve fund's future-value leg is what closes the taker's side. These supplies
    already include it; excluding the reserve fund anywhere here would produce a false
    gap of exactly feeInFV on every taker fill.
    """
    vault = w3.eth.contract(
        address=Web3.to_checksum_address(future_value_vault_address),
        abi=FUTURE_VALUE_VAULT_ABI,
    )
    controller = w3.eth.contract(
        address=Web3.to_checksum_address(lending_market_controller_address),
        abi=LENDING_MARKET_CONTROLLER_ABI,
    )

    lend_supply = vault.functions.getTotalLendingSupply(maturity).call(
        block_identifier=block_identifier
    )
    borrow_supply = vault.functions.getTotalBorrowingSupply(maturity).call(
        block_identifier=block_identifier
    )
    pending_amount = controller.functions.getPendingOrderAmount(
        to_bytes32(ccy_hex), maturity
    ).call(block_identifier=block_identifier)

    return ConservationResult(
        ccy_name=decode_bytes32_string(ccy_hex),
        maturity=maturity,
        lend_supply=lend_supply,
        borrow_supply=borrow_supply,
        pending_amount=pending_amount,
    )


def get_maturities(w3, lending_market_controller_address, ccy_hex, block_identifier):
    controller = w3.eth.contract(
        address=Web3.to_checksum_address(lending_market_controller_address),
        abi=LENDING_MARKET_CONTROLLER_ABI,
    )
    return controller.functions.getMaturities(to_bytes32(ccy_hex)).call(
        block_identifier=block_identifier
    )


# ---------------------------------------------------------------------------
# Batched fetchers, used by the sweep.
#
# The sweep evaluates every currency and maturity on every network, which is hundreds of
# view calls - roughly 1.4s each sequentially, so minutes per run. Batched JSON-RPC
# collapses those into a handful of round trips.
#
# Only the FETCHING differs from the single-shot functions above; the invariant predicate
# itself lives on the result dataclasses (`breached`, `gap`, `explainable`) and is shared
# by both paths, so the two cannot disagree about what a breach is.
#
# Block pinning: a batch must be pinned by block NUMBER, not hash - web3.py resolves a
# hash by calling get_block, which inside a batch context returns a placeholder rather
# than a block and raises. Reorg safety is preserved by verify_block_unchanged() below:
# take the hash up front, pin the batch by number, then confirm the hash at that number
# is still the same afterwards. If it isn't, the reads spanned a reorg and the run is
# discarded rather than reported.
# ---------------------------------------------------------------------------


def verify_block_unchanged(w3, block_number, block_hash):
    return w3.eth.get_block(block_number)["hash"] == block_hash


def fetch_solvency_batch(w3, token_vault_address, ccy_hexes, block_number):
    """Solvency for many currencies in two batched round trips.

    Currencies with no token registered in the vault are omitted from the results - that
    isn't a breach, it just isn't a vault currency.
    """
    vault_address = Web3.to_checksum_address(token_vault_address)
    token_vault = w3.eth.contract(address=vault_address, abi=TOKEN_VAULT_ABI)

    batch = w3.batch_requests()
    for ccy_hex in ccy_hexes:
        batch.add(
            token_vault.functions.getTokenAddress(to_bytes32(ccy_hex)).call(
                block_identifier=block_number
            )
        )
    token_addresses = batch.execute()

    live = [
        (ccy_hex, token)
        for ccy_hex, token in zip(ccy_hexes, token_addresses)
        if int(token, 16) != 0
    ]

    batch = w3.batch_requests()
    for ccy_hex, token in live:
        erc20 = w3.eth.contract(
            address=Web3.to_checksum_address(token), abi=ERC20_ABI
        )
        batch.add(
            erc20.functions.balanceOf(vault_address).call(
                block_identifier=block_number
            )
        )
        batch.add(
            token_vault.functions.getTotalDepositAmount(to_bytes32(ccy_hex)).call(
                block_identifier=block_number
            )
        )
    values = batch.execute()

    return [
        SolvencyResult(
            ccy_name=decode_bytes32_string(ccy_hex),
            token_address=token,
            held=values[i * 2],
            owed=values[i * 2 + 1],
        )
        for i, (ccy_hex, token) in enumerate(live)
    ]


def fetch_conservation_batch(
    w3,
    future_value_vault_address,
    lending_market_controller_address,
    ccy_hex,
    maturities,
    block_number,
):
    """Conservation for every maturity of one currency, in one batched round trip."""
    vault = w3.eth.contract(
        address=Web3.to_checksum_address(future_value_vault_address),
        abi=FUTURE_VALUE_VAULT_ABI,
    )
    controller = w3.eth.contract(
        address=Web3.to_checksum_address(lending_market_controller_address),
        abi=LENDING_MARKET_CONTROLLER_ABI,
    )
    ccy_bytes32 = to_bytes32(ccy_hex)

    batch = w3.batch_requests()
    for maturity in maturities:
        batch.add(
            vault.functions.getTotalLendingSupply(maturity).call(
                block_identifier=block_number
            )
        )
        batch.add(
            vault.functions.getTotalBorrowingSupply(maturity).call(
                block_identifier=block_number
            )
        )
        batch.add(
            controller.functions.getPendingOrderAmount(ccy_bytes32, maturity).call(
                block_identifier=block_number
            )
        )
    values = batch.execute()

    return [
        ConservationResult(
            ccy_name=decode_bytes32_string(ccy_hex),
            maturity=maturity,
            lend_supply=values[i * 3],
            borrow_supply=values[i * 3 + 1],
            pending_amount=values[i * 3 + 2],
        )
        for i, maturity in enumerate(maturities)
    ]


GENESIS_VALUE_VAULT_ABI = [
    {
        "inputs": [{"name": "ccy", "type": "bytes32"}],
        "name": name,
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
    for name in (
        "getTotalLendingSupply",
        "getTotalBorrowingSupply",
        "getTotalLockedBalance",
        "getCurrentMaturity",
    )
] + [
    {
        "inputs": [
            {"name": "ccy", "type": "bytes32"},
            {"name": "maturity", "type": "uint256"},
        ],
        "name": "getMaturityGenesisValue",
        "outputs": [{"name": "", "type": "int256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


@dataclass
class GenesisValueObservation:
    """I4 - genesis value, OBSERVATION ONLY. Never breaches, never alerts.

    The spec asks for the real GV relation to be derived before any threshold is trusted.
    Two candidate formulations were tested against live testnet state and BOTH FAIL:

      1. `lendSupply - borrowSupply == totalLockedBalance`
         Rationale: lock/unlock move value out of `balances` without touching the
         supplies, but record it in totalLockedBalances, so they should cancel.
         Result: fails by orders of magnitude. WBTC and JPYC on Ethereum Sepolia show
         a large lending supply against zero borrowing supply and zero locked balance,
         so the signed GV balances plainly do not sum to zero - conversion from future
         value is lazy and per-user, so lender GV can exist with no matching borrower GV
         yet converted.

      2. `lendSupply - borrowSupply == Σ getMaturityGenesisValue(ccy, m)`
         Rationale: `_updateBalance` writes both sides by the same amount
         (GenesisValueVault.sol:664-668).
         Result: fails. Every maturityBalance reads zero on both testnets while the
         supply gap is large - and the auto-roll log chain terminates immediately
         (`prev == 0`, one hop), meaning no auto-roll has run there yet. So the supplies
         are being populated through a path that writes no maturityBalance at all.

    There are five `_updateTotalSupply` call sites (:507, :508, :538, :539, :664), and the
    :538/:539 pair in the balance-cleanup path was missed in the earlier analysis. The
    real relation has to be derived from all of them together, ideally with the contracts
    team, on an environment where an auto-roll has actually occurred.

    Until then this records the quantities so their behaviour can be learned from real
    data. It must not gain a threshold, and it must never be wired to the responder.
    """

    ccy_name: str
    lend_supply: int
    borrow_supply: int
    locked_balance: int
    current_maturity: int
    maturity_genesis_value: int

    @property
    def gap(self):
        return self.lend_supply - self.borrow_supply


def observe_genesis_value(
    w3, genesis_value_vault_address, ccy_hexes, block_number
):
    """Records I4's quantities for every currency in one batched round trip."""
    vault = w3.eth.contract(
        address=Web3.to_checksum_address(genesis_value_vault_address),
        abi=GENESIS_VALUE_VAULT_ABI,
    )

    batch = w3.batch_requests()
    for ccy_hex in ccy_hexes:
        ccy = to_bytes32(ccy_hex)
        batch.add(
            vault.functions.getTotalLendingSupply(ccy).call(
                block_identifier=block_number
            )
        )
        batch.add(
            vault.functions.getTotalBorrowingSupply(ccy).call(
                block_identifier=block_number
            )
        )
        batch.add(
            vault.functions.getTotalLockedBalance(ccy).call(
                block_identifier=block_number
            )
        )
        batch.add(
            vault.functions.getCurrentMaturity(ccy).call(
                block_identifier=block_number
            )
        )
    values = batch.execute()

    maturities = [values[i * 4 + 3] for i in range(len(ccy_hexes))]

    batch = w3.batch_requests()
    for ccy_hex, maturity in zip(ccy_hexes, maturities):
        batch.add(
            vault.functions.getMaturityGenesisValue(
                to_bytes32(ccy_hex), maturity
            ).call(block_identifier=block_number)
        )
    maturity_values = batch.execute()

    return [
        GenesisValueObservation(
            ccy_name=decode_bytes32_string(ccy_hex),
            lend_supply=values[i * 4],
            borrow_supply=values[i * 4 + 1],
            locked_balance=values[i * 4 + 2],
            current_maturity=values[i * 4 + 3],
            maturity_genesis_value=maturity_values[i],
        )
        for i, ccy_hex in enumerate(ccy_hexes)
    ]


def currency_for_address(targets_for_network, concern_key, address):
    """Maps an emitting proxy address back to its currency name.

    The per-currency proxies (FutureValueVault, LendingMarket) don't put `ccy` in every
    event, so the generator writes the currency -> address map for each splitByAddress
    concern into sweep-targets.json and scripts reverse it here. Avoids an RPC round trip
    per match just to learn which currency fired.
    """
    wanted = address.lower()
    for name, mapped in targets_for_network.get(concern_key, {}).items():
        if mapped.lower() == wanted:
            return name
    return None


def currency_hex(name):
    """Currency name -> bytes32 hex, the inverse of decode_bytes32_string."""
    return "0x" + name.encode("utf-8").ljust(32, b"\x00").hex()


def post_alert(title, lines, severity="CRITICAL"):
    """Posts to the severity-appropriate Slack webhook.

    CRITICAL routes to SLACK_WEBHOOK_URL_CRITICAL when configured, so invariant breaches
    can be separated from routine alert traffic. Falls back to the existing webhook when
    it isn't set, so this works before the channel split is made.
    """
    webhook = os.environ.get("SLACK_WEBHOOK_URL_CRITICAL") or os.environ["SLACK_WEBHOOK_URL"]
    env_label = os.environ.get("SF_ENV", "")

    requests.post(
        webhook.strip(),
        json={"text": "\n".join([f"[{severity}] [{env_label}] {title}", *lines])},
        timeout=10,
    )
