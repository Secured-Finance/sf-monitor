import { Signer } from 'ethers';
import { Alert } from './alerts';
import { ProtocolHandles } from './protocol';

export type ResponsePolicy = {
  /**
   * When false the responder only reports what it would have done. Keep this
   * off until the detectors have run against live traffic long enough to trust
   * them.
   */
  armed: boolean;
  /**
   * Pause the whole TokenVault in addition to the affected market. The market
   * pause stops new phantom future value being minted; the vault pause is what
   * stops already-minted value being withdrawn.
   */
  pauseVault: boolean;
  signer?: Signer;
};

export type ResponseRecord = {
  action: string;
  executed: boolean;
  txHash?: string;
  error?: string;
};

/**
 * `pauseLendingMarket` and `TokenVault.pause` are both `onlyOperator`, which is
 * a distinct role from `onlyOwner`. The monitor should therefore hold an
 * operator key that can pause and do nothing else - no upgrades, no fund
 * movement - so arming it is a bounded risk: a false positive costs an
 * unnecessary pause that an operator lifts.
 */
export const respond = async (
  protocol: ProtocolHandles,
  alerts: Alert[],
  policy: ResponsePolicy,
): Promise<ResponseRecord[]> => {
  const actionable = alerts.filter(
    (alert) => alert.severity === 'CRITICAL' && alert.pauseCcy,
  );

  if (actionable.length === 0) {
    return [];
  }

  const currencies = [
    ...new Set(actionable.map((alert) => alert.pauseCcy as string)),
  ];

  const records: ResponseRecord[] = [];

  for (const ccy of currencies) {
    records.push(
      await execute(
        `pauseLendingMarket(${protocol.currencyNames[ccy] ?? ccy})`,
        policy,
        async (signer) => {
          const tx = await protocol.lendingMarketController
            .connect(signer)
            .pauseLendingMarket(ccy);
          return tx.wait().then(() => tx.hash);
        },
      ),
    );
  }

  if (policy.pauseVault) {
    records.push(
      await execute('TokenVault.pause()', policy, async (signer) => {
        const tx = await protocol.tokenVault.connect(signer).pause();
        return tx.wait().then(() => tx.hash);
      }),
    );
  }

  return records;
};

const execute = async (
  action: string,
  policy: ResponsePolicy,
  send: (signer: Signer) => Promise<string>,
): Promise<ResponseRecord> => {
  if (!policy.armed || !policy.signer) {
    return { action, executed: false };
  }

  try {
    return { action, executed: true, txHash: await send(policy.signer) };
  } catch (error) {
    return {
      action,
      executed: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
};
