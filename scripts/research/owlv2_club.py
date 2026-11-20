"""Zero-shot club detection with OWLv2 (Apache-2.0) on the key moments.

    .venv\\Scripts\\python.exe scripts\\research\\owlv2_club.py VIDEO

Prompts "golf club", "golf club head" and "golf club shaft" on each key
frame and draws every box above a score floor, so a human can judge whether
an off-the-shelf open-vocabulary detector finds the club without training.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402
from transformers import Owlv2ForObjectDetection, Owlv2Processor  # noqa: E402

MOMENTS = {108: "Address", 116: "Takeaway", 120: "Mid-Back", 127: "Top", 132: "Mid-Down", 134: "Impact"}
PROMPTS = ["a golf club", "a golf club head", "a golf club shaft"]
COLOURS = [(0, 220, 255), (80, 220, 80), (255, 140, 60)]
FLOOR = 0.12


def main() -> int:
    video = sys.argv[1]
    out = ROOT / "var" / "research" / "owlv2"
    out.mkdir(parents=True, exist_ok=True)
    model_id = "google/owlv2-base-patch16-ensemble"
    processor = Owlv2Processor.from_pretrained(model_id)
    model = Owlv2ForObjectDetection.from_pretrained(model_id).eval()
    capture = cv2.VideoCapture(video)
    tiles, log = [], {}
    for index, moment in MOMENTS.items():
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        crop = frame[250:832, 0:464]
        image = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        started = time.monotonic()
        inputs = processor(text=[PROMPTS], images=image, return_tensors="pt")
        with torch.no_grad():
            outputs = model(**inputs)
        size = max(image.size)
        result = processor.post_process_grounded_object_detection(
            outputs, threshold=FLOOR, target_sizes=torch.tensor([[size, size]])
        )[0]
        elapsed = time.monotonic() - started
        drawn = crop.copy()
        boxes = []
        for box, score, label in zip(result["boxes"], result["scores"], result["labels"]):
            x0, y0, x1, y1 = [int(v) for v in box.tolist()]
            colour = COLOURS[int(label)]
            cv2.rectangle(drawn, (x0, y0), (x1, y1), colour, 2)
            cv2.putText(drawn, f"{PROMPTS[int(label)].split()[-1]} {float(score):.2f}", (x0, max(12, y0 - 3)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, colour, 1, cv2.LINE_AA)
            boxes.append({"prompt": PROMPTS[int(label)], "score": round(float(score), 3), "box": [x0, y0 + 250, x1, y1 + 250]})
        cv2.putText(drawn, f"{moment} f{index}", (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
        tiles.append(drawn)
        log[moment] = {"frame": index, "seconds": round(elapsed, 2), "boxes": boxes}
        print(moment, f"{elapsed:.1f}s", [(b["prompt"].split()[-1], b["score"]) for b in boxes])
    rows = [np.hstack(tiles[:3]), np.hstack(tiles[3:])]
    cv2.imwrite(str(out / "moments.png"), np.vstack(rows))
    (out / "boxes.json").write_text(json.dumps(log, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
