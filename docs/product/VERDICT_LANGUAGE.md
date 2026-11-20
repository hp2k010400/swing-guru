# Swing Guru verdict language (locked, v2)

Generated from `src/swingguru/verdicts.py` by `scripts/verdict_matrix.py` - edit the code, then regenerate.
A test fails if this file and the code disagree.

## Rules

* **Short, coach-like, readable in under a second.** One verdict per check; the same kind of measurement
  always uses the same pattern: `<thing> good` / `Slightly <x>` / `Too <x>` (or the golf term: `Reaching`,
  `Loss of posture`, `Early extension`).
* **Colour has one meaning everywhere** (status dot, overlay line, active progress dot):
  green = within the target band; amber = slight deviation; coral = clear deviation; neutral = information.
  Colour never floods the screen: the dark verdict pill stays the calm, dominant element.
* **The verdict is the strongest text.** The measured value is secondary (`43° · Estimated`).
* **Nothing is shown that the engine cannot support.** Unsupported vocabulary is agreed below but never shown.
* **Validation status** is recorded on every verdict (`check.verdict.mapping.validation`):
  `validated_rule` (none yet), `provisional_ui_mapping` (all current verdicts: a stated rule, not yet
  calibrated against coach or 3D labels), `unsupported` (not shown).
* Each shown verdict also records `verdict_id`, `phase`, `sources`, `band`, `confidence`, `basis` and
  `approximation`; `?dev=1` in the player displays them.

## Matrix

| Phase | Check | Verdict | Colour | Source measurement | Band | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Address | Hip hinge | Too upright | coral | forward tilt of the hip-to-shoulder line from vertical | < 20° | provisional_ui_mapping |
| Address | Hip hinge | Slightly upright | amber | forward tilt of the hip-to-shoulder line from vertical | 20 to 30° | provisional_ui_mapping |
| Address | Hip hinge | Hip hinge good | green | forward tilt of the hip-to-shoulder line from vertical | 30 to 50° | provisional_ui_mapping |
| Address | Hip hinge | Slightly bent over | amber | forward tilt of the hip-to-shoulder line from vertical | 50 to 58° | provisional_ui_mapping |
| Address | Hip hinge | Too bent over | coral | forward tilt of the hip-to-shoulder line from vertical | >= 58° | provisional_ui_mapping |
| Address | Knee bend | Too straight | coral | trail knee bend from a straight leg | < 5° | provisional_ui_mapping |
| Address | Knee bend | Slightly straight | amber | trail knee bend from a straight leg | 5 to 10° | provisional_ui_mapping |
| Address | Knee bend | Knee bend good | green | trail knee bend from a straight leg | 10 to 30° | provisional_ui_mapping |
| Address | Knee bend | Slightly flexed | amber | trail knee bend from a straight leg | 30 to 40° | provisional_ui_mapping |
| Address | Knee bend | Too much knee bend | coral | trail knee bend from a straight leg | >= 40° | provisional_ui_mapping |
| Address | Arm hang | Too crowded | coral | near-arm line from shoulder to hands vs straight down (+ = hands out toward the ball) | < -20° | provisional_ui_mapping |
| Address | Arm hang | Slightly crowded | amber | near-arm line from shoulder to hands vs straight down (+ = hands out toward the ball) | -20 to -10° | provisional_ui_mapping |
| Address | Arm hang | Arm hang good | green | near-arm line from shoulder to hands vs straight down (+ = hands out toward the ball) | -10 to 10° | provisional_ui_mapping |
| Address | Arm hang | Slightly reaching | amber | near-arm line from shoulder to hands vs straight down (+ = hands out toward the ball) | 10 to 20° | provisional_ui_mapping |
| Address | Arm hang | Reaching | coral | near-arm line from shoulder to hands vs straight down (+ = hands out toward the ball) | >= 20° | provisional_ui_mapping |
| Mid-Back | Plane | Too shallow | coral | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | < -12° | provisional_ui_mapping |
| Mid-Back | Plane | Slightly shallow | amber | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | -12 to -5° | provisional_ui_mapping |
| Mid-Back | Plane | On plane | green | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | -5 to 5° | provisional_ui_mapping |
| Mid-Back | Plane | Slightly steep | amber | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | 5 to 12° | provisional_ui_mapping |
| Mid-Back | Plane | Too steep | coral | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | >= 12° | provisional_ui_mapping |
| Top | Posture | Loss of posture | coral | change of the hip-to-shoulder line from Address (- = more upright) | < -10° | provisional_ui_mapping |
| Top | Posture | Slight loss of posture | amber | change of the hip-to-shoulder line from Address (- = more upright) | -10 to -5° | provisional_ui_mapping |
| Top | Posture | Posture maintained | green | change of the hip-to-shoulder line from Address (- = more upright) | -5 to 5° | provisional_ui_mapping |
| Top | Posture | Slight loss of posture | amber | change of the hip-to-shoulder line from Address (- = more upright) | 5 to 10° | provisional_ui_mapping |
| Top | Posture | Loss of posture | coral | change of the hip-to-shoulder line from Address (- = more upright) | >= 10° | provisional_ui_mapping |
| Mid-Down | Plane | Too shallow | coral | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | < -12° | provisional_ui_mapping |
| Mid-Down | Plane | Slightly shallow | amber | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | -12 to -5° | provisional_ui_mapping |
| Mid-Down | Plane | On plane | green | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | -5 to 5° | provisional_ui_mapping |
| Mid-Down | Plane | Slightly steep | amber | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | 5 to 12° | provisional_ui_mapping |
| Mid-Down | Plane | Too steep | coral | shaft angle minus the Address shaft angle (+ = steeper, - = flatter) | >= 12° | provisional_ui_mapping |
| Impact | Extension | Posture maintained | green | back of the pelvis vs its Address line (tush line), % of height (+ = toward the ball) | < 2% | provisional_ui_mapping |
| Impact | Extension | Slight early extension | amber | back of the pelvis vs its Address line (tush line), % of height (+ = toward the ball) | 2 to 5% | provisional_ui_mapping |
| Impact | Extension | Early extension | coral | back of the pelvis vs its Address line (tush line), % of height (+ = toward the ball) | >= 5% | provisional_ui_mapping |
| Impact | Head | Head steady | green | head movement from Address, % of body height | < 4% | provisional_ui_mapping |
| Impact | Head | Slight head movement | amber | head movement from Address, % of body height | 4 to 8% | provisional_ui_mapping |
| Impact | Head | Excessive head movement | coral | head movement from Address, % of body height | >= 8% | provisional_ui_mapping |
| Takeaway | Takeaway plane | On plane / Slightly inside / Inside / Slightly outside / Outside | - | none | - | unsupported (not shown) |
| Top | Hand depth | Hand depth good / Slightly shallow hand depth / Too shallow / Slightly deep / Too deep | - | hand_depth: hands vs trail shoulder, % of height (measured, descriptive only) | - | unsupported (not shown) |
