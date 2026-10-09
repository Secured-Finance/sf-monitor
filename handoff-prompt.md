# Session handoff — invariant monitoring

Paste this into the new session, or just tell it to read this file.

---

## What we're doing

sf-monitor is a config-driven deployment of OpenZeppelin Monitor watching Secured
Finance's contracts. In September 2026 the protocol was exploited: a red-black tree
balancing bug in the order book orphaned resting orders, which the code then read as
"filled", minting future value with no counterparty — ~$176k extracted plus ~$2.6M of
standing phantom claims, over 34 hours, with four copycat attackers arriving behind the
first.

**That bug is fixed and deployed.** The objective is NOT to catch it again. The objective
is a monitor that catches the *next* attack, whose mechanism nobody has thought of yet.

The design principle we settled on: **monitor effects, not mechanisms.** Attack mechanisms
are unbounded; attack effects are nearly bounded, because to profit an attacker must end
up holding value they didn't start with, and that has to show up as a broken accounting
identity. So the monitor is given a description of *correct*, not a description of any
attack.

Read `invariant-monitoring-spec.md` in the repo root for the full design. **Note the spec
itself contains at least one claim that is empirically false** — see below.

## Repos

- Monitor: `/Users/arpit/Documents/SecuredFinance/sf-monitor` (this repo)
- Contracts source: `/Users/arpit/Documents/SecuredFinance/contracts` — use this, not the
  `node_modules/@secured-finance/contracts` build artifacts, when verifying behaviour
- `monitor/` in this repo is a prototype copied from the contracts repo. It depends on
  hardhat and **cannot run here**. It's a reference implementation, not the runtime.

## What was built yesterday

| File | Purpose |
|---|---|
| `tools/concerns/solvency.ts` | I2 — custody solvency, on `TokenVault.Withdraw` |
| `tools/concerns/fv-conservation.ts` | I1 — future value conservation, on `FutureValueVault.Transfer` |
| `config/triggers/scripts/invariants.py` | shared checks + batch fetchers; one implementation for event path and sweep |
| `config/triggers/scripts/solvency_check.py` | I2 trigger |
| `config/triggers/scripts/fv_conservation_check.py` | I1 trigger |
| `config/triggers/scripts/sweep.py` | timed sweep over all networks/currencies/maturities; also records I4 observationally |
| `config/triggers/scripts/soak.py` | replays invariants across historical blocks, asserts zero alerts |
| `config/triggers/scripts/test_invariants.py` | 26 synthetic-violation assertions on the predicates |

Modified: `tools/types.ts` (added `ResolvedAddress`, `splitByAddress`, `GenesisValueVault`),
`tools/generate-monitors.ts` (per-address monitor files + emits `config/sweep-targets.json`),
`tools/concerns/lending-market-addresses.ts` (generalised the per-currency proxy resolver),
`tools/concerns/index.ts`, `config/triggers/scripts.json`, `config/triggers/slack.json`
(re-tiered to an Info webhook), `.env.example`, `.gitignore`,
`.github/workflows/monitor-gke-deploy.yml` (sweep CronJob + the two new webhook secrets),
and the Info-tier webhook fallback in `total_deposits_check.py` / `block_unit_price_check.py`.

All existing concerns were left functionally intact — the invariant work is additive.

## Assume this is wrong until you re-check it

The previous session got the same *class* of thing wrong three times. Please do not inherit
its conclusions.

1. It asserted **future-value conservation is exact at all times, no tolerance band**. False
   — fully filled makers settle lazily, so there's a legitimate transient gap carried in
   `getPendingOrderAmount`. Caught only because a different session's code disagreed.
2. It then accepted that **genesis-value conservation is exact**, with the formulation
   `gvLend - gvBorrow == Σ getMaturityGenesisValue(ccy, m)`. Also false.
3. It then proposed **two** replacement GV formulations and tested both against live state.
   Both failed.

The common failure: declaring an invariant exact after reading *some* of the code paths
that write the quantities. Enumerate every write site before believing anything.

## Verification status — treat the tiers differently

**Checked against contracts source (file:line in the code comments) — still worth
spot-checking, but these were done carefully:**
- `FutureValueVault.sol:336-364` — supplies are `Σ max(balance,0)` / `Σ max(-balance,0)`
- Order fee is taker-only (`updateFundsForMaker` passes `_feeInFV = 0`,
  `FundManagementLogic.sol:279`); the reserve fund's FV leg is **inside** the balancing set
- Mint/burn legs emit `Transfer` from `address(0)`; the peer-to-peer path
  (`FutureValueVault.sol:242`) emits a single nonzero-value `Transfer`, so log-based sums
  must filter to the zero address
- `OPERATOR_ROLE` gates exactly six functions, all pause/unpause (and it includes unpause)
- Native currency needs no special case in I2 — `TransferHelper` wraps on deposit and
  unwraps on withdrawal (`:45-46`, `:54`), so the vault only holds the wrapped token

**Empirically validated against live testnets:**
- Sweep runs clean on both Sepolia networks (66 checks, ~8s)
- Soak clean: 360 checks over 150k Ethereum Sepolia blocks, 300 over 2M Arbitrum Sepolia
- The pending term does real work: Ethereum Sepolia USDC showed `gap=202,429,149` against
  `pending=200,000,000` — a flat equality would have false-positived
- Arbitrum Sepolia USDC @1791504000 carries a **1-base-unit** rounding residue with
  `pending=0`, stable since that market first traded

**Guessed, assumed, or never validated — highest priority to attack:**
- **`FV_DUST = 1000`** is an empirical guess from that single 1-unit observation. The real
  per-maturity worst case should be derived from the number of rounding sites per fill, or
  the residue removed in the contracts. Nothing should be auto-paused on I1 until then.
- **`FV_MULTIPLIER = 2`** came from the spec, not from analysis. §02 of the spec admits the
  exposure it leaves.
- **No trigger script has ever run against a real OZ Monitor match payload.** The payload
  shape (`monitor_match.EVM`, `matched_on_args.events[0].args`, `transaction.blockHash`,
  `monitor.addresses[0].address`) was copied from the two pre-existing scripts' comments and
  assumed. If any field name or nesting is wrong, the new triggers crash or silently do
  nothing. **This is the single biggest untested assumption in the whole thing.**
- `transaction.blockHash` may not exist in the payload at all — there's a fallback to
  `blockNumber`, but it has never been exercised either way.
- The `splitByAddress` design rests on `monitor.addresses[0]` identifying the emitter.
  Nobody confirmed OZ Monitor doesn't expose the emitting log address directly, which would
  be simpler.
- I4 genesis value is **observation-only** and must stay that way. See the long docstring
  on `GenesisValueObservation` in `invariants.py` for exactly which two formulations failed
  and why. Note there are five `_updateTotalSupply` call sites (`:507, :508, :538, :539,
  :664`) and the `:538/:539` pair was missed in the first analysis — assume more was missed.
- On both testnets the auto-roll log chain terminates at one hop (`prev == 0`), meaning no
  auto-roll has ever run there, yet GV supplies are populated. That is unexplained and
  probably matters.
- The batch/block-pinning change: batches are pinned by block **number** (web3.py can't
  resolve a hash inside a batch context), with the block hash re-verified afterwards to
  catch reorgs. The reasoning seems sound but the reorg path has never actually triggered.

## Not built, and why

- **I3, bounded per-user loss** — conservation can't see an attack that *takes* value from a
  user rather than creating it (an oracle manipulation forcing an early liquidation
  conserves future value exactly). Needs an independent price source, and "independent" is
  the whole point, so it's a decision not a coding task.
- **The responder** (auto-pause via an operator key) — I2 could be armed today; I1 should not
  be while `FV_DUST` is a guess.
- **CLAUDE.md** has not been updated for any of this — deliberately deferred by the user.

## The task for this session

The user's plan: **run against the production environment and replay the actual attack
blocks to see whether the detectors fire on real exploit state.** This is the first test that
would exercise the arithmetic and the plumbing together; so far everything has been
validated only on healthy state.

Attack window: 2026-09-05 22:11 UTC to 2026-09-06 18:01 UTC. Protocol paused 2026-09-07
07:55. Fix merged 2026-06-17 (`f3b7a169` in the contracts repo), deployed 2026-09-24.
Recovery flow: `bd466da1` / `218fd220` (`[#434]`) — this may have corrected the corrupt state,
so the breach could be visible in a window and then cleared.

Known blockers and gaps for that run:
- `production` env covers `ethereum_mainnet` + `arbitrum_one`; both RPC URLs are set
- **Archive access is the gate** — replaying ~26-day-old blocks needs `eth_call` against
  pruned state. Verify before anything else.
- `npm run pregenerate` wipes the existing monitor set, so generating for production
  replaces the development config on disk
- `soak.py` samples backwards from head over a span; it needs `--from-block/--to-block` to
  bracket a window 26 days back
- `sweep-targets.json` resolves the currency set and FutureValueVault addresses from
  *current* state. Beacon proxies survive implementation upgrades so they're probably valid
  for September, but **a silently empty or mismatched currency list would also produce "no
  breaches"** — prove the replay is actually looking at something before trusting a clean
  result.

Predictions written down in advance, so the run is a test and not a rationalisation:
- **I1 should fire hard** — a phantom mint raises lending supply with no borrowing
  counterpart, orders of magnitude past `FV_DUST`, with `pending = 0`
- **I2 should NOT fire** — the attacker withdrew through the normal path, which decrements
  `totalDepositAmount` in step with the real outflow, so the accounting stayed internally
  consistent. If solvency *does* fire, something about the attack is misunderstood
- **The breach should persist across blocks**, not flicker, since it's a state property

If I1 does not fire on real exploit blocks, the invariant or the thresholds are wrong and
that is the most important thing to find out.

## How to work

Verify claims against the contracts source yourself. Don't trust the spec, don't trust the
code comments, and don't trust this document — all three were written by a session that was
confidently wrong more than once. When you find something wrong, say so plainly.
