"""CPU runner for the GolfDB SwingNet baseline (research / benchmarking only).

LICENCE: GolfDB code and weights are CC BY-NC 4.0 (non-commercial). This
folder is quarantined research code: nothing under ``src/`` imports it, and it
must not ship in a commercial product.

    .venv\\Scripts\\python.exe third_party\\golfdb\\run_swingnet.py VIDEO \
        [--start F --end F] [--crop x0,y0,x1,y1] [--json out.json]

Preprocessing follows the upstream ``test_video.py``: frames are letterboxed
to 160x160 with ImageNet-mean padding, RGB, ImageNet-normalised; the model
predicts one of 8 events or "no event" per frame and each event is the frame
with the highest probability for that class.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from MobileNetV2 import MobileNetV2  # noqa: E402

EVENTS = [
    "Address",
    "Toe-up",
    "Mid-backswing (arm parallel)",
    "Top",
    "Mid-downswing (arm parallel)",
    "Impact",
    "Mid-follow-through (shaft parallel)",
    "Finish",
]


class SwingNet(nn.Module):
    """Same layers as upstream ``EventDetector`` (inference, CPU, no dropout)."""

    def __init__(self) -> None:
        super().__init__()
        net = MobileNetV2(width_mult=1.0)
        self.cnn = nn.Sequential(*list(net.children())[0][:19])
        self.rnn = nn.LSTM(1280, 256, 1, batch_first=True, bidirectional=True)
        self.lin = nn.Linear(512, 9)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, steps, channels, height, width = x.size()
        features = self.cnn(x.view(batch * steps, channels, height, width)).mean(3).mean(2)
        output, _ = self.rnn(features.view(batch, steps, -1))
        return self.lin(output).view(batch * steps, 9)


def load_model() -> SwingNet:
    model = SwingNet()
    state = torch.load(HERE / "models" / "swingnet_1800.pth.tar", map_location="cpu", weights_only=False)
    model.load_state_dict(state["model_state_dict"])
    model.eval()
    return model


def prepare(frames: list[np.ndarray], size: int = 160) -> torch.Tensor:
    height, width = frames[0].shape[:2]
    ratio = size / max(height, width)
    new_w, new_h = int(width * ratio), int(height * ratio)
    dw, dh = size - new_w, size - new_h
    top, left = dh // 2, dw // 2
    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    batch = []
    for frame in frames:
        resized = cv2.resize(frame, (new_w, new_h))
        padded = cv2.copyMakeBorder(
            resized, top, dh - top, left, dw - left, cv2.BORDER_CONSTANT,
            value=[0.406 * 255, 0.456 * 255, 0.485 * 255],
        )
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        batch.append(((rgb - mean) / std).transpose(2, 0, 1))
    return torch.from_numpy(np.stack(batch))[None]


def predict(frames: list[np.ndarray], seq_length: int = 64) -> tuple[list[int], list[float], np.ndarray]:
    model = load_model()
    images = prepare(frames)
    probs = []
    with torch.no_grad():
        for start in range(0, images.shape[1], seq_length):
            logits = model(images[:, start : start + seq_length])
            probs.append(F.softmax(logits, dim=1).numpy())
    probs = np.concatenate(probs, 0)
    events = [int(i) for i in np.argmax(probs, axis=0)[:-1]]
    confidence = [float(probs[e, i]) for i, e in enumerate(events)]
    return events, confidence, probs


def read(video: str, start: int, end: int | None, crop: tuple[int, int, int, int] | None) -> list[np.ndarray]:
    capture = cv2.VideoCapture(video)
    frames = []
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index >= start and (end is None or index <= end):
            if crop is not None:
                x0, y0, x1, y1 = crop
                frame = frame[y0:y1, x0:x1]
            frames.append(frame)
        index += 1
    capture.release()
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--crop", default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    crop = tuple(int(v) for v in args.crop.split(",")) if args.crop else None
    frames = read(args.video, args.start, args.end, crop)  # type: ignore[arg-type]
    events, confidence, _ = predict(frames)
    result = {
        name: {"frame": args.start + frame, "confidence": round(conf, 3)}
        for name, frame, conf in zip(EVENTS, events, confidence)
    }
    print(json.dumps(result, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
