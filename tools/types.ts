import type { PublicClient } from 'viem';
import { ChainTarget } from './environments';

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

// Every concern alerts through a Python script trigger (config/triggers/scripts.json),
// not through a declarative Slack trigger. Two of them need it for logic - total-deposits
// computes a balance-drop percentage, block-unit-price keeps a rolling history - and the
// rest need it for formatting: upstream's declarative notification templating is plain
// `${...}` substitution over the decoded log, so it can only print a bytes32 currency as
// hex, a maturity as a Unix timestamp and an amount in base units, with no explorer link.
// See config/triggers/scripts/sf_alerts.py.
export interface ScriptTrigger {
  type: 'script';
  id: string;
}

export type Trigger = ScriptTrigger;

// Contracts the generator can resolve an address for via deployments.ts - add a name
// here when a new contract's concerns are added to tools/concerns/. CurrencyController
// is needed to enumerate currencies for concerns using resolveAddresses below, not
// itself the target of any concern.
export const CONTRACT_NAMES = [
  'LendingMarketController',
  'TokenVault',
  'CurrencyController',
] as const;
export type ContractName = (typeof CONTRACT_NAMES)[number];

export interface ContractArtifact {
  abi: unknown[];
  address: string;
}

// Passed to a concern's resolveAddresses hook (see below). `client` has no chain
// specified - reads only, and OZ Monitor config has no notion of a "chain id" beyond
// the network slug, so there's nothing for viem to validate an RPC's chain id against
// here anyway.
export interface ResolveAddressesContext {
  chain: ChainTarget;
  client: PublicClient;
  artifacts: Record<ContractName, ContractArtifact>;
}

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
  // Omit for concerns whose trigger works identically on every chain - which is all of
  // them currently: every script trigger resolves network name/RPC/explorer/contract
  // addresses itself at runtime (see config/triggers/scripts/sf_alerts.py) rather than
  // needing per-network config here.
  networks?: string[];
  // Only for concerns whose watched address can't be resolved from a single deployment
  // artifact - e.g. block-unit-price/itayose, which are emitted by a per-currency
  // LendingMarket beacon proxy (delegatecall'd from OrderActionLogic/OrderBookLogic),
  // never by LendingMarketController itself - verified directly with the Solidity team.
  // Resolved live via RPC at generate time (tools/concerns/lending-market-addresses.ts)
  // instead of a static artifact address. Concerns without this still resolve to
  // artifacts[contractName].address, as before.
  resolveAddresses?: (ctx: ResolveAddressesContext) => Promise<string[]>;
}
