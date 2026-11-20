# Swing Guru Engine v0.1

A local experimental golf swing pose and geometry engine, plus a golfer-facing
Streamlit viewer. It takes a golf swing video, tracks the golfer's body frame
by frame, plays the clip between automatically estimated swing moments, holds
one measurement-specific overlay at a time while the video is held still, and
keeps the raw landmark, moment and measurement data in collapsed sections for
inspection.

This is a proof of concept. It runs entirely on your machine: no accounts, no
cloud services, no language-model analysis, no scoring, no coaching advice, and
no mobile app. Videos never leave the local filesystem.

## What it does

1. Upload a swing video in the viewer (or point the CLI at a file path).
2. Decode it frame by frame and run MediaPipe's pose landmarker on every frame.
3. Derive five families of projected swing measurements from the landmarks
   using deterministic geometry (see [docs/measurements.md](docs/measurements.md)).
4. Estimate the seven swing moments - Address, Takeaway, Mid-Back, Top, Mid-Down, Impact
   (labelled as an estimate, never claimed as contact) and Finish - from the
   measured hand path (see [docs/moment-detection.md](docs/moment-detection.md)),
   then play the video between them, holding one measurement at a time.
5. Write an annotated video plus CSV/JSON exports of landmarks, measurements,
   moments, phases and run metadata.

## Architecture

| Module | Responsibility |
| --- | --- |
| `src/swingguru/video.py` | video probing, streaming decode, annotated video writing, H.264 transcode |
| `src/swingguru/pose.py` | MediaPipe Tasks pose adapter, model resolution and download |
| `src/swingguru/landmarks.py` | landmark topology, side/handedness mapping, pose observation types |
| `src/swingguru/geometry.py` | deterministic aspect-corrected geometry |
| `src/swingguru/measurements.py` | measurement definitions, validity and quality rules |
| `src/swingguru/phases.py` | manual phase assignment and the interface reserved for automatic detection |
| `src/swingguru/auto_moments.py` | experimental moment estimates from measured body motion |
| `src/swingguru/overlays.py` | visual debugging overlays |
| `src/swingguru/pipeline.py` | detection pass, reference resolution, render pass, orchestration |
| `src/swingguru/export.py` | CSV and JSON exports |
| `src/swingguru/ui/app.py` | Streamlit analysis viewer |
| `src/swingguru/ui/presentation.py` | checkpoint wording, display values and the clean hero-still renderer |
| `src/swingguru/ui/timeline.py` | moments to ordered checks, with normalised geometry and payload |
| `src/swingguru/ui/media.py` | bounded, verified browser-playable preview preparation |
| `src/swingguru/ui/player.py` + `ui/player_assets/` | the guided playback component (plain HTML/CSS/JS) |
| `src/swingguru/ui/guided_fixture.py` | deterministic local swing sample used for guided playback QA |
| `src/swingguru/synthetic.py` | deterministic synthetic clips and scripted pose provider for tests |

## Dependencies

Python 3.12 (3.12.10 was used for v0.1), MediaPipe Tasks 1.0.1, OpenCV 5.0.0,
NumPy 2.5.3, Streamlit 1.64.0, pytest 9.1.1. Exact pins are in
`requirements.txt`; `pyproject.toml` carries compatible ranges.

MediaPipe depends on `opencv-contrib-python`, so that is the single OpenCV
distribution this project installs. Installing `opencv-python` as well would
place two distributions into the same `cv2` namespace and make the installed
version ambiguous. If you already have both, remove one:

```powershell
.\.venv\Scripts\python.exe -m pip uninstall -y opencv-python
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps opencv-contrib-python==5.0.0.93
.\.venv\Scripts\python.exe -m pip check
```

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m swingguru.cli download-model
```

`pip install -e .` uses the standard PEP 517 build with isolation, so it
fetches its own build backend and no manual `setuptools` step is needed. On a
machine without network access, install the build requirement first and then
skip isolation:

```powershell
.\.venv\Scripts\python.exe -m pip install "setuptools>=68" wheel
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
```

`requirements.txt` is the exact frozen set from the v0.1 environment if you want
the recorded versions instead of the range-based install.

FFmpeg is optional but recommended. When it is on `PATH` the annotated video is
re-encoded to H.264 so it plays in a browser; when it is missing the engine
keeps the raw MPEG-4 output and says so in the run warnings.

`download-model` fetches the official MediaPipe float16 pose landmarker from
`https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task`
and stores it at `models/pose_landmarker_full.task` (about 9.4 MB).

Check the environment at any time:

```powershell
.\.venv\Scripts\python.exe -m swingguru.cli doctor
```

## Run the viewer

```powershell
.\.venv\Scripts\python.exe -m streamlit run src/swingguru/ui/app.py
```

The bundled `.streamlit/config.toml` binds the server to `127.0.0.1` and
disables Streamlit usage statistics. It also sets the golfer-facing palette:
an off-white `#F7F7F2` page, white cards, golf green `#4F7F5F` for the primary
action and measurement geometry, and charcoal `#292929` text. Open the printed
local URL if the browser does not open automatically.

The viewer is a single narrow column that plays the real video:

1. **Upload and settings** - choose the video, the **Playing hand** and the
   camera angle, then press **Analyse swing**. The pose model runs once over the
   video and progress is shown in plain language.
2. **Guided playback** - the clip plays between automatically estimated
   moments. At each moment the video holds still and one measurement-specific
   overlay and value are shown at a time (Address shows torso inclination and
   both knee flexes; movement checks compare against the Address moment). The
   transport controls are Play/Pause, **Prev**, **Next**, **Replay**,
   0.25x/0.5x/1x speed and a scrubber; **Prev**/**Next** step through the
   checks, including several checks that share one video time.
3. **Adjust moments** - collapsed by default: re-estimate, clear or hand-pick
   any of the seven moments. The editor always previews the slider's *candidate*
   frame, whether or not a moment was already saved, and a hand-picked frame is
   kept through re-estimation. Each moment is labelled **Estimated moment** or
   **Selected manually**.
4. **Advanced analysis** - collapsed by default: every measurement with its
   validity and warnings, the raw landmarks with their usability verdict, the
   measurement definitions, the moment-estimate table with provenance, the
   check timeline, the preview-media record, the comparison-frame control, the
   render button and the CSV/JSON downloads. The annotated video is labelled as
   a full-skeleton debug video.
5. **Developer details** - collapsed by default: runtime versions, the pose
   model check, the local-file and sample-clip source tools, the engine tuning
   sliders, the check hold time, the full-skeleton debug still and the run
   identifiers.

Measurements on screen, the annotated video and the exports all update from the
same cached records, so changing the comparison frame changes every relative
value consistently. The moment estimates and their limits are described in
[docs/moment-detection.md](docs/moment-detection.md).

If FFmpeg is unavailable, the preview cannot be prepared and the page says so
and falls back to stills; it never pretends playback worked.

## Analyse a swing from the command line

```powershell
.\.venv\Scripts\python.exe -m swingguru.cli info --input "C:\path\to\swing.mp4"

.\.venv\Scripts\python.exe -m swingguru.cli process `
  --input "C:\path\to\swing.mp4" `
  --output-dir runs\swing01 `
  --handedness right `
  --camera-view face-on `
  --reference-frame 12 `
  --phase address=12 --phase top=48 --phase impact=70
```

Useful flags: `--min-visibility`, `--min-presence`, `--max-frames`,
`--start-frame`, `--no-derive-reference`, `--no-transcode`,
`--no-annotated-video`, `--json`.

The reference frame anchors relative measurements. If you assign an address
phase and do not pass `--reference-frame`, the address frame is used and the
derivation is recorded in `phases.json` and the run warnings. Pass
`--no-derive-reference` to require an explicit reference; relative measurements
then stay unavailable rather than being anchored automatically.

## Outputs

Each run writes into its output directory:

| File | Contents |
| --- | --- |
| `annotated.mp4` | annotated video, H.264 when FFmpeg is available |
| `annotated_raw.mp4` | the raw OpenCV output, kept when transcoding ran |
| `measurements.csv` | one row per decoded frame: quality, every measurement and its validity, warnings |
| `landmarks.csv` | long-format landmarks with visibility, presence, in-frame and usable flags |
| `metadata.json` | video properties, configuration, measurement definitions, runtime versions, stats, warnings |
| `phases.json` | assigned phase frames and the reference frame |
| `moments.json` | every moment slot with its estimate or hand-picked frame, provenance, confidence and evidence |
| `summary.json` | per-measurement validity counts and stability statistics |
| `run.log` | the CLI run summary |

## Current measurements

Five families, all projected and single-camera: torso inclination, lead knee
flex, trail knee flex, head displacement (proxy: nose), pelvis displacement
(proxy: hip midpoint), plus torso angle change relative to the reference frame.
Head and pelvis stability are reported as the spread of their displacement
series. Full definitions, units, assumptions and limitations are in
[docs/measurements.md](docs/measurements.md).

Measurements are never silently invented. A frame with no pose, a landmark
below the visibility threshold, a landmark outside the frame, or a collapsed
segment produces an unavailable measurement with an explicit reason, and the
annotated video and CSV both show it.

## Current limitations

* Single uncalibrated camera: angles are in-image projections, distances are
  pixels, and nothing is reported in centimetres.
* The camera must stay fixed for the whole clip. Relative measurements are
  differences against the reference frame, so camera motion between frames is
  indistinguishable from golfer motion and will corrupt every relative value.
* Timestamps are derived from frame index assuming a constant frame rate:
  `timestamp_ms = index * 1000 / fps`. Variable-frame-rate sources, and
  containers that report no usable frame rate, will drift. The engine warns
  when the reported rate is missing, and `metadata.json` records the value used.
* The annotated video is silent. FFmpeg is invoked with `-an`, so any audio in
  the source is intentionally dropped; the exports never contain audio paths.
* Mirrored input is recorded (`--mirrored-input`, or the viewer checkbox) and
  surfaced as a warning, but the engine does **not** flip anatomy. Lead and
  trail labels always assume an unmirrored video, so a mirrored clip must be
  interpreted with lead = screen-right, or corrected before analysis.
* No validated automatic phase detection. The guided playback moments are
  experimental estimates from the measured body motion, always labelled as
  estimates and open to manual correction (see
  [docs/moment-detection.md](docs/moment-detection.md)); the engine's phase
  interface and the CLI still take manual frame indices, and the estimator
  withholds moments rather than guessing.
* No scores, ranks, ideal ranges or coaching output.
* Tracking accuracy on real golf footage is unverified. There was no golf video
  available while v0.1 was built, so the test evidence proves the plumbing, not
  the quality of tracking on a real swing.
* A full-body golfer with reasonable lighting and a mostly side-on camera is
  the intended input. Occlusion by the club, tight framing and heavy motion
  blur will all reduce validity counts.

## Next milestones

1. Validate tracking on real golf footage at address, top, impact and finish,
   and record per-frame pose quality across a real swing.
2. Automatic phase detection built on the retained landmark series, replacing
   manual frame selection while keeping the same interface.
3. Multi-view and calibration support so that rotation and depth become
   measurable.
4. Swing speed and club-aware timing, still from deterministic geometry.
5. Only then: per-swing scoring and long-term Golf IQ, using measurements that
   have been validated against real footage.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The suite covers deterministic geometry, measurement validity, quality warning
cases, handedness, portrait/landscape clips, frame rates, video corruption and
codec failure, exports, overlays, phases, and an end-to-end pipeline run with a
deterministic scripted pose provider. Integration tests against the real
MediaPipe model run automatically when `models/pose_landmarker_full.task`
exists and skip otherwise.

The viewer has its own coverage: `tests/test_ui.py` drives the real Streamlit
script with Streamlit's `AppTest` harness (upload panel, guided sample
analysis, the adjust-moments editor, provenance in the exports, reference
invalidation and the friendly failure path). `tests/test_auto_moments.py`
checks the estimator on generated movement series at several frame rates and
aspects, `tests/test_timeline.py` pins the check grouping and payload contract,
`tests/test_media.py` covers preview preparation and alignment, and
`tests/test_presentation.py` pins the checkpoint mapping, the degree symbol and
the em-dash treatment of rejected values.

For real-browser evidence, `scripts/qa_ui_screenshots.py` drives the installed
Edge in headless mode over the Chrome DevTools Protocol and writes desktop and
390px mobile screenshots plus a `capture-log.json` (rendered theme colours and
measured horizontal overflow) into `var/qa/analysis-ui/`:

```powershell
.\.venv\Scripts\python.exe -m streamlit run src/swingguru/ui/app.py
.\.venv\Scripts\python.exe scripts\qa_ui_screenshots.py --url http://127.0.0.1:8501
```

The capture run uses the local sample clip only; it is plumbing evidence, not
evidence that the model tracks a real golfer.

`scripts/qa_guided_playback.py` adds the playback-timing evidence: it records
that `currentTime` really advances, that two different checks are held at the
same video time, that pausing freezes a hold, that playback resumes, that the
speed buttons change `playbackRate` *and* the observed traversal, that the
scrubber seeks, that Prev/Next and Replay behave, and that a portrait clip and
a 390px viewport stay aligned with no overflow:

```powershell
.\.venv\Scripts\python.exe scripts\qa_guided_playback.py --url http://127.0.0.1:8501
```

Both QA scripts use the deterministic local samples (and, for the guided pass,
the scripted guided fixture) and the headless browser's autoplay policy is
relaxed for that harness only; a person pressing Play is a real user gesture.

Two environment notes:

* On managed Windows accounts the user profile can be read-only for Matplotlib.
  The engine redirects `MPLCONFIGDIR` to `.cache/matplotlib` before importing
  MediaPipe, so this fixes itself. Set `MPLCONFIGDIR` yourself to override.
* The test suite keeps its temporary directories in `.tmp/pytest` inside the
  repository, because the default user-profile temp directory is not always
  usable.

## Repository layout

```text
src/swingguru/        engine package
src/swingguru/ui/     Streamlit debug viewer
tests/                pytest suite
scripts/              small helper scripts
docs/measurements.md  measurement definitions
docs/moment-detection.md  moment estimator, thresholds and limits
docs/agent-work/engine-v01/  plan and v0.1 evidence report
docs/agent-work/analysis-ui/ plan, baseline snapshot and this redesign's report
docs/agent-work/guided-playback/  guided playback plan, baseline and report
models/               downloaded pose landmarker (not committed)
var/                  viewer working directory (uploads, runs, fixtures)
var/qa/analysis-ui/   browser QA screenshots and capture log
var/qa/guided-playback/  playback timing log and screenshots
```

## References

Official sources used for the version and API decisions in v0.1:

* MediaPipe pose landmarker for Python (Tasks API, VIDEO mode, model bundle):
  <https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker/python>
* MediaPipe Python setup requirements:
  <https://developers.google.com/edge/mediapipe/solutions/setup_python>
* Pose landmarker model bundle actually downloaded:
  <https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task>
* BlazePose landmark topology and indices (33-point model):
  <https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker>
* OpenCV video reading and writing:
  <https://docs.opencv.org/4.x/dd/d43/tutorial_py_video_display.html>
* Streamlit app testing (`AppTest`, used by `tests/test_ui.py`):
  <https://docs.streamlit.io/develop/api-reference/app-testing>
* Streamlit configuration reference (loopback binding, usage stats):
  <https://docs.streamlit.io/develop/api-reference/configuration/config.toml>
* FFmpeg H.264 encoding and `yuv420p`/`faststart` browser playback guidance:
  <https://trac.ffmpeg.org/wiki/Encode/H.264>
