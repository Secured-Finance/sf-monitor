// dev/staging/production split, adapted for OZ Monitor. Only one environment ever runs
// on a given monitor instance at a time (per the old Defender deploy pipeline too - CI
// picked exactly one NETWORK per run), so the
// generator resolves one SfEnv into monitor files for all the chains that env supports.
//
// `deployStage` is the folder name inside @secured-finance/contracts/deployments/ used
// to resolve that chain's address for this environment - development/staging use
// different deployed instances despite both living on the Sepolia chains.

export const SF_ENVS = ['development', 'staging', 'production'] as const;
export type SF_ENV = (typeof SF_ENVS)[number];

export const DEPLOY_STAGES = [
  'development',
  'staging',
  'mainnet',
  'development-arb',
  'staging-arb',
  'arbitrum-one',
] as const;

export type DeployStage = (typeof DEPLOY_STAGES)[number];

export interface ChainTarget {
  network: string; // OZ Monitor network slug (config/networks/<network>.json)
  deployStage: DeployStage;
  display: string;
}

// Add a chain to an environment here as support grows (e.g. Filecoin) - the generator
// picks this up automatically, no other changes needed.
// `network` values must match a slug in config/networks/*.json, not the old Defender
// SDK's own Network type values (which used a different vocabulary, e.g. "arbitrum-one"
// vs our "arbitrum_one") - OZ Monitor can't bind a monitor to a network it can't find.
export const ENV_CHAINS: Record<SF_ENV, ChainTarget[]> = {
  development: [
    {
      network: 'ethereum_sepolia',
      deployStage: 'development',
      display: 'Ethereum Sepolia',
    },
    {
      network: 'arbitrum_sepolia',
      deployStage: 'development-arb',
      display: 'Arbitrum Sepolia',
    },
  ],
  staging: [
    {
      network: 'ethereum_sepolia',
      deployStage: 'staging',
      display: 'Ethereum Sepolia',
    },
    {
      network: 'arbitrum_sepolia',
      deployStage: 'staging-arb',
      display: 'Arbitrum Sepolia',
    },
  ],
  production: [
    {
      network: 'ethereum_mainnet',
      deployStage: 'mainnet',
      display: 'Ethereum Mainnet',
    },
    {
      network: 'arbitrum_one',
      deployStage: 'arbitrum-one',
      display: 'Arbitrum One',
    },
  ],
};

export function isSfEnv(value: string): value is SF_ENV {
  return (SF_ENVS as readonly string[]).includes(value);
}
