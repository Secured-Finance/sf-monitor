#!/usr/bin/env python3
"""On a BlockUnitPriceHistoryUpdated match (emitted by a per-currency LendingMarket
beacon proxy, not LendingMarketController - see
tools/concerns/lending-market-addresses.ts), tracks a rolling 5-entry block-unit-price
history per (currency, maturity) market and alerts Slack on any change within that
window.

Ported from the old Defender Autotask handler (block-unit-price-fluctuations/handler.ts)
with minimal changes: same 5-entry rolling window, same "alert on any nonzero percent
change" condition (a real fluctuation threshold is still open - to be revisited with
the team). The only structural difference is where per-market state lives: Defender's
KV store there, a JSON file per market under data/block_unit_price_history/ here (that
directory is under the `data` volume mounted read-write into the container, same as the
monitor's own last-processed-block cursor).

Per-event, not batched: OZ Monitor invokes this script once per matched event, so each
run only updates the (currency, maturity) market of the event that triggered it.

Invocation contract (OZ Monitor's script trigger): the process is spawned as
`python3 -c <script>` with no extra argv - any configured trigger "arguments" arrive
bundled into the JSON on stdin as `args`, not as sys.argv (see
services/trigger/script/executor.rs in the OpenZeppelin Monitor source). stdin is
`{"monitor_match": {"EVM": {...}}, "args": [...]}`; the script exits 0 on success,
non-zero + stderr on failure.

Inputs: no "arguments" are configured for this trigger (see
config/triggers/scripts.json) - everything below comes from the match payload or from
config mounted alongside it:
  - network_slug and the human-readable network name: config/networks/<network_slug>.json
    (mounted read-only into the container), same as total_deposits_check.py.
  - the explorer base URL: network_explorers.json next to this script (can't live in
    config/networks/*.json - OZ Monitor's own Network struct rejects unknown fields).
  - the env label: SF_ENV, present via docker-compose's `env_file: .env`.
"""
import json
import os
import sys
from datetime import datetime, timezone

import requests

HISTORY_WINDOW = 5

# OZ Monitor runs this script as `python3 -c <content>`, not as a file, so __file__
# isn't defined - paths below are relative to the container's cwd (/app, set by
# WORKDIR in the upstream openzeppelin-monitor image), same as this trigger's own
# script_path in config/triggers/scripts.json.
NETWORKS_DIR = os.path.join("config", "networks")
EXPLORERS_PATH = os.path.join("config", "triggers", "scripts", "network_explorers.json")
HISTORY_DIR = os.path.join("data", "block_unit_price_history")


def decode_bytes32_string(hex_str):
    raw = bytes.fromhex(hex_str[2:] if hex_str.startswith("0x") else hex_str)
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def main():
    payload = json.load(sys.stdin)
    match_ = payload["monitor_match"]["EVM"]

    network_slug = match_["network_slug"]
    tx_hash = match_["transaction"]["hash"]

    event_args = {
        arg["name"]: arg["value"]
        for arg in match_["matched_on_args"]["events"][0]["args"]
    }
    ccy_hex = event_args["ccy"]
    currency_name = decode_bytes32_string(ccy_hex)
    maturity = int(event_args["maturity"])
    block_unit_price = int(event_args["blockUnitPrice"])

    with open(os.path.join(NETWORKS_DIR, f"{network_slug}.json")) as f:
        network_config = json.load(f)
    network_name = network_config["name"]

    with open(EXPLORERS_PATH) as f:
        explorer = json.load(f)[network_slug]

    env_label = os.environ.get("SF_ENV", "")

    os.makedirs(HISTORY_DIR, exist_ok=True)
    history_path = os.path.join(
        HISTORY_DIR, f"{network_slug}_{currency_name}_{maturity}.json"
    )

    if os.path.exists(history_path):
        with open(history_path) as f:
            history = json.load(f)
    else:
        history = []

    if len(history) >= HISTORY_WINDOW:
        history.pop(0)
    history.append(block_unit_price)

    with open(history_path, "w") as f:
        json.dump(history, f)

    first = history[0]
    last = history[-1]
    change_percent = ((last - first) / first) * 100
    maturity_str = datetime.fromtimestamp(maturity, tz=timezone.utc).strftime("%B %-d, %Y %-I:%M %p UTC")

    print(f"Block Unit Price Fluctuation: {change_percent:.2f}% ({first} -> {last})")

    if change_percent == 0:
        sys.exit(0)

    requests.post(
        os.environ["SLACK_WEBHOOK_URL"].strip(),
        json={
            "text": (
                f"[{env_label}:{network_name}] BlockUnitPrice Fluctuation Detected\n"
                f"{change_percent:.2f}% fluctuation happened in {currency_name} market\n"
                f"Currency: {currency_name}\n"
                f"Maturity: {maturity_str}\n"
                f"Fluctuation Rate: {history}\n"
                f"Tx: {explorer}/tx/{tx_hash}"
            )
        },
        timeout=10,
    )


if __name__ == "__main__":
    main()
