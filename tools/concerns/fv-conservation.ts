import { resolveFutureValueVaultAddresses } from './lending-market-addresses';
import { Concern } from '../types';

// I1 - future value conservation, per currency per maturity
// (invariant-monitoring-spec.md). Every lender claim carries a matching borrower
// obligation:
//
//   | lendSupply - borrowSupply | <= pendingOrderAmount * FV_MULTIPLIER
//
// This is the check that fires at value creation, before anything is extracted - an
// attacker must mint an unbacked claim before they can withdraw against it.
//
// Watches FutureValueVault.Transfer, which is emitted on every mint and burn
// (increase/decrease, both from the zero address) as well as on peer-to-peer position
// transfers. Any of them can change the supplies, so any of them is worth re-checking.
//
// splitByAddress: Transfer carries `maturity` but not `ccy`, and FutureValueVault is
// deployed per currency - so with several addresses on one monitor, a trigger script
// reading monitor.addresses[0] cannot tell which currency's vault emitted the event.
// One file per currency makes that unambiguous; the generator also writes the
// currency -> vault address map into config/sweep-targets.json, which is how
// fv_conservation_check.py recovers the currency it needs for getPendingOrderAmount.
export const fvConservation: Concern = {
  key: 'fvconservation',
  contractName: 'LendingMarketController',
  label: 'FV Conservation',
  eventSignature: 'Transfer(address,address,uint8,uint256,int256)',
  trigger: {
    type: 'script',
    id: 'sf_monitor_fv_conservation_check',
  },
  resolveAddresses: resolveFutureValueVaultAddresses,
  splitByAddress: true,
  // FutureValueVault's own event, declared here for the same reason the library events
  // are: the generator only bundles the ABI fragment a monitor needs to decode its
  // matched event, not a whole contract ABI.
  libraryEventAbi: {
    anonymous: false,
    inputs: [
      { indexed: true, internalType: 'address', name: 'from', type: 'address' },
      { indexed: true, internalType: 'address', name: 'to', type: 'address' },
      {
        indexed: false,
        internalType: 'uint8',
        name: 'orderBookId',
        type: 'uint8',
      },
      {
        indexed: false,
        internalType: 'uint256',
        name: 'maturity',
        type: 'uint256',
      },
      { indexed: false, internalType: 'int256', name: 'value', type: 'int256' },
    ],
    name: 'Transfer',
    type: 'event',
  },
};
