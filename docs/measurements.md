# Swing Guru Engine v0.1 - measurement definitions

This document defines every measurement the engine produces, what it is computed
from, what it means, and where it breaks down. It is the reference that the
implementation and the tests are held to: a measurement that is not documented
here does not exist in the engine, and the test suite checks that this file
covers every implemented measurement key.

Nothing in this file is a coaching conclusion, an ideal range, or a score. The
engine reports projected geometry from a single camera; interpreting it is a
human job at this stage.

## Conventions

**Coordinate space.** MediaPipe returns normalised image coordinates where `x`
is a fraction of the frame width and `y` is a fraction of the frame height.
Every angle and distance in the engine is computed after scaling `x` by the
frame width and `y` by the frame height. That is what "aspect-corrected pixel
space" means below. Measuring in normalised coordinates would silently distort
every angle on a non-square frame.

**Landmarks.** The engine uses the 33-point MediaPipe BlazePose topology. Index
names used in this document:

| Landmark | Index | Landmark | Index |
| --- | --- | --- | --- |
| nose | 0 | left_shoulder | 11 |
| right_shoulder | 12 | left_elbow | 13 |
| right_elbow | 14 | left_wrist | 15 |
| right_wrist | 16 | left_hip | 23 |
| right_hip | 24 | left_knee | 25 |
| right_knee | 26 | left_ankle | 27 |
| right_ankle | 28 | left_heel | 29 |
| right_heel | 30 | left_foot_index | 31 |
| right_foot_index | 32 | | |

**Derived points.**

* `hip_mid` is the midpoint of `left_hip` and `right_hip`. This is the pelvis
  proxy.
* `shoulder_mid` is the midpoint of `left_shoulder` and `right_shoulder`.
* `lead` and `trail` are anatomical sides derived from handedness. A
  right-handed golfer leads with the left side, so `lead_knee` is
  `left_knee` and `trail_knee` is `right_knee`. A left-handed golfer is the
  mirror image. The convention is recorded in every run's metadata.

**Camera view.** Each run records `face-on`, `down-the-line`, or `unknown`.
This is a label, not something the engine infers. Every measurement result
carries a viewpoint warning because a single uncalibrated camera cannot recover
the third dimension.

**Fixed camera.** The camera is assumed to stay still for the whole clip.
Relative measurements are differences against the reference frame, so any
camera movement between frames is indistinguishable from golfer movement and
will corrupt every displacement and change value. There is no stabilisation in
v0.1.

**Mirrored input.** `mirrored_input` is a recorded flag, not a correction. Lead
and trail labels always describe anatomy as if the video were unmirrored. When
the flag is set, the engine emits a warning in the run metadata rather than
silently flipping landmarks; a mirrored clip must be interpreted with the lead
side read as screen-right, or corrected before analysis.

**Timing.** Frame timestamps are derived from the frame index assuming a
constant frame rate: `timestamp_ms = index * 1000 / fps`. A variable-frame-rate
source, or a container whose frame rate is unreadable, will drift; the engine
warns when the reported rate is missing and records the rate it assumed in
`metadata.json`. A run truncated by `max_frames` also reports it.

**Audio.** The annotated video is written without an audio track (FFmpeg is
invoked with `-an`). Any audio in the source is dropped by design; no export
references audio.

**Confidence.** Visibility and presence are landmark quality signals reported
by the pose model. They are not a probability that a measurement is accurate.
A high-visibility landmark can still produce a misleading angle if the camera
angle is poor.

**Validity.** A measurement is valid only when every required landmark is
present, finite, inside the frame, and above the configured visibility and
presence thresholds. Otherwise the value is `null` and the result carries the
reason it was rejected. Values are never carried forward from a previous frame.
The checks fail closed: a missing visibility value, a non-finite value, or a
value outside `0..1` rejects the landmark rather than being treated as good, and
the same predicate drives the measurements, the landmark CSV's `usable` and
`quality` columns, and the overlay colouring.

## Measurements

### `torso_inclination_deg` - Torso inclination

* **Unit:** degrees
* **Landmarks:** `left_shoulder` (11), `right_shoulder` (12), `left_hip` (23),
  `right_hip` (24)
* **Calculation:** the angle between the `hip_mid` to `shoulder_mid` vector and
  image vertical, in aspect-corrected pixel space. `0` degrees means the
  shoulder midpoint sits directly above the hip midpoint in the image.
* **Assumptions:** the torso is represented by a single straight line between
  two midpoints; the golfer is roughly side-on enough for that line to be
  meaningful.
* **Limitations:** this is a projected angle from one camera. A torso bending
  toward or away from the lens barely changes the value, so a face-on view
  cannot separate forward bend from side bend. Do not label this number
  "forward bend". It is not comparable between videos shot at different
  camera positions, and it is not a clinical posture measurement.

### `lead_knee_flex_deg` - Lead knee flex

* **Unit:** degrees
* **Landmarks:** lead side of `hip` (23 or 24), `knee` (25 or 26), `ankle` (27
  or 28). For a right-handed golfer that is `left_hip`, `left_knee`,
  `left_ankle`.
* **Calculation:** `180 - interior angle at the knee`, where the interior angle
  is measured between the hip-to-knee and knee-to-ankle segments in
  aspect-corrected pixels. A straight leg gives `0`; a right-angled knee gives
  `90`.
* **Assumptions:** hip, knee and ankle are visible enough for the model to
  place them, and the leg is not substantially rotated out of the image plane.
* **Limitations:** measures in-image flexion only. Loose clothing, a club
  obscuring the knee, or significant leg rotation all degrade the estimate.

### `trail_knee_flex_deg` - Trail knee flex

* **Unit:** degrees
* **Landmarks:** trail side of `hip`, `knee`, `ankle`. For a right-handed
  golfer that is `right_hip` (24), `right_knee` (26), `right_ankle` (28).
* **Calculation:** identical to the lead knee definition on the trail side.
* **Assumptions:** as above.
* **Limitations:** the trail leg is often occluded by the lead leg during the
  downswing, so expect more rejected frames on this side than on the lead side.

### `head_displacement_px` - Head displacement

* **Unit:** pixels
* **Landmarks:** `nose` (0)
* **Calculation:** Euclidean distance between the nose position in this frame
  and the nose position in the selected reference frame, in aspect-corrected
  pixels.
* **Assumptions:** the nose is treated as an explicit head proxy. It is not a
  head-centre estimate and it is not a centre of mass.
* **Limitations:** pixel distances depend on camera distance and resolution and
  are not comparable between videos. Head rotation is not captured by a single
  point. If the reference frame is not selected or the nose is not usable in
  that frame, the measurement is unavailable rather than defaulted.

### `head_displacement_pct_height` - Head displacement (frame height)

* **Unit:** percent of frame height
* **Landmarks:** `nose` (0)
* **Calculation:** `head_displacement_px / frame_height * 100`.
* **Assumptions:** the golfer's distance from the camera and the framing are
  broadly comparable between the clips being compared.
* **Limitations:** still camera-relative. A golfer closer to the lens shows a
  larger percentage for the same real movement. Centimetres are deliberately
  not reported: a single uncalibrated camera cannot supply a scale.

### `pelvis_displacement_px` - Pelvis displacement

* **Unit:** pixels
* **Landmarks:** `left_hip` (23), `right_hip` (24)
* **Calculation:** Euclidean distance between `hip_mid` in this frame and
  `hip_mid` in the reference frame, in aspect-corrected pixels.
* **Assumptions:** the hip midpoint is a usable 2D proxy for pelvis location.
* **Limitations:** pelvis rotation and hip tilt are not recovered. Depth
  movement toward or away from the lens does not appear in this number.

### `pelvis_displacement_pct_height` - Pelvis displacement (frame height)

* **Unit:** percent of frame height
* **Landmarks:** `left_hip` (23), `right_hip` (24)
* **Calculation:** `pelvis_displacement_px / frame_height * 100`.
* **Assumptions:** as for pelvis displacement.
* **Limitations:** as for the head percentage; camera-relative only, never
  centimetres.

### `torso_angle_change_deg` - Torso angle change

* **Unit:** degrees
* **Landmarks:** `left_shoulder` (11), `right_shoulder` (12), `left_hip` (23),
  `right_hip` (24)
* **Calculation:** current torso inclination minus the torso inclination of the
  reference frame. A positive value means the torso leaned further from image
  vertical than it did at the reference frame.
* **Assumptions:** both the current frame and the reference frame pass the
  torso inclination quality checks.
* **Limitations:** inherits every limitation of torso inclination. With no
  reference frame selected, or with a reference frame whose torso angle is
  unusable, the value is unavailable. The engine never invents an address
  frame to anchor this measurement.

## Stability

Head stability and pelvis stability are reported as the spread of the
displacement series across the processed frames: minimum, maximum, mean,
standard deviation, and range. They are descriptive statistics over the
processed window, not a comparison against an ideal range.

## Per-frame record contents

Every decoded frame produces a record, including frames where the pose model
found nobody. Each record contains:

* zero-based frame index, timestamp in milliseconds, frame width and height
* whether a pose was detected, plus any per-frame notes
* every available raw normalised landmark with `x`, `y`, `z`, `visibility`,
  `presence`, and an in-frame flag
* every available world landmark (model estimates in metres, for inspection
  only - they are not calibrated measurements)
* all measurements with their value, unit, validity flag and warnings
* the reference frame index used for relative measurements

## What v0.1 deliberately does not do

* No scores, ranks, or coaching conclusions. Raw measurements are kept
  separate from interpretation.
* No centimetre or degree-of-rotation claims: both need camera calibration or
  multiple views.
* No automatic swing phase detection. Phases are assigned manually by frame
  index, and the interface for a future detector is explicit rather than
  faked.
* No inference of biomechanical reliability from landmark visibility alone.

## Confidence and quality warnings

Each rejected measurement carries one or more of these reasons:

| Warning | Meaning |
| --- | --- |
| `no pose detected in this frame` | the pose model returned no person for the frame |
| `landmark missing from pose output` | the required landmark is absent |
| `landmark coordinate is not finite` | the landmark value is NaN or infinite |
| `landmark outside frame bounds` | the landmark falls outside the image |
| `landmark visibility is missing` | the model returned no visibility value |
| `landmark visibility is not finite or outside 0..1` | visibility is NaN, infinite or out of range |
| `landmark visibility below threshold` | visibility is under the configured floor |
| `landmark presence is missing (required)` | presence is required but absent |
| `landmark presence is not finite or outside 0..1` | presence is NaN, infinite or out of range |
| `landmark presence below threshold` | presence is under the configured floor |
| `degenerate geometry` | a segment collapsed to a point, so no angle exists |
| `no reference frame selected` | a relative measurement has no anchor |
| `face-on camera view ...` | in-image angles cannot separate forward and side bend |
| `down-the-line camera view ...` | perspective dominates the in-image torso angle |
| `single-camera 2D projection ...` | absolute body angles are not measured |
