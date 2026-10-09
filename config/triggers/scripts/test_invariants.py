"""Synthetic violation tests for the invariant predicates
(invariant-monitoring-spec.md §07, "break each invariant deliberately; assert the
detector fires").

Why this exists: every live run of the sweep and the soak has been clean, which only ever
exercises the negative case. A detector that never fires and a detector that cannot fire
look identical from a clean run. These tests drive the predicates with constructed values,
including the shape of the September incident, so we know the alarm works before anything
is wired to a pause.

Scope is deliberately the predicates, not the RPC plumbing: `breached`, `gap`,
`explainable` and friends live on the result dataclasses and are shared by the event path
and the sweep, so they are the single place a breach is defined. Exercising real contract
state under a manipulated chain belongs in the contracts repo's fork harness.

No test framework - the repo has none, and this needs to run inside the monitor image:

    python3 config/triggers/scripts/test_invariants.py
"""
import sys

sys.path.insert(0, "config/triggers/scripts")

import invariants  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition):
    (PASSED if condition else FAILED).append(name)


def solvency(held, owed):
    return invariants.SolvencyResult(
        ccy_name="USDC", token_address="0x" + "11" * 20, held=held, owed=owed
    )


def conservation(lend, borrow, pending):
    return invariants.ConservationResult(
        ccy_name="USDC",
        maturity=1791504000,
        lend_supply=lend,
        borrow_supply=borrow,
        pending_amount=pending,
    )


def test_solvency():
    check("solvency: exact cover is not a breach", not solvency(1000, 1000).breached)
    check("solvency: surplus is not a breach", not solvency(1001, 1000).breached)
    check("solvency: empty vault is not a breach", not solvency(0, 0).breached)

    # The violation: one base unit short is still insolvent.
    short = solvency(999, 1000)
    check("solvency: one unit short BREACHES", short.breached)
    check("solvency: shortfall is exact", short.shortfall == 1)

    drained = solvency(0, 97_600_000_000_000)
    check("solvency: fully drained vault BREACHES", drained.breached)
    check("solvency: drained shortfall is whole balance",
          drained.shortfall == 97_600_000_000_000)
    check("solvency: surplus reports no shortfall", solvency(1001, 1000).shortfall == 0)


def test_conservation_quiescent():
    """With no pending settlement the only tolerance is rounding dust."""
    check("conservation: balanced books are clean",
          not conservation(1000, 1000, 0).breached)

    # The real residue observed on Arbitrum Sepolia USDC @1791504000.
    check("conservation: observed 1-unit rounding residue does not fire",
          not conservation(303_927_520, 303_927_519, 0).breached)

    # Boundaries of FV_DUST.
    dust = invariants.FV_DUST
    check("conservation: gap exactly at the dust bound is clean",
          not conservation(1000 + dust, 1000, 0).breached)
    check("conservation: one unit past the dust bound BREACHES",
          conservation(1000 + dust + 1, 1000, 0).breached)

    # Direction must not matter - an excess of borrower obligation is equally wrong.
    check("conservation: breach detected in the borrow direction too",
          conservation(1000, 1000 + dust + 1, 0).breached)


def test_conservation_with_pending():
    """While settlement is outstanding the gap must still be explainable by it."""
    # Real shape from Ethereum Sepolia: a gap larger than pending in nominal terms,
    # because the gap is future value and pending is present value.
    live = conservation(19_930_508_492, 20_132_937_641, 200_000_000)
    check("conservation: live PV/FV gap within the band is clean", not live.breached)

    pending = 1_000_000
    band = pending * invariants.FV_MULTIPLIER + invariants.FV_DUST
    check("conservation: gap at the band edge is clean",
          not conservation(band, 0, pending).breached)
    check("conservation: gap past the band BREACHES",
          conservation(band + 1, 0, pending).breached)

    over = conservation(band + 5000, 0, pending)
    check("conservation: unexplained excess is reported exactly",
          over.unexplained == 5000)
    check("conservation: clean result reports no unexplained amount",
          conservation(0, 0, 0).unexplained == 0)


def test_september_incident_shape():
    """The incident the design was validated against.

    Reported post-incident state for USDC: 2,507,561 lend future value against 38.96
    borrow, with no pending settlement to explain it. Scaled to USDC's 6 decimals.
    """
    incident = conservation(2_507_561_000_000, 38_960_000, 0)
    check("september: phantom mint BREACHES", incident.breached)
    check("september: substantially all of the gap is unexplained",
          incident.unexplained > 2_507_000_000_000)

    # It must also fire on the FIRST transaction, not only at the final scale - a single
    # phantom mint is already orders of magnitude past the dust bound.
    first_tx = conservation(1_000_000_000 + 38_960_000, 38_960_000, 0)
    check("september: a single phantom mint BREACHES on its own", first_tx.breached)


def test_helpers():
    round_trip = invariants.decode_bytes32_string(invariants.currency_hex("USDC"))
    check("helpers: currency hex round-trips", round_trip == "USDC")
    check("helpers: currency hex is 32 bytes",
          len(invariants.currency_hex("USDC")) == 66)

    targets = {"fvconservation": {"USDC": "0xAbCdEf" + "00" * 17}}
    check("helpers: address maps back to its currency case-insensitively",
          invariants.currency_for_address(
              targets, "fvconservation", "0xabcdef" + "00" * 17) == "USDC")
    check("helpers: unknown address maps to nothing",
          invariants.currency_for_address(
              targets, "fvconservation", "0x" + "99" * 20) is None)
    check("helpers: unknown concern maps to nothing",
          invariants.currency_for_address(targets, "nope", "0x" + "99" * 20) is None)


def main():
    for test in (
        test_solvency,
        test_conservation_quiescent,
        test_conservation_with_pending,
        test_september_incident_shape,
        test_helpers,
    ):
        test()

    for name in PASSED:
        print(f"  ok    {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
