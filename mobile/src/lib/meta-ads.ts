// Meta (Facebook) SDK: app-install attribution for Meta App Promotion ads, nothing else.
//
// What it does: the SDK reports installs and app launches to Meta (automatic app events), and
// Meta attributes installs through Apple's SKAdNetwork. No Facebook login, no sharing.
//
// Privacy: the app never shows the App Tracking Transparency prompt, so iOS never grants access to
// the advertising identifier (IDFA). Advertiser ID collection is also switched off in Info.plist
// (app.json plugin config) and advertiser tracking is set off here.
//
// Purchases: ONE source only, or Meta counts them twice. That is the Meta SDK's own automatic
// in-app purchase logging (Meta app setting, on). The app never logs purchases to Meta itself, and
// nothing is passed to RevenueCat (its Meta integration stays off).
//
// The native module is imported lazily and only on iOS, and every failure is swallowed (but recorded
// for the diagnostics panel): ads attribution must never stop the app from working.

import * as Application from 'expo-application';
import Constants from 'expo-constants';
import { NativeModules, Platform } from 'react-native';

type FbModule = typeof import('react-native-fbsdk-next');

export interface MetaDiagnostics {
  platform: string;
  appId: string | null;
  clientTokenConfigured: boolean;
  nativeModules: { settings: boolean; appEvents: boolean; graph: boolean };
  /** 'not_started' until startMetaSdk runs; then each step's outcome. */
  steps: { at: string; step: string; ok: boolean; detail: string | null }[];
}

const diag: MetaDiagnostics = {
  platform: Platform.OS,
  appId: null,
  clientTokenConfigured: false,
  nativeModules: { settings: false, appEvents: false, graph: false },
  steps: [],
};

function note(step: string, ok: boolean, detail: string | null = null) {
  diag.steps.push({ at: new Date().toISOString().slice(11, 19), step, ok, detail: detail ? detail.slice(0, 160) : null });
  if (diag.steps.length > 30) diag.steps.shift();
}

function message(error: unknown): string {
  if (error instanceof Error) return error.message;
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

/** The plugin config compiled into this build (app.json → react-native-fbsdk-next). */
function pluginConfig(): { appID?: string; clientToken?: string } {
  const plugins = (Constants.expoConfig?.plugins ?? []) as unknown[];
  const entry = plugins.find((p) => Array.isArray(p) && p[0] === 'react-native-fbsdk-next') as [string, Record<string, string>] | undefined;
  return entry?.[1] ?? {};
}

let started: Promise<FbModule | null> | null = null;

function load(): Promise<FbModule | null> {
  if (Platform.OS !== 'ios') return Promise.resolve(null);
  if (!started) {
    started = (async () => {
      const cfg = pluginConfig();
      diag.appId = cfg.appID ?? null;
      diag.clientTokenConfigured = Boolean(cfg.clientToken && !cfg.clientToken.startsWith('REPLACE_'));
      diag.nativeModules = {
        settings: Boolean(NativeModules.FBSettings),
        appEvents: Boolean(NativeModules.FBAppEventsLogger),
        graph: Boolean(NativeModules.FBGraphRequest),
      };
      note('native modules', diag.nativeModules.settings && diag.nativeModules.appEvents,
        `FBSettings=${diag.nativeModules.settings} FBAppEventsLogger=${diag.nativeModules.appEvents} FBGraphRequest=${diag.nativeModules.graph}`);
      try {
        const fb = await import('react-native-fbsdk-next');
        try {
          await fb.Settings.setAdvertiserTrackingEnabled(false);
          note('advertiser tracking off', true);
        } catch (error) {
          note('advertiser tracking off', false, message(error));
        }
        fb.Settings.initializeSDK();
        note('initializeSDK called', true);
        return fb;
      } catch (error) {
        note('SDK start', false, message(error));
        return null;
      }
    })();
  }
  return started;
}

/** Start the SDK once at launch (safe to call repeatedly). */
export function startMetaSdk(): void {
  void load();
}

export function metaDiagnostics(): MetaDiagnostics {
  return { ...diag, steps: [...diag.steps] };
}

/**
 * On-device check (Profile → long-press the version line): proves the SDK is running, that this
 * phone can reach Meta with the app's client token through the SDK's own networking, and sends one
 * labelled test event ("sg_device_diagnostic") with an immediate flush, so it can be looked up in
 * Events Manager. It never logs a purchase or an install.
 */
export async function runMetaTest(): Promise<MetaDiagnostics> {
  const fb = await load();
  if (!fb) {
    note('test', false, Platform.OS === 'ios' ? 'SDK did not start (see steps above)' : 'iOS only');
    return metaDiagnostics();
  }
  try {
    const ate = await fb.Settings.getAdvertiserTrackingEnabled();
    note('advertiser tracking state', true, `enabled=${ate}`);
  } catch (error) {
    note('advertiser tracking state', false, message(error));
  }
  try {
    const anon = await fb.AppEventsLogger.getAnonymousID();
    note('anonymous id', Boolean(anon), anon ? 'present' : 'missing');
  } catch (error) {
    note('anonymous id', false, message(error));
  }
  const cfg = pluginConfig();
  if (cfg.appID && cfg.clientToken) {
    await new Promise<void>((resolve) => {
      const request = new fb.GraphRequest(cfg.appID!, {
        httpMethod: 'GET',
        accessToken: `${cfg.appID}|${cfg.clientToken}`,
        parameters: { fields: { string: 'name' } },
      }, (error, result) => {
        if (error) note('reach Meta with client token', false, message(error));
        else note('reach Meta with client token', true, `app=${String((result as { name?: unknown } | undefined)?.name ?? '?')}`);
        resolve();
      });
      try {
        new fb.GraphRequestManager().addRequest(request).start(15);
      } catch (error) {
        note('reach Meta with client token', false, message(error));
        resolve();
      }
    });
  } else {
    note('reach Meta with client token', false, 'app id or client token missing from the build');
  }
  try {
    fb.AppEventsLogger.logEvent('sg_device_diagnostic', { build: String(Application.nativeBuildVersion ?? '') });
    fb.AppEventsLogger.flush();
    note('test event sent + flush', true, 'look for "sg_device_diagnostic" in Events Manager');
  } catch (error) {
    note('test event sent + flush', false, message(error));
  }
  return metaDiagnostics();
}
