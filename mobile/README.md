# Swing Guru — iOS app (Expo SDK 57, TypeScript, Expo Router)

The production mobile client. The hosted analysis service is `src/swingguru/service`; see `docs/deployment/PRODUCTION.md`.

| Screen / route | What it does |
| --- | --- |
| `(tabs)/index` Home | In-progress analyses (persisted), latest swing, recent swings |
| `(tabs)/library` | Analysed swings with thumbnails; club filter; pull to refresh |
| `analyze` (sheet) | Record with the camera or choose from Photos (original file, `Passthrough`), with filming tips |
| `processing/[id]` | Uploading → the server's real stages; survives leaving, backgrounding and restarts |
| `swing/[id]` Summary | Real verdicts, breakdown (tap → replay at that check), club tag, drills, delete |
| `replay/[id]` | The guided viewer in a WebView, with short-lived signed URLs |
| `(tabs)/drills`, `(tabs)/profile` | Drill library; privacy, support, delete account |
| `paywall` | Plans, App Store prices, free-trial eligibility, restore and manage |

Key files:

- `src/lib/api.ts`: API v1 client.
- `src/lib/analysis.ts`: pick → job → background upload → start; pending-job store.
- `src/lib/session.ts`: Supabase anonymous session, or a dev id.
- `src/lib/billing.ts`: RevenueCat boundary (configure per Supabase UUID, purchase, restore).
- `src/lib/billing-core.ts`: pure price/trial/access decisions, exercised by `tests/`.

Billing is off unless `EXPO_PUBLIC_REVENUECAT_IOS_API_KEY` is set *and* the server has
`SG_BILLING_MODE=on`; see `docs/agent-work/revenuecat/SETUP.md`.

## Run locally (against the service on your PC)

```bash
# terminal 1 (repo root): the analysis service in development mode
set PYTHONPATH=src && .venv\Scripts\python.exe -m swingguru.service

# terminal 2
cd mobile
copy .env.example .env   # then set EXPO_PUBLIC_API_URL=http://<your PC's LAN IP>:8790 and EXPO_PUBLIC_AUTH_MODE=dev
npx expo start           # scan the QR code with Expo Go on the iPhone (same Wi-Fi)
```

## Checks

```bash
npx tsc --noEmit && npx expo lint && npx expo-doctor
npx expo export --platform ios   # bundles the JS exactly as a build would
npm test                         # node:test unit tests for the billing decisions
```

## Build for TestFlight

1. `npx eas-cli@latest init`, then set the `EXPO_PUBLIC_*` variables in EAS for the environment.
2. `npx eas-cli@latest build --platform ios --profile production`
3. `npx eas-cli@latest submit --platform ios --latest`
