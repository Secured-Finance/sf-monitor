import { BigNumber, Contract, utils } from 'ethers';
import { Alert } from './alerts';
import { MarketHandles, PRICE_DIGIT, ProtocolHandles } from './protocol';

export type DetectorConfig = {
  /**
   * Lowest unit price the monitor treats as economically plausible for a
   * settlement. The 2026-09 incident settled orders at unit price 2 (a 5,000x
   * future value multiple); any real fill sits far closer to par (10,000).
   */
  minPlausibleUnitPrice: number;
  /**
   * How much future value one unit of pending (not-yet-settled) order amount is
   * allowed to explain. Pending maker amounts are denominated in present value,
   * so a multiplier of 2 tolerates settlement anywhere down to unit price 5,000.
   */
  maxPendingFvMultiplier: number;
};

export const DEFAULT_DETECTOR_CONFIG: DetectorConfig = {
  minPlausibleUnitPrice: 5000,
  maxPendingFvMultiplier: 2,
};

const impliedUnitPrice = (
  amount: BigNumber,
  futureValue: BigNumber,
): BigNumber =>
  futureValue.isZero()
    ? BigNumber.from(PRICE_DIGIT)
    : amount.mul(PRICE_DIGIT).div(futureValue);

/**
 * Detector 1 - future value conservation.
 *
 * Every filled order must record a lender claim and an equal borrower
 * obligation. Settlement is deferred for fully filled makers, so the two totals
 * are not equal at every instant: the outstanding difference is carried in the
 * protocol's own `pendingOrderAmount` counter until the maker runs cleanup.
 *
 * That gives two checks:
 *   - with nothing pending, any difference at all is unbacked future value;
 *   - with something pending, the difference must still be explainable by that
 *     pending amount at a plausible unit price.
 *
 * The phantom mint breaks both by orders of magnitude - the incident left USDC
 * at 2,507,561 lend future value against 38.96 borrow.
 */
export const detectSupplyImbalance = async (
  protocol: ProtocolHandles,
  market: MarketHandles,
  config: DetectorConfig,
): Promise<Alert[]> => {
  const { lendingMarketController } = protocol;
  const { ccy, ccyName, futureValueVault, maturities } = market;

  const results = await Promise.all(
    maturities.map(async (maturity) => {
      const [lendSupply, borrowSupply, pendingAmount] = await Promise.all([
        futureValueVault.getTotalLendingSupply(maturity),
        futureValueVault.getTotalBorrowingSupply(maturity),
        lendingMarketController.getPendingOrderAmount(ccy, maturity),
      ]);

      const gap = lendSupply.sub(borrowSupply).abs();

      if (gap.isZero()) {
        return undefined;
      }

      const explainable = pendingAmount.mul(config.maxPendingFvMultiplier);

      if (gap.lte(explainable)) {
        return undefined;
      }

      const detail = {
        maturity,
        lendFutureValue: lendSupply.toString(),
        borrowFutureValue: borrowSupply.toString(),
        pendingOrderAmount: pendingAmount.toString(),
        unexplainedGap: gap.sub(explainable).toString(),
      };

      return {
        severity: 'CRITICAL',
        detector: 'supply-imbalance',
        ccy: ccyName,
        pauseCcy: ccy,
        message: pendingAmount.isZero()
          ? `lend and borrow future value differ by ${gap.toString()} with no pending settlement to explain it`
          : `future value gap of ${gap.toString()} exceeds what ${pendingAmount.toString()} of pending settlement can explain`,
        details: detail,
      } as Alert;
    }),
  );

  return results.filter((alert): alert is Alert => alert !== undefined);
};

/**
 * Detector 2 - settlement at an implausible price.
 *
 * `OrdersCleaned` carries the amount and the future value credited when a
 * resting order is settled, so the price the order actually settled at is
 * `amount * 10000 / futureValue`. An orphaned order is credited at the maker's
 * own limit price, which is what makes the attack profitable: the payload order
 * sat at unit price 2 precisely because nobody would ever fill it honestly.
 *
 * Stateless, fires in the same block as the mint.
 */
export const detectImplausibleSettlement = (
  market: MarketHandles,
  logs: utils.LogDescription[],
  txHash: string,
  config: DetectorConfig,
): Alert[] =>
  logs
    .filter(({ name }) => name === 'OrdersCleaned')
    .map(({ args }) => {
      const price = impliedUnitPrice(args.amount, args.futureValue);

      if (price.gte(config.minPlausibleUnitPrice)) {
        return undefined;
      }

      return {
        severity: 'CRITICAL',
        detector: 'implausible-settlement',
        ccy: market.ccyName,
        pauseCcy: market.ccy,
        txHash,
        message: `order settled at unit price ${price.toString()} (floor ${
          config.minPlausibleUnitPrice
        }), crediting ${args.futureValue.toString()} future value for ${args.amount.toString()}`,
        details: {
          maker: args.maker,
          maturity: args.maturity.toString(),
          orderIds: args.orderIds.map(String).join(','),
        },
      } as Alert;
    })
    .filter((alert): alert is Alert => alert !== undefined);

/**
 * Detector 3 - phantom credit waiting to be minted.
 *
 * Settlement is lazy, so an orphaned order is misclassified as filled some time
 * before `cleanUpFunds` actually mints the future value. In that window the
 * protocol already reports the pending credit through
 * `getTotalAmountFromLendOrders`, and its implied price is already absurd.
 *
 * This is the only detector here that can fire before any value moves.
 */
export const detectPendingPhantomCredit = async (
  market: MarketHandles,
  makers: string[],
  config: DetectorConfig,
): Promise<Alert[]> => {
  const { lendingMarket, ccy, ccyName, orderBookIds } = market;

  const checks = orderBookIds.flatMap((orderBookId) =>
    makers.map(async (maker) => {
      const { inactiveAmount, inactiveFutureValue, maturity } =
        await lendingMarket.getTotalAmountFromLendOrders(orderBookId, maker);

      if (inactiveFutureValue.isZero()) {
        return undefined;
      }

      const price = impliedUnitPrice(inactiveAmount, inactiveFutureValue);

      if (price.gte(config.minPlausibleUnitPrice)) {
        return undefined;
      }

      return {
        severity: 'CRITICAL',
        detector: 'pending-phantom-credit',
        ccy: ccyName,
        pauseCcy: ccy,
        message: `${maker} is owed ${inactiveFutureValue.toString()} future value for ${inactiveAmount.toString()}, an implied unit price of ${price.toString()} - not yet minted`,
        details: { maturity: maturity.toString(), orderBookId: String(orderBookId) },
      } as Alert;
    }),
  );

  const results = await Promise.all(checks);

  return results.filter((alert): alert is Alert => alert !== undefined);
};

/**
 * Decodes the protocol logs in a receipt that belong to a market we watch, so
 * the log-driven detectors do not have to know about ABI plumbing.
 */
export const decodeMarketLogs = (
  contract: Contract,
  logs: { address: string; topics: string[]; data: string }[],
): utils.LogDescription[] =>
  logs
    .filter(
      ({ address }) => address.toLowerCase() === contract.address.toLowerCase(),
    )
    .map((log) => {
      try {
        return contract.interface.parseLog(log);
      } catch {
        return undefined;
      }
    })
    .filter((log): log is utils.LogDescription => log !== undefined);
