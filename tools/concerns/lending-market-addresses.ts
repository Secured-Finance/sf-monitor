import { ResolveAddressesContext } from '../types';

// Resolves every currently-active currency's per-currency LendingMarket beacon proxy
// address - the actual emitter of BlockUnitPriceHistoryUpdated/ItayoseExecuted (both
// delegatecall'd from that proxy, via OrderActionLogic/OrderBookLogic respectively -
// never LendingMarketController itself). Verified with the Solidity team - see
// memory/project_lendingmarket_event_emission.md.
//
// LendingMarketController.getUsedCurrencies() is NOT the right getter here - it's
// per-user and actively mutated, not a market registry. The correct enumeration is
// CurrencyController.getCurrencies() -> LendingMarketController.getLendingMarket(ccy),
// one proxy per currency (not per maturity - maturities are an internal orderBookId
// inside the same proxy). No getter anywhere returns the full proxy list directly.
//
// Known gap, deliberately deferred: CurrencyController.removeCurrency() can de-list a
// currency from getCurrencies() without clearing its LendingMarket proxy mapping, so a
// de-listed-but-still-live market could silently drop out of future regenerations.
const CURRENCY_CONTROLLER_ABI = [
  {
    inputs: [],
    name: 'getCurrencies',
    outputs: [{ internalType: 'bytes32[]', name: '', type: 'bytes32[]' }],
    stateMutability: 'view',
    type: 'function',
  },
] as const;

const LENDING_MARKET_CONTROLLER_ABI = [
  {
    inputs: [{ internalType: 'bytes32', name: '_ccy', type: 'bytes32' }],
    name: 'getLendingMarket',
    outputs: [{ internalType: 'address', name: '', type: 'address' }],
    stateMutability: 'view',
    type: 'function',
  },
] as const;

const ZERO_ADDRESS = '0x0000000000000000000000000000000000000000';

export async function resolveLendingMarketAddresses({
  client,
  artifacts,
}: ResolveAddressesContext): Promise<string[]> {
  const currencies = await client.readContract({
    address: artifacts.CurrencyController.address as `0x${string}`,
    abi: CURRENCY_CONTROLLER_ABI,
    functionName: 'getCurrencies',
  });

  const addresses = await Promise.all(
    currencies.map((ccy) =>
      client.readContract({
        address: artifacts.LendingMarketController.address as `0x${string}`,
        abi: LENDING_MARKET_CONTROLLER_ABI,
        functionName: 'getLendingMarket',
        args: [ccy],
      }),
    ),
  );

  // getLendingMarket(ccy) returns the zero address for a currency CurrencyController
  // knows about but that has no initialized LendingMarket yet on this deploy stage
  // (isInitializedLendingMarket(ccy) checks genesisDates[ccy] != 0) - nothing will ever
  // emit from 0x0, so watching it is pure noise.
  return addresses.filter((address) => address !== ZERO_ADDRESS);
}
