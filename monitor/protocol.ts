import { Contract } from 'ethers';
import { HardhatRuntimeEnvironment } from 'hardhat/types';
import { fromBytes32, toBytes32 } from '../strings';

/**
 * The unit price scale used across the order books. 10,000 is par, so the
 * future value of a filled order is `amount * PRICE_DIGIT / unitPrice`.
 */
export const PRICE_DIGIT = 10000;

export type ProtocolHandles = {
  currencyController: Contract;
  lendingMarketController: Contract;
  tokenVault: Contract;
  currencies: string[];
  currencyNames: Record<string, string>;
};

/**
 * Resolves the live proxy addresses through the ProxyController, the same way
 * the operational tasks in this repository do, so the monitor always follows an
 * upgrade instead of pinning a stale implementation.
 */
export const connectProtocol = async (
  hre: HardhatRuntimeEnvironment,
): Promise<ProtocolHandles> => {
  const { deployments, ethers } = hre;

  const proxyController = await deployments
    .get('ProxyController')
    .then(({ address }) => ethers.getContractAt('ProxyController', address));

  const [currencyController, lendingMarketController, tokenVault] =
    await Promise.all(
      ['CurrencyController', 'LendingMarketController', 'TokenVault'].map(
        async (name) => {
          const address = await proxyController.getAddress(toBytes32(name));
          return ethers.getContractAt(name, address);
        },
      ),
    );

  const currencies: string[] = await currencyController.getCurrencies();
  const currencyNames = Object.fromEntries(
    currencies.map((ccy) => [ccy, fromBytes32(ccy)]),
  );

  return {
    currencyController,
    lendingMarketController,
    tokenVault,
    currencies,
    currencyNames,
  };
};

export type MarketHandles = {
  ccy: string;
  ccyName: string;
  lendingMarket: Contract;
  futureValueVault: Contract;
  maturities: string[];
  orderBookIds: number[];
};

export const connectMarkets = async (
  hre: HardhatRuntimeEnvironment,
  protocol: ProtocolHandles,
): Promise<MarketHandles[]> => {
  const { ethers } = hre;
  const { lendingMarketController, currencies, currencyNames } = protocol;

  return Promise.all(
    currencies.map(async (ccy) => {
      const [lendingMarketAddress, futureValueVaultAddress, maturities, ids] =
        await Promise.all([
          lendingMarketController.getLendingMarket(ccy),
          lendingMarketController.getFutureValueVault(ccy),
          lendingMarketController.getMaturities(ccy),
          lendingMarketController.getOrderBookIds(ccy),
        ]);

      const [lendingMarket, futureValueVault] = await Promise.all([
        ethers.getContractAt('LendingMarket', lendingMarketAddress),
        ethers.getContractAt('FutureValueVault', futureValueVaultAddress),
      ]);

      return {
        ccy,
        ccyName: currencyNames[ccy],
        lendingMarket,
        futureValueVault,
        maturities: maturities.map(String),
        orderBookIds: ids.map(Number),
      };
    }),
  );
};
