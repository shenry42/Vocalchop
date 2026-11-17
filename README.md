# Vocal Chop Sampler

Chops clean acapellas and stems into single-note slices, detects each slice's pitch, renders each slice at every
semitone (Natural mode, formants kept), and (from Milestone 4) plays any slice live in Ableton at the key you
press: C plays a real C. See `SPEC.md` for the full v1 design and
`STATUS.md` for where the build is.

Personal use, Windows. Code repo: `Documents\Vocalchop-Dev` (GitHub `shenry42/vocalchop`). Projects and test files:
`Documents\Vocalchop`.

## Scripts (double-click, all in `scripts\`)

| Script | What it does |
|---|---|
| `setup.bat` | Setup: Python environment, packages (including Rubber Band for the Natural render), Git. Safe to run again (run it once after updating to v0.3: it adds Rubber Band). |
| `run_app.bat` | Opens the editor. |
| `check_m3.bat` | Milestone 3 automatic check: renders 3 slices of every test file at all 25 shifts and measures pitch, length, formants, clicks, level and harmonicity; times a 32-slice Save and checks the cache. Written to `Documents\Vocalchop\m3_check_results.txt`. |
| `check_m2.bat` | Milestone 2 automatic check: cuts, clicks and flags on every test file, written to `Documents\Vocalchop\m2_check_results.txt`. |
| `analyze_acapellas.bat` | Analyzes every file in `Documents\Vocalchop\Acapellas`; writes `analysis_summary.txt` and the Milestone 1 note check `check_results.txt`. |
| `run_tests.bat` | Runs the automated tests (results in `test_results.txt`). |
| `backup.bat` | End of a milestone: tests, commit, tag, push to GitHub, zip backup. |
| `commit_docs.bat` | Commits and pushes doc-only changes (SPEC/STATUS/README/CHANGELOG). |

## The editor (Milestone 2)

1. **Pick a file and press Analyze.** The range preset is remembered per file. If the song was saved before, its
   slices come back.
2. **Choose a region.** Drag along the thin strip at the top of the picture (or hold Shift and drag anywhere) over
   a phrase. A slice map holds 32 slices, and a whole song has hundreds of notes. **Whole song** clears the region.
3. **Pick a chop mode and press Chop region** (Ctrl+K):
   - **Pitch:** a cut where the note changes or the voice stops.
   - **Silence gate:** a cut at gaps quieter than −45 dBFS lasting 60 ms or more (whole words or phrases).
   - **Equal:** 4, 8, 16 or 32 equal slices.
   - **Manual:** the region starts as one slice. Click where each new slice should start, or use **Tap to chop**.

   Every mode applies the same rules:
   - Starts move 20 ms earlier to keep consonants.
   - Edges sit on zero crossings and get 3 ms fades.
   - Slices under 60 ms merge into a neighbour.
4. **Fix what needs fixing.** Select a slice in the list or click it in the picture. Then:

   | Action | How |
   |---|---|
   | Move a boundary | Drag the line. A line shared by two slices moves both. |
   | Split | **/** splits at the suggested point (the middle of a glide, or the note change). Double-click splits where you click. |
   | Merge with the next slice | **M** |
   | Delete | **Del** |
   | Octave down / up | **−** / **+** |
   | Set the note by hand | **N**, then type a name like E4 or F#3 |
   | Keep as sung | **K**, or tick the box in the list |
   | Undo / redo | Ctrl+Z / Ctrl+Y |
   | Edit menu | Right-click in the picture |

5. **Audition.**
   - **Space** plays the slice as the device will (with its fades).
   - **C** plays it shifted to the nearest C. The built-in tuner reads what you hear and the line above the list
     says *"it is a C"* (or not), so you don't have to judge by ear. Once the slice's Natural renders are ready this
     is exactly what the device will play (the nearest rendered semitone plus a tiny speed change for the cents);
     before that it is a plain speed change. The line says which.
   - **R** plays the region; **S** stops.
6. **Save** (Ctrl+S). This writes `slicemap.json` (what the device will load), `editor.json` (your region, mode and
   every edit) and `render_queue.json`, and starts the **Natural render** in the background (below).

**Flags.** Purple outline / **G** in the strip = **Glide**: the pitch slides more than 1.5 semitones (vibrato doesn't
count); press **/** to split it. Orange outline / **?** = **low confidence** (under 60): check the note. The line
above the list shows the built-in tuner's reading for the selected slice.

**MIDI keyboard.** Pick it under **MIDI in**. Keys from Live's **C1** (MIDI 36) up play slice 1, 2, 3... (like
the device's Slices mode). **Tap to chop** (T) plays the region; press **Space** or any MIDI key where a slice should
start. Taps within 60 ms of a detected note start land exactly on it. On Windows a MIDI keyboard can usually be open
in only one program: if it won't open, turn its Track/Remote input off in Live's MIDI preferences (or close Live).

Note names: standard (C4 = middle C) in the list and Live's names (Live calls middle C "C3") next to them.

## The Natural render (Milestone 3)

Saving renders every slice at every semitone from −12 to +12 with Rubber Band (R3 engine, formants kept), so the
device can play Natural mode with no delay. It runs in the background: keep editing while it works.

- The **Natural** column in the slice list shows each slice: **ready**, **rendering 12/25**, **waiting** (saved,
  in the queue) or **save to render** (changed since the last save). The top right shows the total
  ("Natural render: 7 of 12 slices ready").
- Files: `renders\slice_03\+05.wav` etc. in the song's project folder: 24-bit, the source's sample rate and
  channels, the same length as the slice, 3 ms fades, the same loudness as the slice. `+00.wav` is the slice
  itself. `render.json` in each folder is written last and records how each file was made.
- **Cache:** only slices whose audio changed render again. Gain, note, octave and Keep as sung never re-render (the
  device applies them live). Splitting, merging or deleting renumbers the slices: their renders move with them
  instead of rendering again. Deleted slices' renders wait in `renders\_unused` (up to 500 MB), so Undo + Save
  brings them back instantly.
- Closing the app mid-render is safe: the rest renders the next time that song is opened or saved.
- **Formant guard.** Some notes are almost a pure tone (Crazy has many): the voice's strongest "formant" is its own
  fundamental, and keeping it in place filters the note away when it moves far (up to 20 dB quieter at +12). When a
  render would come out more than 3 dB quieter, its formants follow the pitch a quarter, half, three quarters or all
  the way, whichever is the first that keeps the level. `render.json` lists where this happened, and
  `check_m3.bat` reports it.

## Milestone 1 note check (still available)

`analyze_acapellas.bat` re-runs the pitch check: for each file, 10 sustained notes spread across the song are
compared with the **built-in tuner** (a spectral method independent of the analysis). Results go to
`Documents\Vocalchop\check_results.txt`. Each project folder also has `guide.wav` (reference tones),
`notes.mid` (detected notes at 120 BPM), `tuner_check.wav` / `tuner_check.txt` (for Ableton's Tuner),
`notes.json` and `analysis_log.txt`.
