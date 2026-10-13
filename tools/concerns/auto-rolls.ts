import { Concern } from '../types';

export const autoRolls: Concern = {
  key: 'autorolls',
  contractName: 'LendingMarketController',
  label: 'Auto Rolls',
  eventSignature: 'OrderBooksRotated(bytes32,uint256,uint256)',
  // A script trigger purely for formatting: a declarative Slack trigger can only
  // print raw event args (bytes32 currency, Unix maturity) - see
  // config/triggers/scripts/event_alert.py.
  trigger: { type: 'script', id: 'sf_monitor_autorolls_alert' },
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
