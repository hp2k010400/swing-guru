"""Offline study of a candidate Address rule (no engine change): later of the hand-speed and body-wide set-ups.

    .venv\\Scripts\\python.exe scripts\\address_rule_study.py

Today the Address is the end of the latest still window found by the hand-speed test; a single
jittering wrist frame can disqualify the real final set-up, leaving an earlier pause (swing 02: 2.6 s
before the Top). Candidate: also run the existing body-wide stillness test (net drift of hands,
pelvis, shoulders and ankles, with its posture and no-drift guards) and take the LATER of the two
set-up ends, provided it is before the takeaway. Replayed on cached pose records:

* the 67 validation swings: how many Address frames change, by how much, and where;
* the trim-stability variants: Address agreement with the original clip, before vs after.

Read-only; prints a summary and writes docs/agent-work/analysis-v2/ADDRESS_RULE.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Only an Address this long before the takeaway is re-checked (median gap on 67 swings: 0.25 s).
SUSPICIOUS_GAP_S = float(next((a.split('=')[1] for a in sys.argv if a.startswith('--gap=')), '0.6'))
sys.path.insert(0, str(ROOT / "src"))

from swingguru import auto_moments as am  # noqa: E402
from swingguru.pose_cache import load_records_any  # noqa: E402


def addresses(records) -> tuple[float | None, float | None, float | None, float | None]:
    """(today's address, candidate address, takeaway, top) in seconds, from the engine's own functions."""

    cfg = am.MomentConfig()
    m = am.estimate_moments(records)
    by = {e.phase.value if hasattr(e.phase, "value") else e.phase: e for e in m.estimates}
    def t(name):
        e = by.get(name)
        return None if e is None or e.timestamp_ms is None else e.timestamp_ms / 1000
    today, takeaway, top = t("address"), t("takeaway"), t("top")
    if today is None or takeaway is None:
        return today, today, takeaway, top
    s = am.build_motion_series(records, config=cfg)
    sm = am._smoothed(s, cfg)
    sp = am._speeds(s, sm)
    body = am._find_episodes(s, sm, sp, cfg, body_wide=True)
    # the body-wide episode for the same swing: same top (within 2 frames)
    same = [e for e in body if top is not None and abs(s.times_s[e.top] - top) <= 2 / max(1e-6, 1 / (s.times_s[1] - s.times_s[0]))]
    candidate = today
    if takeaway - today <= SUSPICIOUS_GAP_S:
        return today, candidate, takeaway, top          # a normal set-up gap: untouched
    for e in same:
        b = s.times_s[e.address]
        if today < b < takeaway:
            candidate = max(candidate, b)
    return today, candidate, takeaway, top


def main() -> int:
    lines = ["# Candidate Address rule: later of the hand-speed and body-wide set-ups (offline study)\n"]
    changed, total, big = [], 0, []
    for root in ("validation_prod", "validation_new", "validation_1_0_1"):
        for rec in sorted((ROOT / "var/qa" / root / "candidate").glob("*/pose_records.pkl")):
            today, cand, takeaway, top = addresses(load_records_any(rec))
            if today is None:
                continue
            total += 1
            if cand != today:
                changed.append((f"{root[11:]}/{rec.parent.name}", round(today, 2), round(cand, 2), round(takeaway, 2)))
    lines.append(f"**Validation swings:** {total} analysed; Address changes on {len(changed)}:\n")
    lines.append("| Swing | Address today (s) | Candidate (s) | Takeaway (s) | Today: gap to takeaway | Candidate: gap |\n|---|---|---|---|---|---|")
    for name, a, b, tk in changed:
        lines.append(f"| {name} | {a} | {b} | {tk} | {tk - a:.2f} s | {tk - b:.2f} s |")
    # trim variants: agreement with the original clip
    agree = {"today": [0, 0], "candidate": [0, 0]}
    for swing in sorted((ROOT / "var/qa/trim_stability").iterdir()):
        base = swing / "orig" / "run" / "bundle" / "pose_records.pkl"
        if not base.is_file():
            continue
        o_today, o_cand, *_ = addresses(load_records_any(base))
        for variant in ("start", "end", "reenc"):
            p = swing / variant / "run" / "bundle" / "pose_records.pkl"
            if not p.is_file() or o_today is None:
                continue
            v_today, v_cand, *_ = addresses(load_records_any(p))
            off = 0.2 if variant == "start" else 0.0
            for key, a, b in (("today", o_today, v_today), ("candidate", o_cand, v_cand)):
                if b is None:
                    continue
                agree[key][1] += 1
                agree[key][0] += abs((b + off) - a) <= 0.07     # within ~2 frames at 30 fps
    lines.append("\n**Trim and re-encode variants (Address within about 2 frames of the original clip):**\n")
    for key in ("today", "candidate"):
        ok, n = agree[key]
        lines.append(f"* {key}: {ok}/{n} ({100 * ok / max(n, 1):.0f}%)")
    text = "\n".join(lines) + "\n"
    (ROOT / "docs/agent-work/analysis-v2/ADDRESS_RULE.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
