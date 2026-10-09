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
// Addresses the periodic invariant sweep needs (config/triggers/scripts/sweep.py). The
// sweep runs the same checks as the script triggers but on a timer rather than off an
// event, so it has no match payload to read a contract address from - and it's Python,
// so it can't call deployments.ts itself. Emitting them here keeps the "never hand-type
// an address" rule intact and bakes them into the image alongside the monitors.
const SWEEP_TARGETS_PATH = path.join(REPO_ROOT, 'config', 'sweep-targets.json');

function parseEnv(): SF_ENV {
  const value = process.env.SF_ENV;
  if (!value || !isSfEnv(value)) {
    console.error(`Set SF_ENV to one of: ${SF_ENVS.join(', ')}`);
    process.exit(1);
  }
  return value;
}

function monitorPath(
  network: string,
  concern: Concern,
  label?: string,
): string {
  const suffix = label ? `-${label.replace(/[^A-Za-z0-9_-]/g, '')}` : '';
  return path.join(MONITORS_DIR, `${network}-${concern.key}${suffix}.json`);
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
  label?: string,
) {
  return {
    name: `${concern.label}${label ? ` ${label}` : ''} - ${chain.display}`,
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
  const sweepTargets: Record<string, unknown> = {};
  let count = 0;

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

    const client = getPublicClient(chain.network);

    sweepTargets[chain.network] = {
      display: chain.display,
      TokenVault: artifacts.TokenVault.address,
      CurrencyController: artifacts.CurrencyController.address,
      LendingMarketController: artifacts.LendingMarketController.address,
      GenesisValueVault: artifacts.GenesisValueVault.address,
    };

    for (const concern of concerns) {
      if (!appliesTo(concern, chain.network)) continue;

      const resolved = concern.resolveAddresses
        ? await concern.resolveAddresses({ chain, client, artifacts })
        : [{ address: artifacts[concern.contractName].address }];

      if (!concern.splitByAddress) {
        writeJson(
          monitorPath(chain.network, concern),
          buildMonitor(
            chain,
            resolved.map(({ address }) => address),
            concern,
          ),
        );
        count += 1;
        continue;
      }

      // One file per address, so a trigger script can identify the emitter from
      // monitor.addresses[0]. The label -> address map goes into sweep-targets.json so
      // the script (and the sweep) can map that address back to its currency.
      const labelled: Record<string, string> = {};

      for (const { address, label } of resolved) {
        writeJson(
          monitorPath(chain.network, concern, label),
          buildMonitor(chain, [address], concern, label),
        );
        count += 1;
        if (label) labelled[label] = address;
      }

      (sweepTargets[chain.network] as Record<string, unknown>)[concern.key] =
        labelled;
    }
  }

  writeJson(SWEEP_TARGETS_PATH, sweepTargets);

  // eslint-disable-next-line no-console
  console.log(
    `generated ${count} monitor file(s) + sweep targets for env=${env}`,
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
