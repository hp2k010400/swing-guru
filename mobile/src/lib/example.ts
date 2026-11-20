// Opening the example analysis: straight into the replay, with its Summary underneath, so closing the
// replay (or "View Summary") lands on the score, breakdown and "Analyse your own swing".
import { router } from 'expo-router';

import { EXAMPLE_ID } from './api';

export function openExample({ replace = false }: { replace?: boolean } = {}): void {
  const summary = { pathname: '/swing/[id]' as const, params: { id: EXAMPLE_ID } };
  if (replace) router.replace(summary);
  else router.push(summary);
  router.push({ pathname: '/replay/[id]', params: { id: EXAMPLE_ID } });
}
