import { resolveLendingMarketAddresses } from './lending-market-addresses';
import { Concern } from '../types';

export const blockUnitPrice: Concern = {
  key: 'blockunitprice',
  contractName: 'LendingMarketController',
  label: 'Block Unit Price Fluctuations',
  eventSignature: 'BlockUnitPriceHistoryUpdated(bytes32,uint256,uint256)',
  // Needs a rolling per-market price history across invocations, which a plain
  // event->Slack mapping can't express - see
  // config/triggers/scripts/block_unit_price_check.py.
  trigger: { type: 'script', id: 'sf_monitor_block_unit_price_check' },
  // Emitted by the per-currency LendingMarket beacon proxy, not LendingMarketController
  // - see tools/concerns/lending-market-addresses.ts.
  resolveAddresses: resolveLendingMarketAddresses,
  libraryEventAbi: {
    anonymous: false,
    inputs: [
      { indexed: true, internalType: 'bytes32', name: 'ccy', type: 'bytes32' },
      {
        indexed: true,
        internalType: 'uint256',
        name: 'maturity',
        type: 'uint256',
      },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'blockUnitPrice',
        type: 'uint256',
      },
    ],
    name: 'BlockUnitPriceHistoryUpdated',
    type: 'event',
  },
};
