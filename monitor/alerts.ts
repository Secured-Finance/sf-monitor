/**
 * Severity drives the response policy in `responder.ts`.
 *
 * CRITICAL is reserved for invariants that cannot break during legitimate
 * operation, so they are safe to act on without a human in the loop.
 * HIGH findings are economic heuristics: they need a threshold, so they page
 * an operator instead of pausing on their own.
 */
export type Severity = 'CRITICAL' | 'HIGH' | 'INFO';

export type Alert = {
  severity: Severity;
  detector: string;
  ccy: string;
  message: string;
  /** Currency to pause, when the alert is actionable. */
  pauseCcy?: string;
  txHash?: string;
  details?: Record<string, string>;
};

export const formatAlert = (alert: Alert): string => {
  const details = alert.details
    ? Object.entries(alert.details)
        .map(([key, value]) => `      ${key}: ${value}`)
        .join('\n')
    : '';

  return [
    `[${alert.severity}] ${alert.detector} (${alert.ccy})`,
    `      ${alert.message}`,
    alert.txHash ? `      tx: ${alert.txHash}` : '',
    details,
  ]
    .filter(Boolean)
    .join('\n');
};
