import { Concern } from '../types';

export const autoRolls: Concern = {
  key: 'autorolls',
  contractName: 'LendingMarketController',
  label: 'Auto Rolls',
  eventSignature: 'OrderBooksRotated(bytes32,uint256,uint256)',
  trigger: { type: 'slack', id: 'sf_monitor_slack_autorolls' },
  libraryEventAbi: {
    anonymous: false,
    inputs: [
      { indexed: false, internalType: 'bytes32', name: 'ccy', type: 'bytes32' },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'oldMaturity',
        type: 'uint256',
      },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'newMaturity',
        type: 'uint256',
      },
    ],
    name: 'OrderBooksRotated',
    type: 'event',
  },
};
