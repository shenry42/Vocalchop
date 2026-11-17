"""Synthetic 'voices' with known notes, for the automated tests.

A voice here is a harmonic tone (15 harmonics, a vowel-like spectral tilt) whose pitch follows a path built from
notes, with optional vibrato, scoops into notes, a global detune, breath noise and gaps.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SR = 44100


@dataclass
class SungNote:
    midi: float           # the note as sung (may include cents, e.g. 64.18)
    seconds: float
    gap_after: float = 0.15
    scoop: float = 0.0    # semitones below the note the voice starts from (slides up over 60 ms)
    glide_to: float | None = None   # if set, the pitch slides evenly from midi to this over the whole note


def sing(notes: list[SungNote], detune_cents: float = 0.0, vibrato_semitones: float = 0.0,
         vibrato_hz: float = 5.5, noise_db: float = -60.0, sr: int = SR, lead_in: float = 0.3,
         seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    parts = [np.zeros(int(lead_in * sr))]
    for n in notes:
        k = int(n.seconds * sr)
        t = np.arange(k) / sr
        path = np.full(k, n.midi + detune_cents / 100.0)
        if n.glide_to is not None:
            path = np.linspace(n.midi, n.glide_to, k) + detune_cents / 100.0
        if n.scoop:
            s = min(k, int(0.06 * sr))
            path[:s] -= np.linspace(n.scoop, 0.0, s)
        if vibrato_semitones:
            onset = np.clip(t / 0.25, 0, 1)   # vibrato fades in over 250 ms, like a singer
            path = path + vibrato_semitones * onset * np.sin(2 * np.pi * vibrato_hz * t + rng.uniform(0, 6.28))
        f = 440.0 * 2 ** ((path - 69) / 12)
        phase = 2 * np.pi * np.cumsum(f) / sr
        y = np.zeros(k)
        for h in range(1, 16):
            amp = (1.0 / h) * (1.6 if h in (2, 3) else 1.0)   # a little vowel colour
            y += amp * np.sin(h * phase)
        env = np.ones(k)
        a = min(k // 2, int(0.03 * sr))
        r = min(k // 2, int(0.06 * sr))
        env[:a] = np.linspace(0, 1, a)
        env[-r:] = np.linspace(1, 0, r)
        parts.append(0.25 * y / 3.0 * env)
        parts.append(np.zeros(int(n.gap_after * sr)))
    out = np.concatenate(parts)
    out += 10 ** (noise_db / 20) * rng.standard_normal(len(out))
    return out.astype(np.float32)


def write(path, y: np.ndarray, sr: int = SR, subtype: str | None = None, stereo: bool = False) -> None:
    import soundfile as sf

    data = np.stack([y, y * 0.9], axis=1) if stereo else y
    sf.write(str(path), data, sr, subtype=subtype)
