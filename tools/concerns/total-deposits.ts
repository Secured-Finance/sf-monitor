import { Concern } from '../types';

export const totalDeposits: Concern = {
  key: 'totaldeposits',
  contractName: 'TokenVault',
  label: 'Total Deposits',
  eventSignature: 'Withdraw(address,bytes32,uint256)',
  // The script trigger (config/triggers/scripts.json) resolves network, RPC env var,
  // TokenVault address and explorer itself from the match payload and config/networks/
  // at runtime, so this concern needs no per-network restriction or per-network trigger.
  trigger: {
    type: 'script',
    id: 'sf_monitor_total_deposits_check',
  },
  libraryEventAbi: {
    anonymous: false,
    inputs: [
      { indexed: true, internalType: 'address', name: 'user', type: 'address' },
      { indexed: false, internalType: 'bytes32', name: 'ccy', type: 'bytes32' },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'amount',
        type: 'uint256',
      },
    ],
    name: 'Withdraw',
    type: 'event',
  },
};
