#!/usr/bin/env python3
"""On a TokenVault Withdraw match, checks whether the withdrawn currency's vault
balance dropped >=1% block-over-block and this withdrawal accounts for >=1% of the
prior balance, and if so alerts Slack.

Per-transaction, not per-event: OZ Monitor invokes this script once per matching
transaction, with every Withdraw log in that transaction bundled into
matched_on_args.events - a single transaction can withdraw more than one currency
(e.g. closing positions in several at once), so this checks every distinct currency
touched, not just the first. The "amount withdrawn" used for the share-of-balance
threshold is the real net TokenVault balance drop (prev_balance - cur_balance), not
any single event's amount - a transaction can net several Withdraw/deposit legs for
the same currency, and the balance read already reflects the true net regardless of
how many legs occurred.

Invocation contract (stdin payload, trigger "arguments") and alert formatting/delivery:
see sf_alerts.read_payload and sf_alerts.AlertContext. No "arguments" are configured for
this trigger (config/triggers/scripts.json). Everything else comes from the match
payload or from config mounted alongside it:
  - network_slug and the watched TokenVault address: monitor_match.EVM
    (network_slug, monitor.addresses[0].address).
  - the RPC env var name, human-readable network name, explorer base URL and env label:
    sf_alerts.AlertContext.
  - the withdrawn currency's ERC20 token address and decimals: resolved live via
    TokenVault.getTokenAddress (sf_alerts.TokenInfo, using web3.py - installed into the
    monitor image via this repo's Dockerfile) - no static currency map to maintain, no
    hand-rolled ABI encoding.
"""
import os
import sys

sys.path.insert(0, os.path.join("config", "triggers", "scripts"))

import sf_alerts  # noqa: E402 - path above must be set before this import

BALANCE_DROP_THRESHOLD_PCT = 1
WITHDRAWAL_SHARE_THRESHOLD_PCT = 1


def main():
    match_, _ = sf_alerts.read_payload()

    token_vault_address = match_["monitor"]["addresses"][0]["address"]
    tx_hash = match_["transaction"]["hash"]
    cur_block = sf_alerts.to_int(match_["transaction"]["blockNumber"])
    prev_block = cur_block - 1

    # Group every matched Withdraw event by currency - a transaction can withdraw
    # more than one currency, and each currency's events are checked independently
    # below. The user shown in the alert is whichever event we saw first for that
    # currency; with multiple withdrawers of the same currency in one tx this is
    # representative, not exhaustive - the amount is what matters and that comes
    # from the real balance drop, not from any single event.
    events_by_ccy = {}
    for event_args in sf_alerts.matched_events(match_):
        events_by_ccy.setdefault(event_args["ccy"], event_args)

    ctx = sf_alerts.AlertContext.from_match(match_)
    w3 = ctx.w3()
    # The TokenVault address comes from the match (the contract this monitor actually
    # watched), not from the generator's contract_addresses.json - the balances below
    # must be read from exactly the contract that emitted the Withdraw. Token addresses
    # are resolved at the matched block, the same as the balances they are read for.
    tokens = ctx.token_info(token_vault_address, block_identifier=cur_block)
    vault_address = w3.to_checksum_address(token_vault_address)

    for ccy_hex, event_args in events_by_ccy.items():
        currency_name = sf_alerts.decode_bytes32_string(ccy_hex)

        token_address = tokens.token_address(ccy_hex)
        if token_address is None:
            print(f"TokenVault has no token registered for currency {currency_name} ({ccy_hex})", file=sys.stderr)
            continue

        token = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=sf_alerts.ERC20_ABI)
        cur_balance = token.functions.balanceOf(vault_address).call(block_identifier=cur_block)
        prev_balance = token.functions.balanceOf(vault_address).call(block_identifier=prev_block)

        if not prev_balance:
            continue

        balance_movement_pct = ((cur_balance - prev_balance) / prev_balance) * 100
        print(f"{currency_name} vault balance movement: {balance_movement_pct:.4f}% ({prev_balance} -> {cur_balance})")

        if balance_movement_pct > -BALANCE_DROP_THRESHOLD_PCT:
            continue

        # The real net amount withdrawn, from the balance itself - not any single
        # event's amount, which can be one of several legs in the same transaction.
        net_withdrawn = prev_balance - cur_balance
        share_pct = (net_withdrawn / prev_balance) * 100
        print(f"Withdrawal share of prior balance: {share_pct:.4f}%")

        if share_pct < WITHDRAWAL_SHARE_THRESHOLD_PCT:
            continue

        user = event_args.get("user", "")
        sf_alerts.send_alert(
            ctx,
            "Big Withdrawal Detected",
            [
                ("User", ctx.address_link(user) if user else "unknown"),
                ("Currency", currency_name),
                ("Amount", tokens.format_amount(ccy_hex, net_withdrawn)),
                ("Vault Balance", tokens.format_amount(ccy_hex, cur_balance)),
                ("Tx", ctx.tx_link(tx_hash)),
            ],
            summary=f"{share_pct:.2f}% of the {currency_name} held in TokenVault was withdrawn",
        )


if __name__ == "__main__":
    main()
