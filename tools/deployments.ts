// Pulls contract ABI/address for a given deploy-stage straight out of the private
// @secured-finance/contracts package, which bundles deployments/<stage>/<Contract>.json.
// No hardcoded addresses.
import { DeployStage } from './environments';
import { ContractName } from './types';

export interface ContractArtifact {
  abi: unknown[];
  address: string;
}

const getContractArtifact = (
  contractName: ContractName,
  stage: DeployStage,
): ContractArtifact => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const resolved = require(
    `@secured-finance/contracts/deployments/${stage}/${contractName}.json`,
  );
  return { abi: resolved.abi, address: resolved.address };
};

export { getContractArtifact };
