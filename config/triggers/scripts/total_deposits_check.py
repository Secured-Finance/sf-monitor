#!/usr/bin/env python3
"""On a TokenVault Withdraw match, checks whether the withdrawn currency's vault
balance dropped >=1% block-over-block and this withdrawal accounts for >=1% of the
prior balance, and if so alerts Slack.

Per-event, not batched: OZ Monitor invokes this script once per matched event, so
each run checks only the currency of the withdrawal that triggered it (rather than
re-checking every vault currency in a block, as the old Defender Autotask did).

Invocation contract (OZ Monitor's script trigger): the process is spawned as
`python3 -c <script>` with no extra argv - any configured trigger "arguments" arrive
bundled into the JSON on stdin as `args`, not as sys.argv (see
services/trigger/script/executor.rs in the OpenZeppelin Monitor source). stdin is
`{"monitor_match": {"EVM": {...}}, "args": [...]}`; the script exits 0 on success,
non-zero + stderr on failure.

Inputs: no "arguments" are configured for this trigger (see
config/triggers/scripts.json) - everything below comes from the match payload or from
config mounted alongside it:
  - network_slug and the watched TokenVault address: monitor_match.EVM
    (network_slug, monitor.addresses[0].address).
  - the RPC env var name and human-readable network name: config/networks/
    <network_slug>.json, mounted read-only into the container.
  - the explorer base URL: network_explorers.json next to this script (can't live in
    config/networks/*.json - OZ Monitor's own Network struct rejects unknown fields).
  - the env label: SF_ENV, present via docker-compose's `env_file: .env`.
  - the withdrawn currency's ERC20 token address: resolved live via
    TokenVault.getTokenAddress (web3.py, installed into the monitor image via this
    repo's Dockerfile) - no static currency map to maintain, no hand-rolled ABI
    encoding.
"""
import json
import os
import sys

import requests
from web3 import Web3

BALANCE_DROP_THRESHOLD_PCT = 1
WITHDRAWAL_SHARE_THRESHOLD_PCT = 1

# OZ Monitor runs this script as `python3 -c <content>`, not as a file, so __file__
# isn't defined - paths below are relative to the container's cwd (/app, set by
# WORKDIR in the upstream openzeppelin-monitor image), same as this trigger's own
# script_path in config/triggers/scripts.json.
NETWORKS_DIR = os.path.join("config", "networks")
EXPLORERS_PATH = os.path.join("config", "triggers", "scripts", "network_explorers.json")

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
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def decode_bytes32_string(hex_str):
    raw = bytes.fromhex(hex_str[2:] if hex_str.startswith("0x") else hex_str)
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def main():
    payload = json.load(sys.stdin)
    match_ = payload["monitor_match"]["EVM"]

    network_slug = match_["network_slug"]
    token_vault_address = Web3.to_checksum_address(
        match_["monitor"]["addresses"][0]["address"]
    )
    tx_hash = match_["transaction"]["hash"]
    block_number_raw = match_["transaction"]["blockNumber"]
    cur_block = (
        int(block_number_raw, 16)
        if isinstance(block_number_raw, str)
        else int(block_number_raw)
    )
    prev_block = cur_block - 1

    event_args = {
        arg["name"]: arg["value"]
        for arg in match_["matched_on_args"]["events"][0]["args"]
    }
    ccy_hex = event_args["ccy"]
    currency_name = decode_bytes32_string(ccy_hex)
    withdrawn_amount = int(event_args["amount"])

    with open(os.path.join(NETWORKS_DIR, f"{network_slug}.json")) as f:
        network_config = json.load(f)
    rpc_url_env = network_config["rpc_urls"][0]["url"]["value"]
    network_name = network_config["name"]

    with open(EXPLORERS_PATH) as f:
        explorer = json.load(f)[network_slug]

    env_label = os.environ.get("SF_ENV", "")

    w3 = Web3(Web3.HTTPProvider(os.environ[rpc_url_env].strip()))

    token_vault = w3.eth.contract(address=token_vault_address, abi=TOKEN_VAULT_ABI)
    ccy_bytes32 = bytes.fromhex(ccy_hex[2:] if ccy_hex.startswith("0x") else ccy_hex)
    token_address = token_vault.functions.getTokenAddress(ccy_bytes32).call(block_identifier=cur_block)
    if int(token_address, 16) == 0:
        print(f"TokenVault has no token registered for currency {currency_name} ({ccy_hex})", file=sys.stderr)
        sys.exit(1)

    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
    cur_balance = token.functions.balanceOf(token_vault_address).call(block_identifier=cur_block)
    prev_balance = token.functions.balanceOf(token_vault_address).call(block_identifier=prev_block)

    if not prev_balance:
        sys.exit(0)

    balance_movement_pct = ((cur_balance - prev_balance) / prev_balance) * 100
    print(f"{currency_name} vault balance movement: {balance_movement_pct:.4f}% ({prev_balance} -> {cur_balance})")

    if balance_movement_pct > -BALANCE_DROP_THRESHOLD_PCT:
        sys.exit(0)

    share_pct = (withdrawn_amount / prev_balance) * 100
    print(f"Withdrawal share of prior balance: {share_pct:.4f}%")

    if share_pct < WITHDRAWAL_SHARE_THRESHOLD_PCT:
        sys.exit(0)

    requests.post(
        os.environ["SLACK_WEBHOOK_URL"].strip(),
        json={
            "text": (
                f"[{env_label}:{network_name}] Big Withdrawal Detected\n"
                f"{share_pct:.2f}% of {currency_name} was withdrawn.\n"
                f"User: {event_args.get('user', '')}\n"
                f"Currency: {currency_name}\n"
                f"Amount: {withdrawn_amount}\n"
                f"Tx: {explorer}/tx/{tx_hash}"
            )
        },
        timeout=10,
    )


if __name__ == "__main__":
    main()
