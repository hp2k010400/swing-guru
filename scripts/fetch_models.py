"""Download every model the engine needs, once, at image build time.

    python scripts/fetch_models.py [models_dir]

Standalone on purpose (no ``swingguru`` imports), so the Docker layer that runs
it is cached and a code change never re-downloads ~1 GB of models:

* MediaPipe pose landmarker -> ``<models_dir>/pose_landmarker_full.task`` (swing finder + segmentation);
* RTMW whole-body ("balanced": YOLOX-m + RTMW-x) via rtmlib into ``$XDG_CACHE_HOME/rtmlib``;
* OWLv2 (club fallback) into ``$HF_HOME``.

At runtime the container sets ``HF_HUB_OFFLINE=1``; ``/ready`` fails if any model is missing.
Keep these identifiers in sync with ``swingguru.pose`` and ``swingguru.club.fallback``.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

POSE_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
            "pose_landmarker_full/float16/latest/pose_landmarker_full.task")
OWL_MODEL = "google/owlv2-base-patch16-ensemble"


def main() -> int:
    models = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "models")
    models.mkdir(parents=True, exist_ok=True)
    target = models / "pose_landmarker_full.task"
    if not target.is_file() or target.stat().st_size == 0:
        urllib.request.urlretrieve(POSE_URL, target)
    print("mediapipe:", target, target.stat().st_size)

    from rtmlib import Wholebody

    Wholebody(mode="balanced", backend="onnxruntime", device="cpu")
    print("rtmw: balanced whole-body models cached")

    from transformers import Owlv2ForObjectDetection, Owlv2Processor

    Owlv2Processor.from_pretrained(OWL_MODEL)
    Owlv2ForObjectDetection.from_pretrained(OWL_MODEL)
    print("owlv2:", OWL_MODEL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
