"""I1 - future value conservation. On a FutureValueVault Transfer match, asserts every
lender claim at that maturity still carries a matching borrower obligation.

    | lendSupply - borrowSupply | <= pendingOrderAmount * FV_MULTIPLIER

Transfer is emitted on every mint and burn, so this runs at the moment unbacked future
value would come into existence - before an attacker can withdraw against it. The check
itself lives in invariants.py, shared with the periodic sweep - see
invariant-monitoring-spec.md (I1).

Identifying the currency: Transfer carries `maturity` but not `ccy`, and FutureValueVault
is deployed per currency. The fvconservation concern therefore sets splitByAddress, so
each monitor holds exactly one vault address, and the generator writes the
currency -> vault map into config/sweep-targets.json. This reads the address off the
monitor and reverses that map - no RPC round trip to work out which currency fired.

Invocation contract (OZ Monitor's script trigger): the process is spawned as
`python3 -c <script>` with no extra argv - any configured trigger "arguments" arrive
bundled into the JSON on stdin as `args`, not as sys.argv. stdin is
`{"monitor_match": {"EVM": {...}}, "args": [...]}`; the script exits 0 on success,
non-zero + stderr on failure.
"""
import json
import sys

sys.path.insert(0, "config/triggers/scripts")

import invariants  # noqa: E402


def main():
    payload = json.load(sys.stdin)
    match_ = payload["monitor_match"]["EVM"]

    network_slug = match_["network_slug"]
    vault_address = match_["monitor"]["addresses"][0]["address"]
    tx_hash = match_["transaction"]["hash"]

    # cleanUpFunds settles every maturity a user holds a position in within one call
    # (FundManagementLogic.sol:819), so a single transaction can emit one
    # FutureValueVault.Transfer per maturity - check every distinct maturity touched,
    # not just the first matched event.
    maturities = {int(event["maturity"]) for event in invariants.all_matched_events(match_)}

    targets = invariants.load_sweep_targets()[network_slug]
    ccy_name = invariants.currency_for_address(
        targets, "fvconservation", vault_address
    )
    if ccy_name is None:
        # The monitor set and sweep-targets.json are written by the same generator run,
        # so this means they've drifted - a stale monitor left behind, or a regenerate
        # that didn't complete. Fail loudly rather than skipping the check.
        print(
            f"No currency mapped to FutureValueVault {vault_address} on {network_slug}",
            file=sys.stderr,
        )
        sys.exit(1)

    _, network_name = invariants.load_network(network_slug)
    explorer = invariants.load_explorer(network_slug)
    w3 = invariants.web3_for(network_slug)
    block_identifier = invariants.block_ref(match_)

    any_breach = False
    for maturity in maturities:
        # All three reads for one maturity pinned to the same block - reads at different
        # heights would produce a phantom breach.
        result = invariants.check_fv_conservation(
            w3,
            vault_address,
            targets["LendingMarketController"],
            invariants.currency_hex(ccy_name),
            maturity,
            block_identifier,
        )

        print(
            f"{result.ccy_name}@{result.maturity} conservation: "
            f"lend={result.lend_supply} borrow={result.borrow_supply} "
            f"pending={result.pending_amount} "
            f"({'BREACH' if result.breached else 'ok'})"
        )

        if not result.breached:
            continue

        any_breach = True
        invariants.post_alert(
            f"Future value conservation breach on {network_name}",
            [
                (
                    f"{result.ccy_name} lend and borrow future value differ by "
                    f"{result.gap} with no pending settlement to explain it."
                    if result.pending_amount == 0
                    else (
                        f"{result.ccy_name} future value gap of {result.gap} exceeds what "
                        f"{result.pending_amount} of pending settlement can explain."
                    )
                ),
                f"Currency: {result.ccy_name}",
                f"Maturity: {result.maturity}",
                f"Lend future value: {result.lend_supply}",
                f"Borrow future value: {result.borrow_supply}",
                f"Pending order amount: {result.pending_amount}",
                f"Unexplained: {result.unexplained}",
                f"Tx: {explorer}/tx/{tx_hash}",
            ],
        )

    if not any_breach:
        sys.exit(0)


if __name__ == "__main__":
    main()
