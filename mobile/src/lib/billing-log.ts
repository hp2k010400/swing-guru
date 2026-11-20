// Development-only purchase lifecycle log. Never logs user ids, receipts, tokens, prices paid or any
// payment details: only the step, the outcome and booleans/states the app already shows on screen.
type Detail = Record<string, string | number | boolean | null | undefined>;

export function billingLog(step: string, detail: Detail = {}): void {
  const dev = typeof __DEV__ !== 'undefined' && __DEV__;
  if (!dev) return;
  console.info(`[billing] ${step}`, detail);
}
