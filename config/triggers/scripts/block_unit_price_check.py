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

Per-transaction, not per-event: matched_on_args.events bundles every matching log in the
transaction, and a single transaction can touch more than one (currency, maturity)
market - e.g. a batch action spanning multiple maturities of the same currency. Every
distinct (ccy, maturity) pair gets its own rolling history file and is checked
independently, not just the first matched event.

Invocation contract (stdin payload, trigger "arguments") and alert formatting/delivery:
see sf_alerts.read_payload and sf_alerts.AlertContext. No "arguments" are configured
for this trigger (config/triggers/scripts.json); the network name, explorer base URL and
environment label all come from there. Everything this script adds on top is the rolling
history below.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join("config", "triggers", "scripts"))

import sf_alerts  # noqa: E402 - path above must be set before this import

HISTORY_WINDOW = 5

# OZ Monitor runs this script as `python3 -c <content>`, not as a file, so __file__
# isn't defined - paths below are relative to the container's cwd (/app, set by
# WORKDIR in the upstream openzeppelin-monitor image), same as this trigger's own
# script_path in config/triggers/scripts.json.
HISTORY_DIR = os.path.join("data", "block_unit_price_history")


def main():
    match_, _ = sf_alerts.read_payload()

    network_slug = match_["network_slug"]
    tx_hash = match_["transaction"]["hash"]

    # Group every matched event by (ccy, maturity) - a single transaction can touch
    # more than one market for the same currency (or across currencies, if this
    # monitor ever watched more than one address).
    markets = {}
    for event_args in sf_alerts.matched_events(match_):
        ccy_hex = event_args["ccy"]
        maturity = sf_alerts.to_int(event_args["maturity"])
        markets[(ccy_hex, maturity)] = event_args

    ctx = sf_alerts.AlertContext.from_match(match_)

    os.makedirs(HISTORY_DIR, exist_ok=True)

    for (ccy_hex, maturity), event_args in markets.items():
        currency_name = sf_alerts.decode_bytes32_string(ccy_hex)
        block_unit_price = sf_alerts.to_int(event_args["blockUnitPrice"])

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

        print(f"{currency_name}@{maturity} Block Unit Price Fluctuation: {change_percent:.2f}% ({first} -> {last})")

        if change_percent == 0:
            continue

        # Unit prices are out of Constants.PRICE_DIGIT (10000 = par), so the history is
        # shown as percentages of face value rather than as the raw integers it stores.
        price_history = " -> ".join(sf_alerts.format_unit_price(price) for price in history)

        sf_alerts.send_alert(
            ctx,
            "Block Unit Price Fluctuation Detected",
            [
                ("Currency", currency_name),
                ("Maturity", sf_alerts.format_timestamp(maturity)),
                ("Block Unit Price", sf_alerts.format_unit_price(last)),
                ("Recent Prices", price_history),
                ("Tx", ctx.tx_link(tx_hash)),
            ],
            summary=f"{change_percent:.2f}% fluctuation happened in the {currency_name} market",
        )


if __name__ == "__main__":
    main()
