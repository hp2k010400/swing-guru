"""Shared fixtures and import wiring for the Swing Guru Engine test suite."""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Keep pytest's per-test temporary directories inside the repository. The
# default user-profile temp directory is not always readable on managed
# Windows accounts, and a repo-local root keeps the suite self-contained.
_TEMP_ROOT = ROOT / ".tmp" / "pytest"
_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(_TEMP_ROOT)
os.environ["TEMP"] = str(_TEMP_ROOT)
os.environ["TMP"] = str(_TEMP_ROOT)
tempfile.tempdir = str(_TEMP_ROOT)

from swingguru.runtime_env import ensure_runtime_env  # noqa: E402

# MediaPipe pulls in Matplotlib, which needs a writable config directory.
ensure_runtime_env()

from swingguru.synthetic import SyntheticClip, ScriptedPoseProvider  # noqa: E402

SMALL_CLIP = SyntheticClip(width=320, height=180, fps=24.0, frame_count=24)

_CASE_ROOT = _TEMP_ROOT / "cases"


@pytest.fixture()
def tmp_path(request: pytest.FixtureRequest) -> Path:
    """A writable per-test directory inside the repository.

    Pytest's built-in ``tmp_path`` creates its base directory with mode
    ``0o700``. On managed Windows accounts the resulting ACL can make that
    directory unlistable, which fails fixture setup before any test runs, so
    the suite uses an ordinary directory under ``.tmp/pytest`` instead.
    """

    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in request.node.nodeid)
    # The cap is deliberately short. A long test name plus the dated case root plus the service's
    # own `<user>/<swing>/<file>` object path can reach Windows' 260-character MAX_PATH, and the
    # resulting ``open`` failure is a misleading ``FileNotFoundError`` (not a permission error).
    path = _CASE_ROOT / f"{safe[:40]}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def small_clip() -> SyntheticClip:
    return SMALL_CLIP


@pytest.fixture()
def fixture_video(tmp_path: Path) -> Path:
    """A small deterministic landscape clip with the drawn golfer figure."""

    from swingguru.synthetic import make_synthetic_clip

    return make_synthetic_clip(tmp_path / "fixture.mp4", clip=SMALL_CLIP)


@pytest.fixture()
def scripted_provider() -> ScriptedPoseProvider:
    return ScriptedPoseProvider(clip=SMALL_CLIP)
