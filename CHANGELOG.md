# Changelog

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
