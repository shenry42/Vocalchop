# Vocal Chop Sampler

Chops clean acapellas and stems into single-note slices, detects each slice's pitch, and (from Milestone 4) plays
any slice live in Ableton at the key you press: C plays a real C. See `SPEC.md` for the full v1 design and
`STATUS.md` for where the build is.

Personal use, Windows. Code repo: `Documents\Vocalchop-Dev` (GitHub `shenry42/vocalchop`). Projects and test files:
`Documents\Vocalchop`.

## Scripts (double-click, all in `scripts\`)

| Script | What it does |
|---|---|
| `setup.bat` | One-time setup: Python environment, packages, Git. Safe to run again. |
| `run_app.bat` | Opens the analysis window. |
| `analyze_acapellas.bat` | Analyzes every file in `Documents\Vocalchop\Acapellas` and writes `Documents\Vocalchop\analysis_summary.txt`. |
| `run_tests.bat` | Runs the automated tests (results in `test_results.txt`). |
| `backup.bat` | End of a milestone: tests, commit, tag, push to GitHub, zip backup. |

## Milestone 1: checking the detected notes (no listening needed)

The app checks its own notes with a **built-in tuner** that measures each note a different way (from the spectrum,
like a hardware tuner) than the analysis does (pYIN, from the waveform). When both name the same note, two
independent methods agree.

1. Run `analyze_acapellas.bat` (or `run_app.bat` → pick a file → **Analyze**). The range for each test file is
   already saved.
2. That's the test. For each file the app picks **10 sustained notes spread across the song** and compares them.
   The top line of the window says e.g. *"Built-in tuner agrees on 10 of 10 check notes (goal met)"*, and
   `Documents\Vocalchop\check_results.txt` collects all files for the next build chat.
3. Look at any note: click it in the table or the picture. The large line under the picture shows the tuner's
   reading next to the app's note:
   - **same note**: they agree.
   - **borderline**: the singer is near halfway between two notes, so the two name neighbouring notes. At 100%
     Correction both names give the same played pitch, so this counts as agreeing.
   - **DIFFERENT note**: a real disagreement. These are listed in check_results.txt.
4. Optional, to check the built-in tuner itself with Ableton's Tuner: each project folder has
   **`tuner_check.wav`**: the 10 check notes (then any DIFFERENT notes), each held for 2 seconds, one every 3 seconds.
   Put it on an audio track (Warp off) with Live's **Tuner** on it, press play, and compare the note names with
   `tuner_check.txt` (Live names). Mark any note in the app with **P** / **F** if you like.

Show: picks which notes the table lists (the 10 check notes, all sustained notes, or all notes). **V** / **T** /
**Space** still play the voice, a reference tone, or both. Note names: standard (C4 = middle C) and Live's (Live
calls middle C "C3"); the tuner's cents are shown the way a tuner shows them, before the song's tuning offset.

### Other ways to check (optional)
Each analyzed file gets a project folder `Documents\Vocalchop\<file name>\` with:
- `guide.wav` reference tones at every detected note, lined up with the source. In Ableton, put the acapella and
  `guide.wav` on two audio tracks with **Warp off**, both starting at bar 1, and listen together; or put Live's
  **Tuner** on the acapella track.
- `notes.mid` the detected notes as MIDI. It's written at 120 BPM, so set the Live set to 120 BPM before dragging it
  in, and it lines up with the unwarped acapella.
- `slicemap.json` a test slice map in the spec's format (up to 32 notes; **Export slice map from this note** in the
  app writes one starting at the selected note).
- `notes.json` every detected note; `analysis_log.txt` what was found.
