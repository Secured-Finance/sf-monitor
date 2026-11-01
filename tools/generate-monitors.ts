// Generates config/monitors/<network>-<concern>.json for every chain the selected
// environment supports. Instead of calling the Defender SDK to create a monitor (as the
// old Autotask deploy scripts did), writes the equivalent OZ Monitor JSON straight to
// disk. Each concern's watched address(es) come from either a single deployment
// artifact (deployments.ts, same require() mechanism the old code used) or, for
// concerns needing more than that, a live RPC call (concern.resolveAddresses - see
// below); event ABI/match-conditions/trigger-wiring are defined once per concern in
// tools/concerns/.
//
// Only one environment ever runs on a given monitor instance (see environments.ts), so
// the "pregenerate" npm script (package.json) rimrafs every previously generated
// monitor file before this runs - each `npm run generate` call leaves only the
// selected environment's files on disk, never a mix of environments. Hand edits to a
// generated file will be lost - edit tools/concerns/* or tools/environments.ts instead.
//
// Usage: SF_ENV=development|staging|production npm run generate
//
// Some concerns (block-unit-price, itayose) can't resolve their watched address from a
// single deployment artifact - they're emitted by a per-currency LendingMarket beacon
// proxy, not LendingMarketController itself (see
// tools/concerns/lending-market-addresses.ts). Those need a live RPC call at generate
// time via concern.resolveAddresses, so this loads .env locally the same way
// docker-compose's env_file does for the monitor container - the generator never
// needed an RPC connection before this.
import * as dotenv from 'dotenv';
import * as fs from 'fs';
import * as path from 'path';

import { CONCERN_DEFINITIONS } from './concerns';
import { getContractArtifact } from './deployments';
import {
  ChainTarget,
  ENV_CHAINS,
  SF_ENVS,
  SF_ENV,
  isSfEnv,
} from './environments';
import { getPublicClient } from './rpc';
import {
  CONTRACT_NAMES,
  Concern,
  ContractArtifact,
  ContractName,
} from './types';

dotenv.config();

const REPO_ROOT = path.resolve(__dirname, '..');
const MONITORS_DIR = path.join(REPO_ROOT, 'config', 'monitors');
// Trigger scripts need addresses for contracts their own monitor doesn't watch - a
// liquidation or itayose alert formats amounts in token decimals, which only TokenVault
// can map a currency to (getTokenAddress), while the matched address is
// LendingMarketController or a LendingMarket proxy. The scripts can't require()
// @secured-finance/contracts (no Node in the runtime image, no npm auth either), so the
// same artifacts this generator already resolved are written out for them here, per
// network, for the active SF_ENV only - same lifecycle as config/monitors/*.json.
const CONTRACT_ADDRESSES_PATH = path.join(
  REPO_ROOT,
  'config',
  'triggers',
  'scripts',
  'contract_addresses.json',
);

function parseEnv(): SF_ENV {
  const value = process.env.SF_ENV;
  if (!value || !isSfEnv(value)) {
    console.error(`Set SF_ENV to one of: ${SF_ENVS.join(', ')}`);
    process.exit(1);
  }
  return value;
}

function monitorPath(network: string, concern: Concern): string {
  return path.join(MONITORS_DIR, `${network}-${concern.key}.json`);
}

function appliesTo(concern: Concern, network: string): boolean {
  return !concern.networks || concern.networks.includes(network);
}

function writeJson(filePath: string, data: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2) + '\n');
}

function buildMonitor(
  chain: ChainTarget,
  addresses: string[],
  concern: Concern,
) {
  return {
    name: `${concern.label} - ${chain.display}`,
    paused: false,
    networks: [chain.network],
    addresses: addresses.map((address) => ({
      address,
      contract_spec: [concern.libraryEventAbi],
    })),
    match_conditions: {
      events: [{ signature: concern.eventSignature, expression: null }],
      functions: [],
      transactions: [{ status: 'Success', expression: null }],
    },
    trigger_conditions: [],
    triggers: [concern.trigger.id, 'sf_monitor_log'],
  };
}

async function main(): Promise<void> {
  const env = parseEnv();
  const chains = ENV_CHAINS[env];
  const concerns = Object.values(CONCERN_DEFINITIONS);
  let count = 0;
  const contractAddresses: Record<string, Record<string, string>> = {};

  for (const chain of chains) {
    // Every known contract's artifact is resolved up front, regardless of which
    // concerns apply to this chain - CONTRACT_NAMES is small and concerns needing
    // resolveAddresses (see tools/types.ts) need more than just their own
    // contractName's artifact (e.g. block-unit-price/itayose need both
    // LendingMarketController and CurrencyController).
    const artifacts = Object.fromEntries(
      CONTRACT_NAMES.map((contractName) => [
        contractName,
        getContractArtifact(contractName, chain.deployStage),
      ]),
    ) as Record<ContractName, ContractArtifact>;

    contractAddresses[chain.network] = Object.fromEntries(
      CONTRACT_NAMES.map((contractName) => [
        contractName,
        artifacts[contractName].address,
      ]),
    );

    const client = getPublicClient(chain.network);

    for (const concern of concerns) {
      if (!appliesTo(concern, chain.network)) continue;

      const addresses = concern.resolveAddresses
        ? await concern.resolveAddresses({ chain, client, artifacts })
        : [artifacts[concern.contractName].address];

      const filePath = monitorPath(chain.network, concern);
      writeJson(filePath, buildMonitor(chain, addresses, concern));
      count += 1;
    }
  }

  // Rewritten whole, never merged into: it must describe exactly the environment whose
  // monitors are on disk, so a leftover entry from a previous SF_ENV can't have a
  // trigger script formatting an alert against another deployment's TokenVault.
  writeJson(CONTRACT_ADDRESSES_PATH, contractAddresses);

  // eslint-disable-next-line no-console
  console.log(
    `generated ${count} monitor file(s) + contract addresses for ${
      chains.length
    } network(s) for env=${env}`,
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
