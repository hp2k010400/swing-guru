# Swing Guru — production deployment (V1 / TestFlight)

## 1. Architecture (decided 2026-09-30)

```
iPhone (Expo app)
  │  Supabase Auth: anonymous session (Sign in with Apple links later, same user id)
  │
  ├─► Analysis service  (one Docker container on Railway: FastAPI API + in-process worker)
  │      POST /v1/analysis/jobs ───────► creates swing row (Supabase Postgres), returns signed upload URL
  │      phone PUTs the video ─────────► Supabase Storage (private bucket "swings")
  │      POST /v1/analysis/jobs/:id/start ► status queued
  │      worker claims the job (SQL: FOR UPDATE SKIP LOCKED), downloads the video,
  │      runs the unchanged engine (scripts/build_analysis.py), stores analysis.json +
  │      replay.mp4 + thumb.jpg privately, deletes the original, marks the swing complete
  │      GET /v1/analysis/jobs/:id ────► real stages; GET /v1/swings[/:id] ► Library / Summary
  │
  └─► Replay: WebView loads /v1/viewer/index.html with short-lived signed URLs
```

**Why this, and not something else**

| Option | Verdict for V1 |
| --- | --- |
| **Railway (chosen)** | Deploys the Dockerfile from GitHub or the CLI. Gives logs, one-click rollback to any previous deployment, env-var UI and a health-checked deploy. Billing is usage-based and scales down to a small idle cost. One always-on process runs the API and the worker, so there's no extra queue service: the job queue is a Postgres table. No request/job duration limits matter, because jobs are asynchronous. |
| Google Cloud Run | Good fit on cost (scale to zero, per-second billing, 60-minute request limit). But background work after a response needs either instance-based CPU billing or Cloud Tasks / Cloud Run Jobs. That's more moving parts than V1 needs. **The first migration target** if scale-to-zero matters: the same Dockerfile runs there. |
| Render | Works, but the API and a background worker are two paid services at fixed instance prices. |
| Fly.io | Works; slightly more operations (machines, regions, volumes) for no V1 benefit. |
| Modal | Excellent for bursty CPU/GPU batch work, but it couples the worker to Modal's SDK and deploy model. Worth revisiting if a GPU or a club keypoint model arrives. |
| RunPod / AWS | GPU-first or too much setup for V1. |

**Data/auth: Supabase.**

- **Auth:** anonymous sign-in now; Apple sign-in links to the same user later.
- **Database:** Postgres with RLS.
- **Storage:** a private bucket with signed URLs.
- **Payments:** RevenueCat can be added later against the same Supabase user id.
- **Access:** the app talks only to the analysis service. It uses Supabase only to sign in.

**Scaling path, no rewrite needed:**

1. Raise the replica count. Job claims are atomic, so several workers are safe.
2. Move the worker to its own service (`SG_WORKER_ENABLED=false` on the API service).
3. Move to Cloud Run Jobs or Modal only if cost or GPU demands it.

## 2. Components in the repo

| Path | What |
| --- | --- |
| `src/swingguru/service/` | API (`api.py`), worker, auth (Supabase JWT via JWKS or HS256), storage (Supabase / local), repo (PostgREST / SQLite), config |
| `src/swingguru/analysis_runner.py` | The shared analysis run: validation → swing finder → engine → rejection rules |
| `Dockerfile`, `requirements/service.txt`, `scripts/fetch_models.py` | Image with every model baked in (MediaPipe, RTMW, OWLv2); runs offline |
| `railway.json` | Dockerfile build; health check `/ready` (database reachable + models present) |
| `supabase/migrations/20260930000000_swings.sql` | `swings` table, RLS, `claim_swing_job()`, private `swings` bucket |
| `.env.example` | Every service environment variable |
| `mobile/` | The Expo app (`mobile/.env.example`, `mobile/eas.json`) |
| `scripts/smoke_service.py` | End-to-end check of any running service, exactly as the phone uses it |

## 3. One-time setup

**Supabase**

1. Create a project in a region near your users (EU West for the UK).
2. Apply the migration: `supabase link --project-ref <ref>` then `supabase db push`, or paste the SQL file into the SQL editor.
3. Go to Authentication → Sign In / Providers:
   - enable **Anonymous sign-ins**;
   - leave email/password on if you want a fallback;
   - configure **Apple** before App Store release.
4. Enable CAPTCHA or turnstile for anonymous sign-ins once the app is public; that's Supabase's recommended abuse control.
5. Note these values:
   - `Project URL` (for `SUPABASE_URL` and `EXPO_PUBLIC_SUPABASE_URL`);
   - the **publishable/anon key** (mobile only);
   - the **service role / secret key** (the service only; never in the app).
6. If the project still uses legacy HS256 JWTs, set `SUPABASE_JWT_SECRET`. Otherwise leave it empty and the service verifies tokens against the project's JWKS.

**Railway**

1. Create a New Project from the GitHub repo (or `railway up` from this folder). It builds `Dockerfile` using `railway.json`.
2. Choose a service size with **at least 2 vCPU and 4 GB RAM**. Peak memory with OWLv2 loaded is roughly 2.5–3 GB, which is an estimate to confirm in the Railway metrics.
3. Add the variables from `.env.example`:
   - `SG_ENV=production`, `SG_AUTH_MODE=supabase`, `SG_STORAGE=supabase`, `SG_DB=supabase`;
   - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`;
   - `SG_PUBLIC_URL` = the Railway public domain (https).
4. Generate a public domain. The deploy only goes live when `/ready` returns 200. The service refuses to start with an unsafe configuration, such as dev auth, http, or a missing key.
5. Run the smoke test:

   ```
   python scripts/smoke_service.py --api https://<domain> --video <a swing.mp4> \
       --supabase-url https://<ref>.supabase.co --anon-key <anon key>
   ```

   It signs in anonymously, uploads, analyses, checks the Summary and the signed replay URLs, confirms another user gets 404, and deletes the swing.

**Mobile (EAS)**

1. Run `cd mobile && npx eas-cli@latest login`, then `npx eas-cli@latest init`. This links the Expo project and writes `projectId`.
2. Set the public variables per environment with `npx eas-cli@latest env:create`: `EXPO_PUBLIC_API_URL`, `EXPO_PUBLIC_SUPABASE_URL`, `EXPO_PUBLIC_SUPABASE_ANON_KEY`, `EXPO_PUBLIC_AUTH_MODE=supabase`, and the privacy/support URLs.
3. Build:
   - `npx eas-cli@latest build --platform ios --profile preview` for internal devices;
   - `--profile production` for TestFlight.
   - Both need an Apple Developer account; EAS manages signing.
4. Submit with `npx eas-cli@latest submit --platform ios --latest`, which uploads to App Store Connect / TestFlight.

## 4. Local vs production

| | Local development | Production |
| --- | --- | --- |
| Run | `set PYTHONPATH=src` then `python -m swingguru.service` (port 8790) | Railway container |
| Auth | `SG_AUTH_MODE=dev`: bearer `dev:<id>` (refused in production) | Supabase JWT (anonymous → Apple) |
| Database | SQLite `var/service/swings.sqlite3` | Supabase Postgres |
| Storage | `var/service/objects` + HMAC-signed URLs served by the service | Supabase Storage private bucket + signed URLs |
| Mobile | `mobile/.env`: `EXPO_PUBLIC_API_URL=http://<PC LAN IP>:8790`, `EXPO_PUBLIC_AUTH_MODE=dev`; `npx expo start` + Expo Go or a dev build | EAS build with production env |

The local browser app (`scripts/serve_app.py`) is unchanged and still uses `var/`. It's a development and review tool, not the product.

## 5. CPU threads and speed (measured 2026-10-01)

The Railway container sees the host's 48 cores, but its cgroup quota is **8 CPUs** (`cpu.max 800000 100000`, 8 GB).
Left alone, ONNX Runtime (RTMW pose) and OpenCV sized their thread pools from the 48 host cores and oversubscribed
the quota, and torch (OWLv2) was capped at 2 threads. The image therefore fixes every pool at 6:

| Variable | Used by | Value |
| --- | --- | --- |
| `SG_ORT_THREADS` | RTMW pose sessions (`pose_rtm._ort_threads`) | 6 |
| `OMP_NUM_THREADS` | torch / OWLv2 club fallback | 6 |
| `OPENCV_FOR_THREADS_NUM` | OpenCV (decode, classical club tracker) | 6 |

**Why 6.** On the reference laptop, RTMW keypoints are bit-identical at 2, 4 and 6 threads, but **8 threads changes
them** (float reduction order); OWLv2 boxes are identical at 2, 4 and 6. Do not raise these without re-running
`scripts/bench_pose_threads.py` and comparing verdicts. Results can still differ between CPU models (Railway hosts
vary between AMD EPYC and Intel Icelake), which is the remaining determinism item in the TestFlight checklist.

Measured end to end (`scripts/smoke_service.py`, from the UK, same swing):

| Setting | Untrimmed 19.6 s clip | Same swing trimmed to 6 s | Pose stage (trimmed) |
| --- | --- | --- | --- |
| Before: ORT unset, torch 2 | 156 s | 174 s | 121.5 s |
| ORT 6, torch 2, OpenCV unset | 99 s | 93 s | 23.1 s |
| **ORT 6, torch 6, OpenCV 6 (current)** | **58 s** | **47–56 s** | **9.6–10.4 s** |
| Current, 60 fps 6 s clip | | 60 s | 18.3 s |

Stage timings for every job are in the logs (`job <id> timings {...}`): download, swing finding, cut, the engine's own
stages (pose, club classical, OWLv2 load/inference, club temporal, preview), thumbnail, storage writes, database write.

## 6. Logs, monitoring, rollback

- **Logs:** Railway → service → *Logs*. One line per request (request id, route, status, ms) and per job:

  ```
  job <id> claimed / rejected: <code> / timings {...} / finished in N s
  ```

  At startup: `cpu: os.cpu_count=… cgroup cpu.max=… SG_ORT_THREADS=… OMP_NUM_THREADS=…`.

  Crashes log a stack trace server-side only; clients get `{"error":{"code":"server_error"}}` and the `X-Request-Id` to quote.
- **Health:**
  - `/health` is liveness;
  - `/ready` is readiness (database reachable, every model file present).
- **Rollback:** Railway → *Deployments* → pick the previous deployment → *Redeploy*. Database migrations are additive; keep them backward-compatible so a rollback never needs a down-migration.
- **Stuck jobs:** a job whose worker died stops heartbeating. After 15 minutes it's claimed again, at most twice, and then marked failed.

## 7. Cost model (rough — see the assumptions)

**Measured:**

- 58–107 s per analysis on a 6-core laptop CPU (Ryzen 5 7530U);
- about 110 KB of `analysis.json`;
- about 3 MB of replay video, 11 MB at most in the batch;
- test originals averaged 3 MB, but they were WhatsApp-compressed.

**Assumed, not benchmarked:**

- A 2-vCPU cloud container takes 1.5–2.5× longer: **about 2.5–4 min and roughly 6–8 vCPU-minutes per analysis**.
- About 3 GB peak memory for about 4 minutes.
- iPhone originals are 10–40 MB and are deleted after analysis.
- Each swing's replay is viewed about 5 times.
- Railway's published usage rates are about $20 per vCPU-month and $10 per GB-month.
- Supabase Pro is $25 per month and includes 100 GB storage and 250 GB egress.
- **Check both providers' current price pages before relying on these.**

| Per analysis | Estimate |
| --- | --- |
| Compute (7 vCPU-min + 12 GB-min) | ≈ $0.006 |
| Stored per swing (replay + JSON + thumb) | ≈ 3–4 MB |
| Egress (5 replay views) | ≈ 15–20 MB |

| Monthly | Railway (incl. about $5 idle for the API process) | Supabase | Total |
| --- | --- | --- | --- |
| 100 analyses | ≈ $5 (Hobby plan with its included usage) | $0 on Free, but Free projects pause when idle, so use Pro for TestFlight: $25 | **≈ $5–30** |
| 1,000 analyses | ≈ $11 usage (Pro plan minimum $20) | $25 (about 4 GB added storage, about 20 GB egress) | **≈ $45–50** |
| 10,000 analyses | ≈ $70–150 (1 → 2–3 replicas at peaks; 10k × 3 min ≈ 0.7 worker-equivalents on average) | $25–40 (storage grows about 40 GB per month) | **≈ $100–200** |

Before 10k/month:

- benchmark the container, then decide on the instance size (4 vCPU may finish in half the time for similar cost);
- set a storage lifecycle for old replays.

**No GPU is needed for V1.**
