# Swing Guru vs Swing Sensei — gap analysis

Date: 2026-09-28. Compared against `swing-sensei-teardown.md` (public facts and
screenshot inference only). This compares the build *before* the changes made
in the same session; the "after" column records what changed.

Ratings: **close**, **needs work**, **materially behind**, **not implemented**.

| Area | Swing Guru before | Rating before | After this session |
| --- | --- | --- | --- |
| Playback smoothness | Real video with holds, but the hold fires only after `currentTime` has passed the moment, so every hold paused late and **seeked backwards** (a visible jump). Every check completion posted a value to Streamlit, forcing a **full app rerun** (18 per walkthrough) with Streamlit's stale-dim flicker. | materially behind | Frame-predictive stop with `requestVideoFrameCallback` (measured on the real swing: 7 holds, 0 backward seeks, largest forward step 1.9 frames because re-encoded frame times are not exact multiples of 1/fps; 816 time samples never went backwards), no Streamlit messages during playback, a seek/reload counter exposed and tested. |
| Phase detection | 7 moments from the hand path; on one real swing all 7 matched a human check (Top one frame early from smoothing). No club/ball detection. | needs work | Top and Impact refined to the unsmoothed hand path. Still pose-only. One real swing is not validation. |
| Checkpoint sequencing | Holds every moment in order; several checks share one held frame. Walkthrough started at frame 0, so **3.6 s of waggle** played before Address. | needs work | The walkthrough runs from just before Address to just after Finish; the scrubber shows moment ticks. |
| Timing | Hold 1.8 s wall clock; speeds 0.25/0.5/1x; the preview had one keyframe for the whole clip, so seeks decoded from frame 0. | needs work | Keyframe every ~0.2 s, so seeks are short. Holds unchanged (1.8 s, configurable). |
| Measurement overlays | Plain SVG lines/dots in dark muted green (low contrast on video), no reference for absolute angles, no callout, no animation. Head/pelvis as a small dot. Positions scaled by the element box, which **misaligns if the box is letterboxed**. | materially behind | Measurement-specific geometry: torso + dashed vertical reference; knee chain + dashed straight-leg reference; head/pelvis rings (current solid, address dashed); anchor + leader + callout pill; contrast halo; draw-in animation; letterbox-correct mapping recomputed on resize. |
| Video quality | 720p cap, CRF 26, `veryfast`. | needs work | 1080p cap (native for phone clips), CRF 20, short GOP. |
| UI hierarchy | Portrait video stretched to page width on desktop (≈1340 px tall; card and controls below the fold); text buttons; a status badge always visible; several explanatory captions around the player. | materially behind | Video sized to the viewport; on wide containers the controls and card sit beside it, so the whole analysis fits one 1440×900 screen; phones get a stacked phone column. Icon transport (prev / large play / next), speed pill, a slim scrubber with moment ticks inside the video, one short note. |
| Result cards | Phase, provenance, label, value, dots and "Check n of m". Functional but engineering-toned. | needs work | Large value with unit, measured-against line ("vs vertical", "vs address"), dots, quiet provenance, tracking-confidence chip. |
| Confidence handling | The engine's valid/invalid is honoured (em dash + reason); no per-check tracking confidence; "Estimated · review" on every card. | needs work | Per-check tracking confidence from the measured landmarks' visibility/presence (clear / uncertain); cannot-measure shows no geometry, an amber state and a reason. |
| Measurement validity | Fail-closed engine; no invented metrics. Sensei has calibrated verdicts and club metrics we do not. | close (on honesty) / not implemented (plane, hand depth, arm hang, verdicts) | Unchanged by design: no verdicts without validated rules. |
| Mobile layout | No horizontal overflow at 390 px, but the text-button row wraps into two lines, and the card and notes push the page long. | needs work | Single-column phone layout, 44 px+ touch targets, no overflow (tested at 390 px). |
| Result experience (score, priority, drills, trends, share) | None. | not implemented | Deliberately deferred until core presentation is right. |

## Biggest remaining gaps after this session
1. **No club/shaft or ball model.** Every plane check and hand depth in Swing
   Sensei depends on one. That is the largest *content* gap.
2. **No validated thresholds**, so we show numbers rather than plain-language
   verdicts. That is honest, but it reads as less helpful than "Head steady".
3. **Pose-only moment detection validated on one swing.** More real clips are
   needed, including down-the-line views and a left-handed golfer.
4. **Streamlit shell.** The viewer is now a self-contained iframe that never
   talks back during playback, so Streamlit no longer limits smoothness.
   It still limits the product shell (page chrome, full-screen phone layout,
   gestures such as swipe-up). A real mobile product needs a standalone
   frontend.
