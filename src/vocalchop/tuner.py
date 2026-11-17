"""A built-in tuner that measures a note independently of the pYIN analysis.

It works in the frequency domain (harmonic product spectrum), like many hardware tuners, while the analysis works on
the waveform (pYIN + period refinement). It looks only at the audio of the note, never at the analysis's pitch
curve, so when both name the same note that is two different methods agreeing.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .notes import Note


@dataclass
class TunerReading:
    hz: float          # measured frequency (0 if nothing found)
    midi: float        # fractional MIDI
    note: int          # nearest note
    cents: int         # offset from that note (not corrected for the song's tuning offset)
    clarity: float     # 0..1, how clearly one pitch stands out

    @property
    def ok(self) -> bool:
        return self.hz > 0


def _note_core(mono: np.ndarray, sr: int, n: Note) -> np.ndarray:
    """The steady middle 70% of the note (skips scoops and tails)."""
    s, e = n.start + 0.15 * n.length, n.end - 0.15 * n.length
    if e - s < 0.1:
        s, e = n.start, n.end
    return mono[int(s * sr):int(e * sr)].astype(np.float64)


def read_tuner(mono: np.ndarray, sr: int, n: Note, low_midi: int, high_midi: int, harmonics: int = 5,
               ) -> TunerReading:
    x = _note_core(mono, sr, n)
    if len(x) < int(0.05 * sr) or not np.any(x):
        return TunerReading(0.0, 0.0, 0, 0, 0.0)
    # several overlapping 186 ms windows averaged (more stable through vibrato), zero-padded 4x for resolution
    win = min(len(x), int(0.186 * sr))
    hop = max(1, win // 2)
    nfft = 1 << int(np.ceil(np.log2(win * 4)))
    w = np.hanning(win)
    spec = np.zeros(nfft // 2 + 1)
    for a in range(0, len(x) - win + 1, hop):
        spec += np.abs(np.fft.rfft(x[a:a + win] * w, nfft))
    spec /= max(1e-12, spec.max())
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    fmin = 440.0 * 2 ** ((low_midi - 1 - 69) / 12)
    fmax = 440.0 * 2 ** ((high_midi + 1 - 69) / 12)
    lo, hi = np.searchsorted(freqs, fmin), np.searchsorted(freqs, fmax)
    # harmonic product spectrum (in logs): the fundamental is where all harmonics line up
    logspec = np.log(spec + 1e-6)
    hps = np.zeros(hi - lo)
    for h in range(1, harmonics + 1):
        idx = np.arange(lo, hi) * h
        valid = idx < len(logspec)
        hps[valid] += logspec[idx[valid]]
        hps[~valid] += np.log(1e-6)
    k = lo + int(np.argmax(hps))
    # missing or weak fundamental (e.g. a low-cut on a male voice): harmonics also sit at 1.5x and 2.5x the chosen
    # pitch only if the real note is an octave lower
    half = k / 2.0
    if half * sr / nfft >= fmin:
        def peak_near(c):
            a, b = max(1, int(c * 0.98)), min(len(spec) - 1, int(np.ceil(c * 1.02)))
            return float(spec[a:b + 1].max()) if b >= a else 0.0
        odd = [peak_near(m * half) for m in (3, 5)]
        if min(odd) > 0.1 * max(peak_near(k), 1e-9):
            k = int(round(half))
    # refine: find each of the first 8 harmonics near h × f0 (parabolic peak on log magnitude), and combine their
    # f/h estimates weighted by strength; higher harmonics give finer resolution than the fundamental alone
    f0 = k * sr / nfft
    ests, wts = [], []
    for h in range(1, 9):
        c = h * f0 * nfft / sr
        a, b = max(1, int(c * 0.97)), min(len(spec) - 2, int(np.ceil(c * 1.03)))
        if b <= a:
            break
        j = a + int(np.argmax(spec[a:b + 1]))
        if spec[j] < 0.02:
            continue
        y0, y1, y2 = (np.log(spec[j + d] + 1e-9) for d in (-1, 0, 1))
        den = y0 - 2 * y1 + y2
        shift = 0.5 * (y0 - y2) / den if den < 0 else 0.0
        ests.append((j + float(np.clip(shift, -1, 1))) * sr / nfft / h)
        wts.append(spec[j])
    if not ests:
        return TunerReading(0.0, 0.0, 0, 0, 0.0)
    order = np.argsort(ests)
    c = np.cumsum(np.array(wts)[order])
    hz = float(np.array(ests)[order][np.searchsorted(c, c[-1] / 2)])
    midi = 69 + 12 * np.log2(hz / 440.0)
    note = int(round(midi))
    srt = np.sort(hps)
    clarity = float(np.clip((srt[-1] - np.median(srt)) / 10.0, 0, 1))
    return TunerReading(hz=float(hz), midi=float(midi), note=note, cents=int(round(100 * (midi - note))),
                        clarity=clarity)


def agrees(reading: TunerReading, n: Note, tuning_offset_cents: float) -> bool:
    """Same note once the song's tuning offset is taken into account."""
    if not reading.ok:
        return False
    return int(round(reading.midi - tuning_offset_cents / 100.0)) == n.note


BORDERLINE_CENTS = 35   # both readings within this of the halfway point between two notes = borderline


def compare(reading: TunerReading, n: Note, tuning_offset_cents: float) -> tuple[str, float]:
    """('match' | 'borderline' | 'different' | 'no reading', cents between the two pitch readings).

    'borderline': the two name neighbouring notes for a pitch sung close to halfway between them (both readings
    within 35 cents of the halfway point, and at most 50 cents apart). At 100% Correction both names give the same
    played pitch, so this is not a naming error.
    """
    if not reading.ok:
        return "no reading", float("nan")
    t = reading.midi - tuning_offset_cents / 100.0     # tuner, in the song's tuning
    a = n.note + n.cents / 100.0                       # analysis
    apart = abs(t - a) * 100.0
    tn = int(round(t))
    if tn == n.note:
        return "match", apart
    if abs(tn - n.note) == 1 and apart <= 50:
        half = (tn + n.note) / 2.0
        if abs(t - half) * 100 <= BORDERLINE_CENTS and abs(a - half) * 100 <= BORDERLINE_CENTS:
            return "borderline", apart
    return "different", apart
