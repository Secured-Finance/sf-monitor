#!/usr/bin/env python3
"""Formats and sends the alert for every concern that needs no state and no threshold -
liquidations, auto rolls and itayose - one Slack message per matched event.

These three used to be declarative Slack triggers (config/triggers/slack.json). They
are scripts now for formatting alone: upstream's notification templating is plain
`${events.0.args.x}` substitution, so a declarative trigger can only print the raw
decoded arg - a bytes32 currency as 64 hex characters, a maturity as a Unix timestamp,
an amount in base units, and a bare transaction hash with no explorer link. Everything
in CONCERNS below is that translation; the conditions themselves are still entirely in
the monitor's own match_conditions, as before - this script never decides whether to
alert, only how the alert reads.

One script rather than three: the per-concern difference is only the field list, and
which concern is being rendered arrives as the trigger's configured `arguments` (see
config/triggers/scripts.json), which upstream delivers as `args` on stdin.

Invocation contract and inputs: see sf_alerts.read_payload and sf_alerts.AlertContext.
"""
import os
import sys

sys.path.insert(0, os.path.join("config", "triggers", "scripts"))

import sf_alerts  # noqa: E402 - path above must be set before this import

# Field formatters. Each takes (raw value, the event's other args, the TokenInfo
# resolver) so a value needing another arg for context - an amount needs its currency's
# decimals - can reach it.
def currency(value, args, tokens):
    return sf_alerts.decode_bytes32_string(value)


def maturity(value, args, tokens):
    return sf_alerts.format_timestamp(value)


def unit_price(value, args, tokens):
    return sf_alerts.format_unit_price(value)


def address_of(ctx):
    def formatter(value, args, tokens):
        return ctx.address_link(value)

    return formatter


def amount_in(ccy_arg):
    """An amount denominated in the currency named by another arg of the same event."""

    def formatter(value, args, tokens):
        return tokens().format_amount(args[ccy_arg], value)

    return formatter


# One entry per concern using this script, keyed by the string passed as the trigger's
# `arguments`. `fields` is (label, event arg name, formatter) in the order they appear in
# the alert; `title` is the alert's heading, which sf_alerts prefixes uniformly with
# [<SF_ENV>:<network>].
def concerns(ctx):
    address = address_of(ctx)
    return {
        "liquidations": {
            "title": "Liquidation Detected",
            "fields": [
                ("User", "user", address),
                ("Collateral Currency", "collateralCcy", currency),
                ("Debt Currency", "debtCcy", currency),
                ("Debt Maturity", "debtMaturity", maturity),
                ("Debt Amount", "debtAmount", amount_in("debtCcy")),
            ],
        },
        "autorolls": {
            "title": "Auto Roll Detected",
            "fields": [
                ("Currency", "ccy", currency),
                ("Old Maturity", "oldMaturity", maturity),
                ("New Maturity", "newMaturity", maturity),
            ],
        },
        "itayose": {
            "title": "Itayose Detected",
            "fields": [
                ("Currency", "ccy", currency),
                ("Maturity", "maturity", maturity),
                ("Opening Unit Price", "openingUnitPrice", unit_price),
                ("Last Lend Unit Price", "lastLendUnitPrice", unit_price),
                ("Last Borrow Unit Price", "lastBorrowUnitPrice", unit_price),
                ("Offset Amount", "offsetAmount", amount_in("ccy")),
            ],
        },
    }


def build_fields(spec, event_args, tokens):
    """(label, formatted value) pairs for one event, skipping args it doesn't carry."""
    return [
        (label, formatter(event_args[arg_name], event_args, tokens))
        for label, arg_name, formatter in spec["fields"]
        if arg_name in event_args
    ]


def main():
    match_, args = sf_alerts.read_payload()
    if not args:
        raise SystemExit(
            "no concern argument configured for this trigger - see "
            "config/triggers/scripts.json"
        )
    concern_key = args[0]

    ctx = sf_alerts.AlertContext.from_match(match_)
    spec = concerns(ctx)[concern_key]
    tx_hash = match_["transaction"]["hash"]

    # Lazily built, so a concern with no amount to format (auto rolls) makes no RPC call.
    tokens = ctx.token_info

    failures = 0
    # Every matched log in the transaction gets its own message - a single transaction
    # can liquidate, auto-roll or run itayose for more than one market, and the old
    # declarative trigger reported only events.0.
    for event_args in sf_alerts.matched_events(match_):
        fields = build_fields(spec, event_args, tokens)
        fields.append(("Tx", ctx.tx_link(tx_hash)))
        try:
            sf_alerts.send_alert(ctx, spec["title"], fields)
        except Exception as error:  # noqa: BLE001 - one bad event must not drop the rest
            print(f"failed to alert on {concern_key} event: {error}", file=sys.stderr)
            failures += 1

    if failures:
        raise SystemExit(f"{failures} {concern_key} alert(s) failed to send")


if __name__ == "__main__":
    main()
