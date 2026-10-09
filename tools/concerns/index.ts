import { autoRolls } from './auto-rolls';
import { blockUnitPrice } from './block-unit-price';
import { fvConservation } from './fv-conservation';
import { itayose } from './itayose';
import { liquidations } from './liquidations';
import { solvency } from './solvency';
import { totalDeposits } from './total-deposits';
import { Concern } from '../types';

// Every concern the generator knows about. total-deposits, block-unit-price and
// solvency need custom logic beyond a plain event -> Slack mapping - see
// config/triggers/scripts/total_deposits_check.py, block_unit_price_check.py and
// solvency_check.py.
//
// solvency is the first of the protocol invariants in invariant-monitoring-spec.md.
// The invariant concerns are additive: they run alongside the original concerns rather
// than replacing them.
export const DEFINITIONS = [
  liquidations,
  autoRolls,
  itayose,
  totalDeposits,
  blockUnitPrice,
  solvency,
  fvConservation,
];

// Keyed by concern key. New concerns/contracts only need adding to DEFINITIONS above -
// this map and generate-monitors.ts pick them up automatically.
export const CONCERN_DEFINITIONS: Record<string, Concern> = Object.fromEntries(
  DEFINITIONS.map((concern) => [concern.key, concern]),
);
