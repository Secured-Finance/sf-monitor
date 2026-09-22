export interface AbiEventInput {
  indexed: boolean;
  internalType: string;
  name: string;
  type: string;
}

export interface AbiEvent {
  anonymous: boolean;
  inputs: AbiEventInput[];
  name: string;
  type: 'event';
}

// A concern's trigger is either a declarative Slack alert (config/triggers/slack.json)
// or an internal trigger that runs custom logic before deciding whether to alert
// (config/triggers/scripts.json) - e.g. total-deposits needs to compute a balance-drop
// percentage itself, which a plain event->Slack mapping can't express.
export interface SlackTrigger {
  type: 'slack';
  id: string;
}

export interface ScriptTrigger {
  type: 'script';
  id: string;
}

export type Trigger = SlackTrigger | ScriptTrigger;

// Contracts the generator can resolve an address for via deployments.ts - add a name
// here when a new contract's concerns are added to tools/concerns/.
export const CONTRACT_NAMES = [
  'LendingMarketController',
  'TokenVault',
] as const;
export type ContractName = (typeof CONTRACT_NAMES)[number];

// One contract event OZ Monitor should watch and alert on.
export interface Concern {
  key: string;
  contractName: ContractName;
  label: string;
  eventSignature: string;
  trigger: Trigger;
  // Library events aren't in the caller contract's own bundled ABI (Solidity doesn't
  // include a library's events in the caller's ABI), so declared by hand here for
  // LendingMarketController concerns. TokenVault concerns can use its own bundled ABI
  // event instead.
  libraryEventAbi: AbiEvent;
  // Restricts which chains this concern is generated for, by OZ Monitor network slug.
  // Omit for concerns whose trigger works identically on every chain - both the LMC
  // concerns' Slack triggers and total-deposits' script trigger, which resolves
  // network/RPC/TokenVault address itself from the match payload at runtime (see
  // config/triggers/scripts/total_deposits_check.py) rather than needing per-network
  // config here.
  networks?: string[];
}
