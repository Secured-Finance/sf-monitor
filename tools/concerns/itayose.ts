import { resolveLendingMarketAddresses } from './lending-market-addresses';
import { Concern } from '../types';

export const itayose: Concern = {
  key: 'itayose',
  contractName: 'LendingMarketController',
  label: 'Itayose',
  eventSignature:
    'ItayoseExecuted(bytes32,uint256,uint256,uint256,uint256,uint256)',
  trigger: { type: 'slack', id: 'sf_monitor_slack_itayose' },
  // Emitted by the per-currency LendingMarket beacon proxy, not LendingMarketController
  // - see tools/concerns/lending-market-addresses.ts.
  resolveAddresses: resolveLendingMarketAddresses,
  libraryEventAbi: {
    anonymous: false,
    inputs: [
      { indexed: false, internalType: 'bytes32', name: 'ccy', type: 'bytes32' },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'maturity',
        type: 'uint256',
      },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'openingUnitPrice',
        type: 'uint256',
      },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'lastLendUnitPrice',
        type: 'uint256',
      },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'lastBorrowUnitPrice',
        type: 'uint256',
      },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'offsetAmount',
        type: 'uint256',
      },
    ],
    name: 'ItayoseExecuted',
    type: 'event',
  },
};
