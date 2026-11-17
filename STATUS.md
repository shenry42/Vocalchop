# Vocal Chop Sampler — Status

> Read this first in every build chat, together with SPEC.md.
> The chat that finishes a milestone updates this file before it ends.

## Current state
- **Version:** v0.3 (tag `v0.3` on GitHub once `backup.bat` has run)
- **Last milestone completed:** 3 — Natural render (2026-11-16): 93 tests pass in Claude's workspace and
  on Sam's PC (`run_tests.bat`, 131 s; run before the file-move retry fix below, which `backup.bat`'s test run
  covers); `check_m3.bat` meets every goal on all 4 songs on Sam's PC (`m3_check_results.txt`, 2026-11-16 23:08; identical numbers to Claude's workspace).
- **Next:** Milestone 4 — Device: Natural playback (SPEC §7).
- **Repo:** https://github.com/shenry42/vocalchop (public). On disk:
  `Documents\Vocalchop-Dev`. Projects, test files, settings and check results: `Documents\Vocalchop`.
- **Last updated:** 2026-11-16 (Milestone 3 done)

## Milestones (SPEC §7)
- [x] 1 — v0.1 Pitch analysis (2026-11-16)
- [x] 2 — v0.2 Chopping and editor (2026-11-16)
- [x] 3 — v0.3 Natural render (2026-11-16)
- [ ] 4 — Device: Natural playback
- [ ] 5 — Device: Vintage mode and playing controls
- [ ] 6 — Polish and v1.0 release

## Render time (measured on Sam's PC)
From `check_m3.bat` on Sam's PC (i7-9700K, 7 worker processes), 2026-11-16:
- **One slice, all 25 files: 1.2 s per second of slice** (median of 11 slices; range 0.8-2.7 s; the slowest are
  Crazy's long notes, where the formant guard renders some shifts twice or more).
- **A 32-slice Save** (Crave You, 11.8 s of audio): **all ready in 21.1 s**; the first slice playable after 0.7 s.
- Cache: a second request takes 0.1 s and renders nothing.
- For comparison, Claude's 2-core workspace (1 worker): 4.1 s per second of slice, 55 s for the same Save.
- Disk: stereo 44.1 kHz renders take about 260 KB per second of slice per file (25 files per slice).

## Milestone 3 check (automatic, no listening)
Goal (SPEC §7): renders of 3 slices at −12, −5, +5 and +12 check out, measured. `check_m3.bat` picks 3 slices per
song by fixed rules (held vowel, consonant start, vibrato), renders all 25 shifts with the real background renderer
into `Documents\Vocalchop\m3_check` and writes `Documents\Vocalchop\m3_check_results.txt`. The goals count all 24
shifted files of every checked slice, not only the 4 headline shifts. 4 songs (Ms. Jackson added; it has no slice
with vibrato, so 11 slices, 264 renders).

| Measurement (goal) | Claude's workspace | Sam's PC (2026-11-16 23:08) |
|---|---|---|
| Pitch, built-in tuner frame by frame (≥ 95% within 10 c, no wrong notes) | 264/264, median 0.5 c | same |
| Pitch curve, pYIN minus the shift (≥ 90% within 10 c) | 257/257, median 1.6 c | same |
| Length (100% exact; envelope lag ≤ 5 ms on 100%) | 264/264; 264/264 | same |
| Formants kept (≥ 90% of unguarded renders; control must move) | 228/234 (97%), median 0.25 st; control 264/264 | same |
| Formant guard used | 30 of 264 (Crazy 24, Crave 6), formants follow 0.25-0.75 | same |
| Clicks (0) and ends on 0 | 0 of 528 edges; 0 | same |
| Level within ±1.5 dB; new clipping | 264/264; 0 | same |
| Harmonicity within 3 dB (≥ 90%) | 253/264 (96%), median +0.7 dB | same |
| +00 identical to the slice | 11/11 | same |
| Cache: second request / renumbering 27 slices / undo + Save | 0 / 0 (27 kept) / 0 re-rendered | same |

What the numbers show:
- **Pitch** is exact: median 0.5 c from the requested shift, worst 6 c; the pitch curve (vibrato included) follows
  the original to a median 1.6 c.
- **Formants:** where the guard didn't step in, 97% of renders fit "formants kept" better than "moved", median
  movement 0.25 st. The 6 that didn't: Lana's consonant slice at −7 and +12, and Ms. Jackson's held note at +9;
  the other 3 are ±1-2 st shifts (Lana −2/−1, Ms. Jackson −1), where "kept" and "moved" are too close to tell apart.
- **Harmonicity** (harmonics vs the gaps between them): 8 of the 11 misses are Ms. Jackson's held note shifted
  down 5-12 st (3-6 dB rougher); the other 3 are Crazy's vibrato note at −12, −11 and +8 (3.3-3.5 dB). Upward
  shifts are usually cleaner than the original (+1 to +5 dB).
- **pYIN voiced share (information only):** at −12, pYIN calls 3-93% of frames voiced, while harmonicity stays
  within −3.5 to +1.9 dB of the original, so pYIN's voicing is not a fault measure here.

### Changes to the check after the first run on the real files (with evidence)
The first run met pitch curve, length (samples), formants and cache, and missed the others. Each change below was
made only after showing that the measure itself was wrong, or by fixing the renderer, never by loosening a goal:
- **Tuner frame by frame.** Read over the whole slice, the tuner was off by up to 73 c on vibrato slices, and by
  55-65 c on a plain speed change of the same slice, whose pitch is exact by construction (averaging over
  the wobble lets the formants decide which part of it wins). Frame by frame (100 ms, same times in both): within
  4 c on the same files.
- **Harmonicity instead of pYIN's voiced share.** pYIN's voiced share fell to 0-30% on formant-kept octave-down
  renders of Lana and Crazy (a plain speed change of the Lana slice kept 86%). Their harmonic-to-gap level stayed within
  1 dB of the original, and the odd/even harmonic balance was normal, so pYIN's voicing is a poor fault measure
  here.
- **Formant guard (a renderer change):** see below. It fixed the level misses (up to −18 dB) and the envelope-lag
  misses (10-18 ms, all on the same quiet renders).
- **Raw edges instead of 50 ms context (a renderer change):** the one click (Crave, +11) was the next syllable's
  consonant bleeding back through the engine's window.

## Decisions made in Milestone 3 (also in SPEC §9)
- **Engine: `pylibrb`** (Rubber Band Library, R3 "finer", formants preserved, offline mode), pinned to 0.1.2.
  Option A (Sam). Same build in the tests and on Sam's PC; `setup.bat` installs it; `verify_setup.py` checks a
  +5 st shift of 220 Hz.
- **Faded slice or raw edges? → raw edges, fades after.** Study (`fade_study`: 40 slices × −12/−5/+5/+12, 320 edges
  per variant; then 100 slices × 10 shifts, 2000 edges):

  | Variant | Clicks | HF bleed at the outer 5 ms (> +6 dB) | Edge level vs middle |
  |---|---|---|---|
  | Faded slice in, nothing after | **33/320** | – | −1.1 dB |
  | Faded slice in, fades again | 0/320 | – | −1.2 dB |
  | Raw edges, fades after (**chosen**) | 0/320, 1/2000 | **0.5%** | −1.1 dB |
  | 50 ms context, trim, fades | 0/320, 0/2000 (1/528 in the check) | 4.4% | −0.7 dB |
  | 200 ms context, trim, fades | 0/320 | – | −0.7 dB |

  Rendering the faded slice leaves non-zero ends (the engine smears the fade). Context helps the edge level a
  little, but lets neighbouring sound (the next consonant) bleed into the slice. Raw edges keep a chop to its own
  audio. A click safety covers the rest: an edge that still measures as a click gets a 6 ms, then 10 ms fade
  (logged as `fadeMs` per file).
- **Level matching:** the engine loses about 1 dB (median), more at the edges. Each render is scaled to the
  slice's RMS (at most ±6 dB, peak ≤ 0.999), so a slice plays equally loud on every key.
- **Formant guard:** Crazy's flute-like notes (fundamental 24-37 dB above the 2nd harmonic) lost 9-20 dB at +9 to
  +12 with formants kept: formant keeping treats the fundamental as a formant and filters it away when it moves.
  Scan of 48 slices: 21% of +12 renders lost more than 6 dB, almost all on Crazy. Rule: a render more than 3 dB
  quieter than the slice lets its formants follow the pitch ¼, ½, ¾ or all the way, the least that keeps the level
  within 3 dB (`GUARD_DB`, `GUARD_FOLLOW` in `render.py`). In the check it acted on 30 of 264 renders (Crazy's two
  long notes from ±4 up, Crave's consonant slice from +7). The rest keep their formants fully.
- **Glide at 1.5 st: no change.** Ms. Jackson repeats one gesture (a fall of about a semitone, 720 ms) four times:
  spreads 1.40, 1.47, 1.51, 1.52 st; the tuner reads falls of 0.84-1.22 st. Any line through that gesture splits it.
  On the Pitch slices of all 4 songs:

  | Rule | Flagged | Tuner confirms | Slides ≥ 1 st not flagged |
  |---|---|---|---|
  | spread > 1.5 (current) | 10 | 6/8 | 4 |
  | spread > 1.55 | 7 | 5/5 | 5 |
  | spread > 1.6 | 6 | 4/4 | 6 |
  | spread > 1.5 and ≥ 1 st travel | 9 | 5/7 | 5 |

  No rule was better on both counts, and Glide is only a hint with a one-click split. `check_m2.bat` now lists a
  Glide flag within 0.1 st of the line whose ends the tuner reads 0.75-1 st apart as *borderline* (not counted).
  Ms. Jackson's two flags are borderline, so all 4 songs now meet the Milestone 2 goals.
- **Cache and folders:** renders follow the renderHash. A slice renders in `renders\_rendering\<hash>` and moves to
  its `slice_XX` folder complete; `render.json` (written last) marks it playable. Renumbering moves folders;
  deleted slices' renders go to `renders\_unused` (undo + Save restores them; trimmed above 500 MB).
  `render_queue.json` lists `ready` and `pending`.
- **Background:** one renderer thread for the app, the 25 files of a slice spread over worker processes (all cores
  but one), slices in order. A new Save restarts the pass after the current slice (or cancels it at once if that
  slice changed). Closing the app stops between files; the next open or Save resumes.
- **Audition "shifted to C"** now plays the Natural render the device's way: nearest rendered semitone + leftover
  speed change (≤ ±50 c), and the tuner panel says which file and how many cents.

## Test results (latest run)
- Automated, Claude's workspace (Linux, Python 3.13): **93 passed** (79 before). New:
  - `test_render` 13: pitch and length, formants kept vs a speed-change control, the device's nearest-semitone plan,
    formant guard, 25 files 24-bit faded and level-matched, +00 identical, cache (gain/note never re-render; edges
    do), renumber/park/restore, interrupted renders cleaned up, background render (threads and processes), saving
    mid-render, a second request renders nothing, Natural audition is a C.
  - `test_app` +1: Save → Natural column turns "ready", audition uses the render, an edit marks the slice
    "save to render".
- Windows `run_tests.bat` (Sam): **93 passed** in 131 s.

## Notes for the next chat (Milestone 4: device, Natural playback)
- `Documents\Vocalchop\m3_check_results.txt` holds the Milestone 3 numbers from Sam's PC.
- A Windows-only fix found on Sam's PC: right after the 25 files were written, renaming the slice's folder failed
  once (the first check run stopped on Lana; most likely the virus scanner holding the new files). File moves and
  renames now retry for up to 10 s (`render._retry`), and the check reports a render error instead of waiting.
- **Which files to play:** `renders/slice_XX/±NN.wav` (`+00`, `+05`, `-12`...). A slice is playable only if
  `renders/slice_XX/render.json` exists and its `renderHash` equals the slice's `renderHash` in `slicemap.json`;
  otherwise fall back to Vintage-style varispeed of the source (or stay silent) until it is ready.
  `render_queue.json` has `ready` / `pending` slice numbers for a quick look; poll it while rendering.
- **The plan per note** is `render.natural_plan(shift)`: nearest whole semitone k (clamped to ±12) plus a speed
  change of the leftover (≤ ±50 c inside the range; beyond ±12 the end file plus varispeed). The editor's audition
  already does exactly this (`audition.natural_clip`), with the tuner confirming a C.
- **Open for Milestone 4:**
  - The device formula (SPEC §5.2) doesn't use `tuningOffsetCents`, so pressing C plays C + the song's offset.
    Decide whether the device should add or remove the offset. (The editor's "it is a C" judges after removing the
    song's offset, i.e. the song's own tuning.)
  - The renders already have their 3 ms fades; the device's `fadeMs` fades on top are harmless (a few files have
    6-10 ms fades from the click safety, see `render.json` → `files` → `fadeMs`).
  - Per-slice `gain` is not in the renders: the device applies it.
- Render files are stereo when the source is (all 4 test songs are): about 260 KB per second of slice per file at
  44.1 kHz, so 32 one-second slices ≈ 210 MB. polybuffer~ loads all of them; check memory in Live.
- Docs-only changes after a release: `scripts\commit_docs.bat`. Milestone 4 is the first Max for Live work (no
  Python version bump needed unless the app changes).
