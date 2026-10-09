"""Clean soak - replays the invariants across historical blocks and asserts zero alerts
(invariant-monitoring-spec.md §07).

This is the step that matters more than synthetic-violation testing. A false-positive rate
above zero is what gets monitoring switched off, and switched off is how it isn't there on
the day it matters. Nothing should be armed to a responder until this run is clean over a
meaningful stretch of real traffic.

It has already earned its place: the first full run of the sweep found a 1-base-unit
future-value gap on Arbitrum Sepolia USDC that no amount of reading the contracts had
predicted, which is why FV_DUST exists.

Usage, from the repo root:

    python3 config/triggers/scripts/soak.py [--samples N] [--span BLOCKS] [--network SLUG]
    python3 config/triggers/scripts/soak.py --from-block X --to-block Y [--samples N] [--network SLUG]

Walks N evenly spaced blocks back over SPAN blocks from head, evaluating every invariant
at each. Reports any breach with the block it occurred at so it can be investigated
directly. Exits non-zero if the soak is not clean.

--from-block/--to-block bracket an explicit historical block range instead of sampling
backward from head - for replaying a known window (e.g. the September attack) rather than
soaking recent traffic. Both are required together; --span is ignored when they're given,
--samples still controls how many points are sampled within the range.

Note this samples rather than replaying every block: invariants are state properties, so a
breach introduced at some block persists until something corrects it, and sampling finds
persistent breaches cheaply. It will miss a breach that opened and closed entirely between
two samples - the event-path triggers are what cover that case, since they run on every
matched event.
"""
import argparse
import sys
import traceback
from collections import Counter

sys.path.insert(0, "config/triggers/scripts")

import invariants  # noqa: E402


def evaluate_at(w3, target, block_number):
    """Every invariant at one block. Returns (checks_run, [breach strings])."""
    breaches = []
    checks = 0

    currencies = invariants.get_currencies(
        w3, target["CurrencyController"], block_number
    )

    for result in invariants.fetch_solvency_batch(
        w3, target["TokenVault"], currencies, block_number
    ):
        checks += 1
        if result.breached:
            breaches.append(
                f"I2 solvency {result.ccy_name}: held={result.held} "
                f"owed={result.owed} shortfall={result.shortfall}"
            )

    controller = target["LendingMarketController"]
    for ccy_name, vault_address in target.get("fvconservation", {}).items():
        ccy_hex = invariants.currency_hex(ccy_name)
        maturities = invariants.get_maturities(w3, controller, ccy_hex, block_number)

        for result in invariants.fetch_conservation_batch(
            w3, vault_address, controller, ccy_hex, maturities, block_number
        ):
            checks += 1
            if result.breached:
                breaches.append(
                    f"I1 conservation {ccy_name}@{result.maturity}: "
                    f"lend={result.lend_supply} borrow={result.borrow_supply} "
                    f"pending={result.pending_amount} gap={result.gap} "
                    f"unexplained={result.unexplained}"
                )

    # I4 is observation-only and cannot breach, so it is not evaluated here.
    return checks, breaches


def soak_network(slug, target, samples, span, from_block=None, to_block=None):
    w3 = invariants.web3_for(slug)
    name = target.get("display", slug)

    if from_block is not None:
        step = max((to_block - from_block) // max(samples - 1, 1), 1)
        blocks = sorted({min(from_block + step * i, to_block) for i in range(samples)})
        print(
            f"\n=== {name}: {len(blocks)} samples over blocks {from_block}-{to_block} ==="
        )
    else:
        head = w3.eth.block_number
        step = max(span // max(samples - 1, 1), 1)
        blocks = sorted({head - step * i for i in range(samples) if head - step * i > 0})
        print(f"\n=== {name}: {len(blocks)} samples over {span} blocks to head {head} ===")

    total_checks = 0
    findings = []
    skipped = 0

    for block_number in blocks:
        try:
            checks, breaches = evaluate_at(w3, target, block_number)
        except Exception as error:
            # Pruned state on a non-archive RPC is expected the further back we go, and
            # is not a soak failure - it just means that sample can't be evaluated.
            print(f"  block {block_number}: unavailable ({type(error).__name__})")
            skipped += 1
            continue

        total_checks += checks
        if breaches:
            for breach in breaches:
                print(f"  block {block_number}: BREACH {breach}")
                findings.append((block_number, breach))
        else:
            print(f"  block {block_number}: clean ({checks} checks)")

    print(
        f"  -> {total_checks} checks across {len(blocks) - skipped} blocks, "
        f"{len(findings)} breaches, {skipped} samples unavailable"
    )
    return total_checks, findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--span", type=int, default=200_000)
    parser.add_argument("--network", default=None)
    parser.add_argument("--from-block", type=int, default=None)
    parser.add_argument("--to-block", type=int, default=None)
    args = parser.parse_args()

    if (args.from_block is None) != (args.to_block is None):
        parser.error("--from-block and --to-block must be given together")

    targets = invariants.load_sweep_targets()
    if args.network:
        targets = {args.network: targets[args.network]}

    total_checks = 0
    all_findings = []

    for slug, target in targets.items():
        try:
            checks, findings = soak_network(
                slug, target, args.samples, args.span, args.from_block, args.to_block
            )
        except Exception:
            print(f"[{slug}] soak failed:\n{traceback.format_exc()}", file=sys.stderr)
            all_findings.append((slug, "soak could not run"))
            continue
        total_checks += checks
        all_findings.extend((f"{slug}@{b}", f) for b, f in findings)

    print(f"\n{'=' * 60}")
    if not all_findings:
        print(f"SOAK CLEAN - {total_checks} invariant checks, zero alerts")
        return

    print(f"SOAK NOT CLEAN - {len(all_findings)} findings across {total_checks} checks")
    for kind, count in Counter(f.split(":")[0] for _, f in all_findings).most_common():
        print(f"  {count:>4}  {kind}")
    sys.exit(1)


if __name__ == "__main__":
    main()
