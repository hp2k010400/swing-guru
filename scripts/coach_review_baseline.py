"""Capture the pre-implementation baseline for the coach review bundle.

    .venv\\Scripts\\python.exe scripts\\coach_review_baseline.py
    .venv\\Scripts\\python.exe scripts\\coach_review_baseline.py --copy pyproject.toml tests/conftest.py

The workspace has no Git, so this is the audit trail: a SHA-256 inventory of
source, tests, scripts and docs (relative paths only) plus byte-for-byte copies
of any existing file this work is about to change. It writes nothing outside
``var/qa/coach_review_baseline`` and never reads secrets, media or models.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "var" / "qa" / "coach_review_baseline"

# Source, tests, scripts and docs only. No var/, .tmp/, .venv/, node_modules, media or models.
SCAN_DIRS = ("src", "tests", "scripts", "docs")
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".css", ".html", ".js", ".ts", ".tsx", ".sql", ".txt", ".yml", ".yaml"}
SKIP_PARTS = {"__pycache__", "node_modules", ".venv", ".git", ".tmp", "var", "baseline", "models", "third_party"}
SKIP_SUFFIXES = {".mp4", ".mov", ".m4v", ".jpg", ".jpeg", ".png", ".pkl", ".csv", ".onnx", ".pt", ".pth", ".zip"}
MAX_BYTES = 4 * 1024 * 1024  # skip anything surprising and large; the inventory is text artefacts


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for folder in SCAN_DIRS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            parts = set(path.relative_to(root).parts)
            if parts & SKIP_PARTS or path.suffix.lower() in SKIP_SUFFIXES or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if path.stat().st_size > MAX_BYTES:
                continue
            found.append(path)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--copy", nargs="*", default=[], help="existing files about to change, copied verbatim")
    parser.add_argument("--label", default="pre-implementation")
    parser.add_argument("--diff-against", default=None,
                        help="path to an earlier inventory.json; writes changed_files.json here")
    args = parser.parse_args(argv)
    out = Path(args.out)
    (out / "copies").mkdir(parents=True, exist_ok=True)

    files = {}
    for path in iter_files(ROOT):
        rel = path.relative_to(ROOT).as_posix()
        files[rel] = {"sha256": sha256(path), "size": path.stat().st_size,
                      "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")}

    copies = []
    for item in args.copy:
        src = (ROOT / item).resolve()
        if not src.is_file():
            print(f"cannot copy (not a file): {item}", file=sys.stderr)
            return 2
        relative = src.relative_to(ROOT)
        target = out / "copies" / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        copies.append({"path": relative.as_posix(), "sha256": sha256(src), "copy": target.relative_to(ROOT).as_posix()})

    inventory = {
        "label": args.label,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "root": ROOT.name,
        "note": "SHA-256 inventory of source/tests/scripts/docs captured before implementation (no Git in this workspace).",
        "counts": {"files": len(files), "copies": len(copies)},
        "files": files,
        "copies": copies,
    }
    (out / "inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(files)} files hashed, {len(copies)} copied -> {out / 'inventory.json'}")
    if args.diff_against:
        before = json.loads(Path(args.diff_against).read_text(encoding="utf-8"))["files"]
        added = sorted(set(files) - set(before))
        removed = sorted(set(before) - set(files))
        changed = sorted(rel for rel in set(files) & set(before) if files[rel]["sha256"] != before[rel]["sha256"])
        diff = {"against": str(args.diff_against), "generated_at": inventory["generated_at"], "added": added,
                "modified": changed, "removed": removed}
        (out / "changed_files.json").write_text(json.dumps(diff, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"added {len(added)}, modified {len(changed)}, removed {len(removed)} -> {out / 'changed_files.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
