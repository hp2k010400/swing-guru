"""Consumer analysis bundle: the data behind the standalone swing viewer.

Turns one analysed swing (pose records, estimated moments and the club track)
into ``analysis.json``: six phases, the checks that can be shown credibly at
each phase, plain-language wording, self-explaining overlay geometry and a
summary. The viewer only draws what this module decides.

Rules this module enforces:

* Six phases only (Address, Takeaway, Mid-Back, Top, Mid-Down, Impact).
* A check is shown only at HIGH or MEDIUM confidence; LOW and unavailable
  checks go to ``hidden`` with a reason and never appear in the walkthrough.
* Values are factual ("3° more upright than Address", "moved 2.1 % of your
  height toward the ball"). There are no verdicts (good/bad, steep/shallow,
  early extension, on plane): Swing Guru has no validated rules for them yet.
* Every check draws what it is measured against: the vertical for torso
  angle, a straight leg for knee bend, the Address position for movement, the
  Address shaft line or the horizontal for the shaft.

Confidence for body checks is the pose model's own lowest score among the
joints used (in both frames for comparisons); for the shaft it is the club
tracker's level. Neither is a statement that the number is right.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .club.geometry import direction_angle_deg, unit_from_angle
from .club.tracker import LEVEL_HIGH, LEVEL_MEDIUM, ClubTrack
from .landmarks import NAME_TO_INDEX
from .phases import PHASE_LABELS, Phase

ANALYSIS_VERSION = 1

ANALYSIS_PHASES: tuple[Phase, ...] = (
    Phase.ADDRESS,
    Phase.TAKEAWAY,
    Phase.MID_BACK,
    Phase.TOP,
    Phase.DOWNSWING,
    Phase.IMPACT,
)

# Which checks are attempted at each phase. Only those that pass the
# confidence gate are shown.
PHASE_CHECKS: Mapping[Phase, tuple[str, ...]] = {
    Phase.ADDRESS: ("torso_angle", "lead_knee", "trail_knee", "arm_hang", "shaft"),
    Phase.TAKEAWAY: ("shaft", "head"),
    Phase.MID_BACK: ("shaft", "posture", "head"),
    Phase.TOP: ("posture", "head", "pelvis", "shaft", "hand_depth"),
    Phase.DOWNSWING: ("shaft", "posture", "pelvis"),
    Phase.IMPACT: ("posture", "head", "pelvis", "shaft"),
}

EXPLAIN: Mapping[str, str] = {
    "torso_angle": "How far your upper body leans forward from upright, measured from your hips to your shoulders.",
    "lead_knee": "How much your front knee is bent, compared with a straight leg.",
    "trail_knee": "How much your back knee is bent, compared with a straight leg.",
    "posture": "Whether your upper body has stood up or bent over compared with your set-up at Address.",
    "head": "How far your head has moved from where it was at Address, as a share of your height.",
    "pelvis": "How far your hips have moved from where they were at Address, as a share of your height.",
    "shaft": "The angle of the club shaft against the horizontal, as this camera sees it.",
    "arm_hang": "How your arms hang from your shoulders at set-up: the line from your near shoulder to your "
                "hands, compared with straight down.",
    "hand_depth": "Where your hands are at the top, side to side in this view: in front of or behind your "
                  "trail shoulder, as a share of your height.",
}

TITLES: Mapping[str, str] = {
    "torso_angle": "Torso angle",
    "lead_knee": "Lead knee bend",
    "trail_knee": "Trail knee bend",
    "posture": "Posture",
    "head": "Head movement",
    "pelvis": "Hip movement",
    "shaft": "Shaft angle",
    "arm_hang": "Arm hang",
    "hand_depth": "Hand position",
}

# Joint-score gates per pose model. The two models' scores live on different
# scales: MediaPipe reports ~0.99 visibility for clearly visible joints, while
# RTMW's SimCC scores for the same, correctly placed joints sit around
# 0.65-0.9 (10th percentile ~0.62 for shoulders and hips on the test swing,
# all visually correct). A single threshold would mislabel one of them.
SCORE_GATES: Mapping[str, tuple[float, float]] = {
    "mediapipe": (0.75, 0.55),
    "rtmw": (0.60, 0.45),
}
HIGH_SCORE, MEDIUM_SCORE = SCORE_GATES["mediapipe"]
MOVEMENT_STILL_PCT = 0.5
POSTURE_STILL_DEG = 1.0


def body_level(score: float | None, pose_model: str = "mediapipe") -> str | None:
    """HIGH / MEDIUM / None (hidden) from the lowest joint score used."""

    if score is None or not math.isfinite(score):
        return None
    high, medium = SCORE_GATES.get(pose_model, SCORE_GATES["mediapipe"])
    if score >= high:
        return LEVEL_HIGH
    if score >= medium:
        return LEVEL_MEDIUM
    return None


# ---------------------------------------------------------------------------
# Small helpers over records
# ---------------------------------------------------------------------------


def _record_at(records: Sequence[object], frame: int | None):
    if frame is None:
        return None
    for record in records:
        if int(getattr(record, "index")) == int(frame):
            return record
    return None


def _px(record, name: str) -> np.ndarray | None:
    points = getattr(record, "points", None)
    if points is None:
        return None
    value = points.pixels.get(name)
    return None if value is None else np.asarray(value, float)


def _score(record, names: Sequence[str]) -> float | None:
    points = getattr(record, "points", None)
    if points is None:
        return None
    values = []
    sources = {
        "hip_mid": ("left_hip", "right_hip"),
        "shoulder_mid": ("left_shoulder", "right_shoulder"),
        "lead_hip": ("left_hip", "right_hip"),
        "trail_hip": ("left_hip", "right_hip"),
        "lead_knee": ("left_knee", "right_knee"),
        "trail_knee": ("left_knee", "right_knee"),
        "lead_ankle": ("left_ankle", "right_ankle"),
        "trail_ankle": ("left_ankle", "right_ankle"),
    }
    expanded: list[str] = []
    for name in names:
        expanded.extend(sources.get(name, (name,)))
    for name in expanded:
        landmark = points.landmarks.get(name)
        if landmark is None:
            return None
        for value in (landmark.visibility, landmark.presence):
            if value is None or not math.isfinite(float(value)):
                return None
            values.append(float(value))
    return min(values) if values else None


def _norm(point: np.ndarray | None, width: int, height: int) -> list[float] | None:
    if point is None:
        return None
    return [round(float(point[0]) / width, 5), round(float(point[1]) / height, 5)]


def _measurement(record, key: str):
    measurements = getattr(record, "measurements", {}) or {}
    result = measurements.get(key)
    if result is None or not result.valid or result.value is None:
        return None
    return float(result.value)


# ---------------------------------------------------------------------------
# Context shared by all checks of one swing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwingContext:
    records: Sequence[object]
    address: object | None
    width: int
    height: int
    torso_px: float
    body_height_px: float | None
    toward_ball: float  # +1 when the ball side is image +x, else -1
    pose_model: str = "mediapipe"
    toward_ball_known: bool = False  # False when Address hands/hips were missing
    # Engine profile (engine_profile.py): ``current`` keeps every field below at its default.
    profile: object = None
    handedness: str = "right"
    handedness_source: str = "default"
    # The Address nose-to-ankle height. Camera checks and corrections keep using it (their
    # tolerances were set against it); ``body_height_px`` is the movement normaliser, which the
    # ``stature`` fix replaces with a posture-independent estimate.
    camera_height_px: float | None = None

    def radius(self, fraction_of_torso: float) -> float:
        return fraction_of_torso * self.torso_px / self.height

    def fix(self, name: str) -> bool:
        return bool(self.profile is not None and self.profile.on(name))


def swing_context(records: Sequence[object], address_frame: int | None, pose_model: str = "mediapipe",
                  profile: object = None) -> SwingContext:
    address = _record_at(records, address_frame)
    first = records[0]
    width = int(getattr(first, "width"))
    height = int(getattr(first, "height"))
    lengths = []
    for record in records:
        hip, shoulder = _px(record, "hip_mid"), _px(record, "shoulder_mid")
        if hip is not None and shoulder is not None:
            lengths.append(float(np.linalg.norm(shoulder - hip)))
    torso = float(np.median(lengths)) if lengths else 0.25 * height
    body = None
    toward = 1.0
    toward_known = False
    if address is not None:
        nose = _px(address, "nose")
        ankles = [p for p in (_px(address, "left_ankle"), _px(address, "right_ankle")) if p is not None]
        if nose is not None and ankles:
            body = float(np.linalg.norm(np.mean(ankles, axis=0) - nose))
        hands = [p for p in (_px(address, "left_wrist"), _px(address, "right_wrist")) if p is not None]
        hip = _px(address, "hip_mid")
        if hands and hip is not None:
            toward = 1.0 if float(np.mean(hands, axis=0)[0] - hip[0]) >= 0 else -1.0
            toward_known = True
    handedness, source, normaliser = "right", "default", body
    if profile is not None and profile.on("handedness"):
        from .engine_profile import infer_handedness

        handedness, source = infer_handedness(toward, toward_known)
    if profile is not None and profile.on("stature") and address is not None:
        from .engine_profile import stature_px

        normaliser = stature_px(address) or body
    return SwingContext(records, address, width, height, torso, normaliser, toward, pose_model, toward_known,
                        profile=profile, handedness=handedness, handedness_source=source, camera_height_px=body)


# ---------------------------------------------------------------------------
# Wording
# ---------------------------------------------------------------------------


def posture_wording(change_deg: float) -> tuple[str, str]:
    """Callout and sentence for a torso-angle change from Address."""

    magnitude = abs(change_deg)
    if magnitude < POSTURE_STILL_DEG:
        return "Same as Address", "Your upper body kept the same angle as at Address."
    direction = "more upright" if change_deg < 0 else "more bent over"
    return (
        f"{magnitude:.0f}° {direction}",
        f"Your upper body is {magnitude:.1f}° {direction} than at Address.",
    )


def movement_direction(delta: np.ndarray, toward_ball: float) -> str:
    """Plain-language direction of a 2-D movement in the camera view."""

    dx, dy = float(delta[0]), float(delta[1])
    size = math.hypot(dx, dy)
    if size == 0:
        return ""
    parts = []
    if abs(dy) >= 0.35 * size:
        parts.append("down" if dy > 0 else "up")
    if abs(dx) >= 0.35 * size:
        parts.append("toward the ball" if dx * toward_ball > 0 else "away from the ball")
    return " and ".join(parts)


def movement_wording(kind: str, percent: float, direction: str) -> tuple[str, str]:
    subject = "Head" if kind == "head" else "Hips"
    if percent < MOVEMENT_STILL_PCT:
        return "Stayed put", f"{subject} stayed within {MOVEMENT_STILL_PCT:.1f}% of your height of the Address position."
    where = f", {direction}" if direction else ""
    return (
        f"{percent:.1f}% of height",
        f"{subject} moved {percent:.1f}% of your height{where}, compared with Address.",
    )


def shaft_line_angle(angle_deg: float) -> float:
    """Angle of the shaft *line* from horizontal, 0-90 degrees."""

    folded = abs(((angle_deg + 90.0) % 180.0) - 90.0)
    return folded


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check(kind: str, phase: Phase, frame: int, level: str, *, callout: str, value: str,
           detail: str, geometry: list[dict], anchor: list[float] | None,
           raw: Mapping[str, float], confidence: float | None, qualifier: str = "",
           summary: str | None = None) -> dict:
    return {
        "id": f"{phase.value}:{kind}",
        "kind": kind,
        "phase": phase.value,
        "frame_index": frame,
        "title": TITLES[kind],
        "value": value,
        "qualifier": qualifier,
        "summary": summary if summary is not None else detail,
        "callout": callout,
        "detail": detail,
        "explain": EXPLAIN[kind],
        "level": level,
        "estimated": level == LEVEL_MEDIUM,
        "confidence": None if confidence is None else round(float(confidence), 3),
        "geometry": geometry,
        "anchor": anchor,
        "raw": {key: round(float(value), 3) for key, value in raw.items()},
    }


def _line(points, role="primary", **extra) -> dict:
    return {"kind": "line" if len(points) == 2 else "polyline", "role": role, "points": points, **extra}


def build_body_check(kind: str, phase: Phase, record, context: SwingContext) -> tuple[dict | None, str]:
    """One body check, or ``(None, reason)`` when it cannot be shown."""

    frame = int(getattr(record, "index"))
    width, height = context.width, context.height
    address = context.address

    if kind == "torso_angle":
        value = _measurement(record, "torso_inclination_deg")
        score = _score(record, ("left_shoulder", "right_shoulder", "left_hip", "right_hip"))
        level = body_level(score, context.pose_model)
        hip, shoulder = _px(record, "hip_mid"), _px(record, "shoulder_mid")
        if value is None or hip is None or shoulder is None:
            return None, "torso angle could not be measured"
        if level is None:
            return None, f"pose confidence {score:.2f} too low"
        length = float(np.linalg.norm(shoulder - hip))
        upright = hip + np.array([0.0, -length])
        torso_dir = direction_angle_deg(shoulder - hip)
        geometry = [
            _line([_norm(hip, width, height), _norm(upright, width, height)], "reference"),
            {"kind": "arc", "role": "accent", "points": [_norm(hip, width, height)],
             "radius": round(0.42 * length / height, 5), "start_deg": 90.0, "end_deg": round(torso_dir, 2)},
            _line([_norm(hip, width, height), _norm(shoulder, width, height)]),
            {"kind": "dot", "role": "primary", "points": [_norm(hip, width, height)]},
            {"kind": "dot", "role": "primary", "points": [_norm(shoulder, width, height)]},
        ]
        return _check(kind, phase, frame, level, callout=f"{value:.0f}°", value=f"{value:.0f}°",
                      detail="Forward lean from upright", geometry=geometry,
                      anchor=_norm((hip + shoulder) / 2, width, height),
                      raw={"torso_inclination_deg": value}, confidence=score), ""

    if kind in ("lead_knee", "trail_knee"):
        side = kind.split("_")[0]
        value = _measurement(record, f"{side}_knee_flex_deg")
        hip, knee, ankle = (_px(record, f"{side}_{part}") for part in ("hip", "knee", "ankle"))
        if context.fix("side_confidence"):
            # Only the measured leg's joints: an occluded far leg must not lower this leg's confidence.
            from .engine_profile import physical_side

            real = physical_side(side, context.handedness)
            score = _score(record, (f"{real}_hip", f"{real}_knee", f"{real}_ankle"))
        else:
            score = _score(record, (f"{side}_hip", f"{side}_knee", f"{side}_ankle"))
        level = body_level(score, context.pose_model)
        if value is None or hip is None or knee is None or ankle is None:
            return None, f"{side} knee could not be measured"
        if level is None:
            return None, f"pose confidence {score:.2f} too low"
        thigh = knee - hip
        straight = knee + thigh / max(np.linalg.norm(thigh), 1e-6) * np.linalg.norm(ankle - knee)
        geometry = [
            _line([_norm(knee, width, height), _norm(straight, width, height)], "reference"),
            _line([_norm(hip, width, height), _norm(knee, width, height), _norm(ankle, width, height)]),
            {"kind": "dot", "role": "primary", "points": [_norm(hip, width, height)]},
            {"kind": "dot", "role": "primary", "points": [_norm(knee, width, height)]},
            {"kind": "dot", "role": "primary", "points": [_norm(ankle, width, height)]},
        ]
        return _check(kind, phase, frame, level, callout=f"{value:.0f}°", value=f"{value:.0f}°",
                      detail="Bend from a straight leg", geometry=geometry,
                      anchor=_norm(knee, width, height), raw={"knee_flex_deg": value},
                      confidence=score), ""

    if kind == "arm_hang":
        # The near (trail) arm: in a down-the-line view it faces the camera, so
        # its wrist is visible even when the lead hand hides behind it.
        if not context.toward_ball_known:
            return None, "which side is the ball could not be told, so the near arm is unknown"
        side = "right" if context.toward_ball > 0 else "left"
        shoulder, wrist = _px(record, f"{side}_shoulder"), _px(record, f"{side}_wrist")
        score = _score(record, (f"{side}_shoulder", f"{side}_wrist"))
        level = body_level(score, context.pose_model)
        if shoulder is None or wrist is None:
            return None, "arm could not be measured"
        if level is None:
            return None, f"pose confidence {score:.2f} too low"
        arm = wrist - shoulder
        if arm[1] <= 0:
            return None, "hands not below the shoulder"
        # + = hands out toward the ball from straight down, - = in toward the body.
        angle = math.degrees(math.atan2(float(arm[0]) * context.toward_ball, float(arm[1])))
        down = shoulder + np.array([0.0, float(np.linalg.norm(arm))])
        geometry = [
            _line([_norm(shoulder, width, height), _norm(down, width, height)], "reference"),
            _line([_norm(shoulder, width, height), _norm(wrist, width, height)]),
            {"kind": "dot", "role": "primary", "points": [_norm(shoulder, width, height)]},
            {"kind": "dot", "role": "primary", "points": [_norm(wrist, width, height)]},
        ]
        direction = "toward the ball" if angle >= 0 else "toward your body"
        return _check(kind, phase, frame, level, callout=f"{abs(angle):.0f}°", value=f"{abs(angle):.0f}°",
                      qualifier=direction, detail=f"Hands {abs(angle):.0f}° {direction} from straight down.",
                      geometry=geometry, anchor=_norm((shoulder + wrist) / 2, width, height),
                      raw={"arm_angle_deg": angle}, confidence=score), ""

    if kind == "hand_depth":
        # Hands vs the trail shoulder, horizontally, in the same frame (so camera
        # drift cancels). + = hands out toward the ball, - = behind the shoulder.
        if not context.toward_ball_known or not context.body_height_px:
            return None, "ball side or body size unknown"
        trail = "right" if context.toward_ball > 0 else "left"
        shoulder = _px(record, f"{trail}_shoulder")
        wrists = [(n, _px(record, n)) for n in ("left_wrist", "right_wrist")]
        wrists = [(n, p) for n, p in wrists if p is not None and body_level(_score(record, (n,)), context.pose_model)]
        score = _score(record, (f"{trail}_shoulder",))
        level = body_level(score, context.pose_model)
        if shoulder is None or not wrists or level is None:
            return None, "hands or trail shoulder not tracked"
        hands = np.mean([p for _, p in wrists], axis=0)
        offset_pct = 100.0 * float(hands[0] - shoulder[0]) * context.toward_ball / context.body_height_px
        scores = [_score(record, (n,)) for n, _ in wrists] + [score]
        confidence = min(s for s in scores if s is not None)
        level = body_level(confidence, context.pose_model) or level
        drop = shoulder + np.array([0.0, 0.35 * context.torso_px])
        top_ref = shoulder - np.array([0.0, 0.9 * context.torso_px])
        geometry = [
            _line([_norm(top_ref, width, height), _norm(drop, width, height)], "reference"),
            _line([_norm(np.array([shoulder[0], hands[1]]), width, height), _norm(hands, width, height)], "accent"),
            {"kind": "dot", "role": "primary", "points": [_norm(hands, width, height)]},
        ]
        direction = "in front of" if offset_pct >= 0 else "behind"
        return _check(kind, phase, frame, level, callout=f"{abs(offset_pct):.1f}%", value=f"{abs(offset_pct):.1f}%",
                      qualifier=f"{direction} trail shoulder",
                      detail=f"Hands {abs(offset_pct):.1f}% of your height {direction} your trail shoulder.",
                      geometry=geometry, anchor=_norm(hands, width, height),
                      raw={"hand_offset_pct_height": offset_pct, "hands_used": len(wrists)}, confidence=confidence), ""

    if address is None:
        return None, "no Address moment to compare with"

    if kind == "posture":
        value = _measurement(record, "torso_angle_change_deg")
        names = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
        scores = [_score(record, names), _score(address, names)]
        score = None if None in scores else min(scores)  # type: ignore[type-var]
        level = body_level(score, context.pose_model)
        hip, shoulder = _px(record, "hip_mid"), _px(record, "shoulder_mid")
        a_hip, a_shoulder = _px(address, "hip_mid"), _px(address, "shoulder_mid")
        if value is None or any(point is None for point in (hip, shoulder, a_hip, a_shoulder)):
            return None, "posture could not be compared with Address"
        if level is None:
            return None, f"pose confidence {score:.2f} too low"
        callout, sentence = posture_wording(value)
        magnitude = abs(value)
        still = magnitude < POSTURE_STILL_DEG
        qualifier = "" if still else ("more upright" if value < 0 else "more bent over")
        geometry = [
            _line([_norm(a_hip, width, height), _norm(a_shoulder, width, height)], "reference"),
            _line([_norm(hip, width, height), _norm(shoulder, width, height)]),
            {"kind": "dot", "role": "primary", "points": [_norm(hip, width, height)]},
            {"kind": "dot", "role": "primary", "points": [_norm(shoulder, width, height)]},
        ]
        return _check(kind, phase, frame, level, callout=callout,
                      value="Same" if still else f"{magnitude:.0f}°", qualifier=qualifier, detail=sentence,
                      summary="Compared with Address", geometry=geometry,
                      anchor=_norm((hip + shoulder) / 2, width, height),
                      raw={"torso_angle_change_deg": value}, confidence=score), ""

    if kind in ("head", "pelvis"):
        name = "nose" if kind == "head" else "hip_mid"
        joints = ("nose",) if kind == "head" else ("left_hip", "right_hip")
        current, reference = _px(record, name), _px(address, name)
        scores = [_score(record, joints), _score(address, joints)]
        score = None if None in scores else min(scores)  # type: ignore[type-var]
        level = body_level(score, context.pose_model)
        if current is None or reference is None or not context.body_height_px:
            return None, f"{TITLES[kind].lower()} could not be compared with Address"
        if level is None:
            return None, f"pose confidence {score:.2f} too low"
        delta = current - reference
        percent = 100.0 * float(np.linalg.norm(delta)) / context.body_height_px
        direction = movement_direction(delta, context.toward_ball) if percent >= MOVEMENT_STILL_PCT else ""
        callout, sentence = movement_wording(kind, percent, direction)
        radius = context.radius(0.24 if kind == "head" else 0.2)
        geometry = [
            {"kind": "ring", "role": "reference", "points": [_norm(reference, width, height)], "radius": round(radius, 5)},
            {"kind": "ring", "role": "primary", "points": [_norm(current, width, height)], "radius": round(radius, 5)},
        ]
        if percent >= MOVEMENT_STILL_PCT:
            geometry.append(_line([_norm(reference, width, height), _norm(current, width, height)], "accent"))
        geometry.append({"kind": "dot", "role": "primary", "points": [_norm(current, width, height)]})
        still = percent < MOVEMENT_STILL_PCT
        return _check(kind, phase, frame, level, callout=callout,
                      value="Still" if still else f"{percent:.1f}%",
                      qualifier="" if still else "of your height",
                      detail=sentence,
                      summary=(direction[0].upper() + direction[1:] + ", from Address") if direction else "From Address",
                      geometry=geometry, anchor=_norm(current, width, height),
                      raw={"movement_pct_height": percent, "dx_px": float(delta[0]), "dy_px": float(delta[1])},
                      confidence=score), ""

    return None, f"unknown check {kind}"


def build_shaft_check(phase: Phase, frame: int, track: ClubTrack, address_frame: int | None,
                      context: SwingContext,
                      overrides: Mapping[int, object] | None = None,
                      reference_shaft: object | None = None) -> tuple[dict | None, str]:
    """``reference_shaft`` (engine profile ``candidate``, address_consensus): the Address reference chosen
    by neighbouring-frame consensus, used instead of the Address frame's own detection. None = as before."""

    shaft = (overrides or {}).get(int(frame)) or track.at(frame)
    if shaft is None or not shaft.available or shaft.interpolated:
        return None, "club not detected in this frame"
    if shaft.level not in (LEVEL_HIGH, LEVEL_MEDIUM):
        return None, f"club detection confidence {shaft.level}"
    width, height = context.width, context.height
    angle = shaft_line_angle(float(shaft.angle_deg))
    grip, tip = np.asarray(shaft.grip, float), np.asarray(shaft.tip, float)
    geometry = []
    detail = "Measured against the horizontal, as the camera sees it."
    raw = {"shaft_direction_deg": float(shaft.angle_deg), "shaft_from_horizontal_deg": angle}
    reference = reference_shaft or (track.at(address_frame) if address_frame is not None else None)
    if phase is Phase.ADDRESS or reference is None or not reference.available or reference.interpolated:
        length = float(np.linalg.norm(tip - grip))
        low = tip if tip[1] >= grip[1] else grip
        geometry.append(_line([_norm(low - [0.55 * length, 0], width, height),
                               _norm(low + [0.55 * length, 0], width, height)], "reference"))
    else:
        a_grip, a_tip = np.asarray(reference.grip, float), np.asarray(reference.tip, float)
        direction = a_tip - a_grip
        geometry.append(_line([_norm(a_grip - 0.9 * direction, width, height),
                               _norm(a_tip + 0.15 * direction, width, height)], "reference"))
        # Where the clubhead sat at Address: the origin of the reference line.
        geometry.append({"kind": "dot", "role": "origin", "points": [_norm(a_tip, width, height)]})
        address_angle = shaft_line_angle(float(reference.angle_deg))
        change = angle - address_angle
        raw["change_vs_address_deg"] = change
        # The dashed line is a reference: where the shaft was at Address. It is
        # not a swing plane, an ideal or a target.
        if abs(change) < 1.0:
            detail = "Same angle as the Address shaft reference."
        else:
            detail = f"{abs(change):.0f}° {'more upright' if change > 0 else 'flatter'} than the Address shaft reference."
    geometry.append(_line([_norm(grip, width, height), _norm(tip, width, height)], "shaft"))
    geometry.append({"kind": "dot", "role": "primary", "points": [_norm(grip, width, height)]})
    check = _check("shaft", phase, frame, shaft.level, callout=f"{angle:.0f}°", value=f"{angle:.0f}°",
                   detail=detail, geometry=geometry, anchor=_norm((grip + tip) / 2, width, height),
                   raw=raw, confidence=shaft.confidence)
    # Developer detail only; the viewer never shows it in the normal flow.
    check["source"] = "owlv2-fallback" if getattr(shaft, "method", "") == "owlv2-fallback" else "classical"
    return check, ""


def attach_tush_line_geometry(analysis: dict, pelvis_depth: Mapping, context: SwingContext) -> None:
    """Overlay on the hip checks: Address tush line, rear point, shift.

    Stored as ``dev_geometry``; the viewer draws it on the hip check itself
    (only present when the rear pelvis was measured, which excludes a moving
    camera) and on every check in developer mode (``?dev=1``).
    """

    line_x = pelvis_depth.get("tush_line_x")
    if line_x is None:
        return
    width, height = context.width, context.height
    by_phase = pelvis_depth.get("phases", {})
    for phase in analysis["phases"]:
        result = by_phase.get(phase["phase"])
        if not result or not result.get("available") or not result.get("rear"):
            continue
        rear = result["rear"]
        rx, ry = rear["point"]
        top, bottom = rear["band"]
        span = bottom - top
        matrix = result.get("camera_matrix")  # the camera moved: draw the Address line where it now appears

        def shown(x, y):
            if matrix is None:
                return [round(x / width, 5), round(y / height, 5)]
            from .camera_correction import to_later

            p = to_later((x, y), matrix)
            return [round(float(p[0]) / width, 5), round(float(p[1]) / height, 5)]

        tush = [shown(line_x, top - 1.5 * span), shown(line_x, bottom + 2.5 * span)]
        if matrix is None:
            line_at_rear = [round(line_x / width, 5), round(ry / height, 5)]
        else:
            from .camera_correction import to_address

            line_at_rear = shown(line_x, float(to_address((rx, ry), matrix)[1]))
        geometry = [
            {"kind": "line", "role": "reference", "points": tush},
            {"kind": "line", "role": "accent", "points": [line_at_rear, [round(rx / width, 5), round(ry / height, 5)]]},
            {"kind": "dot", "role": "primary", "points": [[round(rx / width, 5), round(ry / height, 5)]]},
        ]
        for check in phase["checks"]:
            if check["kind"] == "pelvis":
                check["dev_geometry"] = geometry
                check["pelvis_depth"] = {k: result[k] for k in ("displacement_pct_height", "direction", "level")}


# ---------------------------------------------------------------------------
# Guided walkthrough selection
# ---------------------------------------------------------------------------

# The walkthrough shows a few distinct, visual checks per moment (about 11 in
# total); every check stays on the results screen. Priority order within a
# phase, then a cap. Repeated head/hip/posture readings at every moment added
# length without new information, so each appears where it is most telling:
# set-up geometry at Address, the club through the backswing, posture at Top
# and Impact, and hip movement at Impact.
GUIDED_PRIORITY: Mapping[Phase, tuple[str, ...]] = {
    Phase.ADDRESS: ("torso_angle", "trail_knee", "shaft", "lead_knee"),
    Phase.TAKEAWAY: ("shaft", "head"),
    Phase.MID_BACK: ("shaft", "posture", "head"),
    Phase.TOP: ("shaft", "posture", "head", "pelvis"),
    Phase.DOWNSWING: ("shaft", "posture", "pelvis"),
    Phase.IMPACT: ("posture", "pelvis", "shaft", "head"),
}
GUIDED_CAP: Mapping[Phase, int] = {
    Phase.ADDRESS: 3,
    Phase.TAKEAWAY: 1,
    Phase.MID_BACK: 1,
    Phase.TOP: 2,
    Phase.DOWNSWING: 1,
    Phase.IMPACT: 3,
}


def mark_guided(phase: Phase, checks: list[dict]) -> None:
    """Flag the checks shown during playback and order the phase by priority."""

    priority = GUIDED_PRIORITY[phase]
    checks.sort(key=lambda check: priority.index(check["kind"]) if check["kind"] in priority else 99)
    chosen = 0
    for check in checks:
        check["guided"] = chosen < GUIDED_CAP[phase]
        chosen += 1


# ---------------------------------------------------------------------------
# Whole analysis
# ---------------------------------------------------------------------------


POSITION_KINDS = ("head", "pelvis")  # movement measured as a pixel position change from Address


def _map_geometry(geometry: list[dict], matrix, width: int, height: int) -> list[dict]:
    """Address-coordinate geometry drawn where it appears in the later frame."""

    from .camera_correction import to_later

    out = []
    for g in geometry:
        pts = []
        for p in g.get("points", []):
            q = to_later((p[0] * width, p[1] * height), matrix)
            pts.append([round(float(q[0]) / width, 5), round(float(q[1]) / height, 5)])
        out.append({**g, "points": pts})
    return out


def apply_position_correction(analysis: dict, by_phase: Mapping[str, object], correction: Mapping[str, dict],
                              context: SwingContext) -> None:
    """Rebuild head and hip movement in Address camera coordinates where corrected."""

    from types import SimpleNamespace

    from .camera_correction import CORRECTED_VALID, to_address

    width, height = context.width, context.height
    for phase in analysis["phases"]:
        result = correction.get(phase["phase"]) or {}
        record = by_phase.get(phase["phase"])
        if result.get("status") != CORRECTED_VALID or record is None:
            continue
        matrix = result["matrix"]
        pixels = {name: to_address(p, matrix) for name, p in record.points.pixels.items()}
        shifted = SimpleNamespace(index=record.index, timestamp_ms=getattr(record, "timestamp_ms", 0.0),
                                  points=SimpleNamespace(pixels=pixels, landmarks=record.points.landmarks),
                                  measurements=getattr(record, "measurements", {}))
        for i, check in enumerate(phase["checks"]):
            if check["kind"] not in POSITION_KINDS:
                continue
            fresh, _ = build_body_check(check["kind"], Phase(phase["phase"]), shifted, context)
            if fresh is None:
                continue
            fresh["geometry"] = _map_geometry(fresh["geometry"], matrix, width, height)
            anchor = _map_geometry([{"points": [fresh["anchor"]]}], matrix, width, height)[0]["points"][0]
            fresh["anchor"] = anchor
            fresh["raw"]["uncorrected_movement_pct_height"] = check["raw"].get("movement_pct_height")
            fresh["camera"] = CORRECTED_VALID
            if result.get("accepted_by") == "small_shift":
                # Confirmed registration, foot witness not decisive: counts as an estimate.
                fresh["level"], fresh["estimated"], fresh["camera"] = LEVEL_MEDIUM, True, "CORRECTED_SMALL_SHIFT"
            fresh.update({k: check[k] for k in ("time_s", "guided") if k in check})
            phase["checks"][i] = fresh


def correct_pelvis_depth(pelvis_depth: Mapping, correction: Mapping[str, dict], context: SwingContext, *,
                         camera_moved: bool) -> dict:
    """Rear-pelvis displacement in Address coordinates; withheld where not correctable."""

    from .camera_correction import CORRECTED_VALID, to_address

    out = dict(pelvis_depth)
    line_x = out.get("tush_line_x")
    phases = {}
    for name, result in (out.get("phases") or {}).items():
        result = dict(result)
        status = (correction.get(name) or {}).get("status")
        if camera_moved and result.get("available") and result.get("rear") and line_x is not None:
            if status == CORRECTED_VALID and context.body_height_px:
                matrix = correction[name]["matrix"]
                corrected_x = float(to_address(result["rear"]["point"], matrix)[0])
                toward = context.toward_ball if context.toward_ball_known else 0.0
                px = (corrected_x - line_x) * toward
                result["uncorrected_displacement_pct_height"] = result.get("displacement_pct_height")
                result["displacement_px"] = round(px, 2)
                result["displacement_pct_height"] = round(100.0 * px / context.body_height_px, 3)
                result["direction"] = ("on the Address line" if abs(result["displacement_pct_height"]) < 0.5
                                       else "toward the ball" if px > 0 else "away from the ball")
                result["camera"] = CORRECTED_VALID
                result["camera_matrix"] = matrix
                if (correction.get(name) or {}).get("accepted_by") == "small_shift":
                    result["camera"] = "CORRECTED_SMALL_SHIFT"
                    result["level"] = "MEDIUM"
            else:
                result.update({"available": False, "displacement_pct_height": None, "level": "UNAVAILABLE",
                               "reason": "the camera moved and the correction could not be confirmed"})
        phases[name] = result
    out["phases"] = phases
    return out


def align_plane_references(analysis: dict, records: Sequence[object], frames: Mapping, correction: Mapping[str, dict],
                           reference_motion: Mapping | None, context: SwingContext, *, camera_moved: bool) -> None:
    """Draw the Address shaft reference where it appears in a moved camera's frame.

    Only the drawing moves: the plane angle is measured in each frame as before.
    Mid-Down reuses the position correction; Mid-Back has its own registration
    judged by the same planted-foot test. When the camera moved and the
    placement cannot be confirmed, the reference stays short and the Address
    clubhead marker is dropped (``reference_frame = "unaligned"``).
    """

    from .camera_correction import CORRECTED_VALID, UNCERTAIN, assess

    motion_status = (analysis["camera_check"].get("signals", {}).get("camera_motion") or {}).get("status", UNCERTAIN)
    for phase in analysis["phases"]:
        if phase["phase"] not in ("mid_back", "downswing"):
            continue
        for check in phase["checks"]:
            if check["kind"] != "shaft":
                continue
            if not camera_moved:
                check["reference_frame"] = "address"
                continue
            if phase["phase"] == "downswing":
                result = correction.get("downswing") or {}
            else:
                result = assess(reference_motion, _record_at(records, frames.get(Phase.ADDRESS)),
                                _record_at(records, frames.get(Phase.MID_BACK)), frame_height=context.height,
                                body_height_px=context.body_height_px, motion_status=motion_status)
                analysis["camera_correction"]["mid_back"] = result
            fixed = [g for g in check["geometry"] if g.get("role") in ("reference", "origin")]
            if result.get("status") == CORRECTED_VALID:
                mapped = iter(_map_geometry(fixed, result["matrix"], context.width, context.height))
                check["geometry"] = [next(mapped) if g.get("role") in ("reference", "origin") else g
                                     for g in check["geometry"]]
                check["reference_frame"] = "corrected"
            else:
                check["geometry"] = [g for g in check["geometry"] if g.get("role") != "origin"]
                check["reference_frame"] = "unaligned"


def hide_position_checks(analysis: dict, keep_phases: set[str] | frozenset[str] = frozenset()) -> None:
    """The camera moved: position changes from Address are not the golfer's.

    Angles (torso, knees, posture, shaft) survive a camera shift; head and hip
    movement do not, so those checks move to ``hidden`` with the reason -
    except in ``keep_phases``, where they were corrected for the camera.
    """

    reason = "the camera moved during the swing, so movement from Address cannot be measured"
    for phase in analysis["phases"]:
        kept = []
        for check in phase["checks"]:
            if check["kind"] in POSITION_KINDS and phase["phase"] not in keep_phases:
                analysis["hidden"].append({"phase": phase["phase"], "kind": check["kind"],
                                           "title": TITLES[check["kind"]], "reason": reason})
            else:
                kept.append(check)
        if len(kept) != len(phase["checks"]):
            mark_guided(Phase(phase["phase"]), kept)
        phase["checks"] = kept
    shown = [check for phase in analysis["phases"] for check in phase["checks"]]
    analysis["summary"].update({
        "reliable_checks": len(shown),
        "estimated_checks": sum(1 for check in shown if check["estimated"]),
        "hidden_checks": len(analysis["hidden"]),
        "guided_checks": sum(1 for check in shown if check["guided"]),
    })


def build_analysis(
    records: Sequence[object],
    moments: object,
    track: ClubTrack | None,
    *,
    fps: float,
    video: str,
    source_name: str = "",
    camera_view: str = "down-the-line",
    pose_model: str = "mediapipe",
    shaft_overrides: Mapping[int, object] | None = None,
    pelvis_depth: Mapping | None = None,
    camera_check: Mapping | None = None,
    takeaway_p2: Mapping | None = None,
    reference_motion: Mapping | None = None,
    profile: object = None,
) -> dict:
    """The complete ``analysis.json`` payload.

    ``reference_motion`` is the Address -> Mid-Back background comparison
    (same shape as the camera check's comparisons); it only places the plane
    reference and never feeds the camera check.
    """

    frames = moments.frames()  # type: ignore[attr-defined]
    address_frame = frames.get(Phase.ADDRESS)
    context = swing_context(records, address_frame, pose_model, profile)
    first_index = int(getattr(records[0], "index"))
    # Address shaft reference by neighbouring-frame consensus (candidate profile only). Only an outlier
    # or missing Address detection changes anything; otherwise the plane checks run exactly as before.
    address_ref = None
    if context.fix("address_consensus") and track is not None and address_frame is not None:
        from .club.address_reference import address_reference

        address_ref = address_reference(track, int(address_frame), records, context.torso_px, shaft_overrides)
    phases_out = []
    shown: list[dict] = []
    hidden: list[dict] = []
    plane_neighbour: dict = {}
    for phase in ANALYSIS_PHASES:
        frame = frames.get(phase)
        estimate = moments.get(phase)  # type: ignore[attr-defined]
        record = _record_at(records, frame)
        phase_checks = []
        for kind in PHASE_CHECKS[phase]:
            if record is None:
                hidden.append({"phase": phase.value, "kind": kind, "reason": "moment not found"})
                continue
            if kind == "shaft":
                if track is None:
                    check, reason = None, "club tracking not run"
                elif address_ref is not None and address_ref.changed:
                    overrides = dict(shaft_overrides or {})
                    if phase is Phase.ADDRESS:
                        overrides[int(frame)] = address_ref.shaft
                    check, reason = build_shaft_check(phase, int(frame), track, address_frame, context,
                                                      overrides, reference_shaft=address_ref.shaft)
                    if check is not None:
                        check["address_reference"] = {"status": address_ref.status,
                                                      "frame": int(address_ref.shaft.frame_index)}
                else:
                    check, reason = build_shaft_check(phase, int(frame), track, address_frame, context,
                                                      shaft_overrides)
                if (check is None and track is not None and phase in (Phase.MID_BACK, Phase.DOWNSWING)
                        and context.fix("plane_neighbour")):
                    # Nearest clear frame (+-1) when the moment frame has no usable club (candidate only).
                    from .club.plane_neighbour import nearest_clear_frame

                    near, near_diag = nearest_clear_frame(track, int(frame), phase.value, records,
                                                          context.torso_px, shaft_overrides)
                    plane_neighbour[phase.value] = near_diag
                    if near is not None:
                        reference = address_ref.shaft if address_ref is not None and address_ref.changed else None
                        check, near_reason = build_shaft_check(phase, int(near.frame_index), track, address_frame,
                                                               context, {int(near.frame_index): near},
                                                               reference_shaft=reference)
                        if check is not None:
                            check["level"] = LEVEL_MEDIUM            # measured beside the moment: estimated
                            check["estimated"] = True
                            check["measured_frame"] = {"frame": int(near.frame_index), "moment_frame": int(frame),
                                                       "offset": int(near_diag["offset"]),
                                                       "reason": f"moment frame club: {reason}"}
                        else:
                            near_diag["result"] = f"recovered frame refused: {near_reason}"
            else:
                check, reason = build_body_check(kind, phase, record, context)
            if check is None:
                hidden.append({"phase": phase.value, "kind": kind, "title": TITLES[kind], "reason": reason})
                continue
            check["time_s"] = round((int(check.get("frame_index", frame)) - first_index) / fps, 4)
            phase_checks.append(check)
        mark_guided(phase, phase_checks)
        shown.extend(phase_checks)
        phases_out.append({
            "phase": phase.value,
            "label": PHASE_LABELS[phase],
            "frame_index": frame,
            "time_s": None if frame is None else round((int(frame) - first_index) / fps, 4),
            "source": getattr(estimate, "source", "unavailable"),
            "checks": phase_checks,
        })
    times = [phase["time_s"] for phase in phases_out if phase["time_s"] is not None]
    duration = (int(getattr(records[-1], "index")) - first_index + 1) / fps
    analysis = {
        "version": ANALYSIS_VERSION,
        "source": source_name,
        "camera_view": camera_view,
        "video": video,
        "fps": fps,
        "width": context.width,
        "height": context.height,
        "duration_s": round(duration, 4),
        "segment_start_s": round(max(0.0, min(times) - 0.6), 4) if times else 0.0,
        "segment_end_s": round(min(duration, max(times) + 0.9), 4) if times else duration,
        "phases": phases_out,
        "summary": {
            "reliable_checks": len(shown),
            "phases": sum(1 for phase in phases_out if phase["frame_index"] is not None),
            "estimated_checks": sum(1 for check in shown if check["estimated"]),
            "hidden_checks": len(hidden),
            "guided_checks": sum(1 for check in shown if check["guided"]),
        },
        "hidden": hidden,
        "geometry_context": {
            "toward_ball": context.toward_ball if context.toward_ball_known else None,
            "toward_ball_known": context.toward_ball_known,
            "body_height_px": None if context.body_height_px is None else round(context.body_height_px, 2),
        },
    }
    # Shaft-parallel (P2) is recorded as a diagnostic only: even with temporal
    # club recovery it is found on 4 of 12 robustness swings (all correct), far
    # below what a golfer-facing checkpoint needs (see docs/agent-work/club-temporal/).
    analysis["takeaway_p2"] = dict(takeaway_p2) if takeaway_p2 else {"detected": False, "reason": "not run"}
    analysis["takeaway_p2"]["status"] = "diagnostic_only"
    analysis["camera_check"] = dict(camera_check) if camera_check else {"status": "UNCERTAIN", "signals": {}, "note": "not run"}
    # Camera drift: correct Address-relative positions where the correction is
    # confirmed; everything else stays withheld (fails closed).
    from .camera_correction import CORRECTED_VALID, CorrectionConfig, correction_for

    by_phase = {name: _record_at(records, frames.get(Phase(name))) for name in ("address", "top", "downswing", "impact")}
    correction_config = None
    if context.fix("small_shift"):
        correction_config = CorrectionConfig(small_shift_pct=profile.small_shift_pct,  # type: ignore[union-attr]
                                             foot_parts=("ankle", "heel", "foot_index"))
    correction = correction_for(analysis["camera_check"], by_phase, frame_height=context.height,
                                body_height_px=context.camera_height_px if profile is not None else context.body_height_px,
                                config=correction_config)
    analysis["camera_correction"] = correction
    moved = (analysis["camera_check"].get("signals", {}).get("camera_motion") or {}).get("status") == "INVALID"
    corrected = {p for p, r in correction.items() if r["status"] == CORRECTED_VALID}
    if moved:
        apply_position_correction(analysis, by_phase, correction, context)
        hide_position_checks(analysis, keep_phases=corrected)
    if profile is not None:
        from .engine_profile import reason_code

        analysis["engine_profile"] = profile.to_dict()  # type: ignore[union-attr]
        if address_ref is not None:
            analysis["address_reference"] = {"status": address_ref.status, **address_ref.diagnostics}
        if context.fix("plane_neighbour"):
            analysis["plane_neighbour"] = plane_neighbour
        analysis["handedness"] = {"value": context.handedness, "source": context.handedness_source}
        analysis["geometry_context"]["camera_height_px"] = (None if context.camera_height_px is None
                                                            else round(context.camera_height_px, 2))
        for item in analysis["hidden"]:
            item["code"] = reason_code(item.get("reason", ""))
    align_plane_references(analysis, records, frames, correction, reference_motion, context, camera_moved=moved)
    if pelvis_depth:
        pelvis_depth = correct_pelvis_depth(pelvis_depth, correction, context, camera_moved=moved)
        analysis["pelvis_depth"] = dict(pelvis_depth)
        attach_tush_line_geometry(analysis, pelvis_depth, context)
    # Interpretations come from the separate, deterministic rule layer. The
    # main focus may only come from a validated rule; none is validated yet.
    from .interpretation import interpret

    interpretation = interpret(analysis)
    analysis["interpretations"] = interpretation
    analysis["focus"] = interpretation["focus"]
    # Golfer-facing labels: a separate, provisional presentation layer over the
    # checks above (never read by the interpretation layer or the focus).
    from .verdicts import attach_verdicts

    attach_verdicts(analysis)
    if context.fix("head_direction"):
        from .head_direction import describe_head_direction

        describe_head_direction(analysis, context.toward_ball if context.toward_ball_known else None)
    if context.fix("observations"):
        from .observations import mark_observations

        def stability(phase_name: str, kind: str):
            """Half-range of the reading over the moment frame +-1 (the engine's own measurement)."""

            frame = frames.get(Phase(phase_name))
            key = {"posture": "torso_angle_change_deg", "head": "movement_pct_height"}.get(kind)
            if frame is None or key is None:
                return None
            values = []
            for f in (int(frame) - 1, int(frame), int(frame) + 1):
                record = _record_at(records, f)
                check = build_body_check(kind, Phase(phase_name), record, context)[0] if record is not None else None
                if check is None or key not in check["raw"]:
                    return None
                values.append(float(check["raw"][key]))
            return (max(values) - min(values)) / 2.0

        mark_observations(analysis, stability)
    return analysis


__all__ = [
    "ANALYSIS_PHASES",
    "PHASE_CHECKS",
    "body_level",
    "build_analysis",
    "build_body_check",
    "build_shaft_check",
    "mark_guided",
    "movement_direction",
    "movement_wording",
    "posture_wording",
    "shaft_line_angle",
    "swing_context",
]
