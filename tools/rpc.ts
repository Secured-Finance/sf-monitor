// Builds a read-only viem client for a network, for concerns whose watched address(es)
// need a live contract call to resolve (see tools/concerns/lending-market-addresses.ts).
// The generator has never needed an RPC connection before this - unlike deployments.ts,
// which only reads the static @secured-finance/contracts package.
import * as fs from 'fs';
import * as path from 'path';
import { createPublicClient, http } from 'viem';

const REPO_ROOT = path.resolve(__dirname, '..');
const NETWORKS_DIR = path.join(REPO_ROOT, 'config', 'networks');

// Networks OZ Monitor reaches through a local adapter process instead of talking to the
// public RPC directly (see adapters/filecoin_adapter.py - Filecoin needs null rounds
// translated into empty blocks or the monitor stalls permanently). Their
// config/networks/*.json therefore names the adapter's loopback URL, which only exists
// at runtime inside the monitor Pod. The generator runs during `docker build` and
// locally, where nothing is listening on that port, so it has to use the real upstream
// endpoint instead - hence this override rather than the network file's own env var.
const GENERATOR_UPSTREAM_ENV: Record<string, string> = {
  filecoin_mainnet: 'FILECOIN_MAINNET_RPC_URL',
  filecoin_calibration: 'FILECOIN_CALIBRATION_RPC_URL',
};

// Reads the RPC env var *name* to use from config/networks/<network>.json - the same
// file OZ Monitor itself reads - then resolves that name from process.env. Single
// source of truth for the network -> env-var mapping (no separate map to keep in sync
// here if a network is added/renamed in config/networks/), except for the adapter-backed
// networks above.
function getRpcUrl(network: string): string {
  const networkConfigPath = path.join(NETWORKS_DIR, `${network}.json`);
  const networkConfig = JSON.parse(fs.readFileSync(networkConfigPath, 'utf-8'));
  const envVarName =
    GENERATOR_UPSTREAM_ENV[network] ?? networkConfig.rpc_urls[0].url.value;
  const rpcUrl = process.env[envVarName];
  if (!rpcUrl) {
    throw new Error(
      `${envVarName} is not set - required to resolve dynamic concern addresses for network ${network}`,
    );
  }
  return rpcUrl.trim();
}

// No `chain` passed - viem's http() transport always calls exactly the URL given it,
// never a public fallback; `chain` would only add client-side chain-id validation,
// which OZ Monitor's own config doesn't need either.
export function getPublicClient(network: string) {
  return createPublicClient({ transport: http(getRpcUrl(network)) });
}
