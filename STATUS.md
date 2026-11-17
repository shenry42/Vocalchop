# Vocal Chop Sampler — Status

> Read this first in every build chat, together with SPEC.md.
> The chat that finishes a milestone updates this file before it ends.

## Current state
- **Version:** v0.2 (tag `v0.2` on GitHub once `backup.bat` has run)
- **Last milestone completed:** 2 — Chopping and editor (2026-11-16): 79 tests pass in Claude's workspace;
  `check_m2.bat` meets all three goals on all 3 test files; Milestone 1's note check still met on all 3.
  **To confirm on Sam's PC:** `setup.bat` (adds MIDI), `run_tests.bat`, `check_m2.bat`, then `backup.bat`.
- **Next:** Milestone 3 — Natural render (SPEC §7).
- **Repo:** https://github.com/shenry42/vocalchop (public). On disk:
  `Documents\Vocalchop-Dev`. Projects, test files, settings and check results: `Documents\Vocalchop`.
- **Last updated:** 2026-11-16 (Milestone 2 done)

## Milestones (SPEC §7)
- [x] 1 — v0.1 Pitch analysis (2026-11-16)
- [x] 2 — v0.2 Chopping and editor (2026-11-16)
- [ ] 3 — Natural render
- [ ] 4 — Device: Natural playback
- [ ] 5 — Device: Vintage mode and playing controls
- [ ] 6 — Polish and v1.0 release

## Milestone 2 check (automatic, no listening)
Goal (SPEC §7): Pitch mode lands cuts on note changes; no clicks at slice edges; Glide and low-confidence flags on
the right slices. `check_m2.bat` chops each whole song and writes `Documents\Vocalchop\m2_check_results.txt`
(**the next chat reads it first**). Goals were fixed before running on the real files, except the 1b refinement
noted below.

Result in Claude's workspace (2026-11-16):

| | Crave You | Crazy | Summertime Sadness |
|---|---|---|---|
| Pitch slices (whole song) | 351 | 264 | 174 |
| 1a. legato cuts confirmed as note changes by the tuner (goal ≥ 90%) | 134/134 (100%) | 85/90 (94%) | 21/21 (100%) |
| 1b. slices without vibrato hiding a change (goal ≤ 10%) | 1/180 | 1/26 | 1/22 |
| 2. clicks (goal 0); for comparison, raw cuts 0.7 ms off zero, no fade | 0 of 878 edges (raw: 347) | 0 of 710 (raw: 315) | 0 of 450 (raw: 166) |
| 3a. flags not matching their definition (goal 0) | 0 | 0 | 0 |
| 3b. Glide flags confirmed by the tuner (goal ≥ 80%) | 1/1 | 7/7 | 0/0 |
| 3c. tuner agrees with the note: low-confidence vs confident slices | 100% vs 100% | 80% vs 93% | 98% vs 99% |

- The 5 unconfirmed legato cuts on Crazy are all on bent or wide-vibrato notes (the tuner reads the same note
  name either side, a few tens of cents apart).
- **1b refinement (made after the first run, with evidence):** the first run counted vibrato slices and Crazy came
  out at 6/55 (11%).
  - Four of the six were single held notes with ±0.8–1.4 st vibrato; the other two were bends inside a note.
  - On single synthetic notes (no change at all) the halves test false-alarms on 28 of 131 with detected vibrato,
    and on 16 of 148 without (those 16 are fast, wide vibratos the detector missed; steady notes and scoops gave
    none).
  - So the goal counts slices without vibrato. Vibrato slices are still listed in the report (Crazy: 5 of 29
    differ by halves).
- Milestone 1 note check after all Milestone 2 changes (`analyze_acapellas.bat`): Crave You 10/10, Crazy 9/10,
  Summertime Sadness 10/10 check notes. All sustained notes: Crave 206/208, Crazy 54+3 borderline/62, Lana 28/28.
  There are more sustained notes than before because legato notes are no longer shortened.

## Open question settled: whole note or steadiest part? → neither; vibrato-aware
Synthetic study (`tests/study_note_pitch.py`, 140 notes through the real pYIN pipeline, intended pitch known).
RMS error in cents (worst case):

| Case | Whole note (M1) | Steadiest 200 ms | **Vibrato-aware (now default)** |
|---|---|---|---|
| Scoop into the note | 0.3 (0.6) | 0.3 (0.6) | 0.3 (0.6) |
| Fall-off at the end | 0.6 (2.0) | 0.3 (0.6) | 0.6 (2.0) |
| Symmetric vibrato | 8.7 (25.0) | 16.5 (52.4) | **3.2 (12.7)** |
| Two short dips | 0.3 (0.6) | 0.3 (0.6) | 0.3 (0.6) |
| Bent (starts −45 c) | 0.3 (0.6) | 0.3 (0.6) | 0.3 (0.6) |

- **Why vibrato-aware:**
  - Medians already ignore scoops, falls and dips.
  - Vibrato is where the median errs: with uneven vibrato cycles it lands near the top or the bottom of the wobble.
  - Listening studies put a vibrato note's pitch at its mean.
  - "Steadiest part" is the worst method on vibrato.
- **The rule (`notes.measure_pitch`, method `auto`):**
  - A note with vibrato (`has_vibrato`: 3.5–9 Hz, at least 12 c RMS, at least 1.5 cycles) takes the median of the
    150 ms-smoothed pitch, after dropping frames more than 0.8 st from the whole-note median.
  - Any other note keeps the whole-note median.
- **Effect on the real files:** 3 of 793 notes renamed; Crazy's tuner disagreements remain on wide-vibrato notes,
  where the tuner's middle-70% window is itself unreliable (e.g. #249: a 1.4 st peak-to-peak vibrato).

## Test results (latest run)
- Automated, Claude's workspace (Linux, Python 3.13): **79 passed**. Files and what they cover:
  - `test_analysis` 20, `test_outputs` 14, `test_tuner` 15: from Milestone 1.
  - `test_chop` 19: legato cuts within 25 ms, pre-roll, region clipping, zero crossings, 0 clicks, click detector,
    short-piece merging, silence gate, equal, manual, Glide / low-confidence on the right slices, consonant before
    a legato cut, wide vibrato not a Glide, edits and undo, the 32 limit, save / queue / reopen.
  - `test_app` 8: the editor window headless.
  - `test_pitch_method` 2, `test_m2check` 1.
- Windows `run_tests.bat`: **to be run by Sam** (Milestone 1: 55 passed in 24 s).
- Speed: `check_m2` takes about 5 s for the 3 files with the pitch cache.

## Decisions made in Milestone 2 (also in SPEC §9)
- **Region:** chops run inside a region dragged on the top strip (or Shift + drag); one slice map per song (Sam).
  Save refuses more than 32 slices.
- **MIDI keyboard (Sam):** mido + python-rtmidi.
  - Keys from Live's C1 (MIDI 36) play slice 1, 2, 3...
  - Tap to chop: Space or any MIDI key while the region plays. Taps snap to a detected note start within 60 ms and
    are corrected for the audio output latency.
- **Pre-roll** never into the slice before (Sam: "default to you").
  - Legato cuts get none, except that a consonant burst in the 20 ms before a Pitch / Silence gate legato cut moves
    the cut back to it.
  - Equal and Manual cuts are never moved.
- **Short pieces:** a piece under 60 ms merges into a neighbour within 100 ms; otherwise it's widened to 60 ms.
- **Zero-crossing snap** within 5 ms on the mono mix; 3 ms raised-cosine fades (slice_audio) make the first and
  last sample exactly 0 in every channel.
- **Note splitter changes** (they also changed the Milestone 1 analysis):
  - Legato changes now meet where the pitch crosses halfway. pYIN marks the slide unvoiced, which had started each
    next note 33–52 ms late.
  - A long slide is no longer cut at its start: the halfway-crossing search reached only 80 ms.
  - Glide spread is measured on the vibrato-smoothed pitch.
- **Clicks** are detected automatically (`chop.click_score`). Score = the sharpest sample step within 2 ms of the
  edge (counting the step from silence) ÷ the sharpest step in the next 20 ms. A click is a score of 2 or more,
  ignoring steps under −60 dBFS.
- **Save** writes `slicemap.json` (with `renderHash`, `fadeMs`), `editor.json` and `render_queue.json`. Analysis no
  longer writes `slicemap.json`. The Milestone 1 maps in the project folders stay until the first Save.
- **Audition "shifted to C":** a plain speed change (soxr) to the C nearest the slice's note, using the device's
  formula at 100% Correction. The tuner reads the result and the window says "it is a C".
- The Milestone 1 Pass/Fail marking UI is gone; `analyze_acapellas.bat` still writes `check_results.txt`.

## Notes for the next chat (Milestone 3)
- Start by reading `Documents\Vocalchop\m2_check_results.txt`; if Sam's numbers match the table above, Milestone 2
  is confirmed.
- **Render queue:** `render_queue.json` in each project lists the slices waiting to render (`renderHash`, start,
  end, `renderFolder`). Hook the background render to `session.save`. Render from `chop.slice_audio` (fades
  included) or from the raw edges plus fades applied after rendering: decide and test for clicks with
  `chop.click_score`.
- **Open for Milestone 4:**
  - The device formula (SPEC §5.2) doesn't use `tuningOffsetCents`, so pressing C plays C + the song's offset.
    Decide whether the device should add or remove the offset.
  - The device should apply `fadeMs` at slice edges.
- **Known limitation from Milestone 1:** pYIN marks some passages as unvoiced (e.g. Crazy's intro 0:20–0:27, and
  synthetic vibrato of ±0.8 st or more), so Pitch mode finds few slices there. Silence gate or Manual mode covers
  them.
- Docs-only changes after a release: `scripts\commit_docs.bat`. Milestone 3 needs version 0.3 in
  `src/vocalchop/__init__.py`.
