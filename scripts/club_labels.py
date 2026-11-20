"""Club ground truth: pick held-out frames, label grip + clubhead in a local web page, score the detector.

    .venv\\Scripts\\python.exe scripts\\club_labels.py prep        # once: extract the frames
    .venv\\Scripts\\python.exe scripts\\club_labels.py serve       # label at http://localhost:8765
    .venv\\Scripts\\python.exe scripts\\club_labels.py baseline    # score the current detector against the labels

Everything lives in ``var/qa/club_labels`` (git-ignored, never uploaded):
``manifest.json`` (the frames and why each was picked), ``frames/*.jpg`` and ``labels.json``
(written by the page on every save, so labelling can stop and resume at any time).

The frames are the three plane moments (Address, Mid-Back, Mid-Down) and two frames either side,
on HELD_OUT swings chosen to cover left-handers, 50-60 fps, square / low-resolution crops, wide frames,
a moving camera, the hardest tracking and typical easy swings. These swings are a test set: they must
never be used to train or tune a club model (the auto-labelled training set excludes them).

The page never shows the engine's answer, so the labels are not anchored to it.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var" / "qa" / "club_labels"
PAGE = ROOT / "scripts" / "club_labels.html"
OFFSETS = (-2, -1, 0, 1, 2)
MOMENTS = ("address", "mid_back", "downswing")

# set -> (validation output folder, source swings folder)
SETS = {
    "P": ("var/qa/validation_prod", "var/qa/prod_swings"),
    "N": ("var/qa/validation_new", "var/qa/prod_swings_new"),
    "R": ("var/qa/validation_1_0_1", "var/qa/test_swings"),
}
# 20 held-out swings and why each is in the test set.
HELD_OUT = {
    "P24": "left-handed", "P33": "left-handed",
    "P06": "59 fps", "P46": "60 fps",
    "P14": "square 720x720 crop, club rarely tracked", "P15": "square 576x576 crop, club never tracked",
    "P21": "square 576x576 crop", "P18": "low resolution 480x854",
    "P27": "wide 866x1080 frame", "P41": "wide 864x1080 frame, hard tracking",
    "P05": "hard tracking, moving camera", "P16": "hard tracking, moving camera",
    "P35": "hard tracking", "P38": "hard tracking, AI fallback used",
    "P31": "24 fps, moving camera",
    "P01": "typical, easy", "P50": "typical, easy",
    "N01": "newest production upload", "R04": "reference swing", "R10": "reference swing",
}


def swing_dirs(key: str, run: str) -> tuple[Path, Path]:
    out, src = SETS[key[0]]
    name = key[1:]
    analysis = ROOT / out / run / name
    source = next((d for d in sorted((ROOT / src).glob(f"{name}*")) if (d / "window.mp4").exists()), None)
    if source is None:
        raise SystemExit(f"no window.mp4 for {key}")
    return analysis, source / "window.mp4"


def line_deg(grip, head) -> float:
    """The shaft line's angle from horizontal, 0-90 (as the engine's plane_frames line_deg)."""

    ang = math.degrees(math.atan2(-(head[1] - grip[1]), head[0] - grip[0]))
    return abs(((ang + 90.0) % 180.0) - 90.0)


# ----------------------------------------------------------------------------------------------- prep


def prep(run: str) -> None:
    import cv2

    frames_dir = OUT / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    items, swings = [], {}
    for key, why in HELD_OUT.items():
        adir, video = swing_dirs(key, run)
        a = json.loads((adir / "analysis.json").read_text(encoding="utf-8"))
        moment = {p["phase"]: p.get("frame_index") for p in a["phases"]}
        wanted = {}
        for m in MOMENTS:
            if moment.get(m) is None:
                continue
            for off in OFFSETS:
                wanted.setdefault(int(moment[m]) + off, (m, off))
        cap = cv2.VideoCapture(str(video))
        f, last = 0, max(wanted) if wanted else -1
        got = 0
        while f <= last:
            ok, img = cap.read()
            if not ok:
                break
            if f in wanted:
                m, off = wanted[f]
                fid = f"{key}_{f:04d}"
                cv2.imwrite(str(frames_dir / f"{fid}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
                items.append({"id": fid, "swing": key, "frame": f, "moment": m, "offset": off,
                              "width": img.shape[1], "height": img.shape[0]})
                got += 1
            f += 1
        cap.release()
        swings[key] = {"why": why, "frames": got, "handedness": (a.get("handedness") or {}).get("value"),
                       "fps": round(float(a["fps"]), 1), "size": [a["width"], a["height"]],
                       "camera": a["camera_check"].get("status")}
        print(f"{key}: {got} frames ({why})")
    manifest = {"version": 1, "moments_from_run": run, "offsets": OFFSETS, "swings": swings, "items": items}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"{len(items)} frames from {len(swings)} swings -> {OUT}")


# ---------------------------------------------------------------------------------------------- serve


def serve(port: int) -> None:
    labels_path = OUT / "labels.json"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/manifest.json":
                return self._send(200, (OUT / "manifest.json").read_bytes(), "application/json")
            if path == "/labels.json":
                body = labels_path.read_bytes() if labels_path.exists() else b'{"frames":{},"swings":{}}'
                return self._send(200, body, "application/json")
            if path.startswith("/frames/") and path.endswith(".jpg"):
                target = (OUT / "frames" / Path(path).name)
                if target.exists():
                    return self._send(200, target.read_bytes(), "image/jpeg")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/labels.json":
                return self._send(404, b"not found", "text/plain")
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
            if not isinstance(data, dict) or "frames" not in data:
                return self._send(400, b"bad labels", "text/plain")
            tmp = labels_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(labels_path)
            self._send(200, b'{"ok":true}', "application/json")

    if not (OUT / "manifest.json").exists():
        raise SystemExit("run `club_labels.py prep` first")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Labelling page: http://localhost:{port}   (Ctrl+C to stop; every save is written to {labels_path})")
    server.serve_forever()


# ------------------------------------------------------------------------------------------- baseline


def engine_reading(analysis: dict, frame: int) -> dict:
    """What the engine read at this frame for the plane checks: the line it would use, and where it came from."""

    found = {"read": False, "usable": False, "line_deg": None, "source": None, "level": None}
    for block in ((analysis.get("engine") or {}).get("plane_frames") or {}).values():
        rows = [r for r in block.get("frames", []) if r.get("frame") == frame]
        if not rows:
            continue
        found["read"] = True
        usable = [r for r in rows if r.get("usable")]
        # an override (AI fallback) replaces the track where the engine used it
        pick = next((r for r in usable if r.get("source") == "override"), usable[0] if usable else None)
        if pick is not None:
            found.update(usable=True, line_deg=float(pick["line_deg"]), source=pick["source"], level=pick.get("level"))
            break
    return found


def band(change: float) -> str:
    a = abs(change)
    return "on" if a <= 5 else "slight" if a <= 12 else "clear"


def baseline(runs: list[str]) -> None:
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    labels = json.loads((OUT / "labels.json").read_text(encoding="utf-8"))
    lab = labels.get("frames", {})
    items = [i for i in manifest["items"] if i["id"] in lab and lab[i["id"]].get("status") != "skip"]
    lines = [f"# Club detector baseline vs hand labels\n",
             f"{len(items)} labelled frames of {len(manifest['items'])} on {len(manifest['swings'])} held-out swings.\n"]
    summary = {}
    for run in runs:
        rows = []
        for it in items:
            adir, _ = swing_dirs(it["swing"], run)
            a = json.loads((adir / "analysis.json").read_text(encoding="utf-8"))
            e = engine_reading(a, it["frame"])
            L = lab[it["id"]]
            has_club = L.get("status") == "club" and L.get("grip") and L.get("head")
            truth = line_deg(L["grip"], L["head"]) if has_club else None
            err = None if (truth is None or not e["usable"]) else abs(e["line_deg"] - truth)
            rows.append({**it, "blur": bool(L.get("blur")), "status": L.get("status"), "truth": truth, **e, "err": err})
        read = [r for r in rows if r["read"]]
        clubs = [r for r in read if r["truth"] is not None]
        noclub = [r for r in read if r["status"] in ("none", "hidden")]
        found = [r for r in clubs if r["usable"]]
        errs = sorted(r["err"] for r in found)
        wrong = [r for r in found if r["err"] > 10]
        false_pos = [r for r in noclub if r["usable"]]
        s = {
            "frames_read": len(read), "club_visible": len(clubs), "detected": len(found),
            "detection_rate": round(len(found) / len(clubs), 3) if clubs else None,
            "median_err_deg": round(st.median(errs), 1) if errs else None,
            "p90_err_deg": round(errs[int(0.9 * (len(errs) - 1))], 1) if errs else None,
            "within_3": sum(e <= 3 for e in errs), "within_5": sum(e <= 5 for e in errs),
            "wrong_line_gt10": len(wrong), "no_club_frames": len(noclub), "false_detections": len(false_pos),
        }
        # Plane reading as the score sees it: moment line minus Address line (offset 0 frames only).
        plane = []
        for sw in manifest["swings"]:
            by = {(r["moment"], r["offset"]): r for r in rows if r["swing"] == sw}
            ref = by.get(("address", 0))
            for m in ("mid_back", "downswing"):
                cur = by.get((m, 0))
                if not ref or not cur or ref["truth"] is None or cur["truth"] is None:
                    continue
                true_change = cur["truth"] - ref["truth"]
                eng = None if not (ref["usable"] and cur["usable"]) else cur["line_deg"] - ref["line_deg"]
                plane.append({"swing": sw, "moment": m, "true": true_change, "engine": eng})
        measured = [p for p in plane if p["engine"] is not None]
        s["plane_readings_labelled"] = len(plane)
        s["plane_readings_measured"] = len(measured)
        s["plane_median_err_deg"] = round(st.median(abs(p["engine"] - p["true"]) for p in measured), 1) if measured else None
        s["plane_same_band"] = sum(band(p["engine"]) == band(p["true"]) for p in measured)
        for cat, pick in (("blurred", lambda r: r["blur"]), ("sharp", lambda r: not r["blur"]),
                          ("moment frame", lambda r: r["offset"] == 0),
                          ("left-handed", lambda r: manifest["swings"][r["swing"]]["handedness"] == "left")):
            sub = [r for r in clubs if pick(r)]
            subf = [r for r in sub if r["usable"]]
            s[f"[{cat}] detected"] = f"{len(subf)}/{len(sub)}"
            s[f"[{cat}] median err"] = round(st.median(r["err"] for r in subf), 1) if subf else None
        summary[run] = s
        (OUT / f"baseline_{run}.json").write_text(json.dumps({"summary": s, "rows": rows, "plane": plane}, indent=1),
                                                 encoding="utf-8")
    keys = list(next(iter(summary.values())).keys())
    lines.append("| | " + " | ".join(runs) + " |")
    lines.append("|---|" + "---|" * len(runs))
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(summary[r][k]) for r in runs) + " |")
    (OUT / "BASELINE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("prep", "serve", "baseline"))
    parser.add_argument("--run", default="candidate_b13", help="validation run whose moments pick the frames")
    parser.add_argument("--runs", default="next,candidate", help="baseline: engine runs to score")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.mode == "prep":
        prep(args.run)
    elif args.mode == "serve":
        serve(args.port)
    else:
        baseline(args.runs.split(","))
    return 0


if __name__ == "__main__":
    sys.exit(main())
