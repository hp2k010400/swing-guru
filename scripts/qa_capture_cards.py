"""Capture-mode (export) checkpoint cards: screenshots and measurements per check (read-only).

    python scripts/qa_capture_cards.py <bundle dir> <out dir> [--size 360x640] [--label before]

Opens every walkthrough check (scored + observations) held in the export layout
(``?embed=1&overlay=3&capture=1&check=...``) at the given CSS size with device scale 3 (360x640 =
the 1080x1920 export; 393x852 = an iPhone 15), shows the overlay's final state, and records:

* the card's height (CSS px) and its share of the frame height;
* whether the card text is clipped (scrollHeight > clientHeight on the explanation);
* how many important overlay points (measured geometry, joint markers, the head, the verdict bubble)
  sit under the card;
* whether the card was placed at the top or bottom.

Writes ``<label>_<check>.png``, a contact sheet ``<label>_sheet.png`` and ``<label>.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import overlay_v3_qa as q  # noqa: E402

PROBE = r"""(function () {
  var card = document.getElementById('card'), stage = document.getElementById('stage');
  var cr = card.getBoundingClientRect(), sr = stage.getBoundingClientRect();
  var detail = document.getElementById('card-detail'), title = document.getElementById('card-title');
  var rect = { x: cr.left - sr.left, y: cr.top - sr.top, w: cr.width, h: cr.height };
  // Important overlay content: every drawn line end / marker / value / bubble inside the SVG.
  var pts = [];
  document.querySelectorAll('#overlay path, #overlay circle, #overlay text, #overlay rect').forEach(function (el) {
    if (el.closest('.persist')) return;
    var op = 1, n = el; while (n && n.id !== 'overlay') { var a = n.getAttribute && n.getAttribute('opacity'); if (a !== null && a !== undefined) op *= Number(a); n = n.parentNode; }
    if (op < 0.05) return;
    try {
      if (el.tagName === 'path' && el.getTotalLength && el.getTotalLength() > 0) {
        var L = el.getTotalLength();                       // sample along every drawn line
        for (var k = 0; k <= 16; k++) { var q = el.getPointAtLength(L * k / 16); pts.push([q.x, q.y]); }
      } else { var b = el.getBBox(); pts.push([b.x + b.width / 2, b.y + b.height / 2]); }
    } catch (e) {}
  });
  var under = pts.filter(function (p) { return p[0] >= rect.x && p[0] <= rect.x + rect.w && p[1] >= rect.y && p[1] <= rect.y + rect.h; }).length;
  return JSON.stringify({ height: Math.round(cr.height), frame: Math.round(sr.height), share: +(cr.height / sr.height).toFixed(3),
    clipped: detail.scrollHeight > detail.clientHeight + 1 || title.scrollWidth > title.clientWidth + 1,
    under: under, drawn: pts.length, placement: card.classList.contains('bottom') ? 'bottom' : 'top',
    visible: getComputedStyle(card).display !== 'none' && getComputedStyle(card).opacity !== '0' });
})()"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle")
    parser.add_argument("out")
    parser.add_argument("--size", default="360x640")
    parser.add_argument("--label", default="after")
    args = parser.parse_args(argv)
    width, height = (int(v) for v in args.size.split("x"))
    bundle, out = Path(args.bundle), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = q.default_checks(bundle)
    server, base = q.serve(bundle)
    proc, c = q.open_browser()
    c.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 3, "mobile": True})
    rows, shots = [], []
    try:
        for check in checks:
            c.send("Page.navigate", {"url": f"{base}?embed=1&overlay=3&capture=1&check={check}"})
            c.wait_for("!!window.__swingViewer && window.__swingViewer.checkId === " + json.dumps(check), timeout=60, label=check)
            time.sleep(0.4)
            c.evaluate("window.__swingViewer.v3At(99999)")
            time.sleep(0.5)
            row = {"check": check, **json.loads(c.evaluate(PROBE))}
            rows.append(row)
            path = out / f"{args.label}_{check.replace(':', '_')}.png"
            shot = c.send("Page.captureScreenshot", {"format": "png"})
            import base64

            path.write_bytes(base64.b64decode(shot["data"]))
            shots.append((path, check))
            print(json.dumps(row), flush=True)
    finally:
        proc.terminate()
        server.shutdown()
    from PIL import Image, ImageDraw

    ims = [Image.open(p) for p, _ in shots]
    w, h = ims[0].size
    cols = min(6, len(ims))
    rows_n = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (w // 3 + 8), rows_n * (h // 3 + 22)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (im, (_, check)) in enumerate(zip(ims, shots)):
        x, y = (i % cols) * (w // 3 + 8), (i // cols) * (h // 3 + 22)
        sheet.paste(im.resize((w // 3, h // 3)), (x, y + 18))
        draw.text((x + 4, y + 3), f"{args.label} {check}", fill=(20, 20, 20))
    sheet.save(out / f"{args.label}_sheet.png")
    (out / f"{args.label}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
