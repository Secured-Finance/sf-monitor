// Generates config/monitors/<network>_lendingmarketcontroller_<concern>.json for every
// chain the selected environment supports. Instead of calling the Defender SDK to
// create a monitor (as the old Autotask deploy scripts did), writes the equivalent OZ
// Monitor JSON straight to disk. Contract address is resolved dynamically for that
// environment's deploy-stage via deployments.ts (same require() mechanism the old code
// used); event ABI/match-conditions/trigger-wiring are defined once per concern in
// tools/concerns/.
//
// Only one environment ever runs on a given monitor instance (see environments.ts), so
// the "pregenerate" npm script (package.json) rimrafs every previously generated
// monitor file before this runs - each `npm run generate` call leaves only the
// selected environment's files on disk, never a mix of environments. Hand edits to a
// generated file will be lost - edit tools/concerns/* or tools/environments.ts instead.
//
// Usage: SF_ENV=development|staging|production npm run generate
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
import { Concern, ContractName } from './types';

const REPO_ROOT = path.resolve(__dirname, '..');
const MONITORS_DIR = path.join(REPO_ROOT, 'config', 'monitors');

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

function buildMonitor(chain: ChainTarget, address: string, concern: Concern) {
  return {
    name: `${concern.label} - ${chain.display}`,
    paused: false,
    networks: [chain.network],
    addresses: [
      {
        address,
        contract_spec: [concern.libraryEventAbi],
      },
    ],
    match_conditions: {
      events: [{ signature: concern.eventSignature, expression: null }],
      functions: [],
      transactions: [{ status: 'Success', expression: null }],
    },
    trigger_conditions: [],
    triggers: [concern.trigger.id, 'sf_monitor_log'],
  };
}

function main(): void {
  const env = parseEnv();
  const chains = ENV_CHAINS[env];
  const concerns = Object.values(CONCERN_DEFINITIONS);
  const contractNames = [...new Set(concerns.map((c) => c.contractName))];
  let count = 0;

  for (const chain of chains) {
    const addresses = Object.fromEntries(
      contractNames.map((contractName) => [
        contractName,
        getContractArtifact(contractName, chain.deployStage).address,
      ]),
    ) as Record<ContractName, string>;

    for (const concern of concerns) {
      if (!appliesTo(concern, chain.network)) continue;
      const filePath = monitorPath(chain.network, concern);
      writeJson(
        filePath,
        buildMonitor(chain, addresses[concern.contractName], concern),
      );
      count += 1;
    }
  }

  // eslint-disable-next-line no-console
  console.log(`generated ${count} monitor file(s) for env=${env}`);
}

main();
