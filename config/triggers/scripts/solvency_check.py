"""I2 - custody solvency. On a TokenVault Withdraw match, asserts the vault still holds
at least what its own accounting says it owes for that currency.

    IERC20(token).balanceOf(TokenVault) >= TokenVault.getTotalDepositAmount(ccy)

A withdrawal is the moment tokens actually leave the vault, so it is the moment this
invariant can break. The check itself lives in invariants.py, shared with the periodic
sweep - see invariant-monitoring-spec.md (I2).

This is additive: the existing total-deposits concern still watches the same event with
its own balance-drop heuristic and is unaffected.

Invocation contract (OZ Monitor's script trigger): the process is spawned as
`python3 -c <script>` with no extra argv - any configured trigger "arguments" arrive
bundled into the JSON on stdin as `args`, not as sys.argv. stdin is
`{"monitor_match": {"EVM": {...}}, "args": [...]}`; the script exits 0 on success,
non-zero + stderr on failure.
"""
import json
import sys

# OZ Monitor runs this as `python3 -c <content>`, so this directory isn't on sys.path
# and __file__ is undefined. Paths are relative to the container's cwd (/app).
sys.path.insert(0, "config/triggers/scripts")

import invariants  # noqa: E402


def main():
    payload = json.load(sys.stdin)
    match_ = payload["monitor_match"]["EVM"]

    network_slug = match_["network_slug"]
    token_vault_address = match_["monitor"]["addresses"][0]["address"]
    tx_hash = match_["transaction"]["hash"]

    # A single transaction can withdraw more than one currency (e.g. closing positions
    # in several currencies at once), and each is its own Withdraw log - check every
    # distinct currency the transaction touched, not just the first matched event.
    ccy_hexes = {event["ccy"] for event in invariants.all_matched_events(match_)}

    _, network_name = invariants.load_network(network_slug)
    explorer = invariants.load_explorer(network_slug)
    w3 = invariants.web3_for(network_slug)
    block_identifier = invariants.block_ref(match_)

    any_breach = False
    for ccy_hex in ccy_hexes:
        # All reads for one currency pinned to the same block - two reads at different
        # heights would produce a phantom breach.
        result = invariants.check_solvency(w3, token_vault_address, ccy_hex, block_identifier)

        print(
            f"{result.ccy_name} solvency: held={result.held} owed={result.owed} "
            f"({'BREACH' if result.breached else 'ok'})"
        )

        if not result.breached:
            continue

        any_breach = True
        invariants.post_alert(
            f"Custody solvency breach on {network_name}",
            [
                f"TokenVault holds less {result.ccy_name} than it owes.",
                f"Currency: {result.ccy_name}",
                f"Held: {result.held}",
                f"Owed: {result.owed}",
                f"Shortfall: {result.shortfall}",
                f"Token: {result.token_address}",
                f"Tx: {explorer}/tx/{tx_hash}",
            ],
        )

    if not any_breach:
        sys.exit(0)


if __name__ == "__main__":
    main()
