"""Audition clips for the editor (SPEC §3.4 "audition (original, or shifted to C)").

"Shifted to C" plays the slice the way the device will in Natural mode once its renders are ready (Milestone 3):
the nearest rendered semitone plus the leftover as a tiny speed change. Before the renders exist it is a plain
speed change (varispeed), where the length and the formants move with the pitch. Either way the built-in tuner
reads the clip, so the result can be checked on screen instead of by ear.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .chop import Slice, slice_audio
from .tuner import TunerReading, read_tuner

SLICE_KEY_LOW = 36      # MIDI 36 = Live's C1: slice 1 (SPEC §5.1 Slices mode), 37 = slice 2, ...


@dataclass
class _Span:
    """The minimal note-like object the tuner reads (start, end, length in seconds)."""
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


def nearest_c(sl: Slice) -> int:
    """The C closest to the slice's own note (shift within ±6 semitones, like the device's Nearest mode)."""
    n = sl.note
    return int(round(n / 12.0) * 12)


def shift_to(sl: Slice, target: int, correction: float = 1.0) -> float:
    """Semitones the device shifts the slice to play `target` (SPEC §5.2):
    shift = played note − detected note − (Correction × slice cents / 100); Keep as sung ignores Correction."""
    c = 0.0 if sl.keep_as_sung else correction
    return target - sl.note - c * sl.cents / 100.0


def varispeed(x: np.ndarray, sr: int, semitones: float) -> np.ndarray:
    """Play faster/slower so the pitch moves by `semitones` (length changes the other way)."""
    import soxr

    ratio = 2.0 ** (semitones / 12.0)
    if abs(ratio - 1.0) < 1e-9:
        return x
    out = soxr.resample(np.asarray(x, dtype=np.float32), sr, sr / ratio, quality="HQ")
    return out.astype(np.float32)


def original_clip(data: np.ndarray, sr: int, sl: Slice) -> np.ndarray:
    return slice_audio(data, sr, sl)


def shifted_clip(data: np.ndarray, sr: int, sl: Slice, target: int | None = None) -> tuple[np.ndarray, int, float]:
    """(clip, target note, semitones shifted)."""
    target = nearest_c(sl) if target is None else target
    st = shift_to(sl, target)
    return varispeed(slice_audio(data, sr, sl), sr, st), target, st


def natural_clip(render_folder, sr: int, sl: Slice, target: int | None = None):
    """(clip, target, shift, rendered semitone, leftover) from the slice's Natural renders, like the device."""
    import soundfile as sf

    from .render import natural_plan, shift_name

    target = nearest_c(sl) if target is None else target
    st = shift_to(sl, target)
    k, rest = natural_plan(st)
    x, fsr = sf.read(str(Path(render_folder) / shift_name(k)), dtype="float32", always_2d=True)
    return varispeed(x, fsr, rest), target, st, k, rest


def tuner_on_clip(clip: np.ndarray, sr: int, low_midi: int, high_midi: int) -> TunerReading:
    mono = clip.mean(axis=1) if clip.ndim == 2 else clip
    return read_tuner(mono, sr, _Span(0.0, len(mono) / sr), max(0, low_midi - 12), min(127, high_midi + 12))


def tuner_on_slice(mono: np.ndarray, sr: int, sl: Slice, low_midi: int, high_midi: int) -> TunerReading:
    return read_tuner(mono, sr, _Span(sl.start, sl.end), low_midi, high_midi)


def slice_for_key(note: int, count: int) -> int | None:
    """0-based slice for a MIDI key in Slices mode (C1 = slice 1), or None."""
    k = note - SLICE_KEY_LOW
    return k if 0 <= k < count else None
