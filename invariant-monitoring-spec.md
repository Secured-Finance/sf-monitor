# Invariant Monitoring Spec

Four accounting identities that hold whenever the protocol is behaving, and break whenever
value is created or taken that shouldn't be — regardless of the mechanism that did it.

| | |
|---|---|
| **Target** | sf-monitor (OZ Monitor) |
| **Chains** | Ethereum · Arbitrum · Filecoin |
| **Source** | secured-finance/contracts @ develop |
| **Revised** | 1 Oct 2026 |

---

## 0. Objective

Monitor **effects, not mechanisms**. Attack mechanisms are unbounded and the next one is by
definition the one nobody modelled. Attack *effects* are nearly bounded: to profit, an attacker
must end up holding value they did not start with, and in this protocol there are only two
ledgers that can record that — future value with no counterparty, and tokens leaving the vault
that deposits don't back.

So the monitor is not given a description of any attack. It is given a description of *correct*,
and alarms on everything else. A detector shaped around an orphaned order or an absurd
settlement price is a fingerprint of one bug; a reentrancy, an oracle failure, a bad access
check, or an arithmetic error shipped next year produces none of those signatures and walks
straight past it.

---

## 1. The invariant set

Four checks. Status reflects readiness to arm, not importance — the ordering in §6 ships the
cheapest and least tunable first.

### I1 — Future-value conservation, per currency per maturity · `SHIP`

Every lender claim carries a matching borrower obligation. This is the check that fires at value
**creation**, before anything is extracted.

```
| lendSupply − borrowSupply |  ≤  pendingOrderAmount × FV_MULTIPLIER

lendSupply          FutureValueVault.getTotalLendingSupply(maturity)
borrowSupply        FutureValueVault.getTotalBorrowingSupply(maturity)
pendingOrderAmount  LendingMarketController.getPendingOrderAmount(ccy, maturity)
FV_MULTIPLIER       2
```

**Not exact at all times.** Only the partially filled maker settles synchronously; fully filled
makers settle lazily at their own cleanup. The outstanding amount is carried in the protocol's
own `pendingOrderAmount` counter, which is why the tolerance term exists rather than a flat
equality.

> **When `pendingOrderAmount == 0`, the tolerance is zero and any nonzero gap is a breach.**
> The check is exact whenever the market is quiescent and fuzzy only while settlement is
> genuinely outstanding — from state alone, no log bookkeeping. State this property in the code;
> don't leave it implicit in the arithmetic.

The increment is keyed by the market's maturity and the decrement by the user's current
maturity — different variables. They cannot diverge: `updateUserMaturity` reverts
`PastMaturityOrderExists` while the user holds active order ids, and those ids are pruned by the
same cleanup that decrements. Cover it with a named test anyway, since this property is what
arms the auto-pause.

### I2 — Custody solvency, per currency · `SHIP FIRST`

The vault physically holds what it claims to hold. This is the chokepoint: nothing is extracted
without crossing it, so it catches any value-extracting attack regardless of mechanism — but it
is the last to fire, when money is already moving.

```
IERC20(token).balanceOf(TokenVault)  ≥  TokenVault.getTotalDepositAmount(ccy)
```

One `balanceOf`, one view call, no tolerance, no state, no log semantics, nothing to tune. It is
the cheapest check in this document and should be live before the others are finished.

> Open: confirm how native ETH is custodied versus wrapped before writing the comparison for
> that currency.

### I3 — Bounded per-user loss · `SHIP`

Conservation cannot see an attack that takes value from a *user* rather than creating it from
nothing. An oracle manipulation that fires a liquidation early, or an unfair fill at a
manipulated price, conserves future value exactly — one party's gain is another's loss — and
leaves the vault solvent. Both I1 and I2 see a normal day.

```
no single transaction reduces one account's net position value
by more than LOSS_FRACTION, unless it is a liquidation that:
  · the account was genuinely eligible for, priced from an
    independent feed — not the one the transaction used, and
  · respects LIQUIDATION_THRESHOLD_RATE (12500),
    LIQUIDATOR_FEE_RATE (500), LIQUIDATION_PROTOCOL_FEE_RATE (200)
```

Liquidation is the legitimate case of a user losing a lot at once, so it must be the gated
exception rather than a blanket exemption. The independent price source is what makes this check
meaningful — checking the transaction's own oracle against itself proves nothing.

This one is a design sketch, not verified against the liquidation code the way I1 and I2 were.
It needs a pass over `LiquidationLogic` before anyone builds it.

### I4 — Genesis-value conservation, per currency · `ALERT-ONLY — DO NOT ARM`

The same double-entry property at the post-auto-roll layer. Ship it to *learn* its behaviour. Do
not wire it to a pause, and do not trust any threshold on it yet.

> **The obvious formulation is wrong.** An earlier draft asserted
> `gvLend − gvBorrow == Σ getMaturityGenesisValue(ccy, m)` and called it exact. It isn't.
> `maturityBalances` is written in exactly one place — `GenesisValueVault.sol:668` — while GV
> `balances` are written in eight, and four of those update neither the supplies nor
> `maturityBalances`: `lock` (:463), `unlock` (:486), and both `executeForcedReset` paths
> (:561, :596). The two sides of that equation move independently.

Worse, `executeForcedReset` reduces a user's balance while leaving the supplies untouched, so
after an emergency reset the GV totals permanently overstate and `gvLend == gvBorrow` does not
self-restore either. `FutureValueVault` has explicit `removedLendingSupply` /
`removedBorrowingSupply` counters for exactly this bookkeeping; `GenesisValueVault` has no
equivalent.

Those paths are rare — which is precisely the shape that produces a 3am false positive on an
armed responder. Derive the real relation from all eight write sites, validate it against a
soak, and only then consider a threshold.

Note also that GV is **lagging**: future value converts to genesis value only for maturities that
have already auto-rolled. A mint-and-exit inside a single live maturity never touches this layer
at all. I4 covers a different surface than I1, not a stronger version of it.

---

## 2. Why this catches what we can't predict

The design was validated by replaying the September incident against it. The value of that
exercise is not the incident — it is what the arithmetic shows about the general case.

### Detectability and profitability are the same axis

Unit price runs 0–10,000, where 10,000 is par, so a fill at price `p` mints
`amount × 10000 / p` of future value. I1 breaches when that exceeds `2 × pendingOrderAmount`,
which rearranges to:

```
amount  >  pendingOrderAmount × p / 5000
```

The September payload sat at unit price 2. At that price the attacker cannot mint more than
**one 2,500th of the legitimately-pending settlement pool** without tripping the alarm. The
tolerance band scales with honest pending settlement; the payoff scales with `1 / p`. An attacker
who raises the price to stay inside the band is, by construction, funding the position honestly —
at which point they are not stealing.

> **Known ceiling.** As `p` approaches 5,000 the band permits minting up to roughly the size of
> the pending pool. That is the exposure I1 tolerates today, and it is a consequence of bridging
> a present-value counter to a future-value gap with a multiplier. It collapses to zero once
> pending *future value* is tracked exactly from fill events rather than inferred — see §8.

### What it would have done on 5 September

The phantom mint calls `FutureValueVault.increase`, which raises `totalLendingSupplies` with no
movement on the borrowing side and emits `Transfer(address(0), attacker, …)` — the event I1
subscribes to. The breach is same-block, in the attacker's first transaction. Detection plus
confirmation depth plus inclusion is roughly a minute on Ethereum.

| | Actual | Armed on I1 |
|---|---|---|
| Time to pause | 34 hours | ~1 minute |
| Exploit transactions landed | ~15 | 1 |
| Independent attacker clusters | 5 | 1 |
| Realized loss | $176,200 | one tx of it |
| Standing phantom claims | ~$2.6M | one tx of it |

The first transaction is not recovered — mint and extraction shared a transaction. Everything
after it is. The operational fact worth carrying to whoever approves arming: **the second
independent attacker arrived 75 minutes after the first**, and three more followed. Once a live
bug is being exploited the clock runs in minutes, so alert-only with a human at 30 minutes still
stops four of five clusters, and alert-only with a human asleep reproduces what happened.

At no point does any of this require knowing what the bug was. The monitor knew the books must
balance, saw that they didn't, and stopped the protocol. Root-cause analysis still takes its 34
hours — it just takes them with the money still in the vault.

---

## 3. Implementation notes

Five traps, each of which is the obvious wrong instinct.

1. **The reserve fund is inside the balancing set.** The order fee is charged taker-only —
   `updateFundsForMaker` passes `_feeInFV = 0` (`FundManagementLogic.sol:279`) — and the reserve
   fund's future-value leg is what closes the taker's side. Excluding it as "protocol-internal"
   produces a false gap of exactly `feeInFV` on every taker fill.

2. **Log-based checks must filter to `from == address(0)`.** `increase` / `decrease` emit matched
   mint and burn legs from the zero address, but the peer-to-peer path
   (`FutureValueVault.sol:242`) emits a *single* nonzero-value `Transfer`. Summing all `Transfer`
   values breaks on every ordinary position transfer.

3. **One monitor file per (network, currency).** `FutureValueVault.Transfer` carries `maturity`
   but not `ccy`, so a script reading `monitor.addresses[0]` cannot tell which currency's vault
   emitted it when a monitor holds several. Per-currency generation makes that unambiguous and
   puts the currency in the monitor name for free. `GenesisValueVault.Transfer` does carry `ccy`.

4. **Pin every read in one check to the same block hash** — not a block number, not `latest`. Two
   reads landing at different heights produce a phantom breach. Use the hash from the match
   payload.

5. **Wait for confirmations before acting.** The invariant cannot false-positive; the
   *observation* can. An alert that pauses a live protocol needs depth behind it.

Solvency and the periodic sweep have no natural triggering event, so they need a scheduled job —
a CronJob alongside the deployment running the same checks over `getMaturities(ccy)` per
currency. That also backstops anything the event path misses: a cursor gap, a restart, a dropped
block.

---

## 4. Severity and routing

Severity comes from **which invariant is threatened**, not from transaction size. In a
collateralised protocol, amount thresholds are a weak proxy — a large withdrawal that passed the
collateral check is safe; a small accounting drift is not.

| Tier | Meaning | Routing |
|---|---|---|
| **CRITICAL** | An invariant that cannot break during legitimate operation | Dedicated channel, auto-pause eligible |
| **HIGH** | Economic heuristic, needs a threshold | Page a human — never auto-pause |
| **INFO** | Routine activity with no invariant attached | Low-traffic channel |

Most of what fires today is INFO: `OrderBooksRotated` on schedule, individual `ItayoseExecuted`,
individual `Withdraw` events the contract already collateral-checked before emitting. Moving
those to their own channel and reserving the main channel for CRITICAL is what makes the stream
triageable — an alert in the main channel should always mean something.

**Silence is also a signal.** A monitor matching nothing for an unexpected window may be watching
a quiet market, or may have been orphaned by an upgrade or a renamed event. Heartbeat the
pipeline so those two look different.

---

## 5. Response

`TokenVault.pause()` and `LendingMarketController.pauseLendingMarket(ccy)` are both
`onlyOperator`. Across the entire protocol, `OPERATOR_ROLE` gates **exactly six functions** —
those two, `ReserveFund.pause`, and the three corresponding unpauses. Nothing else: no upgrades,
no fund movement. That is what makes an automated responder a bounded risk; the worst case of a
false positive is an unnecessary pause a human lifts.

Two constraints follow from the role's shape:

- **The role includes unpause**, so a key that can pause can also lift a deliberate pause. The
  asymmetry cannot be enforced at the role level — enforce it in the process: no unpause code
  path in the responder binary.
- **Give the responder its own operator account**, so revoking it does not revoke the team's.

The responder runs as its own minimal process, not inside the monitor container — that container
installs packages and runs trigger scripts, and it should not hold a key.

Which lever depends on what fired: `pauseLendingMarket(ccy)` stops further minting in one
currency and leaves the rest trading; `TokenVault.pause()` stops withdrawals protocol-wide and is
what prevents already-minted value from leaving.

---

## 6. Build order

| | Step | Buys |
|---|---|---|
| 1 | **I2 solvency + the scheduled sweep** | Cheapest check, nothing to tune, closes the extraction chokepoint. Live before anything else is finished. |
| 2 | **I1 future-value conservation, alert-only** | Catches unbacked value at creation. The concern, the script trigger, and the pending-term arithmetic. |
| 3 | **Re-tier existing concerns to INFO** | Makes the main channel mean something before anyone is asked to trust it. |
| 4 | **Clean soak against forked traffic** | Confidence the thresholds are right. Catches a mis-modelled settlement term before it can halt production. |
| 5 | **Responder process, armed on I1 and I2 only** | Turns detection into prevention. |
| 6 | **I3 bounded per-user loss** | Needs an independent price source; closes the conserving-attack gap. |
| 7 | **I4 genesis value, alert-only, indefinitely** | Arm only after its true relation is derived and soaked. |

---

## 7. Testing

Two runs, both on a fork, and the fork harness stays in the contracts repo — it is the one thing
sf-monitor cannot easily do.

- **Synthetic violation.** Break each invariant deliberately; assert the detector fires. For I1,
  mint future value with no counterparty and assert the breach is same-block.
- **Clean soak.** Replay real traffic; assert zero alerts. This matters more. A false-positive
  rate above zero is what gets monitoring switched off — and switched off is how it is not there
  on the day it matters.

Stage the rollout as alert-only → soak → arm. The delay is not caution about the invariants; it
is confidence in the plumbing. If a flaky RPC pauses the protocol twice at 3am, someone disarms
the automation permanently.

---

## 8. Open items

- **Exact pending future value for I1.** Track it from fill events — `OrderExecuted` carries
  `placedOrderId` / `placedAmount`, and maker settlement debits arrive as `OrdersFilledInAsync`
  and `OrderPartiallyFilled` — rather than inferring it from a present-value counter. Collapses
  the band in §2 to zero and removes the only tolerance in the set.
- **Derive the real GV relation** from all eight `balances` write sites before I4 is trusted.
- **Native ETH custody** for the I2 comparison.
- **Independent price source** for I3's liquidation gate — it must not be the feed the
  transaction itself used.

---

## Appendix — source references

Verified against `develop`, 1 Oct 2026.

| Reference | What it establishes |
|---|---|
| `FutureValueVault.sol:336-364` | Supplies maintained as Σ max(balance, 0) and Σ max(−balance, 0) |
| `FutureValueVault.sol:242` | Peer-to-peer transfer emits a single nonzero `Transfer` |
| `FundManagementLogic.sol:279` | `updateFundsForMaker` passes `_feeInFV = 0` |
| `FundManagementLogic.sol:927` | Pending amount decremented at cleanup |
| `LendingMarketUserLogic.sol:219-221` | Pending amount incremented for fully filled makers |
| `OrderBookLib.sol:452-466` | `updateUserMaturity` reverts `PastMaturityOrderExists` |
| `GenesisValueVault.sol:463, :486, :561, :596` | Balance writes with no paired supply or `maturityBalances` update |
| `GenesisValueVault.sol:668` | The sole `maturityBalances` write |
| `TokenVault.sol:615, :622` | `OPERATOR_ROLE` surface |
| `ReserveFund.sol:67, :75` | `OPERATOR_ROLE` surface |
| `LendingMarketController.sol:1026, :1038` | `OPERATOR_ROLE` surface |

Claims against contract source were verified directly; §2's counterfactual and §6's ordering are
judgement. I4 is explicitly unproven and marked as such.
