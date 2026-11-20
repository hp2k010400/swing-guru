"""Head components: repeatability, sensitivity to the drop allowance, and an independent sample. Read-only.

    .venv\\Scripts\\python.exe scripts\\head_validation.py [staging test account id]

1. Repeatability: the vertical (drop / rise) and lateral (toward / away from the ball) parts at Impact on
   the trim-study variants (start / end trimmed, re-encoded) vs the original clip.
2. Sensitivity: tour vs other swings in range under a lateral-first rule for drop allowances of 4-8%.
3. Independent sample: the golfer's own swings on staging (not used to choose any number), classified
   today vs lateral-first at 6%. Optional; needs the staging account id.
Appends to docs/agent-work/analysis-v2/HEAD_COMPONENTS.md. No score or verdict is changed.
"""

from __future__ import annotations

import json
import statistics as st
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from head_component_study import rows as validation_rows  # noqa: E402


def impact_head(a: dict) -> dict | None:
    for ph in a.get("phases", []):
        if ph["phase"] == "impact":
            for c in ph["checks"]:
                if c.get("kind") == "head" and c.get("head_direction"):
                    hd = c["head_direction"]
                    return {"down": hd["down_pct"], "ball": hd.get("toward_ball_pct"), "total": hd["total_pct"],
                            "status": (c.get("verdict") or {}).get("status")}
    return None


def band(x: float) -> str:
    return "good" if x < 4 else "attention" if x < 8 else "problem"


def lateral_first(h: dict, allowance: float) -> str:
    return band(max(abs(h["ball"] or 0), max(0.0, -h["down"]), max(0.0, h["down"] - allowance)))


def main(argv: list[str]) -> int:
    lines = ["\n## Validation round 2 (2026-11-08)\n"]
    # 1. repeatability
    dd, dl, dt = [], [], []
    for swing in sorted((ROOT / "var/qa/trim_stability").iterdir()):
        base = next((swing / "orig" / "run").rglob("analysis.json"), None) if (swing / "orig" / "run").is_dir() else None
        h0 = impact_head(json.loads(base.read_text(encoding="utf-8"))) if base else None
        if not h0:
            continue
        for variant in ("start", "end", "reenc", "crop", "dark", "small"):
            p = swing / variant / "run"
            f = next(p.rglob("analysis.json"), None) if p.is_dir() else None
            h1 = impact_head(json.loads(f.read_text(encoding="utf-8"))) if f else None
            if not h1:
                continue
            dd.append(abs(h1["down"] - h0["down"]))
            dt.append(abs(h1["total"] - h0["total"]))
            if h0["ball"] is not None and h1["ball"] is not None:
                dl.append(abs(h1["ball"] - h0["ball"]))
    if dd:
        q = lambda v: f"median {st.median(v):.2f}, p90 {sorted(v)[int(0.9 * (len(v) - 1))]:.2f}"
        lines.append(f"**1. Repeatability** (same swing, {len(dd)} trimmed / re-encoded / reframed variants; change in % of height):")
        lines.append(f"* vertical (drop / rise): {q(dd)}")
        lines.append(f"* lateral (toward / away from the ball): {q(dl)}")
        lines.append(f"* total (today's scored number): {q(dt)}\n")
    # 2. sensitivity
    data = validation_rows()
    lines.append("**2. Sensitivity to the drop allowance** (validation set; in range = good):\n")
    lines.append("| Allowance | Tour in range | Others in range | Tour clear | Others clear |\n|---|---|---|---|---|")
    for allowance in (4.0, 5.0, 6.0, 7.0, 8.0):
        t = [lateral_first({"down": r["down"], "ball": r["ball"]}, allowance) for r in data if r["tour"]]
        o = [lateral_first({"down": r["down"], "ball": r["ball"]}, allowance) for r in data if not r["tour"]]
        lines.append(f"| {allowance:g}% | {t.count('good')}/{len(t)} | {o.count('good')}/{len(o)} | {t.count('problem')} | {o.count('problem')} |")
    # 3. independent sample
    if argv:
        key = (ROOT / ".tmp" / "staging_access_key.txt").read_text(encoding="utf-8").strip()
        head = {"Authorization": f"Bearer dev:{argv[0]}", "X-SG-Access-Key": key}
        base = "https://swing-guru-staging-production.up.railway.app"
        get = lambda u, h=head: json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=60).read())
        mine = []
        for s in get(base + "/v1/swings")["swings"]:
            d = get(f"{base}/v1/swings/{s['id']}")
            h = impact_head(get(d["replay"]["analysis_url"], {}))
            if h:
                mine.append(h)
        today = [band(h["total"]) for h in mine]
        alt = [lateral_first(h, 6.0) for h in mine]
        c = lambda xs: f"{xs.count('good')} good / {xs.count('attention')} slight / {xs.count('problem')} clear"
        lines.append(f"\n**3. Independent sample** (the golfer's own {len(mine)} staging swings with a direction-split head "
                     f"check; never used to choose a number): today {c(today)}; lateral-first at 6% {c(alt)}. "
                     f"Median drop {st.median([h['down'] for h in mine]):.1f}%, lateral {st.median([abs(h['ball'] or 0) for h in mine]):.1f}%.")
    text = "\n".join(lines) + "\n"
    with open(ROOT / "docs/agent-work/analysis-v2/HEAD_COMPONENTS.md", "a", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
