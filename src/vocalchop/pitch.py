"""pYIN pitch tracking (SPEC §3.3), run in chunks on several cores.

The result is a frame-by-frame track: pitch (as a fractional MIDI number, NaN where unvoiced), the voicing
probability and the frame's loudness. Everything later (notes, offset, confidence) is computed from this track.
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Callable

import numpy as np

ANALYSIS_SR = 22050
FRAME_LENGTH = 2048          # 93 ms: long enough for C1 (33 Hz) periods
HOP_LENGTH = 256             # 11.6 ms per frame
RESOLUTION = 0.1             # pYIN's pitch grid in semitones (finer grids make pYIN collapse to the range floor)
REFINE_WINDOW = 1024         # samples used to refine each voiced frame's pitch to ~1 cent (see refine_f0)
CHUNK_SECONDS = 20.0         # each worker gets this much audio...
PAD_SECONDS = 1.5            # ...plus this much on each side, so chunk edges don't change the result


@dataclass
class PitchTrack:
    midi: np.ndarray          # float, NaN where pYIN found no pitch
    voiced_prob: np.ndarray   # 0..1
    rms: np.ndarray           # linear RMS of the frame (mono, analysis rate)
    hop_seconds: float

    @property
    def n_frames(self) -> int:
        return len(self.midi)

    def frame_time(self, frame: int | np.ndarray) -> float | np.ndarray:
        return np.asarray(frame) * self.hop_seconds

    def times(self) -> np.ndarray:
        return np.arange(self.n_frames) * self.hop_seconds


def to_analysis_rate(mono: np.ndarray, sample_rate: int) -> np.ndarray:
    import librosa

    if sample_rate == ANALYSIS_SR:
        return mono.astype(np.float32)
    return librosa.resample(mono.astype(np.float32), orig_sr=sample_rate, target_sr=ANALYSIS_SR,
                            res_type="soxr_hq").astype(np.float32)


def refine_f0(y: np.ndarray, f0: np.ndarray, voiced: np.ndarray) -> np.ndarray:
    """Sharpen pYIN's 10-cent-grid pitch to about 1 cent.

    For each voiced frame, the squared-difference function of the waveform is computed only for lags within ±7% of
    pYIN's period, and the minimum is found with parabolic interpolation. pYIN still decides which octave and
    whether the frame is voiced; this only moves the pitch within that ±1.2 semitone window.
    """
    out = f0.copy()
    half = REFINE_WINDOW // 2
    ypad = np.pad(y.astype(np.float64), (half, half + ANALYSIS_SR // 30 + 4))
    for j in np.flatnonzero(voiced & np.isfinite(f0)):
        period = ANALYSIS_SR / f0[j]
        lo = max(2, int(np.floor(period * 0.93)))
        hi = int(np.ceil(period * 1.07)) + 1
        c = j * HOP_LENGTH + half                       # frame centre in ypad
        a = c - half
        x0 = ypad[a:a + REFINE_WINDOW]
        lags = np.arange(lo, hi + 1)
        segs = np.lib.stride_tricks.sliding_window_view(ypad[a + lo:a + hi + REFINE_WINDOW + 1], REFINE_WINDOW)
        d = ((segs[:len(lags)] - x0) ** 2).sum(axis=1)
        k = int(np.argmin(d))
        if 0 < k < len(d) - 1:
            den = d[k - 1] - 2 * d[k] + d[k + 1]
            shift = 0.5 * (d[k - 1] - d[k + 1]) / den if den > 0 else 0.0
            lag = lags[k] + float(np.clip(shift, -1, 1))
            new = ANALYSIS_SR / lag
            if abs(12 * np.log2(new / f0[j])) <= 0.6:  # stays inside pYIN's own note
                out[j] = new
    return out


def _pyin_chunk(args):
    y, fmin, fmax = args
    import librosa

    f0, _flag, vp = librosa.pyin(y, fmin=fmin, fmax=fmax, sr=ANALYSIS_SR, frame_length=FRAME_LENGTH,
                                 hop_length=HOP_LENGTH, center=True, resolution=RESOLUTION)
    f0 = refine_f0(y, f0, vp > 0.5)
    return f0, vp


def default_workers() -> int:
    env = os.environ.get("VOCALCHOP_WORKERS")
    if env:
        return max(1, int(env))
    return max(1, min(6, (os.cpu_count() or 2) - 1))


def track_pitch(y: np.ndarray, low_midi: int, high_midi: int, workers: int | None = None,
                progress: Callable[[float], None] | None = None) -> PitchTrack:
    """Run pYIN over `y` (mono, ANALYSIS_SR) with the search range low_midi..high_midi."""
    import librosa

    fmin = float(librosa.midi_to_hz(low_midi))
    fmax = float(librosa.midi_to_hz(high_midi))
    n_frames = 1 + len(y) // HOP_LENGTH

    chunk = int(CHUNK_SECONDS * ANALYSIS_SR) // HOP_LENGTH * HOP_LENGTH
    pad = int(PAD_SECONDS * ANALYSIS_SR) // HOP_LENGTH * HOP_LENGTH
    jobs = []
    for start in range(0, len(y), chunk):
        a = max(0, start - pad)
        b = min(len(y), start + chunk + pad)
        jobs.append((start, a, b))

    f0 = np.full(n_frames, np.nan)
    vp = np.zeros(n_frames)

    def place(job, result):
        start, a, _b = job
        cf0, cvp = result
        first = start // HOP_LENGTH                    # global frame where this chunk's own part begins
        last = min(n_frames, (start + chunk) // HOP_LENGTH)
        off = first - a // HOP_LENGTH                  # same frame, counted inside the padded chunk
        count = min(last - first, len(cf0) - off)
        f0[first:first + count] = cf0[off:off + count]
        vp[first:first + count] = cvp[off:off + count]

    args = [(y[a:b], fmin, fmax) for (_s, a, b) in jobs]
    workers = default_workers() if workers is None else workers
    done = 0
    if workers <= 1 or len(jobs) == 1:
        for job, arg in zip(jobs, args):
            place(job, _pyin_chunk(arg))
            done += 1
            if progress:
                progress(done / len(jobs))
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            for job, result in zip(jobs, pool.map(_pyin_chunk, args)):
                place(job, result)
                done += 1
                if progress:
                    progress(done / len(jobs))

    rms = librosa.feature.rms(y=y, frame_length=FRAME_LENGTH, hop_length=HOP_LENGTH, center=True)[0]
    rms = rms[:n_frames] if len(rms) >= n_frames else np.pad(rms, (0, n_frames - len(rms)))
    with np.errstate(invalid="ignore", divide="ignore"):
        midi = 69.0 + 12.0 * np.log2(f0 / 440.0)
    vp = np.nan_to_num(vp, nan=0.0)
    return PitchTrack(midi=midi, voiced_prob=vp, rms=rms.astype(float), hop_seconds=HOP_LENGTH / ANALYSIS_SR)
