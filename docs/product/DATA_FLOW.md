# Swing Guru — data flow, privacy and retention (V1)

## The journey of one swing

```
 iPhone                    Supabase Storage          Analysis service (Railway)          Supabase Postgres
 ──────                    (private bucket)          ─────────────────────────           ─────────────────
 1 record / choose video
   (stays in Photos;
    the app never edits it)
 2 POST /v1/analysis/jobs ──────────────────────────► checks type/size/rate limit ─────► swings row: awaiting_upload
   ◄──────────── signed upload URL (2 h) ─────────────
 3 PUT video ─────────────► <user>/<swing>/source.mov
 4 POST …/start ────────────────────────────────────► confirms upload exists ──────────► queued
 5                                                    worker claims job ───────────────► processing (+ stage progress)
                          source.mov ───────────────► temp dir in the container
                                                      engine runs (no network use,
                                                      models are in the image)
                          ◄── analysis.json, replay.mp4, thumb.jpg
                          ✕ source.mov deleted ─────────────────────────────────────► complete, source_deleted_at
                                                      temp dir deleted
 6 GET job / swings ◄───── metadata, counts, verdicts ────────────────────────────────── (owner-only)
 7 Replay: WebView ◄────── signed URLs (1 h) to analysis.json + replay.mp4
```

## What exists where

| Data | Where | Who can read it | Kept until |
| --- | --- | --- | --- |
| Original video | Supabase Storage `<user>/<swing>/source.*` | Nobody except the service (private bucket, service key) | **Deleted as soon as the analysis finishes or fails** (`SG_SOURCE_RETENTION_HOURS=0`). Uploads that never started are removed after 24 h |
| Original, while analysing | Container temp directory | The worker process | Deleted when the job ends, success or failure |
| Replay video (shorter, re-encoded clip of the swing) | Storage `replay.mp4` | Owner, via 1-hour signed URLs | Until the user deletes the swing or the account |
| Analysis (`analysis.json`: body-landmark positions, measurements, verdicts) | Storage `analysis.json` | Owner, via signed URLs / the API | Same as above |
| Thumbnail (Address frame) | Storage `thumb.jpg` | Owner, via signed URLs | Same as above |
| Swing record (status, club tag, dates, counts, file name, size) | Postgres `swings` | Owner through the API; RLS owner-only | Same as above |
| Account | Supabase Auth | Supabase | Until the user deletes the account |
| On the phone | Supabase session (SQLite-backed `localStorage`), the in-progress job ids; the original stays in Photos | The app | Session until sign-out / account deletion; job ids until the job ends |

## User controls

- **Delete a swing** (Summary → *Delete swing*): removes its storage objects (original if still present, replay, analysis, thumbnail) and its database row.
- **Delete account** (Profile → *Delete account and all swings*): removes every swing's objects and rows, then the Supabase Auth user. This is required by App Store guideline 5.1.1(v).
- **Deleting only the video while keeping the analysis** isn't offered in V1. The original is already deleted automatically; the replay clip *is* the analysis's video.

## Third parties

| Processor | Receives | Why |
| --- | --- | --- |
| Supabase (EU region recommended) | Account id, swing records, stored videos and results | Auth, database, storage |
| Railway | The video, transiently, while it's being analysed; logs (no video content, no tokens) | Hosting the analysis service |
| Apple / Expo (EAS) | App binaries and crash reports only; **no swing data** | Build and distribution |

- **Model providers:** Hugging Face and OpenMMLab models are downloaded at *image build time* only. The runtime runs offline (`HF_HUB_OFFLINE=1`) and **no user data is sent to any model provider**.
- **No analytics or advertising SDKs** are in the app.
- **Training:** swing videos are **never used to train models**. Any future training-data programme needs explicit, separate opt-in and a new privacy disclosure.

## App Store privacy labels (draft)

- **Data linked to the user:** User Content → *Photos or Videos* (swing videos, for app functionality); Identifiers → *User ID* (the anonymous account id, for app functionality).
- **Not used for tracking.** No third-party advertising.
