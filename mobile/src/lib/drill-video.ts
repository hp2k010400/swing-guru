// Drill demonstration videos: the official YouTube IFrame player page loaded in a WebView, plus the small,
// non-personal analytics properties. Pure (no React, no storage); unit-tested.
import type { DemoVideo, Drill } from './types';

/** The app's identity for YouTube's embedded player inside a WebView (https:// + the bundle id). */
export const EMBED_ORIGIN = 'https://com.swingguru.app';
export const YOUTUBE_TERMS_URL = 'https://www.youtube.com/t/terms';
export const GOOGLE_PRIVACY_URL = 'https://policies.google.com/privacy';

/** How long the app waits for YouTube's player to report "ready" before showing the fallback. */
export const READY_TIMEOUT_MS = 20_000;

/** Frame size for the video: 16:9 full width, or a centred 9:16 frame (Shorts) capped so it fits small iPhones. */
export function frameSize(aspect: string | null | undefined, available: number, maxPortraitHeight = 420): { width: number; height: number } {
  if (aspect === '9:16') {
    const height = Math.min(maxPortraitHeight, Math.round((available * 16) / 9));
    return { width: Math.round((height * 9) / 16), height };
  }
  return { width: Math.round(available), height: Math.round((available * 9) / 16) };
}

/** 102 -> "1:42". */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

const SAFE_ID = /^[A-Za-z0-9_-]{11}$/;

/**
 * A minimal page that runs YouTube's own IFrame Player API (the official player, unmodified: YouTube's
 * branding, controls and attribution stay) and reports state changes back to the app.
 */
export function embedHtml(video: Pick<DemoVideo, 'video_id' | 'start_s'>, autoplay = true): string | null {
  if (!SAFE_ID.test(video.video_id)) return null;
  const start = Math.max(0, Math.floor(video.start_s || 0));
  const vars = { playsinline: 1, rel: 0, start, autoplay: autoplay ? 1 : 0, widget_referrer: EMBED_ORIGIN };
  return `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}#p{position:absolute;inset:0;width:100%;height:100%}</style>
</head><body><div id="p"></div>
<script>
function post(type, data){ if (window.ReactNativeWebView) window.ReactNativeWebView.postMessage(JSON.stringify({ type: type, data: data === undefined ? null : data })); }
function onYouTubeIframeAPIReady(){
  new YT.Player('p', { host: 'https://www.youtube-nocookie.com', videoId: '${video.video_id}', playerVars: ${JSON.stringify(vars)},
    events: {
      onReady: function(e){ readySeen = true; post('ready'); ${autoplay ? 'e.target.playVideo();' : ''} },
      onStateChange: function(e){ if (e.data === 1) post('playing'); if (e.data === 0) post('ended'); },
      onError: function(e){ post('error', String(e.data)); }
    } });
}
var readySeen = false;
setTimeout(function(){ if (!readySeen) post('error', 'api_timeout'); }, 15000);
</script>
<script src="https://www.youtube.com/iframe_api"></script>
</body></html>`;
}

export type PlayerEvent = { type: 'ready' | 'playing' | 'ended' | 'error'; data: string | null };

export function parsePlayerEvent(raw: string): PlayerEvent | null {
  try {
    const msg = JSON.parse(raw);
    return msg && ['ready', 'playing', 'ended', 'error'].includes(msg.type) ? { type: msg.type, data: msg.data ?? null } : null;
  } catch {
    return null;
  }
}

/** YouTube IFrame error codes as a short, non-personal reason (no URLs). */
export function failureReason(code: string | null): string {
  return ({ '2': 'invalid_parameter', '5': 'html5_error', '100': 'unavailable', '101': 'embedding_disabled',
            '150': 'embedding_disabled', '152': 'embedding_disabled', '153': 'missing_referrer', api_timeout: 'api_timeout',
            load_failed: 'load_failed' } as Record<string, string>)[code ?? ''] ?? 'unknown';
}

/** Analytics properties for a drill: id, the swing-check category it trains, and the video source type. */
export function drillProps(drill: Drill): Record<string, string> {
  return {
    drill_id: drill.id,
    check: drill.targets?.[0]?.id ?? drill.checkpoint,
    video_source: drill.demo_video ? drill.demo_video.provider : drill.video ? 'owned' : 'none',
  };
}
