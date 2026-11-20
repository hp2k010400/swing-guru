"""Drill catalogue for the Swing Guru app, mapped to verdict checkpoints.

General, widely taught practice drills written in plain words. Every drill and
every checkpoint -> drill mapping is ``draft_needs_coach_review``: nothing here
has been reviewed by a golf professional, and the app must not imply otherwise.
There are no drill videos yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

DRAFT = "draft_needs_coach_review"

GROUPS = (
    ("setup", "Set-up"),
    ("backswing", "Backswing"),
    ("downswing", "Downswing & impact"),
)


@dataclass(frozen=True)
class Drill:
    id: str
    name: str
    group: str
    checkpoint: str        # checkpoint name shown with it ("Hip hinge")
    phase_label: str       # "Address"
    minutes: int
    icon: str
    why: str
    steps: tuple[str, ...]
    reps: str
    status: str = DRAFT
    video: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


DRILLS: tuple[Drill, ...] = (
    Drill("hip-hinge-setup", "Hip hinge set-up", "setup", "Hip hinge", "Address", 3, "profile",
          "A good tilt from the hips gives your arms room to swing and keeps your posture stable.",
          ("Hold a club flat against your back: head, upper back and tailbone touching it.",
           "Push your hips back and tilt forward until the club still touches all three points.",
           "Let your knees soften, then let your arms hang and take your grip."),
          "5 slow set-ups, then hit 5 balls from the same posture"),
    Drill("athletic-knee-flex", "Athletic knee flex", "setup", "Knee bend", "Address", 2, "swing",
          "Soft, balanced knees let you turn without swaying or standing up.",
          ("Stand tall, feet shoulder-width apart.",
           "Tilt from the hips first, then soften your knees just enough to feel balanced over the balls of your feet.",
           "Check you could hop gently on the spot without adjusting."),
          "5 set-ups before each practice block"),
    Drill("natural-arm-hang", "Natural arm hang", "setup", "Arm hang", "Address", 2, "hand",
          "Letting your arms hang from your shoulders stops you reaching for the ball or crowding it.",
          ("Take your posture without a club and let your arms hang completely loose.",
           "Notice where your hands fall; that is where the grip should be.",
           "Take your grip there and set the club behind the ball; move your feet, not your hands."),
          "3 rehearsals, then 5 balls"),
    Drill("halfway-back-check", "Halfway-back check", "backswing", "Plane", "Mid-Back", 4, "target",
          "At halfway back, the shaft working along your set-up shaft line keeps the swing on a repeatable path.",
          ("Set up with an alignment stick or club on the ground along your target line.",
           "Swing slowly to halfway back (lead arm parallel to the ground) and stop.",
           "Check in a mirror or on video that the shaft points along the line it sat on at address.",
           "Repeat slowly until it feels natural, then add speed gradually."),
          "10 slow checks, then 5 half swings"),
    Drill("posture-hold", "Hold your posture to the top", "backswing", "Posture", "Top", 4, "wall",
          "Keeping your set-up tilt to the top of the backswing makes the downswing far easier to repeat.",
          ("Set up with a chair just touching your rear.",
           "Make slow backswings, keeping your rear lightly in contact with the chair.",
           "Feel your chest turn over your trail hip rather than standing up."),
          "10 slow backswings, then 5 balls"),
    Drill("pump-drill", "Pump drill", "downswing", "Plane", "Mid-Down", 5, "replay",
          "Rehearsing the start of the downswing trains the club to return along a repeatable line.",
          ("Swing to the top and stop.",
           "Start down slowly, letting your trail elbow drop toward your side, until your hands reach hip height.",
           "Return to the top. Do this twice.",
           "On the third pump, swing through and hit the ball."),
          "3 sets of 5 balls"),
    Drill("butt-to-wall", "Butt-to-wall drill", "downswing", "Extension", "Impact", 5, "wall",
          "Keeping your hips back through impact helps you stay in your posture instead of standing up early.",
          ("Set up with your rear just touching a wall behind you (no club needed at first).",
           "Swing back slowly: the trail side of your rear stays touching the wall.",
           "Swing through slowly: the lead side of your rear moves back to touch the wall as you reach impact.",
           "Step away from the wall and make the same feel with a real swing."),
          "10 slow swings, then 5 balls"),
    Drill("quiet-head", "Quiet head drill", "downswing", "Head", "Impact", 3, "profile",
          "A steady head keeps your low point and strike more consistent.",
          ("Face the sun so your shadow falls in front of you, or film yourself.",
           "Place a ball or tee at your head's shadow at address.",
           "Make half swings, keeping your head's shadow near the marker until after impact."),
          "10 half swings, then 5 balls"),
)

# Verdict mapping id -> drills that train it (in order of suggestion).
MAPPINGS: dict[str, dict] = {
    "address.hip_hinge": {"drills": ("hip-hinge-setup",), "status": DRAFT},
    "address.knee_bend": {"drills": ("athletic-knee-flex",), "status": DRAFT},
    "address.arm_hang": {"drills": ("natural-arm-hang",), "status": DRAFT},
    "swing.plane_vs_address_shaft@mid_back": {"drills": ("halfway-back-check",), "status": DRAFT},
    "swing.plane_vs_address_shaft@downswing": {"drills": ("pump-drill",), "status": DRAFT},
    "top.posture": {"drills": ("posture-hold",), "status": DRAFT},
    "impact.early_extension": {"drills": ("butt-to-wall",), "status": DRAFT},
    "impact.head": {"drills": ("quiet-head",), "status": DRAFT},
}

_BY_ID = {d.id: d for d in DRILLS}


def mapping_key(mapping: str | None, phase: str | None) -> str | None:
    """The mapping id, qualified by phase where one mapping serves two phases (plane)."""

    if mapping is None:
        return None
    key = f"{mapping}@{phase}"
    return key if key in MAPPINGS else mapping


def drills_for(mapping: str | None, phase: str | None = None) -> list[dict]:
    entry = MAPPINGS.get(mapping_key(mapping, phase) or "")
    if not entry:
        return []
    return [{**_BY_ID[d].to_dict(), "mapping_status": entry["status"]} for d in entry["drills"]]


def targets(drill_id: str) -> list[dict]:
    """The checkpoints a drill is mapped to, as the Summary names them (id ``<phase>.<kind>``)."""

    from .verdicts import CHECKPOINTS

    out = []
    for cp in CHECKPOINTS:
        key = mapping_key(cp.mapping.mapping_id, cp.phase)
        if drill_id in MAPPINGS.get(key or "", {}).get("drills", ()):
            out.append({"id": f"{cp.phase}.{cp.mapping.kind}", "name": cp.name, "phase": cp.phase,
                        "phase_label": {"address": "Address", "mid_back": "Mid-Back", "top": "Top",
                                        "downswing": "Mid-Down", "impact": "Impact"}[cp.phase]})
    return out


def catalogue() -> dict:
    # `video` stays None until an original, approved demonstration exists (never a placeholder): builds up to
    # 1.0.1 (20) play it natively as an MP4. Externally hosted demonstrations (official embeds, from the
    # separate drill_videos catalogue) are `demo_video`, which only newer builds read.
    # `status` stays draft_needs_coach_review until a professional has reviewed that drill.
    from .drill_videos import active_by_drill

    videos = active_by_drill()
    return {
        "status": DRAFT,
        "groups": [{"id": gid, "label": label,
                    "drills": [{**d.to_dict(), "targets": targets(d.id), "demo_video": videos.get(d.id)}
                               for d in DRILLS if d.group == gid]}
                   for gid, label in GROUPS],
        "mappings": {k: {"drills": list(v["drills"]), "status": v["status"]} for k, v in MAPPINGS.items()},
    }


def drill(drill_id: str) -> dict | None:
    item = _BY_ID.get(drill_id)
    return None if item is None else item.to_dict()


__all__ = ["DRAFT", "DRILLS", "GROUPS", "MAPPINGS", "catalogue", "drill", "drills_for", "mapping_key"]
