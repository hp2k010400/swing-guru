"""Build bounded *synthetic* degradations of one clip for diagnostics comparisons.

These files exist only to exercise the analysis stages on difficult footage.
They are never archive footage and their outcomes must never be reported as the
behaviour of a real uploaded clip. Every output carries a ``variant.json`` that
states the source hash, the tool, the exact parameters and ``synthetic: true``.

    python scripts/make_degraded_variants.py var/test_swings/CLIP.mp4 \\
        --out var/qa/launch-polish/degraded [--variants fps24,fps25,fps30,...]

The source file is only ever read. Each variant is written into its own folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.video import ffmpeg_path  # noqa: E402

SCHEMA = "swingguru.synthetic_variant/1"

# name -> (ffmpeg arguments, human description, parameters recorded in variant.json)
VARIANTS: dict[str, tuple[list[str], str, dict]] = {
    "fps24": (["-vf", "fps=24"], "re-timed to 24 fps", {"fps": 24}),
    "fps25": (["-vf", "fps=25"], "re-timed to 25 fps", {"fps": 25}),
    "fps30": (["-vf", "fps=30"], "re-timed to 30 fps", {"fps": 30}),
    "downscale": (["-vf", "scale=360:-2"], "downscaled to 360 px wide", {"width": 360}),
    "grayscale": (["-vf", "hue=s=0"], "desaturated to grayscale", {"saturation": 0}),
    "lowcontrast": (["-vf", "eq=contrast=0.55"], "contrast reduced to 0.55", {"contrast": 0.55}),
    "compression": (["-crf", "32"], "heavy H.264 compression (crf 32)", {"crf": 32}),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def make_one(binary: str, source: Path, out_dir: Path, name: str) -> dict:
    extra, description, parameters = VARIANTS[name]
    target = out_dir / name
    target.mkdir(parents=True, exist_ok=True)
    output = target / f"{source.stem}.{name}.mp4"
    command = [binary, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), *extra,
               "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        tail = (completed.stderr or "").strip().splitlines()[-3:]
        raise SystemExit(f"variant {name} failed: " + " | ".join(tail))
    record = {
        "schema": SCHEMA,
        "synthetic": True,
        "variant": name,
        "description": description,
        "parameters": parameters,
        "tool": {"name": "ffmpeg", "command": [Path(binary).name, *extra]},
        "source": {"name": source.name, "sha256": sha256(source)},
        "output": {"name": output.name, "sha256": sha256(output), "bytes": output.stat().st_size},
        "path": str(output.resolve()),
        "notes": "synthetic degradation for diagnostics only - never an archive clip",
    }
    (target / "variant.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="a clip, or a folder of clips to pick from with --match")
    parser.add_argument("--match", default="", help="comma-separated substrings when source is a folder")
    parser.add_argument("--out", required=True)
    parser.add_argument("--variants", default=",".join(VARIANTS))
    args = parser.parse_args(argv)
    source = Path(args.source)
    if source.is_dir():
        needles = [needle for needle in args.match.split(",") if needle]
        candidates = sorted(p for p in source.rglob("*")
                            if p.suffix.lower() in {".mp4", ".mov"} and (not needles or any(n in p.name for n in needles)))
        if not candidates:
            print(f"no clip in {source} matched {args.match!r}", file=sys.stderr)
            return 2
        source = candidates[0]
    if not source.is_file():
        print(f"source clip not found: {source}", file=sys.stderr)
        return 2
    names = [name.strip() for name in args.variants.split(",") if name.strip()]
    unknown = [name for name in names if name not in VARIANTS]
    if unknown:
        print(f"unknown variants {unknown}; known: {sorted(VARIANTS)}", file=sys.stderr)
        return 2
    binary = ffmpeg_path()
    if binary is None:
        print("ffmpeg is required to build degraded variants", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    records = [make_one(binary, source, out_dir, name) for name in names]
    manifest = {"schema": SCHEMA, "synthetic": True, "source": {"name": source.name, "sha256": sha256(source)},
                "variants": records}
    (out_dir / "variants.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for record in records:
        print(f"{record['variant']}: {record['path']}")
    print(f"{len(records)} synthetic variant(s) -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
