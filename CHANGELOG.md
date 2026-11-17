# Changelog

## v0.3 — Milestone 3: Natural render (2026-11-16)
- Rubber Band R3 ("finer") with formants kept, through the `pylibrb` package (installed by `setup.bat`).
- Every slice rendered at −12..+12 semitones: `renders/slice_XX/±NN.wav`, 24-bit, source rate and channels, the
  slice's exact length, 3 ms fades, loudness matched to the slice; `+00.wav` is the slice itself.
- Formant guard for near-pure tones (formants follow the pitch only as far as needed to keep the level), and a click
  safety (longer fade on an edge that still measures as a click). Both logged in `render.json`.
- Background rendering starts on Save, several worker processes at once, slice by slice; each slice is playable as
  soon as its 25 files are in place. Saving again mid-render is safe; closing the app resumes on the next open.
- Cache by renderHash: only changed audio re-renders; renumbered slices keep their renders; deleted slices' renders
  are kept in `renders/_unused` for undo. Gain/note/octave/Keep as sung never re-render.
- Editor: **Natural** column (ready / rendering n/25 / waiting / save to render) and a total at the top right;
  "Play shifted to C" plays the Natural render the way the device will (nearest semitone + leftover speed change).
- `check_m3.bat`: renders and measures 3 slices per song (frame-by-frame tuner, pitch curve, length and envelope,
  formants vs a speed-change control, clicks, level, harmonicity), times a 32-slice Save, checks the cache.
- `check_m2.bat`: Glide flags within 0.1 st of the line whose ends are 0.75-1 st apart are listed as borderline.

## v0.2 — Milestone 2: Chopping and editor (2026-11-16)
- Editor window replaces the Milestone 1 checking window: waveform + pitch curve + slice lines, a region dragged
  over a phrase, slice list (number, start, length, note, Live name, cents, confidence, flags, Keep as sung).
- Four chop modes inside the region: Pitch, Silence gate (−45 dBFS, 60 ms), Equal (4/8/16/32), Manual (click, or
  Tap to chop while the region plays). Shared rules: 20 ms pre-roll (never into the slice before), zero-crossing
  snap, 3 ms fades, pieces under 60 ms merged (a lone short piece is widened instead), at most 32 slices per map.
- Edit actions: drag a boundary (shared lines move both slices), split (suggested point or double-click), merge,
  delete, ±1 octave, set the note by hand, Keep as sung, undo/redo; right-click menu.
- Audition: the slice as the device plays it, or shifted to the nearest C with the built-in tuner confirming the
  result on screen. Play region, playhead.
- MIDI keyboard input (mido + python-rtmidi): keys from Live's C1 play slices; MIDI keys or Space tap-chop.
- Save writes slicemap.json (+ `renderHash` per slice, `fadeMs`), editor.json (restored on reopening) and
  render_queue.json for the Milestone 3 render. Analysis no longer overwrites slicemap.json.
- Note naming: vibrato-aware pitch (whole-note median, or the median of the vibrato-smoothed pitch on notes with
  vibrato). The open question "whole note or steadiest part" settled by a synthetic study (STATUS.md).
- Note splitter fixes: legato changes are cut where the pitch crosses halfway (pYIN marks the slide unvoiced, which
  started each next note 33-52 ms late); a consonant burst just before a legato cut goes to the next slice; long
  slides are no longer cut at their start. Glide = spread of the vibrato-smoothed pitch, so wide vibrato is no
  longer flagged Glide.
- `check_m2.bat`: automatic check of cuts (built-in tuner), clicks (edge step vs the audio next to it) and flags,
  written to m2_check_results.txt.

## v0.1 — Milestone 1: Pitch analysis (2026-11-16)
- Loads WAV, AIFF, FLAC and MP3 at any sample rate; analysis on a mono copy at 22.05 kHz.
- pYIN pitch tracking (librosa) in 20 s chunks on several cores, with each voiced frame refined to about 1 cent.
- Range presets Male / Female / Instrument / Custom, remembered per file.
- Basic Pitch-mode note splitter (0.7 semitone change held 80 ms, or the voice stopping), robust to vibrato and
  scoops; glides are flagged rather than chopped.
- Global tuning offset (circular weighted mean), then note, cents, confidence and flags (glide, low confidence) per
  note.
- Outputs per file: slicemap.json (SPEC §2.1), notes.json, notes.mid, guide.wav, analysis log, pitch cache.
- Checking window (PySide6): waveform + pitch curve + notes, sustained-note list, voice / tone / both playback,
  Pass/Fail marks written to check_results.txt.
- Built-in tuner (independent spectral method) on every sustained note; match / borderline / different; 10 check
  notes per file picked evenly across the song; tuner_check.wav (10 notes held 2 s each) for Ableton's Tuner.
- Scripts: setup, run_app, analyze_acapellas, run_tests, backup.
