import { autoRolls } from './auto-rolls';
import { itayose } from './itayose';
import { liquidations } from './liquidations';
import { totalDeposits } from './total-deposits';
import { Concern } from '../types';

// Every concern the generator knows about. total-deposits and
// block-unit-price-fluctuations need custom logic beyond a plain event -> Slack mapping
// - see config/triggers/scripts/total_deposits_check.py.
export const DEFINITIONS = [liquidations, autoRolls, itayose, totalDeposits];

// Keyed by concern key. New concerns/contracts only need adding to DEFINITIONS above -
// this map and generate-monitors.ts pick them up automatically.
export const CONCERN_DEFINITIONS: Record<string, Concern> = Object.fromEntries(
  DEFINITIONS.map((concern) => [concern.key, concern]),
);
