# Swing Guru app — product structure, design system and build plan

This is Phase 1 of the app-shell work: the audit, the information architecture (IA), the design system and the implementation plan. Phases 2–5 build against this.

## 1. Current Swing Guru state

| Area | State | Treatment |
| --- | --- | --- |
| Analysis engine (`src/swingguru/*`, `scripts/build_analysis.py`) | Frozen for V1: pose, phases, club, camera correction, verdicts | **Untouched.** The app only reads `analysis.json`. |
| Guided viewer (`src/swingguru/viewer/`) | Premium guided playback, overlays, holds, per-check card, basic results page | **Kept.** It is embedded as the Replay screen. The only changes are a small "embedded" hook so the app can open a summary / close the viewer. |
| Verdict layer (`verdicts.py`) | Locked vocabulary; every verdict has a status (good / attention / problem / info) and `validation: provisional_ui_mapping` | Source of every coloured label in the app. No new verdicts. |
| Interpretation layer | No validated rule, so `focus = null` | The app's "Start here" is a *presentation ordering* (section 4), not a coaching claim. |
| Streamlit UI (`src/swingguru/ui/app.py`) | Developer prototype | Left as a dev tool; not part of the product. |
| Review pack (`scripts/serve_review.py`, `var/qa/human_review/`) | QA tooling | Untouched. |
| Mobile app shell | **Does not exist** | Built from scratch: `src/swingguru/app/` + `scripts/serve_app.py`. |

**Data the product can honestly use today:**

- per swing: date (from the capture), video, 8 guided checkpoints with verdicts, hand position (info only) and camera quality;
- across swings: count, trend and bests.

**Stubbed (clearly marked):** club per swing (the user tags it), user name, subscription and analyses remaining, reminders, drill videos.

## 2. Swing Sensei screenshots — synthesis

**Their structure:**

- **Tabs:** Home · Library · (+) · Drills · Profile. The raised centre "+" opens a sheet: Live Range Mode / Analyze a Swing / Practice Session.
- **Home:** greeting and streak → Today's focus (watch drill / record) → feature promo (Range Mode) → giveaway → invite friends → performance stats → achievements → free analyses left → improvement program (2/3) → score trend → recent swings.
- **Result:** score ring (8.2/10, "Calibrated on tour swings · PGA-reviewed") → coach paragraph → Coach's priority · fix this first → promo → Replay analysis → Breakdown by phase (a coloured verdict per row, "Fix this: <drill>") → Focus areas (drill cards) → share-to-earn → premium trial → Done.
- **Drills:** priority drill (free) → focus areas (locked) → a 14-video drill library grouped by phase, each with Watch and Drill details.
- **Profile:** identity → 3 stats → preferences (handedness, reminders) → subscription → storage → help → about → account (sign in, friends and leaderboard).

**What they do well — copy:**

- One priority ("fix this first"), and every fault links straight to a drill.
- A calm palette (off-white background, one green, amber/coral for faults) and generous, well-rounded cards.
- A strong loop: result → drill → record again, with the score trend on Home.
- Scores on swing thumbnails and a PR badge make history feel rewarding.
- A raised centre action: recording is always one tap away.

**What is average or weak — improve:**

- **Home is cluttered.** Nine modules, four of them promotional (range mode, giveaway, friends, free analyses) — the focus gets lost.
- **Their score and verdicts over-claim.** "Calibrated on tour swings · PGA-reviewed", "Hand depth good" and "Takeaway: On plane" are not supportable from 2D phone video (see `docs/agent-work/club-temporal/REPORT.md`). Swing Guru shows only supported checkpoints, and says how the score is made.
- **The result screen is long and loses its thread:** priority, promo, replay, breakdown, focus, share, trial.
- **Monetisation feels heavy:** share-to-earn, giveaway and trial on the same screen as the result.
- **Personalised drills are locked**, so the core loop (fault → fix) is behind the paywall.
- **Visual noise:** duplicate status dots, the plane "fix this" repeated in three places, and inconsistent chip styles.

**What Swing Guru does better:**

- The replay *is* the product: live overlays, one checkpoint at a time. The summary always leads back into it.
- Honest labels, with the reason one tap away.
- A shorter result: score → one issue → replay → breakdown → one drill → done.
- The fault → drill loop is free; Pro sells volume and history depth.

## 3. V1 information architecture

The bottom bar has five items with a raised centre action: **Home · Library · (+) Analyze · Drills · Profile**. This matches Sensei because it's the right model: two "look back" tabs, one "act" button and two "improve / me" tabs.

| Tab | Purpose | Key modules | Primary CTA | Data | Free vs Pro (V1) |
| --- | --- | --- | --- | --- | --- |
| **Home** | "What should I do today?" | greeting + streak; **Today's focus** (checkpoint + drill + record); latest swing (score, change, replay); progress (score trend; program ring unlocks after 3 swings); recent swings; one quiet Pro card | Record a swing | latest summary, trend, swing count, streak (from dates) | all free; the Pro card is just information |
| **Library** | "My swing history" | large title; search + club filter chips; grouped This week / Earlier; 2-up swing cards (thumbnail, score pill, PR badge, club, date, main issue); empty state | Tap a card → summary; ▶ → replay | all analysed swings | free: all swings; Pro: unlimited history (limit stubbed off) |
| **(+) Analyze** | Get a swing in | bottom sheet: Record a swing, Upload a video, Filming tips (from `CAPTURE_GUIDE.md`); then processing → summary | Record | — | free: N analyses per month (**stub**, off by default); Pro: unlimited |
| **Drills** | "How do I fix it?" | **Your priority drill**; **For your swing** (drills for each amber/coral checkpoint in the latest swing); library by phase (Set-up / Backswing / Downswing & impact); drill detail sheet (why, steps, reps, checkpoint it trains) | Start drill | drill catalogue (text; videos marked "coming") | priority + focus drills free; Pro: guided programs (stub, locked) |
| **Profile** | Me and settings | identity; stats (swings, average, best); preferences (handedness, reminders); plan (free / Pro, analyses left); help (filming tips, how scores work); about (version, acknowledgements); account (sign in — coming soon) | Upgrade (quiet) | profile + stats | — |
| **Summary** (after analysis, or from Library) | "How did that swing go and what's next?" | close; score ring + "How is this calculated?"; one-sentence summary; **Start here** (one issue → one drill); **Replay analysis**; breakdown by phase (tap a row → replay); hand position (info); record another | Replay analysis / Start drill | `analysis.json` | free |

**The flow:**

1. Record/upload.
2. Processing (steps shown).
3. **Summary**.
4. Replay (guided viewer), back to the summary.
5. Drill.
6. Home shows today's focus plus trend.
7. Library holds history.

**Deliberately *not* in V1:**

- giveaway and leaderboard/friends;
- share-to-earn;
- range mode;
- achievements (streak and PR cover motivation);
- a Takeaway verdict and a hand-depth verdict (not supported).

## 4. Honest product rules

**Decided on 2026-09-30.** These rules replace the proposals kept below for the record.

1. **Swing Score is not calculated yet.**
   - `src/swingguru/app_model.py` defines the `ScoreResult` interface (`swing_score/1`: total / 10, per-check contributions, confidence, version, supported measurement count, and the reason when there's no score).
   - The production scorer, `UncalibratedScorer`, always returns "not available" with a reason.
   - `StubScorer` returns demo numbers derived from the swing id, never from its measurements, flagged `source: "stub"`.
   - The same applies to anything built on the score: PR, trend, average and best are stubs.
2. **"Start here" in production** comes only from an interpretation rule that is eligible for priority (`interpretations.focus`). Otherwise it shows the neutral message: "Your focus will appear here as Swing Guru learns your swing."
   - A flagged demo stub exists (the first checkpoint out of range in swing order, labelled as not a coaching priority).
   - `PriorityInputs` is the hook for a future deterministic priority model: severity, confidence, phase importance, rule priority and repeat count. It never compares raw measurements.
3. **Drills:** every drill and mapping is `draft_needs_coach_review`. The app says "Preview drill · not yet reviewed by a coach" and never implies professional review.
4. **Visibility:**
   - Demo stubs are on by default in the preview server. `--production` (server) or `?demo=0` (page) shows the honest states.
   - `?dev=1` outlines every stubbed value in violet dashes, with a "STUB · …" label.

### Original proposals (superseded)

1. **Swing Guru score (0–10)** is the average of the supported guided checkpoints that have a verdict:
   - in range = 10, slightly out = 6, clearly out = 2;
   - informational checks are excluded;
   - shown with one decimal and "based on N checkpoints".

   "How is this calculated?" states exactly this, and that it isn't a comparison with tour players. It reflects the provisional bands (`verdicts.py`); nothing more is claimed.
2. **Start here** is the checkpoint furthest outside its range: clearly out before slightly out, then by distance from the good band relative to the band's width. It's labelled "Start here", not "Coach's priority". The interpretation layer's `focus` stays null until a rule is validated.
3. **Summary sentence** is generated from the verdicts only (for example: "5 of 8 checkpoints in range. Your set-up is solid; start with Impact extension."). No invented praise or history claims.
4. **Drills** are general, well-known practice drills written in plain words, tagged to the checkpoint they train. They're marked *draft — needs coach review* internally. No invented coach names or videos.
5. **Stubs** (club, plan, analyses left, reminders, sign-in) are visibly stubbed in dev (`?dev=1` shows a STUB tag) and live in one file (`app/js/data/stubs.js`).

## 5. Design system

The palette continues the viewer's (the brand green `#2f6f4e`, warm off-white), extended for a full app.

| Group | Values |
| --- | --- |
| **Colour – surfaces** | bg `#f6f4ef` · surface `#ffffff` · surface-2 `#f1efe9` · line `#e7e3da` |
| **Colour – ink** | ink `#17201b` · ink-2 `#5d665f` · ink-3 `#9aa19b` |
| **Colour – brand** | green `#2f6f4e` · green-deep `#1f4d36` · green-ink `#24563c` · green-soft `#e7f0ea` · green-tint `#f1f6f2` |
| **Colour – premium accent** | gold `#b98b2f` · gold-soft `#f6eedb` (used only for Pro/PR) |
| **Colour – status** | good `#3b9466` · attention `#d39a34` · problem `#d9664f` · info `#a2a79f`, each with a soft tint for chips |
| **Type** | system SF / Segoe. Large title 34/700 · title 1 28/700 · title 2 22/650 · headline 17/600 · body 16/400 · callout 15/400 · subhead 14/500 · footnote 13/400 · caption 12/600 uppercase +0.06 em |
| **Spacing** | 4-pt scale: 4 · 8 · 12 · 16 · 20 · 24 · 32 · 40; screen gutter 20; card padding 18–20; section gap 28 |
| **Radius** | 10 (chips) · 14 (rows, buttons) · 20 (cards) · 26 (hero, video) · pill |
| **Elevation** | card: 1 px line + a very soft shadow (`0 1px 2px` / `0 8px 24px`, 4–5 % ink); raised: tab-bar centre button, sheets |
| **Buttons** | primary (green, 52 px, pill) · secondary (green-soft / green-ink) · quiet (text) · premium (gold outline) · icon (40 px round) |
| **Cards** | default (white) · tint (green-tint) · hero (deep green gradient, white text) · premium (white with gold hairline) · swing card (media 3:4, score pill) |
| **Chips** | status chip (dot + label, tinted) · tag chip (phase / club) · badge (FREE / NEW / PRO / PR, 11 px caps) |
| **Navigation** | large-title headers that collapse into a compact bar on scroll · bottom tab bar (blurred surface, 5 slots, raised 58 px green centre button) · bottom sheets (grabber, 26 px top radius) |
| **States** | empty (icon in a soft circle, title, one line, one CTA) · loading skeletons · locked (lock glyph + one line + quiet upgrade) |

## 6. Implementation plan

- **Stack:** a vanilla HTML/CSS/JS single-page app, the same approach as the viewer. There are no build tools (work laptop, no installs). Hash routing, ES modules, one `tokens.css`, a component CSS file and small render functions.
- **Server:** `scripts/serve_app.py` (port 8780, byte ranges for video, the same pattern as `serve_review.py`) serves:
  - the app at `/`;
  - swing bundles at `/swings/<id>/…`;
  - a small JSON API (`/api/swings`, `/api/swings/<id>`, `/api/profile`, and later `/api/analyze`).
- **Library data** comes from the real analysed bundles (`var/qa/test_swings/*/bundle` today, `var/app/swings/*` for new uploads).
- **Product logic** (score, start-here, summary sentence, trends) lives in a tested Python module, `src/swingguru/app_summary.py`.

| Phase | Deliverable |
| --- | --- |
| 1 | This document |
| 2 | Tokens, components, app shell (tab bar, headers, sheets, routing), `serve_app.py`, the system gallery (`#/system`), placeholder screens with proper empty states, screenshot QA |
| 3 | Home, Library (real swings + thumbnails), Drills (catalogue + detail sheet), Profile — plus `app_summary.py` and its tests |
| 4 | **Done (2026-09-30).** Summary screen; replay integration (viewer in an embedded mode, summary ↔ replay, jump to checkpoint); Analyze sheet → upload → processing → summary; club tags. See the notes below. |
| 5 | Polish: spacing, empty/locked/loading states, motion, accessibility, mobile screenshot QA of every screen |

## Phase 4 notes (built)

**Flow:**

1. Analyze sheet: the file is POSTed to `/api/analyze`.
2. `swingguru/app_jobs.py`:
   - reads and validates the video;
   - finds the swing (`swing_window`), rejecting a clip with no still set-up before any body tracking runs;
   - runs the unchanged `scripts/build_analysis.py`;
   - rejects camera-angle, framing or no-swing results;
   - moves the bundle to `var/app/swings/<id>/bundle`.
3. `#/processing/<job>` shows the stages as their files appear. No percentage is invented.
4. The Summary (`#/swing/<id>/new`) opens.

**Replay:**

- `#/replay/<id>[/<phase:kind>]` embeds the unchanged viewer (`?embed=1&check=`).
- The viewer's back button and the end of the walkthrough post `close` / `ended` to the app, which returns to the Summary at the same scroll position.
- The replay plays the cached bundle; nothing is re-analysed.
- The same file uploaded twice is recognised by its SHA-1 and not analysed again.

**Club tags:** stored in `var/app/swing_meta.json`, set with `POST /api/swings/<id>/meta`. Choices: Driver · 3 Wood · 5 Wood · Hybrid · Irons · Wedges · Other.

**QA:**

- `scripts/qa_app_flow.py` drives the real file input in headless Edge. Screenshots go to `var/qa/app/phase4*`.
- `scripts/qa_app_screens.py` captures any route (`processing/<job>`, `swing/<id>`, …).
