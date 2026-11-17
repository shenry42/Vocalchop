# Vocal Chop Sampler — Status

> Read this first in every build chat, together with SPEC.md.
> The chat that finishes a milestone updates this file before it ends.

## Current state
- **Version:** v0.1 (Milestone 1 built; the note check passes in Claude's workspace, waiting for the same run on
  Sam's PC)
- **Last milestone completed:** none yet. Milestone 1 — Pitch analysis is built and passes the automated tests and
  the built-in tuner check on all 3 test files (below). It is done when Sam's PC gives the same result, then
  `scripts\backup.bat` makes the first GitHub push + zip.
- **Next:** Sam runs setup / tests / analyze on Windows → backup → Milestone 2 (Chopping and editor).
- **Repo:** https://github.com/shenry42/vocalchop (public; Sam creates it empty, first push by `backup.bat`). On disk:
  `Documents\Vocalchop-Dev`. Projects, test files, settings and check results: `Documents\Vocalchop`.
- **Last updated:** 2026-11-16 (Milestone 1 built)

## Milestones (SPEC §7)
- [ ] 1 — Pitch analysis (built 2026-11-16; tuner check passes here, Windows run pending)
- [ ] 2 — Chopping and editor
- [ ] 3 — Natural render
- [ ] 4 — Device: Natural playback
- [ ] 5 — Device: Vintage mode and playing controls
- [ ] 6 — Polish and v1.0 release

## Milestone 1 note check
Goal (SPEC §7): on 3 acapellas, at least 9 of 10 sustained notes match on a tuner.
**Changed 2026-11-16:** Sam can't reliably judge notes by ear, so the check uses the app's **built-in tuner**
(`tuner.py`, a spectral method independent of the pYIN analysis) on 10 sustained notes per file, picked evenly across
the song. Ableton's Tuner is an optional cross-check via each project's `tuner_check.wav` / `tuner_check.txt`.
Results land in `Documents\Vocalchop\check_results.txt` — **the next chat reads that file first.**

Result in Claude's workspace (2026-11-16), check notes / all sustained notes:
- Crave You: **10/10** match; all sustained 196/197 (1 different: #257 181.28 s, app A3 +6, tuner G#3 +42).
- Crazy: **9/10** match (check note #10 = note #254 at 169.04 s is different: app G4 −5, tuner F#4 +33, 72 c apart);
  all sustained 39 match + 4 borderline + 3 different of 46. All disagreements are one semitone on bent notes.
- Summertime Sadness: **10/10**; all 24/24.
- (Ms. Jackson, not a test file: 9/9.)
→ goal met on all 3 files here. Still to do: the same on Sam's PC (`analyze_acapellas.bat`), optionally Ableton's
Tuner on Crazy's `tuner_check.wav` note 10 to see which method is right there.

Test files in `Documents\Vocalchop\Acapellas` (range presets saved in `Documents\Vocalchop\settings.json`):

| File | Range | Offset | Notes / sustained | Notes |
|---|---|---|---|---|
| Crave You (Vox).wav | Female | −3.4 c | 352 / 197 | Clean, digital silence between phrases. |
| Lana Del Rey - Summertime Sadness (Studio Acapella).mp3 | Female | −6.0 c | 175 / 24 | Constant reverb/noise floor (~−48 dB) under the voice; pYIN voicing is low (13% voiced), 16 low-confidence notes. Really sings down to E3. |
| Gnarls-Barkley-Crazy-Studio-Acapella-4B-112-sd43.mp3 | Custom C2–F5 | +9.9 c | 266 / 46 | Male, but sings up to C5, above the Male preset's top, so a custom range. Many bent notes (±30–45 c) and 36 glides: the hard case for note naming. |
| Outkast - Ms. Jackson (Studio Acca).mp3 | Male | −23.2 c | 54 / 9 | **Not one of the 3 test files.** Mostly rap; the chorus looks stacked (several voices), so only 9 sustained notes, all the same E4. Replaced by Crazy. |

## Test results (latest run)
- Automated, Claude's workspace (Linux, Python 3.13): **55 passed** (`tests/test_analysis.py` 20, `test_outputs.py`
  14, `test_app.py` 6, `test_tuner.py` 15). Synthetic voices (`tests/synth.py`) cover: female melody with +14 c detune; offsets −30, 0,
  +45, −48 c; male C2–G3 without octave errors; ±0.6 st vibrato not split; scoops; legato changes; one note 30 c
  sharp; glide flagged; noise; ~1-cent pitch refinement; 3-chunk parallel = single-process and a note held across a
  chunk edge; WAV 24-bit / AIFF / FLAC / MP3 stereo; 48 kHz.
- Windows `run_tests.bat`: not run yet (Sam).
- Speed (Claude's workspace, 2 cores): 4.9–8.2 s per 3–5 minute file from scratch; ~1 s when the pitch cache is used.

## Decisions made in this milestone (also in SPEC §9)
- MP3 accepted as input (Sam, 2026-11-16). Folders are `Documents\Vocalchop` and `Documents\Vocalchop-Dev`.
- UI toolkit PySide6 on Python 3.12, as OrchSketch. Audio playback in the app: `sounddevice` (default Windows output,
  shared mode, so Ableton can stay open).
- **pYIN runs on its default 0.1-semitone grid.** A 0.02 grid made librosa's pYIN collapse to the bottom of the
  range on real vocals. Instead each voiced frame is refined (`pitch.refine_f0`): squared-difference minimum within
  ±7% of pYIN's period, parabolic interpolation, ~1 cent; pYIN still decides octave and voicing.
- Analysis at 22.05 kHz, frame 2048, hop 256 (11.6 ms); 20 s chunks with 1.5 s padding on up to 6 worker processes
  (`VOCALCHOP_WORKERS` overrides).
- Note splitter (`notes.py`, Milestone 1 version): voiced = voicing prob > 0.5 and louder than (peak − 45 dB);
  gaps ≤ 35 ms bridged when the pitch runs smoothly through (fast vibrato drops voicing below 0.5 for 2–3 frames);
  split decisions on pitch averaged over 150 ms (≈ one vibrato cycle); a cut needs > 0.7 st change, the new note
  flat within 0.35 st for 80 ms, and not returning within the next 80 ms; cut placed where the raw pitch crosses
  halfway; pieces < 60 ms merged.
- Note = RMS-weighted median of voiced frames; tuning offset = circular weighted mean of the notes' cents (weights:
  length × steadiness × voicedness; glides excluded); confidence = min(1, voicedness/0.85) × steadiness, where
  steadiness = 1 at ≤ 20 c median absolute deviation, 0 at 100 c. Glide = 10th–90th percentile spread > 1.5 st.
  "Sustained" for the ear test = ≥ 250 ms, confidence ≥ 60, no glide.
- Slice map export in M1 uses the raw note boundaries (no 20 ms pre-roll, zero-crossing snap or fades yet: those are
  chop rules for Milestone 2). The app exports up to 32 notes from the selected note.
- Range warning only when > 15% of notes sit within 0.5 st of the range edge (5% gave a false alarm on Lana).
- Built-in tuner (`tuner.py`): middle 70% of the note, 186 ms Hann windows (50% overlap) averaged, 4x zero
  padding, log harmonic product spectrum (5 harmonics) in the preset range; sub-octave check (strong peaks at 1.5x
  and 2.5x → an octave lower, for weak fundamentals); pitch = strength-weighted median of the first 8 harmonics'
  parabolic peaks / h. Steady synthetic notes C2–B5 read within 3 c; vibrato moves the reading ±20–30 c.
  Verdicts: match / borderline (neighbouring notes, both readings within 35 c of halfway, ≤ 50 c apart: same
  played pitch at 100% Correction) / different. A note sung "between" notes gets different names from the
  whole-note median (analysis) and the middle 70% (tuner) — the source of all of Crazy's disagreements.
- Note names: the app shows scientific (C4 = middle C = MIDI 60) and Live's (C3 = MIDI 60).

## Notes for the next chat
- Start by reading `Documents\Vocalchop\check_results.txt` (and `analysis_summary.txt`). If Windows matches the
  numbers above, Milestone 1 is done. Open question for Milestone 2: on bent notes (Crazy) the whole-note median and
  the steady middle disagree by up to ~70 c; consider basing the note on the steadiest stretch, since the render
  and Correction use note + cents.
- Then, if the goal is met: `backup.bat` (first push), tick Milestone 1, and start Milestone 2.
