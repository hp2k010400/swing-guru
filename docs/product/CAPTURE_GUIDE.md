# Swing capture requirements (V1, down-the-line)

This is what the camera screen should ask for, and when analysis should warn. It's based on the 12-swing robustness batch (`var/qa/test_swings`) and the club-tracking milestone ([`docs/agent-work/club-temporal/REPORT.md`](../agent-work/club-temporal/REPORT.md)).

The engine accepts any frame rate: rotation limits and holds scale with real time. The recommendations below are about how much the engine can *see*.

## What the batch was

- **Frame rate:** every clip was ~30 fps. There were no 60 fps clips.
- **Resolution and compression:** every clip had been through WhatsApp, which re-encodes to 576×1024 or 464×832 at roughly 1–4 Mbit/s. At that size the shaft is 1–3 px wide.
- **Camera:** 4 clips on a tripod, 8 handheld.
- **Light:** daylight, backlit under a range canopy, and floodlit at night.

## Settings

| Setting | Ask for | Accept | Why |
| --- | --- | --- | --- |
| Frame rate | **60 fps** where the phone offers it | 30 fps | Around the shaft-parallel position the club turns a median ~34° per frame at 30 fps (24–62° measured). Halving the rate (15 fps proxy) lost P2 on 5 of 8 subsampled runs. 60 fps should halve the step, but this is **untested**: there's no 60 fps footage yet. |
| Shutter / light | Bright, even light | Floodlit or overcast | Motion blur depends on exposure time, not frame rate. In low light the phone lengthens the exposure and the club smears. |
| Resolution | **1080p or better, the original file** | 720p | Upload the camera file itself, not a messaging-app copy. Re-encoding thins the shaft to 1–3 px, which is the main reason it disappears at speed. |
| Camera support | **Tripod or propped phone** | Handheld | Handheld works: camera correction recovered 7 of 8 handheld clips at Impact. But Takeaway and Mid-Back head checks are withheld on handheld clips. |

## Down-the-line framing

- **Position:** behind the golfer, looking down the target line through the hands, 3–4 m back, lens at about hip or hand height.
- **Frame:** the whole golfer and the club at the Top stay in frame; head and both feet must be visible throughout. The golfer fills about 60–75% of the frame height. Portrait is fine.
- **Stability:** the phone stays still from set-up to finish.
- **Background:** plain behind the hands if possible. Fences, roof edges and the horizon at hand height are the lines most often confused with the club.

## When analysis should warn

| Condition | Detected by | Message intent |
| --- | --- | --- |
| Not down-the-line | `camera_check.orientation_width` / `hands_in_front` | Re-film from behind the hands |
| Head or feet out of frame | `camera_check.full_body` | Step back / reframe |
| Golfer too small | `camera_check.golfer_size` | Move closer |
| Camera moved | `camera_check.camera_motion` + `camera_correction` | Results kept where corrected; some position checks withheld |
| More than one swing, or edited clip | `swing_window` (MULTIPLE_SWINGS, scene cuts) | Which swing was analysed |
| **Frame rate below 50 fps** *(to add)* | video probe | "Filmed at 30 fps: fine for all current checks; a future Takeaway checkpoint will need 60 fps" |
| **Short side under 720 px, or a messaging-app re-encode** *(to add)* | video probe (size, bitrate) | "Upload the original video for best club tracking" |
| **Low light** *(to add; threshold to calibrate)* | mean luminance / noise of Address frame | "More light gives a sharper club" |

The three *to add* rows need thresholds measured on real low-quality uploads before they are shown to golfers.
