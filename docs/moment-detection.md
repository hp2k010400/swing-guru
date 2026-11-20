# Automatic swing-moment estimates (experimental)

The guided playback experience plays the analysed clip between the seven
checkpoints of a swing: Address, Takeaway, Mid-Back, Top, Mid-Down, Impact and
Finish. (Mid-Down keeps the internal phase value `downswing` so existing CLI
arguments and exports still work; `mid_back`, `mid-back`, `mid_down` and
`mid-down` are accepted as aliases.)
Those moments are estimated from the measured body motion - they are not read
from a trained swing model, and they are not fixed percentages of the video.

This document describes the evidence the estimator uses, the thresholds it
applies, and what it deliberately refuses to do.

## What it measures

Everything comes from the landmark points the engine already validated for each
frame. For each frame the estimator takes:

| Feature | Source | Note |
| --- | --- | --- |
| Hands | mean of the usable wrists | both wrists when available |
| Pelvis | hip midpoint | derived by the engine |
| Torso | hip midpoint to shoulder midpoint | used as the body scale |
| Time | frame timestamp | real seconds, not frame counts |

Hand positions are converted to pixels, expressed as an offset from the pelvis,
and divided by the torso length (in pixels, so the aspect ratio is already
applied). Distances are therefore in "torso lengths" and times in seconds, and
the same thresholds work for a phone clip and a broadcast-resolution clip, for
portrait and landscape, at any frame rate.

Motion is smoothed over a short window (default 0.10 s; a longer window blurs the fast part of the downswing) *inside* contiguous
runs of usable frames only. A frame whose wrists, hips or shoulders fail the
engine's fail-closed quality check is a gap; gaps are never bridged.

## Evidence per moment

| Moment | Evidence the estimator looks for |
| --- | --- |
| Address | the start of the one complete *swing episode* (see below): a stable hand window (motion under the stability threshold for at least the stability window) immediately before a sustained departure from that position; the last frame of that window is used |
| Takeaway | the first frame where the hands have left the set-up position by the departure threshold and keep moving for the sustain time |
| Mid-Back | between takeaway and top, the first frame where the hands are `backswing_fraction` of the set-up-to-top distance away from the set-up hands |
| Top | the **first** hand-height reversal after the takeaway: the hands have risen at least the rise threshold above the set-up and then dropped at least the fall threshold below the highest point so far. The highest point in the whole clip is *not* used, because on real swings the finish is usually higher than the top |
| Mid-Down | after the top, the first frame where the hands have come `return_fraction` of the way back toward the set-up hand region |
| Impact (estimated) | from Mid-Down, the frame where the hands come nearest the set-up hand region, and only if that approach is inside the approach threshold. The search stops once the hands have moved `impact_exit_distance` away from that closest approach again (the follow-through), so lowering the club later in the clip cannot drag it late. This is **not** club-ball contact and is labelled "Estimated impact" everywhere |
| Finish | the middle of the first window after the swing where hand movement settles below the stability threshold and stays settled |

The clip is clipped-ended, not invented: the estimator never returns a frame
that the engine did not analyse.

## Thresholds

All thresholds are engineering thresholds in body lengths and seconds. None of
them is a golf ideal, a coaching range or a "correct" position.

| Setting | Default | Meaning |
| --- | --- | --- |
| `min_frames` | 8 | fewer analysed frames than this and nothing is estimated |
| `min_duration_s` | 0.5 | shorter clips cannot hold a moment |
| `min_point_coverage` | 0.6 | fraction of frames that must have usable wrists, hips and shoulders |
| `max_gap_s` | 0.5 | a longer tracking gap breaks the sequence and withholds every moment |
| `smooth_window_s` | 0.10 | moving-average window for the hand path |
| `stability_window_s` | 0.30 | how long the hands must stay still to count as stable |
| `stability_speed` | 0.35 | torso lengths per second that still counts as still |
| `departure_threshold` | 0.30 | displacement from the set-up position that starts the swing |
| `departure_sustain_s` | 0.10 | how long the departure must continue |
| `top_rise_threshold` | 0.50 | hand rise above the set-up that counts as a backswing (full swings rise well over 1.0; very short chips may not qualify) |
| `top_fall_threshold` | 0.15 | elevation fall that confirms the reversal |
| `backswing_fraction` | 0.5 | how far from the set-up toward the top counts as Mid-Back |
| `return_fraction` | 0.5 | how far back toward the set-up region counts as Mid-Down |
| `impact_max_distance` | 0.45 | how near the set-up region the hands must come |
| `refine_window_s` | 0.07 | Impact is found on the smoothed path, then moved to the closest *unsmoothed* frame within this window, because smoothing flattens fast minima by about a frame |
| `top_plateau` | 0.05 | at the top the hands hover while the club finishes setting; Top is the centre of the run of raw frames within this many torso lengths of the highest hand point (searched within twice `refine_window_s`). On the real test swing the single highest hand frame was two frames before the club turned; the plateau centre matched the human check. It is still hand-based: without club detection it cannot see the club's own reversal |
| `impact_exit_distance` | 0.30 | how far the hands must move away from their closest approach before the impact search stops |
| `finish_stability_s` | 0.30 | how long the finish window must stay still |
| `still_radius` | 0.10 | fallback only (the hand-speed test found no swing at all): body-wide set-up stillness, accepted alongside the hand-speed test: hands, pelvis, shoulder centre and ankle centre each stay within this many torso lengths of their own median over `stability_window_s`. It is the same motion budget as `stability_speed` x `stability_window_s` (0.35 x 0.3 ~= 0.10) spent on net drift instead of the worst single frame, so wrist jitter at set-up (hands overlapping) no longer breaks a visibly still address. Robustness batch: real set-ups drift 0.012-0.037 (hands, median) and 0.018-0.049 (body) |
| (net drift) | 0.5 x `still_radius` | in the fallback, both stillness tests also reject a window whose hands drift steadily: the mean of its last third must be within half the budget (0.05 torso lengths) of its first third. Jitter is zero-mean; the slow start of a takeaway is not. The fallback is *not* used when the hand-speed test finds a swing: trying the body-wide test first on every clip moved Address into the first frames of the takeaway on four of twelve batch clips (clubhead moves before the hands; shaft 80 deg instead of 55 deg at "Address") |
| `setup_min_lean_deg` | 15 | the body-wide test also needs a set-up posture: the hip-to-shoulder line leans at least this far from vertical. Without it, standing upright at the bag, walking or pulling a club out passed as set-ups in a 69 s range clip. A detection gate, not a golf ideal; real address torso angles in the batch are in the 40s. The hand-speed test has no posture condition |
| `bridge_gap_s` | 0.15 | only when no swing is found otherwise: wrist dropouts (both wrists under the measurement gate) whose surrounding frames are at most this far apart are linearly interpolated so one swing is not split into pieces. Moments are never placed on a bridged frame; they move to the nearest measured frame. Far below `max_gap_s` |
| `handedness` | `right` | which wrist is treated as the lead wrist when only one wrist is usable; both usable wrists are averaged, so the estimate is handedness-neutral in practice |
| `address_recheck` | `false` | experimental, off in every profile (selectable only explicitly by the validation harness): an Address more than 0.6 s before the takeaway is re-checked with the body-wide stillness test and the later still window used. Held back because on 2 of the 4 validation swings it moves, the club is already moving at the new frame (docs/agent-work/analysis-v2/FIX_address_recheck.md) |

## Swing episodes and ambiguity

A *swing episode* is a still set-up window, followed by a sustained departure,
followed by a hand-height reversal (the top), followed by the hands returning
within `impact_max_distance` of the set-up hands. Two or more episodes in one
clip (for example a practice swing and then the real swing) are treated as
ambiguous: every moment is withheld rather than silently picking one.

Holding the finish and then lowering the club is **not** a second episode: the
hands fall, rather than rise, after leaving that still window. (An earlier
version counted any two hand-height peaks as two swings, which would have
blocked every real clip that shows the club being lowered after the finish.)

If there is no complete episode, partial results are kept where the evidence
supports them: a clip that ends before the top keeps Address and Takeaway; a
clip that starts mid-swing keeps the Top and what follows it.

## Real-footage check

`tests/fixtures/real_swing_hand_path.json` holds the derived body points (no
video) from one real right-handed driver swing filmed on a phone (464x832,
~30 fps, MediaPipe `pose_landmarker_full`). A human checked each estimated
frame against the video; the regression test pins the estimates to within a
few frames of those. One swing is not a validation set: treat it as a sanity
check, not an accuracy figure.

## What it refuses to do

* **No percentages.** Moments are found from motion, so the same movement at a
  different time in a clip is found at that time. The tests exercise cue
  timetables at different positions, frame rates and aspects.
* **No guessing.** A clip with no stable set-up window, too little usable
  tracking, a long tracking gap or two swing-like episodes returns
  `unavailable` with a reason instead of seven frames.
* **No invented landmarks.** Only the engine's accepted points are used; a lost
  frame is never interpolated across.
* **No club, ball or plane claims.** "Impact" is the nearest approach of the
  hands to the set-up region after the top. It is labelled "Estimated impact"
  and is never described as contact.
* **No accuracy claim.** High landmark confidence means the body was tracked,
  not that a phase boundary is right. Real-golf accuracy is unverified until a
  real swing video is analysed and checked by a human.

## Manual correction and provenance

Every moment carries its provenance:

* `estimated` - the estimator's frame, shown as "Estimated moment" and marked
  for review;
* `manual` - a frame the golfer picked in **Adjust moments**, shown as
  "Selected manually";
* `unavailable` - no frame, with a plain-language reason.

Manual choices always win: re-estimating keeps them, and clearing a moment by
hand keeps it cleared. The exported `moments.json` records the source of every
slot alongside the timing and the evidence strings, so a mixed run stays
auditable.

## Where it lives

| Path | Responsibility |
| --- | --- |
| `src/swingguru/auto_moments.py` | the estimator and the moment data contract |
| `src/swingguru/ui/timeline.py` | moments to ordered checks, with geometry and time |
| `src/swingguru/ui/guided_fixture.py` | the deterministic sample clip used for QA |
| `tests/test_auto_moments.py` | estimator tests on generated movement series |
| `tests/test_timeline.py` | check grouping, provenance and payload tests |

The interaction the moments drive - holds, checks, transport controls - is
documented in the README, and the browser timing evidence lives in
`docs/agent-work/guided-playback/REPORT.md`.
