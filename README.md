# Swing Guru

AI golf swing coach, live on the iOS App Store. Film a swing on your phone; the app returns the key positions of the swing, body and club measurements at each one, a score, and drills for what to fix.

Built solo, end to end: computer-vision engine, inference API, billing and accounts, and the React Native app.

> Source shared for portfolio review. All rights reserved: not licensed for reuse.

## How it works

```
iPhone app (Expo / React Native)
  record or import -> on-device trim (native Swift module) -> upload
        |
        v
FastAPI service on Railway (CPU only)
  auth (Supabase, Sign in with Apple) -> job queue -> worker
        |
        v
Analysis engine (src/swingguru)
  1. Pose: RTMW whole-body keypoints via ONNX Runtime (MediaPipe as fallback / segmentation)
  2. Swing window: find the swing inside a longer clip
  3. Moments: 7 positions (Address, Takeaway, Mid-Back, Top, Mid-Down, Impact, Finish)
     from the measured hand path
  4. Club: classical shaft tracker; OWLv2 open-vocabulary detection only on key
     frames the tracker cannot confirm
  5. Measurements: deterministic, aspect-corrected 2D geometry with validity and
     quality rules (camera-angle checks, pelvis depth, head movement, swing plane)
  6. Scoring + interpretation: rule engine turns facts into plain-language feedback
        |
        v
Results, swing history, side-by-side compare, drills and progress in the app
```

## Engineering notes

- **Honest output.** Every measurement carries a quality flag; anything the engine cannot see reliably is withheld rather than guessed. Impact is labelled an estimate, never claimed as ball contact.
- **Expensive models only where needed.** OWLv2 runs on a handful of key frames, never the whole video, so analysis stays fast on CPU-only hosting.
- **Deterministic tests.** 80 pytest modules, including synthetic swing clips and a scripted pose provider, so CV logic is tested without real video or model downloads.
- **Production ops.** Railway infrastructure as code (`.railway/`), separate staging and production, health/readiness checks, restart policy and one-step rollback.
- **Monetisation.** StoreKit subscriptions with free trials, server-side entitlement and monthly allowance checks (`src/swingguru/service/billing`), Apple account-deletion compliance.
- **Product analytics.** Opt-in PostHog events drive onboarding and paywall experiments.

## Repository layout

| Path | What |
| --- | --- |
| `src/swingguru/` | analysis engine: pose, moments, club tracking, measurements, scoring, interpretation |
| `src/swingguru/service/` | FastAPI API, worker, auth, billing, storage |
| `mobile/` | Expo / React Native iOS app, incl. a native Swift trim module |
| `tests/` | pytest suite |
| `supabase/` | database migrations |
| `docs/` | engine, measurement and moment-detection docs; deployment guide |
| `third_party/golfdb/` | SwingNet research code kept for future benchmarking (CC BY-NC 4.0, not used by the product; weights not included) |

## Run the engine locally

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m swingguru.cli download-model
.\.venv\Scripts\python.exe -m pytest
```

See [docs/ENGINE.md](docs/ENGINE.md) for the full engine guide.
