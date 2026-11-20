"""Contact sheets for the Address rule B review: today's Address | rule B's Address | takeaway, per swing.

    .venv\\Scripts\\python.exe scripts\\address_contact_sheet.py <out dir>

For every validation swing whose Address rule B would move (later of the hand-speed and body-wide set-up
ends, before the takeaway), one row of three frames from the analysed clip, cropped around the golfer
(hands and club in view). Read-only; writes PNG sheets of 8 rows each plus index.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from swingguru import auto_moments as am  # noqa: E402
from swingguru.pose_cache import load_records_any  # noqa: E402

SOURCES = {"prod": "var/qa/prod_swings", "new": "var/qa/prod_swings_new", "1_0_1": "var/qa/test_swings"}
VALID = {"prod": "validation_prod", "new": "validation_new", "1_0_1": "validation_1_0_1"}


def rule_b(records):
    m = am.estimate_moments(records)
    by = {getattr(e.phase, "value", e.phase): e for e in m.estimates}
    if by["address"].frame_index is None or by["takeaway"].frame_index is None:
        return None
    cfg = am.MomentConfig()
    s = am.build_motion_series(records, config=cfg)
    sm = am._smoothed(s, cfg)
    sp = am._speeds(s, sm)
    a, tk, top = by["address"].frame_index, by["takeaway"].frame_index, by["top"].frame_index
    idx = {f: i for i, f in enumerate(s.frames)}
    best = None
    for e in am._find_episodes(s, sm, sp, cfg, body_wide=True):
        f = s.frames[e.address]
        if top is not None and abs(s.frames[e.top] - top) <= 2 and a < f < tk:
            best = f if best is None else max(best, f)
    return (a, best, tk) if best is not None else None


def frame(video: Path, index: int) -> np.ndarray | None:
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, img = cap.read()
    cap.release()
    return img if ok else None


def crop(img: np.ndarray, h: int = 360) -> np.ndarray:
    hh, ww = img.shape[:2]
    # lower 70% of the frame (golfer, hands, club, ball), full width, scaled to h
    part = img[int(hh * 0.3):, :]
    scale = h / part.shape[0]
    return cv2.resize(part, (int(part.shape[1] * scale), h))


def main(out: str) -> int:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, index = [], []
    for tag, src in SOURCES.items():
        for rec in sorted((ROOT / "var/qa" / VALID[tag] / "candidate").glob("*/pose_records.pkl")):
            r = rule_b(load_records_any(rec))
            if r is None:
                continue
            today, b, tk = r
            video = ROOT / src / rec.parent.name / "window.mp4"
            imgs = [frame(video, f) for f in (today, b, tk)]
            if any(i is None for i in imgs):
                continue
            tiles = [crop(i) for i in imgs]
            for t, label in zip(tiles, (f"today f{today}", f"rule B f{b}", f"takeaway f{tk}")):
                cv2.rectangle(t, (0, 0), (t.shape[1], 30), (0, 0, 0), -1)
                cv2.putText(t, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
            row = np.hstack(tiles)
            cv2.putText(row, f"{tag}/{rec.parent.name}", (row.shape[1] - 150, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (80, 220, 120), 2, cv2.LINE_AA)
            rows.append(row)
            index.append({"swing": f"{tag}/{rec.parent.name}", "today": today, "rule_b": b, "takeaway": tk})
    width = max(r.shape[1] for r in rows)
    pad = lambda r: np.hstack([r, np.zeros((r.shape[0], width - r.shape[1], 3), np.uint8)]) if r.shape[1] < width else r
    for k in range(0, len(rows), 8):
        sheet = np.vstack([pad(r) for r in rows[k:k + 8]])
        cv2.imwrite(str(out_dir / f"sheet_{k // 8 + 1:02d}.png"), sheet)
    (out_dir / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"{len(rows)} swings in {((len(rows) - 1) // 8) + 1} sheets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
