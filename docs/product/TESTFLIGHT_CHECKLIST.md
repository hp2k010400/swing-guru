# Swing Guru — TestFlight / App Store checklist

Status as of 2026-10-01. ✅ done in the repo · ⬜ to do · 👤 needs you (accounts, money, legal).

## BLOCKERS (before the first TestFlight build)

**Backend**

- ✅ Production API, job model, async worker, private storage, owner isolation, deletion (`src/swingguru/service/`, 11 service tests).
- ✅ Dockerfile with models baked in, `railway.json`, Supabase migration, `.env.example`.
- ✅ Supabase project `nbshkknpobvaqqatvmwz` (Free plan): migration applied, anonymous sign-ins on, ES256 signing keys (no JWT secret needed).
- ✅ Railway project `swing-guru`, service `swing-guru-api`, live at https://swing-guru-api-production.up.railway.app (deployed 2026-10-01; `/ready` passes: database and models).
- ✅ Production smoke test passed: anonymous sign-in → upload → cloud analysis → Summary.
- ✅ Speed: thread pools fixed at 6 (the container saw 48 host cores on an 8-CPU quota). A trimmed 30 fps swing now takes 47–56 s end to end (was 174 s); 60 fps about 60 s. See `PRODUCTION.md` §5.
- 👤 Run `supabase/migrations/20261001000000_club_list.sql` in the Supabase SQL editor (the full club list). Until then, choosing a club other than Driver / 3 Wood / 5 Wood / Other fails to start the analysis.
- ⬜ Check **CORS on Supabase signed URLs** from the replay WebView on a real device.
- ⬜ **Engine determinism:** the same video gave Address frame 75 on Railway and 72 on the laptop, which changed some set-up and plane verdicts. Part of the cause was the unpinned pose thread count (now pinned at 6, the setting that matches the laptop's keypoints); the CPU model can still differ between Railway hosts. After pinning (2026-10-01) one swing gave identical moment frames on Railway and the laptop and all 21 verdicts matched (values within 1° / 0.5%). Confirm on more swings, and make the Address pick robust across the still set-up, before testers compare results.
- ⬜ Upgrade Supabase to Pro before external testers (Free projects pause when idle).

**Mobile build**

- ✅ Expo SDK 57 app: tabs, Analyze sheet (record / choose) → **Trim** (drag handles, plays the selection, default placed on the swing) → **Club** (full list, before upload) → upload of the trimmed selection only (lossless on-device cut, `modules/swing-trim`) → processing, Summary, Library, replay (WebView, instant open with loading state), delete swing / account. `tsc`, lint and `expo-doctor` are clean; the iOS bundle exports.
- 👤 **Apple Developer Program** membership ($99/year) and an **Expo account**.
- ⬜ Run `eas init`, set the EAS environment variables, and build `--profile production`.
- ⬜ Confirm the bundle identifier (`app.swingguru.ios` is a placeholder) and the app name in App Store Connect.
- ⬜ **Real-device test** of the full loop on an iPhone: record, choose from Photos, trim (default lands on the swing? handles, playback loop, 2 s minimum / 10 s maximum), club, 60 fps and 4K originals (trimmed file keeps 60 fps), background the app mid-upload, kill the app mid-analysis and reopen, airplane mode, replay (thumbnail shows at once, video starts), replay deep link, change club from the Summary, delete swing, delete account.

**Privacy / legal (needed even for external TestFlight)**

- ⬜ Privacy policy page (use `docs/product/DATA_FLOW.md`) and a support page; set `EXPO_PUBLIC_PRIVACY_URL` / `EXPO_PUBLIC_SUPPORT_URL`.
- ⬜ App Store Connect privacy "nutrition label" (draft in `DATA_FLOW.md`).
- ⬜ Beta app description, and "what to test" notes for external testers.

## REQUIRED BEFORE APP STORE

- ⬜ **Sign in with Apple** (link to the anonymous user: `supabase.auth.linkIdentity`). This is needed so swings survive reinstalls or a new phone. Apple also requires it if any other third-party login is offered.
- ⬜ Anonymous sign-in abuse protection: Supabase CAPTCHA, plus a global jobs-per-day ceiling.
- ⬜ Terms of use; age rating questionnaire; export compliance (`ITSAppUsesNonExemptEncryption=false` is set: HTTPS only).
- ⬜ App icon (1024 px), launch screen, and brand colours in `app.json` (the template icon is a placeholder).
- ⬜ Screenshots (6.9″ and 6.5″ iPhone), subtitle, keywords, promotional text, category (Sports).
- ⬜ App Review notes. Explain that analysis runs on a server, how to test it (a sample swing video attached), and that there's no login wall (anonymous account).
- ⬜ Sentry or similar for app and service error reporting (not analytics).
- ⬜ Subscriptions via **RevenueCat** keyed on the Supabase user id, if Pro launches with 1.0. That needs the paywall, restore purchases and terms links (guideline 3.1.2).
- ⬜ Rate limits reviewed against real usage, and cost alerts on Railway and Supabase.

## CAN WAIT UNTIL AFTER BETA

- ⬜ In-app 60 fps recorder (`react-native-vision-camera` format selection). V1 uses the system camera plus Photos import, with guidance to record at 60 fps in the Camera app.
- ⬜ A native replay renderer in place of the WebView viewer (only if the WebView proves limiting).
- ⬜ Push notification when an analysis finishes (Expo Notifications).
- ⬜ Home / Drills / Profile parity with the web design (the product design is done; the native screens are functional versions).
- ⬜ Offline cache of the Library and thumbnails.
- ⬜ Moving the worker to its own service or Cloud Run when volume justifies it.
- ⬜ Android build (the code is cross-platform; not tested).
