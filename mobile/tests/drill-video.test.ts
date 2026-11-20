import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  drillProps, duration, embedHtml, EMBED_ORIGIN, failureReason, frameSize, parsePlayerEvent, READY_TIMEOUT_MS,
} from '../src/lib/drill-video.ts';
import type { DemoVideo, Drill } from '../src/lib/types.ts';

const video: DemoVideo = {
  provider: 'youtube', video_id: 'xFOXu1djU-A', start_s: 49, title: 'The Pump Drill', creator: 'Jim Estes',
  watch_url: 'https://www.youtube.com/watch?v=xFOXu1djU-A&t=49s', embed_url: 'https://www.youtube-nocookie.com/embed/xFOXu1djU-A?start=49',
  thumbnail_url: 'https://i.ytimg.com/vi/xFOXu1djU-A/hqdefault.jpg', suitability: 'transcript_verified',
};
const drill: Drill = {
  id: 'pump-drill', name: 'Pump drill', checkpoint: 'Plane', phase_label: 'Mid-Down', minutes: 5, why: '', steps: [], reps: '',
  status: 'draft_needs_coach_review', video: null, demo_video: video,
  targets: [{ id: 'downswing.plane', name: 'Plane', phase: 'downswing', phase_label: 'Mid-Down' }],
};

test('embed page uses the official IFrame Player API with the app identity and start time', () => {
  const html = embedHtml(video)!;
  assert.match(html, /https:\/\/www\.youtube\.com\/iframe_api/);
  assert.match(html, /videoId: 'xFOXu1djU-A'/);
  assert.match(html, /"start":49/);
  assert.match(html, /"playsinline":1/);
  assert.ok(html.includes(`"widget_referrer":"${EMBED_ORIGIN}"`));
  assert.equal(EMBED_ORIGIN, 'https://com.swingguru.app');
  // onYouTubeIframeAPIReady is defined before the API script loads.
  assert.ok(html.indexOf('onYouTubeIframeAPIReady') < html.indexOf('iframe_api"'));
});

test('embed page refuses anything that is not a plain video id', () => {
  assert.equal(embedHtml({ video_id: "abc'); alert(1); //", start_s: 0 }), null);
  assert.equal(embedHtml({ video_id: 'short', start_s: 0 }), null);
  assert.match(embedHtml({ video_id: 'xFOXu1djU-A', start_s: -5 })!, /"start":0/);
});

test('player messages and failure reasons', () => {
  assert.deepEqual(parsePlayerEvent('{"type":"playing","data":null}'), { type: 'playing', data: null });
  assert.deepEqual(parsePlayerEvent('{"type":"error","data":"150"}'), { type: 'error', data: '150' });
  assert.equal(parsePlayerEvent('{"type":"other"}'), null);
  assert.equal(parsePlayerEvent('not json'), null);
  assert.equal(failureReason('150'), 'embedding_disabled');
  assert.equal(failureReason('100'), 'unavailable');
  assert.equal(failureReason('153'), 'missing_referrer');
  assert.equal(failureReason(null), 'unknown');
});

test('a player that never becomes ready reports an error (no endless black frame)', () => {
  const html = embedHtml(video)!;
  assert.match(html, /readySeen = true; post\('ready'\)/);
  assert.match(html, /if \(!readySeen\) post\('error', 'api_timeout'\)/);
  assert.ok(READY_TIMEOUT_MS > 15_000);          // the app's own watchdog backs up the page's 15 s check
});

test('frame sizes fit the smallest supported iPhone and Shorts stay portrait', () => {
  // iPhone SE / 13 mini: 375 pt wide, 20 pt gutters -> 335 pt available.
  assert.deepEqual(frameSize('16:9', 335), { width: 335, height: 188 });
  assert.deepEqual(frameSize(null, 335), { width: 335, height: 188 });
  assert.deepEqual(frameSize('9:16', 335), { width: 236, height: 420 });   // capped height, centred
  assert.deepEqual(frameSize('9:16', 200), { width: 200, height: 356 });   // narrow: limited by width
  // Pro Max: 430 pt -> 390 available.
  assert.deepEqual(frameSize('16:9', 390), { width: 390, height: 219 });
});

test('durations', () => {
  assert.equal(duration(102), '1:42');
  assert.equal(duration(87), '1:27');
  assert.equal(duration(5), '0:05');
});

test('analytics properties: drill id, check category and source type only (no URLs)', () => {
  assert.deepEqual(drillProps(drill), { drill_id: 'pump-drill', check: 'downswing.plane', video_source: 'youtube' });
  assert.deepEqual(drillProps({ ...drill, demo_video: null, targets: [] }), { drill_id: 'pump-drill', check: 'Plane', video_source: 'none' });
  assert.ok(!JSON.stringify(drillProps(drill)).includes('http'));
});
