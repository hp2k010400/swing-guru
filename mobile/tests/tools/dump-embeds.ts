// Writes the app's own embed page for each drill video (from the backend catalogue) to a folder, for the
// headless player check (docs/agent-work/release-1.0.1/DRILL_PLAYER_QA.md). Not part of the app.
//   node --experimental-strip-types tests/tools/dump-embeds.ts <catalogue.json> <out-dir>
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import { embedHtml } from '../../src/lib/drill-video.ts';

const [catalogue, out] = process.argv.slice(2);
mkdirSync(out, { recursive: true });
const data = JSON.parse(readFileSync(catalogue, 'utf-8'));
for (const v of data.videos) {
  const html = embedHtml({ video_id: v.video_id, start_s: v.start_s ?? 0 });
  if (html) writeFileSync(join(out, `${v.drill_id}__${v.video_id}__${v.status}.html`), html);
}
console.log(`wrote ${data.videos.length} embed pages to ${out}`);
