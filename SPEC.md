# Vocal Chop Sampler — v1 Build Spec

> Source: Claude Doc "Vocal Chop Sampler — v1 Build Spec" (2026-11-16, Sam), turned into this file on 2026-11-16.
> Read this together with STATUS.md at the start of every build chat. Changes to the spec are made here and noted
> in §9 (Spec changes).

## 1. Overview

v1 chops a clean acapella or stem into single-note slices, detects each slice's pitch, and plays any slice live in
Ableton at the key you press: C plays a real C.

**In scope**
- Sources: clean acapellas and stems only, one voice or one melodic line at a time.
- Live playing during production from a MIDI keyboard or pads in Ableton.
- Two sound modes: **Natural** (main focus, formants kept, length kept) and **Vintage** (classic varispeed, pitch and
  length change together).
- Two mapping modes: **Exact** (C3 always plays C3) and **Nearest** (C plays the C closest to the slice's own pitch).
- Personal use only, on Sam's Windows PC. Claude writes the code; Sam tests in Ableton.

**Not in v1**
- Chords, full mixes, or stem separation (sources arrive already clean).
- Real-time pitch bend or glide in Natural mode (Vintage can bend).
- A VST3 version. That is the planned v2 path if real-time Natural mode is wanted.
- Anything taken from Akai's MPC firmware. Everything here uses open methods.

## 2. Architecture

Two parts, linked only by files: the **app** analyzes and renders once, and the **device** just plays. This is the
same split as the Arrangement Stencil Tool.

```
 Analysis app (Windows, Python)          Project folder                     Max for Live device
 load → detect → chop → edit → render →  slice map (.json) + renders\  →   load map → play live in Ableton
```

The app never talks to Ableton directly, and the device never analyzes audio, so either side can be rebuilt (for
example as a VST3 in v2) without touching the other.

### 2.1 Slice map (one JSON file per sample)

```json
{
  "version": 1,
  "source": "C:/Users/Sam/Documents/VocalChop/acapellas/hook.wav",
  "sampleRate": 48000,
  "tuningOffsetCents": 14,
  "renderRange": [-12, 12],
  "slices": [
    {"index": 1, "start": 0.412, "end": 0.987, "note": 64, "cents": 18,
     "confidence": 92, "flags": [], "keepAsSung": false, "gain": 0.0,
     "renderFolder": "renders/slice_01"}
  ]
}
```

Times are in seconds, `note` is a MIDI note number (64 = E4), `cents` is the slice's offset from that note after the
global offset is removed, and `gain` is in dB.

### 2.2 Folders
- `Documents\Vocalchop\<project>\` holds the slice map, `renders\` and logs. Test acapellas live in
  `Documents\Vocalchop\Acapellas\`.
- The code repo lives separately in `Documents\Vocalchop-Dev`, with GitHub (`shenry42/vocalchop`, public) + zip
  backups as with OrchSketch.

## 3. Analysis app

The Windows app (Python 3.12, **PySide6** UI — the same toolkit as OrchSketch) does all the hard work up front: it
chops, detects pitch, lets Sam fix mistakes, and renders Natural mode. All numbers below are starting values to tune
by ear in testing.

### 3.1 Load
- WAV, AIFF, FLAC or **MP3** (MP3 added 2026-11-16, see §9), any sample rate. Analysis runs on a mono copy; renders
  keep the source's channels. WAV/AIFF/FLAC are preferred for final use, since MP3 costs some quality in renders.
- A range preset sets the pitch search range: Male vocal (C2 to C5), Female vocal (E3 to C6), Instrument (C1 to C7),
  or Custom.

### 3.2 Chop modes
1. **Pitch (default).** Cut where the pitch moves more than 0.7 semitones and holds the new note for at least 80 ms,
   or where the voice stops.
2. **Silence gate.** Cut at gaps quieter than −45 dBFS for at least 60 ms. For whole words or phrases.
3. **Equal.** Split into N equal slices (4, 8, 16, 32).
4. **Manual.** Click or tap to place slice points.

All modes: slice starts move 20 ms earlier to keep consonants, points snap to zero crossings, each slice gets 3 ms
fades, and slices under 60 ms merge into a neighbour. Maximum slices per map: **32**.

### 3.3 Pitch detection
- Default: pYIN (via librosa). Optional **High accuracy** mode: CREPE (torchcrepe, CPU only).
- A slice's note = the volume-weighted median of its voiced frames (voicing probability above 0.5). Scoops, vibrato
  and tails do not move it.
- A slice whose pitch spreads over more than 1.5 semitones is flagged **Glide**, with a one-click suggestion to split
  it.
- **Global tuning offset:** the average cents error across all slices is measured first and removed before notes are
  assigned, so a sample that is 14 cents sharp reads consistently. The app shows the offset it found.
- **Confidence** (0 to 100) per slice = how much of it is voiced × how steady the pitch is. Below 60 is highlighted
  for checking.

### 3.4 Editor
- Waveform with slice lines and the pitch curve drawn over it.
- Slice list: number, start, length, note, cents, confidence, flags, Keep-as-sung toggle.
- Actions: drag a boundary, split, merge, delete, ±1 octave, set the note by hand, audition (original, or shifted
  to C).
- Saving writes the slice map and starts the Natural render for any slice that changed — **automatically, in the
  background**.

## 4. Natural mode rendering

Natural mode is rendered ahead of time, so live playback has no added delay. Each slice is rendered at every whole
semitone from −12 to +12 around its own pitch: 25 files per slice (render range **±12**; wider costs more files and
render time).

- **Engine:** Rubber Band (R3 "fine" engine, formant preservation on), run through its command-line tool. GPL is fine
  for personal use; a commercial licence would only matter if the tool were ever sold.
- **Fine pitch is handled live, not rendered.** The device picks the nearest rendered semitone and covers the leftover
  (never more than ±50 cents) with a tiny speed change. That keeps the Correction slider, Exact/Nearest switch and
  Octave knob live, with no re-render.
- **Outside ±12:** the device plays the nearest end of the range and makes up the difference with varispeed. Formants
  move a little there; v1 accepts that.
- **Files:** 24-bit WAV at the source's sample rate, one folder per slice, named by shift: `renders/slice_03/+05.wav`.
- **Size example:** a 1-second mono slice at 48 kHz is about 144 KB per file, so 16 such slices × 25 files ≈ 58 MB.
- **Cache:** a slice re-renders only when its boundaries, source or render settings change (a hash of those is stored
  with the render).
- **Render time:** not known yet. It is measured in Milestone 3; if it is slow, rendering runs in the background and
  slices become playable as they finish.

## 5. Max for Live device

The device is a MIDI instrument that loads one slice map and plays it live. It does no analysis.

### 5.1 Keyboard layout (option 1)
- **Slices mode:** from C1 up, each key plays one slice at its corrected pitch (key 1 = slice 1, and so on). The last
  slice played becomes the selected slice.
- **Keys mode:** the whole keyboard plays the selected slice chromatically.
- A **Slices / Keys** switch and a **Selected slice** box on the device, both mappable to a MIDI button or Push.
- No keyswitch zone for picking the selected slice in v1 (device and MIDI mapping only).

### 5.2 Pitch mapping
- **Exact:** C3 always plays C3.
- **Nearest:** each key plays the octave of that note closest to the slice's own pitch (shift always within
  ±6 semitones).
- **Octave** knob, −2 to +2, works in both modes.
- **Correction** slider, 0 to 100% (default 100%): how much of each slice's cents offset is removed. Slices marked
  Keep as sung in the app ignore it.

The pitch sum for every note: shift = played note − detected note − (Correction × slice cents / 100).

### 5.3 Sound modes
- **Natural (default):** plays the pre-rendered file nearest the needed shift, plus the small leftover speed change.
  No added delay.
- **Vintage:** real-time varispeed from the original slice, built in gen~. Interpolation choice: Gritty (linear),
  Warm (cubic), Clean (windowed sinc). Pitch bend and glide work here.

### 5.4 Playing controls
- Mono or Poly (up to 8 voices); in Mono a new note cuts the last one.
- One-shot or Note-on (plays only while held).
- Attack and release, velocity-to-volume amount, per-slice gain, output gain.

### 5.5 Loading
- Drag the slice map file onto the device, or browse to it. The path is saved with the Live set.
- All slice audio loads into memory on load (polybuffer~), so triggering is instant.

## 6. Decisions

Settled 2026-11-16:

| Decision | Choice |
|---|---|
| Source material | Clean acapellas and stems; no chords, no full mixes |
| Sound modes | Both, Natural first and default |
| Mapping | Exact and Nearest, switchable |
| Use | Live playing during production |
| Keyboard layout | Option 1: slice keys plus one selected slice played chromatically |
| Cents correction | Median offset per slice, live 0 to 100% slider (default 100%), per-slice Keep as sung |
| Architecture | Windows Python app for analysis and rendering + Max for Live device for playback |
| Natural mode | Rendered ahead of time with Rubber Band, ±12 semitones per slice |

The seven open questions, settled with their proposed defaults (Sam, 2026-11-16):

| Question | Choice |
|---|---|
| Working name | Vocal Chop Sampler |
| Maximum slices per map | 32 |
| Render range | ±12 semitones |
| Keyswitch zone for the selected slice | No; device and MIDI mapping only in v1 |
| App UI toolkit | Same as OrchSketch: PySide6 (Qt 6) on Python 3.12 |
| Where projects live | `Documents\Vocalchop`, code repo in `Documents\Vocalchop-Dev` |
| Render on save or on a button | Automatically on save, in the background |

## 7. Milestones

Six milestones, one per new chat with SPEC.md + STATUS.md, as with OrchSketch. The riskiest part, correct note
detection, is tested first, before any playback is built.

1. **Pitch analysis.** Load a file, run pYIN, find the global tuning offset, assign note/cents/confidence to each
   detected note, export a test slice map. Includes a simple way to hear and check the detected notes.
   - Check: on 3 of Sam's acapellas, the detected notes match by ear (or against a tuner) on at least 9 of 10
     sustained notes. (2026-11-16: checked with the app's built-in tuner, an independent spectral method, on 10
     sustained notes spread across each song; Ableton's Tuner optional via tuner_check.wav. See §9.)
2. **Chopping and editor.** All four chop modes, slice list, pitch curve, edit actions, audition.
   - Check: Pitch mode lands cuts on note changes; no clicks at slice edges; Glide and low-confidence flags show on
     the right slices.
3. **Natural render.** Rubber Band renders, folder layout, cache, background rendering.
   - Check: ear test of 3 slices at −12, −5, +5 and +12; render time measured and written into STATUS.md.
4. **Device: Natural playback.** Load map, Slices and Keys modes, Exact and Nearest, Octave, Correction slider,
   leftover-cents speed change.
   - Check: in Ableton, pressing C plays C on a tuner in both mapping modes; slices trigger with no audible delay.
5. **Device: Vintage mode and playing controls.** gen~ varispeed with three interpolation choices, Mono/Poly,
   One-shot/Note-on, envelope, velocity.
   - Check: A/B ear test of Natural vs Vintage on the same slices; pitch bend works in Vintage.
6. **Polish and v1.0 release.** High accuracy (CREPE) mode, range presets, settings saved, GitHub repo + zip backup.
   - Check: one full session building a vocal chop part in a real track.

## 8. How we work
- One milestone per chat. Claude writes all the code; Sam tests on his Windows PC and in Ableton and does not edit
  code.
- Each chat starts by reading SPEC.md and STATUS.md; the chat that finishes a milestone updates STATUS.md.
- One-click `.bat` scripts in `scripts\` for setup, running and testing.
- At the end of each milestone: push to GitHub (`shenry42/vocalchop`) and write a zip backup (`scripts\backup.bat`).

## 9. Spec changes
- 2026-11-16: MP3 accepted as an input format (two of Sam's test acapellas are MP3). Pitch analysis is unaffected;
  renders from MP3 sources carry the MP3's quality.
- 2026-11-16: Milestone 1 check by built-in tuner instead of by ear (Sam can't reliably judge notes by ear). The
  tuner (`tuner.py`) works in the frequency domain (harmonic product spectrum, harmonics refined), independent of
  the pYIN analysis. Agreement = same note, or "borderline" (sung within 35 c of halfway between two notes, both
  readings ≤ 50 c apart), which gives the same played pitch at 100% Correction. The 10 check notes are sustained
  notes picked evenly across the song, not the most confident ones.
- 2026-11-16: folder names follow the folders Sam created: `Documents\Vocalchop` and `Documents\Vocalchop-Dev`
  (Windows ignores letter case, so the Doc's `VocalChop` paths point to the same place).
