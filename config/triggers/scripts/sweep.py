"""Periodic invariant sweep - runs the protocol invariants on a timer rather than off an
event (see invariant-monitoring-spec.md).

Why this exists alongside the script triggers: OZ Monitor is event-driven, and some
invariants have no triggering event at all (I2 solvency changes whenever tokens move,
including paths this monitor watches nothing on). The sweep is also the backstop for
anything the event path misses - a cursor gap, a container restart, a dropped block, or a
monitor silently orphaned by a contract upgrade that renamed an event. And unlike the
event path, which only re-checks the maturity that was just touched, the sweep covers
every live maturity including dormant ones.

The invariant predicates live in invariants.py and are shared with the triggers, so the
two paths cannot disagree about what counts as a breach.

Deployment: a Kubernetes CronJob running this in the same image as the monitor
(.github/workflows/monitor-gke-deploy.yml). Lives under config/ because the Dockerfile
copies only that directory into the final image.

Run locally from the repo root with the RPC env vars set:

    python3 config/triggers/scripts/sweep.py

Exits non-zero if any invariant is breached or any network could not be checked, so a
failed CronJob run is visible in Kubernetes as well as in Slack.
"""
import sys
import traceback

sys.path.insert(0, "config/triggers/scripts")

import invariants  # noqa: E402


def _breach(network_name, invariant, lines):
    return {"network": network_name, "invariant": invariant, "lines": lines}


def sweep_network(slug, target):
    """Runs every state invariant for one network. Returns a list of breach summaries."""
    network_name = target.get("display", slug)
    w3 = invariants.web3_for(slug)

    # One block for the whole network's sweep, so every currency and maturity is compared
    # against a single consistent view of chain state rather than drifting across blocks
    # mid-run. Batched calls must be pinned by number; the hash is re-checked at the end
    # to confirm the run didn't span a reorg.
    block = w3.eth.get_block("latest")
    block_number, block_hash = block["number"], block["hash"]

    currencies = invariants.get_currencies(
        w3, target["CurrencyController"], block_number
    )
    print(f"[{network_name}] block {block_number}, {len(currencies)} currencies")

    breaches = []

    # I2 - custody solvency, per currency.
    for result in invariants.fetch_solvency_batch(
        w3, target["TokenVault"], currencies, block_number
    ):
        status = "BREACH" if result.breached else "ok"
        print(
            f"  {result.ccy_name:<6} solvency  held={result.held} "
            f"owed={result.owed} {status}"
        )
        if result.breached:
            breaches.append(
                _breach(
                    network_name,
                    "I2 custody solvency",
                    [
                        f"TokenVault holds less {result.ccy_name} than it owes.",
                        f"Currency: {result.ccy_name}",
                        f"Held: {result.held}",
                        f"Owed: {result.owed}",
                        f"Shortfall: {result.shortfall}",
                        f"Token: {result.token_address}",
                        f"Block: {block_number}",
                    ],
                )
            )

    # I1 - future value conservation, per currency per maturity.
    controller = target["LendingMarketController"]
    for ccy_name, vault_address in target.get("fvconservation", {}).items():
        ccy_hex = invariants.currency_hex(ccy_name)
        maturities = invariants.get_maturities(w3, controller, ccy_hex, block_number)

        for result in invariants.fetch_conservation_batch(
            w3, vault_address, controller, ccy_hex, maturities, block_number
        ):
            status = "BREACH" if result.breached else "ok"
            print(
                f"  {ccy_name:<6} conserv @{result.maturity} "
                f"lend={result.lend_supply} borrow={result.borrow_supply} "
                f"pending={result.pending_amount} {status}"
            )
            if result.breached:
                breaches.append(
                    _breach(
                        network_name,
                        "I1 future value conservation",
                        [
                            (
                                f"{ccy_name} lend and borrow future value differ by "
                                f"{result.gap} with no pending settlement to explain it."
                                if result.pending_amount == 0
                                else (
                                    f"{ccy_name} future value gap of {result.gap} "
                                    f"exceeds what {result.pending_amount} of pending "
                                    "settlement can explain."
                                )
                            ),
                            f"Currency: {ccy_name}",
                            f"Maturity: {result.maturity}",
                            f"Lend future value: {result.lend_supply}",
                            f"Borrow future value: {result.borrow_supply}",
                            f"Pending order amount: {result.pending_amount}",
                            f"Unexplained: {result.unexplained}",
                            f"Block: {block_number}",
                        ],
                    )
                )

    # I4 - genesis value. OBSERVATION ONLY: recorded so its behaviour can be learned from
    # real data, never compared against a threshold and never alerted on. See
    # GenesisValueObservation in invariants.py for the two formulations that were tested
    # against live state and failed, and why a derived relation is still needed.
    genesis_vault = target.get("GenesisValueVault")
    if genesis_vault:
        for obs in invariants.observe_genesis_value(
            w3, genesis_vault, currencies, block_number
        ):
            if obs.lend_supply == 0 and obs.borrow_supply == 0:
                continue
            print(
                f"  {obs.ccy_name:<6} gv(obs)  lend={obs.lend_supply} "
                f"borrow={obs.borrow_supply} locked={obs.locked_balance} "
                f"gap={obs.gap} maturityGV={obs.maturity_genesis_value} "
                f"@{obs.current_maturity}"
            )

    if not invariants.verify_block_unchanged(w3, block_number, block_hash):
        # The reads spanned a reorg, so they may mix two chain branches. Discard rather
        # than report - the next run re-checks everything anyway.
        raise RuntimeError(
            f"block {block_number} changed during the sweep (reorg); results discarded"
        )

    return breaches


def main():
    targets = invariants.load_sweep_targets()
    breaches = []
    failed = []

    for slug, target in targets.items():
        try:
            breaches.extend(sweep_network(slug, target))
        except Exception:  # noqa: BLE001 - one network's RPC failing must not stop the rest
            print(f"[{slug}] sweep failed:\n{traceback.format_exc()}", file=sys.stderr)
            failed.append(slug)

    for breach in breaches:
        invariants.post_alert(
            f"{breach['invariant']} breach on {breach['network']} (sweep)",
            breach["lines"],
        )

    if failed:
        # Not an invariant breach, but a sweep that couldn't run is a blind spot, and
        # silence from a monitor should never look like health.
        invariants.post_alert(
            "Invariant sweep failed to complete",
            [
                f"Networks not checked: {', '.join(failed)}",
                "Invariants were not evaluated for these networks this run.",
            ],
            severity="HIGH",
        )

    if breaches or failed:
        sys.exit(1)

    print("sweep clean")


if __name__ == "__main__":
    main()
