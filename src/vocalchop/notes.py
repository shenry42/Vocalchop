"""From a pitch track to notes: splitting, global tuning offset, note/cents/confidence/flags (SPEC §3.2, §3.3).

Milestone 1 has only the basic Pitch-mode splitter needed to name notes; the full chop rules (pre-roll, zero
crossings, fades, the other chop modes) come in Milestone 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .pitch import PitchTrack


@dataclass
class NoteParams:
    voiced_threshold: float = 0.5     # voicing probability above this = voiced (SPEC §3.3)
    change_semitones: float = 0.7     # a cut needs the pitch to move more than this (SPEC §3.2)...
    hold_ms: float = 80.0             # ...and hold the new note this long
    vibrato_return_semitones: float = 0.35  # if the pitch comes back this close within the next hold, it was vibrato
    flat_semitones: float = 0.35      # a new note must be this flat (smoothed) over hold_ms, so glides aren't chopped
    smooth_ms: float = 150.0          # split decisions use pitch averaged over this (≈ one vibrato cycle)
    bridge_ms: float = 35.0           # unvoiced gaps this short inside a note (dropouts, soft consonants) are bridged
    min_note_ms: float = 60.0         # shorter pieces merge into a neighbour, or are dropped if alone
    gate_below_peak_db: float = 45.0  # frames quieter than (loudest frame − this) count as silence
    glide_semitones: float = 1.5      # pitch spread (10th–90th percentile) above this = Glide (SPEC §3.3)
    low_confidence: int = 60          # below this = highlighted for checking (SPEC §3.3)
    sustained_ms: float = 250.0       # notes at least this long count as "sustained" for the ear test


@dataclass
class Note:
    index: int
    start: float          # seconds
    end: float            # seconds
    pitch: float          # detected pitch, fractional MIDI, before the offset is removed
    note: int = 0         # MIDI note after the global offset is removed
    cents: int = 0        # offset from `note`, after the global offset is removed
    confidence: int = 0   # 0..100
    flags: list[str] = field(default_factory=list)
    spread: float = 0.0   # semitones (10th–90th percentile)
    steadiness: float = 0.0
    voicedness: float = 0.0
    start_frame: int = 0
    end_frame: int = 0

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class NoteAnalysis:
    notes: list[Note]
    tuning_offset_cents: float
    voiced_mask: np.ndarray


# ---------------------------------------------------------------- helpers

def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    if w.sum() <= 0:
        return float(np.median(v))
    c = np.cumsum(w)
    return float(v[np.searchsorted(c, 0.5 * c[-1])])


def weighted_percentile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    if w.sum() <= 0:
        return float(np.percentile(v, q))
    c = np.cumsum(w)
    return float(v[min(len(v) - 1, np.searchsorted(c, q / 100.0 * c[-1]))])


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) of each run of True."""
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def _fill(midi: np.ndarray, a: int, b: int) -> np.ndarray:
    """Pitch inside a run with unvoiced gaps filled in (linear) and single-frame glitches removed (5-frame median)."""
    from scipy.signal import medfilt

    seg = midi[a:b].copy()
    good = ~np.isnan(seg)
    if not good.any():
        return seg
    idx = np.arange(len(seg))
    seg[~good] = np.interp(idx[~good], idx[good], seg[good])
    if len(seg) >= 5:
        seg = medfilt(seg, 5)
    return seg


def _vibrato_smooth(m: np.ndarray, frames: int) -> np.ndarray:
    """Moving average over about one vibrato cycle: vibrato shrinks to ~1/5, real note changes remain."""
    if len(m) < 3 or frames < 2:
        return m
    k = min(frames, len(m))
    padded = np.pad(m, (k // 2, k - 1 - k // 2), mode="edge")
    return np.convolve(padded, np.ones(k) / k, mode="valid")


def voiced_frames(track: PitchTrack, p: NoteParams) -> np.ndarray:
    with np.errstate(divide="ignore"):
        db = 20 * np.log10(np.maximum(track.rms, 1e-10))
    gate = max(db.max() - p.gate_below_peak_db, -70.0)
    return (track.voiced_prob > p.voiced_threshold) & ~np.isnan(track.midi) & (db > gate)


def _bridge(mask: np.ndarray, midi: np.ndarray, max_gap: int, change: float) -> np.ndarray:
    """Join voiced runs separated by a short gap when the pitch on both sides is close."""
    out = mask.copy()
    runs = _runs(mask)
    for (a0, b0), (a1, b1) in zip(runs, runs[1:]):
        gap = a1 - b0
        if 0 < gap <= max_gap:
            through = midi[b0 - 1:a1 + 1]       # last voiced frame, the gap, first voiced frame
            smooth = np.isfinite(through).all() and np.abs(np.diff(through)).max() <= 0.5
            ends_close = np.isfinite(through[0]) and np.isfinite(through[-1]) and abs(through[-1] - through[0]) <= change
            if smooth or ends_close:
                out[b0:a1] = True
    return out


def _split_run(raw: np.ndarray, m: np.ndarray, p: NoteParams, hold: int) -> list[int]:
    """Cut points (indices into the run) where the pitch moves to a new, held note.

    `m` is the vibrato-smoothed pitch used for the decisions; `raw` (gap-filled, glitch-free) places the cut where
    the pitch actually crosses halfway between the old and the new note.
    """
    cuts = []
    seg_start = 0
    depart = None
    i = 1
    n = len(m)
    while i < n:
        ref = float(np.median(m[seg_start:i]))
        if abs(m[i] - ref) > p.change_semitones:
            if depart is None:
                depart = i
            w = m[i:i + hold]
            new = float(np.median(w)) if len(w) else ref
            if len(w) == hold and abs(new - ref) > p.change_semitones and (w.max() - w.min()) <= p.flat_semitones:
                after = m[i + hold:i + 2 * hold]
                came_back = len(after) > 0 and abs(np.median(after) - ref) < p.vibrato_return_semitones
                if not came_back:
                    cut = _crossing(raw, depart, (ref + new) / 2.0, hold, seg_start)
                    if cut - seg_start >= 1:
                        cuts.append(cut)
                        seg_start = cut
                    depart = None
                    i = max(i + 1, cut + 1)
                    continue
            i += 1
        else:
            depart = None
            i += 1
    return cuts


def _crossing(raw: np.ndarray, near: int, level: float, reach: int, floor: int) -> int:
    """Frame near `near` where the raw pitch first crosses `level` (falls back to `near`)."""
    a = max(floor + 1, near - reach)
    b = min(len(raw) - 1, near + reach)
    side = np.sign(raw[a - 1] - level)
    for j in range(a, b + 1):
        if np.sign(raw[j] - level) != side and side != 0:
            return j
    return near


def split_notes(track: PitchTrack, p: NoteParams) -> tuple[list[tuple[int, int]], np.ndarray]:
    """[start, end) frame ranges of the notes, plus the voiced mask."""
    frame_ms = track.hop_seconds * 1000.0
    hold = max(2, int(round(p.hold_ms / frame_ms)))
    smooth = max(1, int(round(p.smooth_ms / frame_ms)))
    min_len = max(1, int(round(p.min_note_ms / frame_ms)))
    voiced = voiced_frames(track, p)
    joined = _bridge(voiced, track.midi, int(round(p.bridge_ms / frame_ms)), p.change_semitones)

    pieces: list[tuple[int, int]] = []
    for a, b in _runs(joined):
        raw = _fill(track.midi, a, b)
        m = _vibrato_smooth(raw, smooth)
        bounds = [0] + _split_run(raw, m, p, hold) + [b - a]
        run_pieces = [(a + s, a + e) for s, e in zip(bounds, bounds[1:]) if e > s]
        # merge pieces shorter than min_len into the neighbour with the closer pitch
        merged = True
        while merged and len(run_pieces) > 1:
            merged = False
            for k, (s, e) in enumerate(run_pieces):
                if e - s < min_len:
                    here = np.nanmedian(track.midi[s:e]) if np.isfinite(track.midi[s:e]).any() else np.nan
                    left = run_pieces[k - 1] if k > 0 else None
                    right = run_pieces[k + 1] if k + 1 < len(run_pieces) else None

                    def dist(nb):
                        if nb is None:
                            return np.inf
                        v = track.midi[nb[0]:nb[1]]
                        return abs(np.nanmedian(v) - here) if np.isfinite(v).any() and np.isfinite(here) else 1e6

                    if dist(left) <= dist(right):
                        run_pieces[k - 1] = (left[0], e)
                    else:
                        run_pieces[k + 1] = (s, right[1])
                    del run_pieces[k]
                    merged = True
                    break
        pieces.extend(pc for pc in run_pieces if pc[1] - pc[0] >= min_len)
    return pieces, voiced


def _circular_mean_cents(devs: np.ndarray, weights: np.ndarray) -> float:
    """Mean of cents deviations on a circle (−50 and +50 are the same place)."""
    if len(devs) == 0 or weights.sum() <= 0:
        return 0.0
    ang = devs / 100.0 * 2 * np.pi
    s, c = (weights * np.sin(ang)).sum(), (weights * np.cos(ang)).sum()
    if abs(s) < 1e-12 and abs(c) < 1e-12:
        return 0.0
    return float(np.arctan2(s, c) / (2 * np.pi) * 100.0)


def analyze_notes(track: PitchTrack, p: NoteParams | None = None) -> NoteAnalysis:
    p = p or NoteParams()
    pieces, voiced = split_notes(track, p)
    notes: list[Note] = []
    for k, (s, e) in enumerate(pieces, start=1):
        frames = np.arange(s, e)
        vmask = voiced[s:e]
        vals = track.midi[s:e][vmask]
        w = track.rms[s:e][vmask]
        if len(vals) == 0:
            continue
        pitch = weighted_median(vals, w)
        spread = weighted_percentile(vals, w, 90) - weighted_percentile(vals, w, 10)
        mad_cents = 100.0 * weighted_median(np.abs(vals - pitch), w)
        steadiness = float(np.clip((100.0 - mad_cents) / 80.0, 0.0, 1.0))
        voicedness = float(np.mean(np.where(vmask, track.voiced_prob[s:e], 0.0)))
        notes.append(Note(index=k, start=float(track.frame_time(frames[0])), end=float(track.frame_time(e)),
                          pitch=pitch, spread=spread, steadiness=steadiness, voicedness=voicedness,
                          start_frame=s, end_frame=e))

    # Global tuning offset: measured first, from every non-glide note, weighted by how much steady voice it has.
    use = [n for n in notes if n.spread <= p.glide_semitones]
    devs = np.array([100.0 * (n.pitch - round(n.pitch)) for n in use])
    weights = np.array([n.length * n.steadiness * n.voicedness for n in use])
    offset = _circular_mean_cents(devs, weights)

    for i, n in enumerate(notes, start=1):
        n.index = i
        corrected = n.pitch - offset / 100.0
        n.note = int(round(corrected))
        n.cents = int(round(100.0 * (corrected - n.note)))
        n.confidence = int(round(100.0 * np.clip(n.voicedness / 0.85, 0, 1) * n.steadiness))
        n.flags = []
        if n.spread > p.glide_semitones:
            n.flags.append("glide")
        if n.confidence < p.low_confidence:
            n.flags.append("low_confidence")
    return NoteAnalysis(notes=notes, tuning_offset_cents=offset, voiced_mask=voiced)


def is_sustained(n: Note, p: NoteParams | None = None) -> bool:
    p = p or NoteParams()
    return n.length * 1000.0 >= p.sustained_ms and n.confidence >= p.low_confidence and "glide" not in n.flags
