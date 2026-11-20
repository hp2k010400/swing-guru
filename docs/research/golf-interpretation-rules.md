# Golf interpretation rules — evidence review

Date: 2026-09-29. Scope: down-the-line (DTL) swings; the measurements Swing
Guru produces today (2D image geometry from RTMW pose + club tracking,
normalised where stated). Purpose: decide which measured facts may become
interpretations, and on what evidence.

Source strength used below:

* **P** — peer-reviewed biomechanics.
* **I** — recognised industry definition / screening system (TPI, Sportsbox,
  GEARS); widely used, usually not peer-reviewed, thresholds often
  proprietary.
* **V** — vendor benchmark data (population ranges from a 3D system).
* **O** — observational coaching opinion (named instructor, photo analysis).

## Key sources

| Source | Strength | What it says | Limits for us |
| --- | --- | --- | --- |
| TPI, "Early Extension" swing characteristic ([mytpi.com](https://www.mytpi.com/articles/swing/early-extension-swing-characteristic)) | I | "Any forward movement of the pelvis toward the ball on the backswing or downswing." 67 % of 90,000+ screened amateurs vs ~1 % of 100+ tour players (TPI's own figures). Linked to physical limits (deep squat). | Definition is about the **pelvis**, not the torso. No numeric threshold published. Figures are TPI's, not peer-reviewed. |
| TPI, "Loss of Posture" ([mytpi.com](https://www.mytpi.com/improve-my-game/swing-characteristics/loss-of-posture)) | I | "Any significant alteration from your body's original set-up angles." | "Significant" is undefined; no threshold. |
| Sportsbox AI, "What is Early Extension?" ([help.sportsbox.ai](https://help.sportsbox.ai/what-is-early-extension)) | I / V | Measures **Pelvis Thrust at Impact** in inches relative to Address (negative = toward the ball), 3D-reconstructed from one DTL video, benchmarked on 100+ tour pros and scaled by height, gender and hip width. Example tour range for a DeChambeau-sized male: −0.9″ to +1.7″. | 3D, body-scaled, proprietary model. **Small movement toward the ball is inside the tour range**, so "any toward-ball movement" is not a fault by itself. |
| Sportsbox tour data via Golf Digest ([golfdigest.com](https://www.golfdigest.com/story/useful-golf-swingfacts-sportsbox-ai-graph)) | V | Tour chest (thorax) forward bend ≈ 30° at both Address and Impact. | Chest-segment bend in 3D, not our 2D hip→shoulder line, which also changes with turn and side-bend. |
| Adam Young, "Early Extension" ([adamyounggolf.com](https://www.adamyounggolf.com/early-extension/)) | O | Tour players *do* extend up through impact, after a squat in transition; extension itself is not the fault, timing is. Camera perspective can hide hip rotation. | Photo analysis, no data. Useful as a caution against labelling "more upright" as a fault. |
| Cheetham et al. / Myers et al., rotational biomechanics ([PubMed 21844613](https://pubmed.ncbi.nlm.nih.gov/21844613/)) | P | Pros show consistent pelvis/torso rotational velocity patterns; amateurs vary widely. | Rotation velocities need 3D/high speed; not measurable from our 30 fps 2D. |
| Low back pain & golf review ([PMC9219256](https://pmc.ncbi.nlm.nih.gov/articles/PMC9219256/)); JOSPT 2010 LBP swing mechanics ([jospt.org](https://www.jospt.org/doi/10.2519/jospt.2010.3152)) | P | Early hip extension, loss of posture and slide are common characteristics; associated with physical screening limits. | Associations, not thresholds; not 2D DTL-calibrated. |
| DTL filming guidance ([GolfWRX](https://www.golfwrx.com/160889/use-the-proper-proper-camera-alignment-when-filming-your-swing/)) | O / I | For DTL checks the camera must sit on the hand line, parallel to the target line; otherwise angles are distorted. | We do not yet verify camera alignment. |
| Takeaway / P2 checkpoints ([golfdistillery.com](https://www.golfdistillery.com/swing-tips/takeaway/club-position/), [foygolfacademy.com](https://foygolfacademy.com/how-to-keep-the-club-outside-the-hands-early-and-stop-taking-the-club-too-far-inside/)) | O | Inside/outside is judged at **shaft parallel** (P2): clubhead vs hands and shaft vs target line. | Needs shaft-parallel timing, depth, and a camera on the hand line. |
| Head movement ([rotaryswing.com](https://rotaryswing.com/golf-instruction/swingfundamentals/golf-head-behind-ball); motion-capture descriptions of the elite swing) | O / P | Lateral and vertical head movement is normal in good swings; some instructors allow ~1.5″ lateral. | No agreed threshold; "keep your head still" is not supported. |

## A. SUPPORTED NOW (descriptive, relative-to-Address, fail-closed)

These rules may speak **factually** to the golfer. None of them is a verdict
and none is eligible for "main focus".

| Rule | Facts | Wording pattern | Why allowed |
| --- | --- | --- | --- |
| Posture change through impact (descriptive) | torso inclination change at Impact vs Address, HIGH/MEDIUM | "Your upper body is 21° more upright at impact than at address." | A measured relative change; no claim it is good or bad. |
| Pelvis movement vs Address (descriptive) | pelvis displacement at Impact, toward/away-from-ball component, % body height | "At impact your hips are 5.8 % of your height closer to the ball than at address." | Relative displacement, scale-normalised, direction from the golfer's own set-up. |
| Head movement vs Address (descriptive) | head displacement, % body height, direction | "Your head moved 6.5 % of your height, away from the ball." | Movement is normal; stated as fact only. |
| Address torso angle, knee flex (reference) | set-up values | "Set-up: torso 46°, knees 16° / 18°." Stored as the golfer's **personal reference** for future swings. | No universal ideal (club, ball position, body proportions); comparison is against the golfer's own history. |
| Shaft vs Address shaft reference | shaft angle at each moment vs Address line | "12° more upright than the Address shaft reference." | Geometric comparison with a line drawn from the golfer's own set-up; **not** a swing plane. |

## B. POSSIBLE BUT NEEDS CALIBRATION

| Candidate | Required signals | Why not yet | What would calibrate it |
| --- | --- | --- | --- |
| **Pelvis toward the ball with a more upright torso through impact** (the early-extension-*related* pattern) | pelvis toward-ball component at Impact **and** torso more upright at Impact, both HIGH, DTL view, valid Address | The TPI concept is pelvis-led; Sportsbox shows tour players can move up to ~0.9″ toward the ball; our 2D hip-midpoint and 2D torso line are not the same quantities as 3D pelvis thrust / chest bend; camera alignment unknown. Any threshold we pick today would be invented. | 20–50 DTL swings with a trusted reference (3D app output such as Sportsbox pelvis thrust, or a coach's label) to fit % body-height thresholds for our 2D measure; camera-alignment check. |
| Loss of posture (torso) | torso change beyond a threshold at Impact | "Significant" undefined; 2D hip→shoulder line changes with turn and side-bend, not only flexion; tour players extend through impact. | Same reference set; separate flexion from rotation (needs a chest orientation cue or 3D lift). |
| Head movement magnitude | head displacement vs threshold | No agreed threshold; movement is normal. | Population data on our 2D measure; then only "unusually large" wording. |
| Shaft comparisons at Mid-Down vs Address reference | angle difference | Plane-style judgements need camera on the hand line and a validated reference (Address shaft vs hand/elbow planes). | Camera-alignment check + coach-labelled set. |

## C. NOT SUPPORTED BY CURRENT VISION

| Concept | Missing |
| --- | --- |
| Takeaway inside / outside (P2) | Our "Takeaway" is the hands leaving set-up (departure threshold), not shaft parallel; we have no depth or target-line direction; camera alignment unknown. |
| Hand depth at Top, arm hang | No reliable hand-to-body depth in 2D DTL. |
| Laid off / across the line at Top | Needs target-line direction and club orientation in 3D. |
| Face angle, path, attack angle | No clubface or ball tracking. |
| Kinematic sequence / rotational speeds | 30 fps 2D; no segment rotation angles. |
| Top posture change | The 2D torso line at Top is dominated by shoulder turn, not flexion. |

## D. DO NOT SHIP YET (terms)

These words must not appear in golfer-facing text until a validated rule
supports them: **early extension, loss of posture, stood up / standing up
(as a fault), on plane, swing plane, ideal plane, correct plane, steep,
shallow, laid off, across the line, over the top, inside, outside, good, bad,
correct, ideal, perfect, fault, error, keep your head still**.

## Rule design consequences

1. Interpretations are a separate, deterministic layer: `MeasurementFact` →
   `InterpretationRule` → `InterpretationResult` (versioned, fail-closed).
2. Only rules with `calibration = VALIDATED` can produce a verdict or be
   "main focus". None qualifies today, so the focus reads "Coaching
   priorities are coming next."
3. Multi-signal patterns may be *observed* and described factually, flagged
   `NEEDS_CALIBRATION`, never labelled with a golf term.
4. Units are never compared across rules; priority uses rule severity,
   confidence, phase importance and rule priority only.
5. Each result carries `rule_id` so a future drill can be mapped
   deterministically (`rule_id → coaching explanation → drill_id`).

## Rear-of-pelvis (tush line) measurement — implemented 2026-09-29

Instruction definition ([RotarySwing](https://rotaryswing.com/golf-instruction/golfbiomechanics/early-extension-in-golf)):
"a vertical line drawn straight down from the rear" at Address, "a couple of
inches behind the heel"; the rear moving forward off it is the cue. Our 2D
approximation (`src/swingguru/pelvis_depth.py`): person silhouette (MediaPipe
pose segmentation, key frames only) + RTMW hips → scan each row of a pelvis
band (−0.05 to +0.30 torso lengths around the hip midpoint) away from the
ball to the silhouette edge (sub-pixel) → rearmost outline (90th percentile)
→ Address x = tush line → later phases: horizontal displacement, + toward the
ball, % of body height. Fails closed on missing hips, hip outside the
silhouette, <70 % clear rows, outline at/near the frame edge (<3 px), soft
mask edge, ragged or implausible outline.

Real swing: still-frame jitter 0.13 px (0.05 % body height, ~10x below the
0.5 % noise floor); rear pelvis +0.9 % (Top), +4.3 % (Mid-Down), +6.7 %
(Impact), all HIGH; hip midpoint +1.9 / +4.5 / +5.9 %, same direction.

DTL camera check (`src/swingguru/camera_check.py`): shoulder/hip width per
torso (≤0.45 valid, >0.75 invalid), hands ahead of hips (≥0.30 valid, <0.12
invalid), head+ankles in frame, body height in px (≥200 valid, <120 invalid —
0.5 % must span ~1 px), background shift Address→Impact (≤1 % valid, >3 %
invalid). Worst signal wins. Real swing: VALID.

**Camera-motion fix (2026-09-29, robustness batch).** The first motion signal
blanked the golfer with one flat patch in both frames and phase-correlated the
whole image; the identical patch edges dominated, so a panning phone read as
~0 (swing 08: ~57-60 px real, 0.3 px measured, camera VALID, rear pelvis
+19.5 % and the pattern rule observed on camera motion). It now uses ORB
features only where *both* frames are background, a RANSAC similarity fit
(pan, zoom, roll), and Address→Top, →Mid-Down and →Impact. Drift moves every
Address-relative position by the same amount, so each reliable comparison is
graded against the position noise floor: VALID only within 0.5 % of body
height, INVALID above it. (The first re-run used the old 1 %-of-frame bound:
with a fresher Address, 08 still drifted 10.2 px Address→Impact = 3 % of body
height, half of its reported +6.1 % rear-pelvis shift, and read VALID - the
frame-height bound was itself failing open.) Too little background, texture,
consensus or spread → UNCERTAIN (never VALID). When motion is INVALID, head
and hip movement (pixel positions relative to Address) are hidden and the
Address-relative position rules (`impact.pelvis_vs_address` v2,
`impact.head_movement` v2, the rear-pelvis rules) are blocked; angle-based
checks remain. Consequence: handheld phone clips (6-12 px of drift) cannot
support tush-line numbers until drift is compensated.

The candidate rule (`impact.hips_toward_ball_with_upright_torso` v2) now uses
the rear-pelvis signal (≥ MEDIUM), posture (HIGH) and a VALID camera; it stays
NEEDS_CALIBRATION.

## The single most useful next measurement

A **camera-alignment check for DTL** (is the camera on the hand line and
parallel to the target line?), plus **one reference set of 20–50 swings with
Sportsbox-style pelvis-thrust values**, would let the pelvis/posture pattern
become the first calibrated verdict. On the vision side alone, the measurement
that unlocks most is **pelvis displacement toward the ball measured from the
rear of the pelvis (the "tush line")** rather than the hip midpoint — the TPI
test is posterior-pelvis based, and the midpoint also moves as the pelvis
rotates and the golfer squats.
