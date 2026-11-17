"""Pitch search ranges (SPEC §3.1) and note naming."""
from __future__ import annotations

from dataclasses import dataclass

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(midi: int) -> str:
    """Scientific pitch name: 60 = C4, 64 = E4 (the convention the spec uses)."""
    midi = int(midi)
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def live_note_name(midi: int) -> str:
    """Ableton Live's name for the same note: Live calls MIDI 60 'C3', one octave lower than scientific."""
    midi = int(midi)
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 2}"


def parse_note(name: str) -> int:
    """'E3' or 'C#5' (scientific) -> MIDI number."""
    name = name.strip().upper()
    letter = name[:2] if len(name) > 2 and name[1] == "#" else name[:1]
    octave = int(name[len(letter):])
    return NOTE_NAMES.index(letter) + (octave + 1) * 12


@dataclass(frozen=True)
class RangePreset:
    key: str
    label: str
    low: int   # MIDI
    high: int  # MIDI


PRESETS = {
    "male": RangePreset("male", "Male vocal (C2 to C5)", parse_note("C2"), parse_note("C5")),
    "female": RangePreset("female", "Female vocal (E3 to C6)", parse_note("E3"), parse_note("C6")),
    "instrument": RangePreset("instrument", "Instrument (C1 to C7)", parse_note("C1"), parse_note("C7")),
}


def preset_from_key(key: str) -> RangePreset:
    """'male' / 'female' / 'instrument' / 'custom:C2-G5'."""
    if key in PRESETS:
        return PRESETS[key]
    if key.startswith("custom:"):
        lo, hi = key[len("custom:"):].split("-")
        low, high = parse_note(lo), parse_note(hi)
        if high <= low:
            raise ValueError("Custom range: the top note must be above the bottom note")
        return RangePreset(key, f"Custom ({lo} to {hi})", low, high)
    raise ValueError(f"Unknown range preset: {key}")


def midi_to_hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
