// The app's one Summary-detail cache, bound to the signed-in account (see detail-cache.ts).
import { createDetailCache } from './detail-cache';
import type { SwingDetail } from './types';

export const swingCache = createDetailCache<SwingDetail>();
