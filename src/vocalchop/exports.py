"""Files for checking the detected notes outside the app: a guide-tone WAV and a MIDI file."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from .notes import Note
from .presets import midi_to_hz

TONE_LEVEL = 0.18      # about −15 dBFS
FADE_SECONDS = 0.010
GUIDE_TEMPO = 120.0    # the MIDI file is written at 120 BPM (see README: set Live to 120 BPM before dragging it in)


def tone(freq: float, seconds: float, sample_rate: int, level: float = TONE_LEVEL) -> np.ndarray:
    """A soft, easy-to-hear reference tone: a sine with a little 2nd and 3rd harmonic, with short fades."""
    n = max(1, int(round(seconds * sample_rate)))
    t = np.arange(n) / sample_rate
    w = 2 * np.pi * freq * t
    y = np.sin(w) + 0.25 * np.sin(2 * w) + 0.1 * np.sin(3 * w)
    y *= level / 1.35
    f = min(n // 2, int(FADE_SECONDS * sample_rate))
    if f > 0:
        ramp = np.linspace(0.0, 1.0, f)
        y[:f] *= ramp
        y[-f:] *= ramp[::-1]
    return y.astype(np.float32)


def note_tone_hz(n: Note, tuning_offset_cents: float) -> float:
    """The detected note played in the singer's own tuning (note + global offset), so it can be compared by ear."""
    return midi_to_hz(n.note + tuning_offset_cents / 100.0)


def guide_wav(notes: list[Note], tuning_offset_cents: float, duration: float, sample_rate: int) -> np.ndarray:
    out = np.zeros(int(round(duration * sample_rate)) + 1, dtype=np.float32)
    for n in notes:
        a = int(round(n.start * sample_rate))
        y = tone(note_tone_hz(n, tuning_offset_cents), n.length, sample_rate)
        b = min(len(out), a + len(y))
        out[a:b] += y[:b - a]
    return out


def write_guide_wav(path: Path, notes: list[Note], tuning_offset_cents: float, duration: float,
                    sample_rate: int) -> None:
    sf.write(str(path), guide_wav(notes, tuning_offset_cents, duration, sample_rate), sample_rate,
             subtype="PCM_16")


def write_notes_midi(path: Path, notes: list[Note], name: str) -> None:
    import mido

    tpb = 480
    sec_per_tick = 60.0 / GUIDE_TEMPO / tpb
    mid = mido.MidiFile(ticks_per_beat=tpb)
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tr.append(mido.MetaMessage("track_name", name=name[:60], time=0))
    tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(GUIDE_TEMPO), time=0))
    events = []
    for n in notes:
        on = int(round(n.start / sec_per_tick))
        off = max(on + 1, int(round(n.end / sec_per_tick)))
        vel = int(np.clip(30 + n.confidence, 1, 127))
        events.append((on, 1, mido.Message("note_on", note=n.note, velocity=vel)))
        events.append((off, 0, mido.Message("note_off", note=n.note, velocity=0)))
    events.sort(key=lambda e: (e[0], e[1]))
    now = 0
    for t, _order, msg in events:
        tr.append(msg.copy(time=t - now))
        now = t
    mid.save(str(path))


# ---------------------------------------------------------------- tuner check file (Milestone 1 check, by tuner)

TUNER_SLOT = 3.0       # one note every 3 seconds...
TUNER_HOLD = 2.0       # ...held for 2 seconds, then 1 second of silence
TUNER_LEAD = 1.0       # silence before the first note
TUNER_XFADE = 0.015


def pick_check_notes(notes: list[Note], count: int = 10) -> list[Note]:
    """`count` sustained notes spread evenly across the song (not the most confident ones, so the test is fair)."""
    from .notes import is_sustained

    sus = [n for n in notes if is_sustained(n)]
    if len(sus) <= count:
        return sus
    picks = []
    for k in range(count):
        a = k * len(sus) // count
        b = (k + 1) * len(sus) // count
        picks.append(sus[(a + b - 1) // 2])
    return picks


def tuner_slot_start(k: int) -> float:
    """Where note k (1-based) starts in tuner_check.wav, in seconds."""
    return TUNER_LEAD + (k - 1) * TUNER_SLOT


def held_note(mono: np.ndarray, sample_rate: int, n: Note, seconds: float = TUNER_HOLD) -> np.ndarray:
    """The steady middle of a note, looped with crossfades to last `seconds`, so a tuner can settle on it."""
    s, e = n.start + 0.15 * n.length, n.end - 0.15 * n.length
    if e - s < 0.15:
        mid = (n.start + n.end) / 2
        s, e = max(n.start, mid - 0.075), min(n.end, mid + 0.075)
    core = mono[int(s * sample_rate):int(e * sample_rate)].astype(np.float32)
    x = max(1, min(int(TUNER_XFADE * sample_rate), len(core) // 4))
    need = int(seconds * sample_rate)
    out = core.copy()
    fade_in, fade_out = np.linspace(0, 1, x, dtype=np.float32), np.linspace(1, 0, x, dtype=np.float32)
    while len(out) < need:
        out[-x:] = out[-x:] * fade_out + core[:x] * fade_in
        out = np.concatenate([out, core[x:]])
    out = out[:need]
    f = min(len(out) // 2, int(0.02 * sample_rate))
    out[:f] *= np.linspace(0, 1, f)
    out[-f:] *= np.linspace(1, 0, f)
    peak = float(np.abs(out).max()) or 1.0
    return out * (0.5 / peak)


def write_tuner_check(folder: Path, mono: np.ndarray, sample_rate: int, picks: list[Note], source_name: str,
                      extra: list[Note] | None = None) -> None:
    from .presets import live_note_name, note_name

    extra = extra or []
    allnotes = list(picks) + list(extra)
    total = tuner_slot_start(len(allnotes) + 1) if allnotes else TUNER_LEAD
    out = np.zeros(int(total * sample_rate), dtype=np.float32)
    lines = [f"Tuner check for {source_name}",
             "Optional double-check of the app's built-in tuner with Ableton's own Tuner:",
             "put tuner_check.wav on an audio track (Warp off) with the Tuner on it and play. One note every 3 s.",
             "Compare the Tuner's note name with the Live name below.",
             "", "  #   in file   Live name   (standard name)   song time"]
    for k, n in enumerate(allnotes, start=1):
        if k == len(picks) + 1:
            lines += ["", "  After the 10 check notes: notes where the built-in tuner and the analysis disagree", ""]
        a = int(tuner_slot_start(k) * sample_rate)
        y = held_note(mono, sample_rate, n)
        out[a:a + len(y)] = y[:len(out) - a]
        t = tuner_slot_start(k)
        lines.append(f"{k:3d}   {int(t // 60)}:{t % 60:04.1f}     {live_note_name(n.note):<6}      ({note_name(n.note)})"
                     f"            {int(n.start // 60)}:{n.start % 60:06.3f}")
    sf.write(str(Path(folder) / "tuner_check.wav"), out, sample_rate, subtype="PCM_16")
    (Path(folder) / "tuner_check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
