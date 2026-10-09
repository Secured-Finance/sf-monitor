import { hexToString } from 'viem';
import { ResolveAddressesContext, ResolvedAddress } from '../types';

// Resolves every currently-active currency's per-currency LendingMarket beacon proxy
// address - the actual emitter of BlockUnitPriceHistoryUpdated/ItayoseExecuted (both
// delegatecall'd from that proxy, via OrderActionLogic/OrderBookLogic respectively -
// never LendingMarketController itself). Verified directly with the Solidity team.
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
  {
    inputs: [{ internalType: 'bytes32', name: 'ccy', type: 'bytes32' }],
    name: 'getFutureValueVault',
    outputs: [{ internalType: 'address', name: '', type: 'address' }],
    stateMutability: 'view',
    type: 'function',
  },
] as const;

const ZERO_ADDRESS = '0x0000000000000000000000000000000000000000';

// Currency names are bytes32-encoded ASCII, right-padded with NULs.
export function currencyName(ccy: `0x${string}`): string {
  return hexToString(ccy, { size: 32 }).replace(/\0+$/, '');
}

// Shared by the LendingMarket and FutureValueVault resolvers: both enumerate the same
// currency set and differ only in which per-currency proxy getter they call.
export async function resolvePerCurrencyProxies(
  { client, artifacts }: ResolveAddressesContext,
  functionName: 'getLendingMarket' | 'getFutureValueVault',
): Promise<ResolvedAddress[]> {
  const currencies = await client.readContract({
    address: artifacts.CurrencyController.address as `0x${string}`,
    abi: CURRENCY_CONTROLLER_ABI,
    functionName: 'getCurrencies',
  });

  const resolved = await Promise.all(
    currencies.map(async (ccy) => ({
      address: await client.readContract({
        address: artifacts.LendingMarketController.address as `0x${string}`,
        abi: LENDING_MARKET_CONTROLLER_ABI,
        functionName,
        args: [ccy],
      }),
      label: currencyName(ccy),
    })),
  );

  // Both getters return the zero address for a currency CurrencyController knows about
  // but that has no initialized market yet on this deploy stage
  // (isInitializedLendingMarket(ccy) checks genesisDates[ccy] != 0) - nothing will ever
  // emit from 0x0, so watching it is pure noise.
  return resolved.filter(({ address }) => address !== ZERO_ADDRESS);
}

export function resolveLendingMarketAddresses(
  ctx: ResolveAddressesContext,
): Promise<ResolvedAddress[]> {
  return resolvePerCurrencyProxies(ctx, 'getLendingMarket');
}

// FutureValueVault is deployed per currency as a beacon proxy, same as LendingMarket,
// and is the emitter of the Transfer events I1 watches.
export function resolveFutureValueVaultAddresses(
  ctx: ResolveAddressesContext,
): Promise<ResolvedAddress[]> {
  return resolvePerCurrencyProxies(ctx, 'getFutureValueVault');
}
