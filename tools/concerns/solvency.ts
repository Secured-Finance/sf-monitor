import { Concern } from '../types';

// I2 - custody solvency (invariant-monitoring-spec.md). Asserts the vault physically
// holds at least what its own accounting says it owes:
//
//   IERC20(token).balanceOf(TokenVault) >= TokenVault.getTotalDepositAmount(ccy)
//
// Watches Withdraw because that is when tokens actually leave the vault, and so the
// moment the invariant can break. The periodic sweep re-runs the same check over every
// currency on a timer as a backstop (see config/triggers/scripts/invariants.py).
//
// This is additive - the total-deposits concern watches the same event with its own
// balance-drop heuristic and is unchanged. Unlike that heuristic this has no threshold
// to tune: a breach is a breach.
//
// TokenVault has a single deployment address, so no resolveAddresses hook is needed, and
// the script resolves network/RPC/explorer itself from the match payload at runtime - so
// no per-network restriction either.
export const solvency: Concern = {
  key: 'solvency',
  contractName: 'TokenVault',
  label: 'Custody Solvency',
  eventSignature: 'Withdraw(address,bytes32,uint256)',
  trigger: {
    type: 'script',
    id: 'sf_monitor_solvency_check',
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
