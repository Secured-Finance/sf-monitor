import { HardhatRuntimeEnvironment } from 'hardhat/types';
import { Alert, formatAlert } from './alerts';
import {
  DEFAULT_DETECTOR_CONFIG,
  DetectorConfig,
  decodeMarketLogs,
  detectImplausibleSettlement,
  detectPendingPhantomCredit,
  detectSupplyImbalance,
} from './detectors';
import { MarketHandles, connectMarkets, connectProtocol } from './protocol';
import { ResponsePolicy, respond } from './responder';

export type MonitorOptions = {
  pollIntervalMs: number;
  /** Run the view-based conservation check every N processed blocks. */
  sweepEveryBlocks: number;
  /** How many recently seen makers to keep in the pending-credit probe. */
  makerWindow: number;
  detectors: DetectorConfig;
  response: ResponsePolicy;
};

export const DEFAULT_MONITOR_OPTIONS: Omit<MonitorOptions, 'response'> = {
  pollIntervalMs: 4000,
  sweepEveryBlocks: 10,
  makerWindow: 200,
  detectors: DEFAULT_DETECTOR_CONFIG,
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export const runMonitor = async (
  hre: HardhatRuntimeEnvironment,
  options: MonitorOptions,
): Promise<void> => {
  const protocol = await connectProtocol(hre);
  const markets = await connectMarkets(hre, protocol);

  console.log(
    `Watching ${markets.length} market(s): ${markets
      .map(({ ccyName }) => ccyName)
      .join(', ')}`,
  );
  console.log(
    options.response.armed
      ? 'Responder ARMED - critical alerts will pause on-chain.'
      : 'Responder in dry-run - critical alerts will only be reported.',
  );

  const recentMakers = new Map<string, Set<string>>();
  let cursor = await hre.ethers.provider.getBlockNumber();
  let processed = 0;

  for (;;) {
    const head = await hre.ethers.provider.getBlockNumber();

    while (cursor < head) {
      cursor += 1;

      const alerts = await scanBlock(
        hre,
        markets,
        cursor,
        recentMakers,
        options,
      );

      processed += 1;

      if (processed % options.sweepEveryBlocks === 0) {
        for (const market of markets) {
          alerts.push(
            ...(await detectSupplyImbalance(
              protocol,
              market,
              options.detectors,
            )),
          );
          alerts.push(
            ...(await detectPendingPhantomCredit(
              market,
              [...(recentMakers.get(market.ccy) ?? [])],
              options.detectors,
            )),
          );
        }
      }

      await handle(protocol, alerts, options.response, cursor);
    }

    await sleep(options.pollIntervalMs);
  }
};

const scanBlock = async (
  hre: HardhatRuntimeEnvironment,
  markets: MarketHandles[],
  blockNumber: number,
  recentMakers: Map<string, Set<string>>,
  options: MonitorOptions,
): Promise<Alert[]> => {
  const alerts: Alert[] = [];

  for (const market of markets) {
    const logs = await hre.ethers.provider.getLogs({
      address: market.lendingMarket.address,
      fromBlock: blockNumber,
      toBlock: blockNumber,
    });

    if (logs.length === 0) {
      continue;
    }

    const byTx = new Map<string, typeof logs>();

    for (const log of logs) {
      byTx.set(log.transactionHash, [
        ...(byTx.get(log.transactionHash) ?? []),
        log,
      ]);
    }

    for (const [txHash, txLogs] of byTx) {
      const decoded = decodeMarketLogs(market.lendingMarket, txLogs);

      alerts.push(
        ...detectImplausibleSettlement(
          market,
          decoded,
          txHash,
          options.detectors,
        ),
      );

      trackMakers(recentMakers, market.ccy, decoded, options.makerWindow);
    }
  }

  return alerts;
};

const trackMakers = (
  recentMakers: Map<string, Set<string>>,
  ccy: string,
  decoded: { name: string; args: Record<string, unknown> }[],
  windowSize: number,
) => {
  const makers = recentMakers.get(ccy) ?? new Set<string>();

  for (const { name, args } of decoded) {
    const maker =
      name === 'PreOrderExecuted' || name === 'OrderExecuted'
        ? (args.user as string)
        : undefined;

    if (maker) {
      makers.delete(maker);
      makers.add(maker);
    }
  }

  while (makers.size > windowSize) {
    makers.delete(makers.values().next().value);
  }

  recentMakers.set(ccy, makers);
};

const handle = async (
  protocol: Awaited<ReturnType<typeof connectProtocol>>,
  alerts: Alert[],
  policy: ResponsePolicy,
  blockNumber: number,
) => {
  if (alerts.length === 0) {
    return;
  }

  console.log(`\n=== block ${blockNumber}: ${alerts.length} alert(s) ===`);
  alerts.forEach((alert) => console.log(formatAlert(alert)));

  const records = await respond(protocol, alerts, policy);

  records.forEach((record) =>
    console.log(
      record.executed
        ? `  -> executed ${record.action} (${record.txHash})`
        : `  -> ${record.error ? 'FAILED' : 'would run'} ${record.action}${
            record.error ? `: ${record.error}` : ''
          }`,
    ),
  );
};
