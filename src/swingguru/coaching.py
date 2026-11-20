"""Plain-language coaching copy for each shown checkpoint: what we saw, why it matters, what to try.

A presentation layer over the stored verdicts, like :mod:`swingguru.verdicts`. It never measures,
never changes a band, a verdict or the Swing Score: it reads ``verdict.mapping.input`` (the number
the verdict was given) and the band edges stored with it, and picks wording by which side of the
"good" band the reading fell on. Old swings get the same wording, since it is built when the
Summary is served, from the stored ``analysis.json``.

The copy is general, widely taught golf advice written in everyday words. It has not been
reviewed by a golf professional (``status``), and it avoids every claim the engine cannot back
(``verdicts.UNSUPPORTED_CLAIMS``; tested).
"""

from __future__ import annotations

from typing import Mapping

DRAFT = "draft_needs_coach_review"
COACHING_VERSION = 2

GOOD, LOW, HIGH = "good", "low", "high"

# (mapping key, side) -> headline, what (format fields: v = size of the reading, lo / hi = the good
# band), why, fix. Plane is keyed per phase, like the drill mappings.
COPY: dict[tuple[str, str], tuple[str, str, str, str | None]] = {
    ("address.hip_hinge", GOOD): (
        "Athletic tilt from the hips",
        "You tilted forward about {v}° from your hips, inside the {lo}–{hi}° range we look for.",
        "A good hip tilt gives your arms room to swing and makes it easier to turn and stay in posture.",
        None),
    ("address.hip_hinge", LOW): (
        "Standing a little tall",
        "You tilted forward about {v}° from your hips. We look for {lo}–{hi}°.",
        "Standing tall leaves your arms less room to swing and makes a full, balanced turn harder.",
        "Push your hips back with a straight back until your chest points down at the ball, then let your arms hang."),
    ("address.hip_hinge", HIGH): (
        "Bent over too far",
        "You tilted forward about {v}° from your hips. We look for {lo}–{hi}°.",
        "Bending too far over makes it hard to turn your shoulders, and golfers often stand up during the "
        "swing to make room.",
        "Stand a little taller: keep your weight in the middle of your feet and lift your chest slightly."),

    ("address.knee_bend", GOOD): (
        "Soft, athletic knees",
        "Your trail knee was bent about {v}°, inside the {lo}–{hi}° range we look for.",
        "Soft knees keep you balanced and let your hips turn freely.",
        None),
    ("address.knee_bend", LOW): (
        "Legs nearly straight",
        "Your trail knee was bent only about {v}°. We look for {lo}–{hi}°.",
        "Locked legs make it hard to turn and stay balanced through the swing.",
        "Tilt from your hips first, then soften both knees just enough to feel springy over the balls of your feet."),
    ("address.knee_bend", HIGH): (
        "Sitting down at set-up",
        "Your trail knee was bent about {v}°. We look for {lo}–{hi}°.",
        "Too much knee bend restricts your hip turn and often makes you rise up during the swing.",
        "Straighten your legs a little and get your tilt from the hips instead of the knees."),

    ("address.arm_hang", GOOD): (
        "Arms hanging naturally",
        "Your arms hung within about {v}° of straight down from your shoulders.",
        "Natural arm hang gives the club room to swing without reaching or crowding.",
        None),
    ("address.arm_hang", LOW): (
        "Hands a little crowded",
        "Your hands sat about {v}° in from a natural hang, closer to your body.",
        "Crowded hands leave your arms no room to swing down, so you have to make room another way, often "
        "by standing up.",
        "Let your arms hang loose from your shoulders and grip the club where your hands fall. Move your feet, not "
        "your hands."),
    ("address.arm_hang", HIGH): (
        "Reaching for the ball",
        "Your hands sat about {v}° out from a natural hang, away from your body.",
        "Reaching pulls your weight onto your toes and lets your arms take over the swing.",
        "Let your arms hang loose from your shoulders and grip the club where your hands fall. Move your feet, not "
        "your hands."),

    ("swing.plane_vs_address_shaft@mid_back", GOOD): (
        "On plane halfway back",
        "Halfway back, your club matched the line it sat on at set-up (within about {v}°).",
        "Starting the club back on its set-up line makes the rest of the swing much easier to repeat.",
        None),
    ("swing.plane_vs_address_shaft@mid_back", HIGH): (
        "Steep halfway back",
        "Halfway back, your club was about {v}° steeper than the line it sat on at set-up.",
        "A club that goes back steep usually has to be rerouted coming down, which makes your strike less "
        "consistent.",
        "Swing slowly to halfway back and check the club points along your set-up shaft line before adding speed."),
    ("swing.plane_vs_address_shaft@mid_back", LOW): (
        "Flat halfway back",
        "Halfway back, your club was about {v}° flatter than the line it sat on at set-up.",
        "A club that gets too flat early tends to get stuck behind you, so the strike becomes harder to control.",
        "Swing slowly to halfway back and check the club points along your set-up shaft line before adding speed."),

    ("swing.plane_vs_address_shaft@downswing", GOOD): (
        "On plane coming down",
        "Coming down, your club matched the line it sat on at set-up (within about {v}°).",
        "Returning the club on its set-up line is what makes solid, repeatable contact possible.",
        None),
    ("swing.plane_vs_address_shaft@downswing", HIGH): (
        "Steep coming down",
        "Coming down, your club was about {v}° steeper than the line it sat on at set-up.",
        "A steep club in the downswing tends to give glancing contact, pulls or slices, and deep divots.",
        "From the top, let your trail elbow drop towards your side as you start down, rehearsing it slowly first."),
    ("swing.plane_vs_address_shaft@downswing", LOW): (
        "Flat coming down",
        "Coming down, your club was about {v}° flatter than the line it sat on at set-up.",
        "A club that drops too flat coming down can lead to pushes, hooks or heavy contact.",
        "Rehearse the start of the downswing slowly, feeling the club work down along your set-up shaft line."),

    ("top.posture", GOOD): (
        "Held your posture",
        "At the top you kept your set-up tilt (within about {v}°).",
        "Keeping your posture keeps your distance to the ball the same, so you don't have to correct on the way "
        "down.",
        None),
    ("top.posture", LOW): (
        "Stood up in the backswing",
        "By the top you'd stood up about {v}° from your set-up tilt.",
        "Standing up changes your distance to the ball, so you have to drop back down to find it again, which "
        "is hard to time.",
        "Feel your chest keep pointing down at the ball as you turn, with your rear staying back."),
    ("top.posture", HIGH): (
        "Bent further over at the top",
        "By the top you'd bent about {v}° further over than at set-up.",
        "Dipping in the backswing changes your distance to the ball and usually needs a lift on the way down.",
        "Keep your head height the same as you turn, as if your back were against a wall."),

    ("impact.early_extension", GOOD): (
        "Hips stayed back",
        "Your hips stayed back on their set-up line through impact (within {v}% of your height).",
        "Keeping your hips back gives your arms room to swing through, which helps a solid, centred strike.",
        None),
    ("impact.early_extension", HIGH): (
        "Hips moved toward the ball",
        "Through impact your hips moved toward the ball by about {v}% of your height. We look for under {hi}%.",
        "When the hips move toward the ball, your arms lose room, which often leads to thin, heel or blocked "
        "shots.",
        "Feel your rear stay back on its set-up line as you turn through, as if a wall were behind you."),

    ("impact.head", GOOD): (
        "Steady head",
        "Your head stayed close to its set-up position (it moved about {v}% of your height).",
        "A steady head keeps the bottom of your swing in the same place, so clean contact is easier.",
        None),
    ("impact.head", HIGH): (
        "Head moved a lot",
        "By impact your head had moved about {v}% of your height from set-up. We look for under {lo_bad}%.",
        "A moving head shifts the bottom of your swing, which makes clean contact much harder.",
        "Keep your eyes on the back of the ball until after impact. Let your head turn, but not slide or dip."),
}


# Headlines that depend on how far out the reading was (verdict status), where the side's headline
# above would overstate a slight reading or understate a clear one.
SEVERITY_HEADLINES: dict[tuple[str, str, str], str] = {
    ("address.hip_hinge", HIGH, "attention"): "A touch bent over",
    ("address.hip_hinge", LOW, "problem"): "Standing too tall",
    ("address.knee_bend", HIGH, "attention"): "A little too much knee bend",
    ("address.arm_hang", LOW, "problem"): "Hands crowded",
    ("address.arm_hang", HIGH, "attention"): "Reaching slightly",
    ("swing.plane_vs_address_shaft@mid_back", HIGH, "attention"): "Slightly steep halfway back",
    ("swing.plane_vs_address_shaft@mid_back", LOW, "attention"): "Slightly flat halfway back",
    ("swing.plane_vs_address_shaft@downswing", HIGH, "attention"): "Slightly steep coming down",
    ("swing.plane_vs_address_shaft@downswing", LOW, "attention"): "Slightly flat coming down",
    ("top.posture", LOW, "attention"): "Rose a little in the backswing",
    ("top.posture", HIGH, "attention"): "Dipped a little at the top",
    ("impact.early_extension", HIGH, "attention"): "Hips moved slightly toward the ball",
    ("impact.head", HIGH, "attention"): "Some head movement",
}

# How much a finding matters, so a slight reading never reads like a fault (coaching v2).
SEVERITY_NOTES = {
    "attention": "A small difference: worth keeping an eye on, not a major fault.",
    "problem": "A clear difference, and worth working on.",
}
# Checks whose bands flag most tour-level swings in our validation set (head 3/21 in range, Mid-Back plane
# 2/14, arm hang 6/21; docs/agent-work/analysis-v2/ANALYSIS_AUDIT.md). Until the bands are re-validated,
# their wording says what is normal, and a slight reading gets no practice push.
LOW_TRUST = {"impact.head", "swing.plane_vs_address_shaft@mid_back", "address.arm_hang"}
LOW_TRUST_NOTES = {
    "impact.head": {
        "attention": "Some head movement is normal, even for very good players. This is a small amount.",
        "problem": "Some head movement is normal, but this is more than we'd like to see.",
    },
    "swing.plane_vs_address_shaft@mid_back": {
        "attention": "Good players vary a lot here, so treat a small difference as a guide rather than a fault.",
        "problem": "Good players vary here, but this is a clear difference from your set-up line.",
    },
    "address.arm_hang": {
        "attention": "Hand position at set-up varies between good players and with club length. This is a small difference.",
        "problem": "Hand position varies with club length, but this is a clear difference from a natural hang.",
    },
}

# The good extension band is open below, so a reading there can mean "moved away from the ball".
EXTENSION_AWAY = "Your hips stayed back through impact: they moved {v}% of your height away from the ball, not toward it."


def _key(mapping: str, phase: str | None) -> str:
    keyed = f"{mapping}@{phase}"
    return keyed if (keyed, GOOD) in COPY else mapping


def _side(value: float, bands: list[Mapping]) -> tuple[str, Mapping | None]:
    good = next((b for b in bands if b.get("status") == "good"), None)
    if good is None:
        return GOOD, None
    if good.get("from") is not None and value < good["from"]:
        return LOW, good
    if good.get("to") is not None and value >= good["to"]:
        return HIGH, good
    return GOOD, good


def _num(value: float, unit: str) -> str:
    return f"{abs(value):.0f}" if unit == "deg" else f"{abs(value):.1f}"


def coaching_for(check: Mapping, phase: str) -> dict | None:
    """The coaching card for one shown checkpoint (its stored verdict), or None without copy."""

    verdict = check.get("verdict") or {}
    mapping = verdict.get("mapping") or {}
    value = mapping.get("input")
    if value is None or not mapping.get("id"):
        return None
    side, good = _side(float(value), mapping.get("bands") or [])
    entry = COPY.get((_key(mapping["id"], phase), side))
    if entry is None:
        return None
    headline, what, why, fix = entry
    key = _key(mapping["id"], phase)
    headline = SEVERITY_HEADLINES.get((key, side, verdict.get("status", "")), headline)
    if key == "impact.early_extension" and float(value) < 0:
        what = EXTENSION_AWAY
    unit = mapping.get("unit", "")
    bad = next((b for b in mapping.get("bands") or [] if b.get("status") != "good" and b.get("from") is not None), None)
    fields = {"v": _num(float(value), unit),
              "lo": f"{good['from']:g}" if good and good.get("from") is not None else "",
              "hi": f"{good['to']:g}" if good and good.get("to") is not None else "",
              "lo_bad": f"{bad['from']:g}" if bad else ""}
    text = what.format(**fields)
    # The candidate engine's direction-aware head sentence says more than a distance; use it.
    if mapping["id"] == "impact.head" and check.get("head_direction") and verdict.get("support"):
        text = verdict["support"]
    status = verdict.get("status", "")
    note = (LOW_TRUST_NOTES.get(key) or SEVERITY_NOTES).get(status)
    return {"headline": headline, "what": text, "why": why, "fix": fix, "side": side, "note": note,
            "low_trust": key in LOW_TRUST, "version": COACHING_VERSION, "status": DRAFT}


def practise_this(check_id_key: str, status: str) -> bool:
    """Whether a finding should push a drill: clear findings always; slight ones only on trusted checks."""

    if status == "problem":
        return True
    return status == "attention" and check_id_key not in LOW_TRUST


__all__ = ["COACHING_VERSION", "COPY", "DRAFT", "LOW_TRUST", "SEVERITY_NOTES", "coaching_for", "practise_this"]
