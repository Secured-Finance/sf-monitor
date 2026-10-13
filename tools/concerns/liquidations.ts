import { Concern } from '../types';

export const liquidations: Concern = {
  key: 'liquidations',
  contractName: 'LendingMarketController',
  label: 'Liquidations',
  eventSignature:
    'LiquidationExecuted(address,bytes32,bytes32,uint256,uint256)',
  // A script trigger purely for formatting: a declarative Slack trigger can only
  // print raw event args (bytes32 currency, Unix maturity, base-unit amount) - see
  // config/triggers/scripts/event_alert.py.
  trigger: { type: 'script', id: 'sf_monitor_liquidations_alert' },
  libraryEventAbi: {
    anonymous: false,
    inputs: [
      { indexed: true, internalType: 'address', name: 'user', type: 'address' },
      {
        indexed: false,
        internalType: 'bytes32',
        name: 'collateralCcy',
        type: 'bytes32',
      },
      {
        indexed: true,
        internalType: 'bytes32',
        name: 'debtCcy',
        type: 'bytes32',
      },
      {
        indexed: true,
        internalType: 'uint256',
        name: 'debtMaturity',
        type: 'uint256',
      },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'debtAmount',
        type: 'uint256',
      },
    ],
    name: 'LiquidationExecuted',
    type: 'event',
  },
};
