import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';

import { api, friendly } from './api';
import type { SwingListItem } from './types';

/** The signed-in user's analysed swings, refreshed whenever the screen gains focus. */
export function useSwings() {
  const [swings, setSwings] = useState<SwingListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      setSwings(await api.swings());
      setError(null);
    } catch (e) {
      setError(friendly(e));
    } finally {
      setRefreshing(false);
    }
  }, []);
  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );
  return { swings, error, refreshing, reload: load };
}
