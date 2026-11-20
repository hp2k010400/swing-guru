"""Process environment fixes that must happen before MediaPipe is imported.

MediaPipe depends on Matplotlib, and Matplotlib insists on a writable
configuration directory under the user profile. On a locked-down Windows
account that path can be read-only, in which case importing MediaPipe aborts
with ``PermissionError: [WinError 5]`` before any of our code runs.

``ensure_runtime_env`` points ``MPLCONFIGDIR`` at a project-local cache so the
import works without touching the user profile or requiring elevated rights.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parents[1]

DEFAULT_CACHE_DIR = _REPO_ROOT / ".cache"


def repo_root() -> Path:
    """Absolute path to the repository root this package is installed from."""

    return _REPO_ROOT


def ensure_runtime_env(cache_dir: Path | str | None = None) -> Path:
    """Make sure third-party libraries have a writable cache directory.

    Returns the Matplotlib configuration directory in use. An existing
    ``MPLCONFIGDIR`` in the environment always wins, so callers can override.
    """

    existing = os.environ.get("MPLCONFIGDIR")
    if existing:
        return Path(existing)

    base = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    target = base / "matplotlib"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = Path(tempfile.mkdtemp(prefix="swingguru-mpl-"))
    os.environ["MPLCONFIGDIR"] = str(target)

    # MediaPipe's C++ runtime logs delegate and ROI notices on startup. These
    # quieten most of them; a couple are emitted through a pre-init glog path
    # and still appear, which is harmless for a CLI run.
    os.environ.setdefault("GLOG_minloglevel", "2")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    return target
