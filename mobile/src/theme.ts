// Swing Guru design tokens (1.0.1): a light and a dark theme around the Swing Guru green, chosen in
// Profile (Light / Dark / Match iPhone). Gold is reserved for ranks, personal bests and highlights.
//
// `colors`, `statusColors` and `shadow` are single objects updated in place when the theme changes,
// so code reading `colors.ink` while rendering always gets the current theme. Module-level style
// sheets are wrapped in `themed(() => StyleSheet.create({...}))`, which rebuilds them on the next read
// after a change. The root layout remounts the screen tree on a change so everything re-renders.
import 'expo-sqlite/localStorage/install';

import { Appearance } from 'react-native';

export type Palette = {
  bg: string; surface: string; surface2: string; surface3: string; line: string; lineStrong: string;
  ink: string; ink2: string; ink3: string;
  green: string; greenDeep: string; greenInk: string; greenSoft: string; greenTint: string; greenLine: string; onAccent: string;
  gold: string; goldDeep: string; goldSoft: string; goldLine: string;
  good: string; goodSoft: string; goodInk: string;
  attention: string; attentionSoft: string; attentionInk: string;
  problem: string; problemSoft: string; problemInk: string;
  info: string; infoSoft: string; scrim: string;
};

// The original Swing Guru light design (Builds 1-14), with the tokens 1.0.1 added.
const LIGHT: Palette = {
  bg: '#f9fafa', surface: '#ffffff', surface2: '#f1f3f2', surface3: '#e7eae9', line: '#e6e9e7', lineStrong: '#d4d9d6',
  ink: '#141a17', ink2: '#5b635e', ink3: '#9aa19d',
  green: '#2f6f4e', greenDeep: '#1f4d36', greenInk: '#24563c', greenSoft: '#e7f0ea', greenTint: '#f1f6f2',
  greenLine: '#e0ebe3', onAccent: '#ffffff',
  gold: '#b98b2f', goldDeep: '#8a6420', goldSoft: '#f6eedb', goldLine: '#e8d6ad',
  good: '#3b9466', goodSoft: '#e6f2eb', goodInk: '#2d7a52',
  attention: '#d39a34', attentionSoft: '#faf0dc', attentionInk: '#a8741c',
  problem: '#d9664f', problemSoft: '#fbe8e3', problemInk: '#bd4f39',
  info: '#a2a79f', infoSoft: '#eeefec', scrim: 'rgba(0,0,0,0.45)',
};

// Dark: the brand green lifted for contrast on a near-black, green-tinted ground.
const DARK: Palette = {
  bg: '#0a0e0c', surface: '#121816', surface2: '#18201d', surface3: '#202a26',
  line: 'rgba(255,255,255,0.07)', lineStrong: 'rgba(255,255,255,0.14)',
  ink: '#f1f4f2', ink2: '#a3ada7', ink3: '#6f7a74',
  green: '#4aa577', greenDeep: '#1f4d36', greenInk: '#7fcfa2', greenSoft: '#15281e', greenTint: '#0f1b15',
  greenLine: 'rgba(74,165,119,0.28)', onAccent: '#ffffff',
  gold: '#d6b56a', goldDeep: '#9a7a3a', goldSoft: '#2a2414', goldLine: 'rgba(214,181,106,0.32)',
  good: '#4fbf85', goodSoft: '#132a1e', goodInk: '#7fd6a7',
  attention: '#e3a948', attentionSoft: '#2d2412', attentionInk: '#f0c47a',
  problem: '#ea7a62', problemSoft: '#301a15', problemInk: '#f59c88',
  info: '#7c8680', infoSoft: '#1a211e', scrim: 'rgba(0,0,0,0.55)',
};

export const PALETTES = { light: LIGHT, dark: DARK } as const;

export type ThemeMode = 'system' | 'light' | 'dark';
export type Scheme = 'light' | 'dark';
export type Status = 'good' | 'attention' | 'problem' | 'info' | 'withheld';

const MODE_KEY = 'sg.theme';
// While 1.0.1 is in testing the dark theme is the default; Profile switches it.
const DEFAULT_MODE: ThemeMode = 'dark';

function storedMode(): ThemeMode {
  try {
    const value = globalThis.localStorage?.getItem(MODE_KEY);
    return value === 'light' || value === 'dark' || value === 'system' ? value : DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

function resolve(mode: ThemeMode): Scheme {
  if (mode !== 'system') return mode;
  return Appearance.getColorScheme() === 'dark' ? 'dark' : 'light';
}

let mode: ThemeMode = storedMode();
let scheme: Scheme = resolve(mode);
let version = 0;
const listeners = new Set<() => void>();

export const colors: Palette = { ...PALETTES[scheme] };

export const statusColors: Record<Status, { dot: string; soft: string; ink: string }> = {} as never;

export const shadow = { shadowColor: '#000', shadowOpacity: 0, shadowRadius: 0, shadowOffset: { width: 0, height: 0 }, elevation: 0 };

function apply(next: Scheme) {
  Object.assign(colors, PALETTES[next]);
  Object.assign(statusColors, {
    good: { dot: colors.good, soft: colors.goodSoft, ink: colors.goodInk },
    attention: { dot: colors.attention, soft: colors.attentionSoft, ink: colors.attentionInk },
    problem: { dot: colors.problem, soft: colors.problemSoft, ink: colors.problemInk },
    info: { dot: colors.info, soft: colors.infoSoft, ink: colors.ink2 },
    withheld: { dot: colors.info, soft: colors.surface2, ink: colors.ink2 },
  });
  // On a dark ground a drop shadow reads as nothing; depth comes from surface steps and a hairline.
  Object.assign(shadow, next === 'dark'
    ? { shadowColor: '#000', shadowOpacity: 0.35, shadowRadius: 16, shadowOffset: { width: 0, height: 8 }, elevation: 3 }
    : { shadowColor: '#17201b', shadowOpacity: 0.06, shadowRadius: 12, shadowOffset: { width: 0, height: 4 }, elevation: 2 });
}
apply(scheme);

function changed() {
  version += 1;
  listeners.forEach((listener) => listener());
}

/** The current choice and the scheme it resolves to. */
export function theme(): { mode: ThemeMode; scheme: Scheme; version: number } {
  return { mode, scheme, version };
}

export function setThemeMode(next: ThemeMode) {
  mode = next;
  try {
    globalThis.localStorage?.setItem(MODE_KEY, next);
  } catch {
    // Not persisted (private storage unavailable); the choice still applies for this session.
  }
  const resolved = resolve(next);
  if (resolved !== scheme) {
    scheme = resolved;
    apply(scheme);
  }
  changed();
}

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// "Match iPhone": follow the system appearance while the app runs.
Appearance.addChangeListener(() => {
  if (mode !== 'system') return;
  const resolved = resolve('system');
  if (resolved === scheme) return;
  scheme = resolved;
  apply(scheme);
  changed();
});

/** A module-level style sheet that is rebuilt from the current theme on the first read after a change. */
export function themed<T extends object>(make: () => T): T {
  let cache: T | null = null;
  let builtAt = -1;
  const current = () => {
    if (cache === null || builtAt !== version) {
      cache = make();
      builtAt = version;
    }
    return cache;
  };
  return new Proxy({} as T, {
    get: (_target, key) => (current() as Record<string | symbol, unknown>)[key],
    has: (_target, key) => key in current(),
    ownKeys: () => Reflect.ownKeys(current()),
    getOwnPropertyDescriptor: (_target, key) => {
      const descriptor = Object.getOwnPropertyDescriptor(current(), key);
      return descriptor ? { ...descriptor, configurable: true } : undefined;
    },
  });
}

export const space = { s1: 4, s2: 8, s3: 12, s4: 16, s5: 20, s6: 24, s7: 32, s8: 40, gutter: 20, section: 32 } as const;
export const radius = { xs: 8, sm: 10, md: 14, lg: 20, xl: 26, pill: 999 } as const;

export const type = {
  large: { fontSize: 34, fontWeight: '800', letterSpacing: -0.9 },
  title1: { fontSize: 28, fontWeight: '800', letterSpacing: -0.6 },
  title2: { fontSize: 21, fontWeight: '700', letterSpacing: -0.35 },
  headline: { fontSize: 17, fontWeight: '600', letterSpacing: -0.2 },
  body: { fontSize: 16, lineHeight: 23 },
  callout: { fontSize: 15, lineHeight: 21 },
  subhead: { fontSize: 14, fontWeight: '500' },
  footnote: { fontSize: 13, lineHeight: 18 },
  caption: { fontSize: 11.5, fontWeight: '700', letterSpacing: 1.3, textTransform: 'uppercase' },
  number: { fontWeight: '800', letterSpacing: -1.2, fontVariant: ['tabular-nums'] as ['tabular-nums'] },
} as const;
