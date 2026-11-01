import { autoRolls } from './auto-rolls';
import { blockUnitPrice } from './block-unit-price';
import { itayose } from './itayose';
import { liquidations } from './liquidations';
import { totalDeposits } from './total-deposits';
import { Concern } from '../types';

// Every concern the generator knows about. All of them alert through a Python script
// trigger: total-deposits and block-unit-price because they need custom logic (see
// config/triggers/scripts/total_deposits_check.py and block_unit_price_check.py), the
// rest because human-readable formatting of currencies/maturities/amounts isn't
// expressible in a declarative trigger (config/triggers/scripts/event_alert.py).
export const DEFINITIONS = [
  liquidations,
  autoRolls,
  itayose,
  totalDeposits,
  blockUnitPrice,
];

// Keyed by concern key. New concerns/contracts only need adding to DEFINITIONS above -
// this map and generate-monitors.ts pick them up automatically.
export const CONCERN_DEFINITIONS: Record<string, Concern> = Object.fromEntries(
  DEFINITIONS.map((concern) => [concern.key, concern]),
);
