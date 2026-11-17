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
    legato_gap_ms: float = 150.0      # a loud gap this short with a pitch path through it = the slide between
                                      # legato notes: the cut goes where the pitch crosses halfway (Milestone 2)
    min_note_ms: float = 60.0         # shorter pieces merge into a neighbour, or are dropped if alone
    gate_below_peak_db: float = 45.0  # frames quieter than (loudest frame − this) count as silence
    glide_semitones: float = 1.5      # pitch spread (10th–90th percentile) above this = Glide (SPEC §3.3)
    low_confidence: int = 60          # below this = highlighted for checking (SPEC §3.3)
    sustained_ms: float = 250.0       # notes at least this long count as "sustained" for the ear test
    pitch_method: str = "auto"        # how a note's pitch is named (see measure_pitch; settled in Milestone 2)


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


PITCH_METHODS = ("whole", "steadiest", "stable", "smooth150", "core150", "auto")
STEADY_WINDOW_MS = 200.0     # "steadiest": length of the window searched for
CORE_SEMITONES = 0.8
FLAT_SLOPE = 3.0             # "stable": semitones/second at which a frame's weight halves


VIBRATO_MIN_HZ, VIBRATO_MAX_HZ = 3.5, 9.0
VIBRATO_MIN_CENTS = 12.0     # RMS wobble around the smoothed pitch
VIBRATO_MIN_CYCLES = 1.5


def has_vibrato(track: PitchTrack, voiced: np.ndarray, s: int, e: int) -> bool:
    """A regular wobble (3.5-9 Hz, RMS 12 c or more, at least 1.5 cycles) around the smoothed pitch.

    Scoops, fall-offs, short dips and slow bends are single events, not a regular wobble, so they don't count.
    """
    vmask = voiced[s:e]
    if vmask.sum() < 10:
        return False
    frame_ms = track.hop_seconds * 1000.0
    filled = _fill(np.where(vmask, track.midi[s:e], np.nan), 0, e - s)
    sm = _vibrato_smooth(filled, max(1, int(round(150.0 / frame_ms))))
    res = filled - sm                     # over the whole note: pYIN often drops voicing on fast, wide vibrato
    core = np.abs(res) <= np.percentile(np.abs(res), 90)
    rms_c = 100.0 * float(np.sqrt(np.mean(res[core] ** 2)))
    if rms_c < VIBRATO_MIN_CENTS:
        return False
    signs = np.sign(res[np.abs(res) > 0.03])
    crossings = int(np.count_nonzero(np.diff(signs) != 0))
    rate = crossings / 2.0 / max((e - s) * track.hop_seconds, 1e-9)
    return crossings / 2.0 >= VIBRATO_MIN_CYCLES and VIBRATO_MIN_HZ <= rate <= VIBRATO_MAX_HZ


def vibrato_depth_cents(track: PitchTrack, voiced: np.ndarray, s: int, e: int) -> float:
    """About how far a vibrato swings either side of its centre, in cents: the 10th-90th percentile range of the
    pitch after removing its straight-line trend, divided by 1.9 (that range is 1.9 x the swing for a sine).
    0 for a steady note, ~80 for ±0.8 st vibrato, whatever the vibrato rate."""
    vmask = voiced[s:e]
    if vmask.sum() < 10:
        return 0.0
    filled = _fill(np.where(vmask, track.midi[s:e], np.nan), 0, e - s)
    x = np.arange(len(filled))
    res = filled - np.polyval(np.polyfit(x, filled, 1), x)
    return float(100.0 * (np.percentile(res, 90) - np.percentile(res, 10)) / 1.9)


def measure_pitch(track: PitchTrack, voiced: np.ndarray, s: int, e: int, method: str = "whole",
                  smooth_ms: float = 150.0) -> float:
    """Pitch (fractional MIDI) of frames [s, e), by one of PITCH_METHODS.

    whole        loudness-weighted median of all voiced frames (Milestone 1)
    steadiest    median of the 200 ms stretch where the vibrato-smoothed pitch moves least
    stable       loudness x flatness weighted median: frames where the smoothed pitch is moving (scoops, falls,
                 dips, bends) count less; flatness = 1 / (1 + (slope / 3 st/s)^2)
    smooth150    median of the vibrato-smoothed pitch (150 ms moving average)
    core150      smooth150 after dropping frames more than 0.8 st from the whole-note median (scoops, falls)
    auto         core150 when the note has vibrato (has_vibrato), otherwise whole. The default since
                 Milestone 2: see tests/study_note_pitch.py and STATUS.md for the numbers behind the choice.
    """
    vmask = voiced[s:e]
    vals = track.midi[s:e][vmask]
    w = track.rms[s:e][vmask]
    if len(vals) == 0:
        return float("nan")
    if method == "auto":
        method = "core150" if has_vibrato(track, voiced, s, e) else "whole"
    if method == "whole" or len(vals) < 5:
        return weighted_median(vals, w)
    frame_ms = track.hop_seconds * 1000.0
    if method.startswith("smooth"):
        smooth_ms = float(method[len("smooth"):])
    if method.startswith("core"):
        m0 = weighted_median(vals, w)
        raw = track.midi[s:e]
        keep = vmask & (np.abs(raw - m0) <= CORE_SEMITONES)
        if keep.sum() < 5:
            return m0
        filled_c = _fill(np.where(keep, raw, np.nan), 0, e - s)
        smc = _vibrato_smooth(filled_c, max(1, int(round(float(method[4:]) / frame_ms))))
        return weighted_median(smc[keep], track.rms[s:e][keep])
    filled = _fill(np.where(vmask, track.midi[s:e], np.nan), 0, e - s)
    sm = _vibrato_smooth(filled, max(1, int(round(smooth_ms / frame_ms))))
    if method.startswith("smooth"):
        return weighted_median(sm[vmask], w)
    if method == "steadiest":
        k = max(3, min(len(sm), int(round(STEADY_WINDOW_MS / frame_ms))))
        if k >= len(sm):
            return weighted_median(vals, w)
        win = np.lib.stride_tricks.sliding_window_view(sm, k)
        vwin = np.lib.stride_tricks.sliding_window_view(vmask, k).mean(axis=1)
        score = win.max(axis=1) - win.min(axis=1) + 10.0 * (vwin < 0.8)
        a = int(np.argmin(score))
        m = vmask[a:a + k]
        if not m.any():
            return weighted_median(vals, w)
        return weighted_median(track.midi[s + a:s + a + k][m], track.rms[s + a:s + a + k][m])
    slope = np.abs(np.gradient(sm)) / track.hop_seconds          # semitones per second
    flat = 1.0 / (1.0 + (slope / FLAT_SLOPE) ** 2)
    ww = (track.rms[s:e] * flat)[vmask]
    if method == "stable":
        return weighted_median(vals, ww)
    raise ValueError(f"unknown pitch method {method}")


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
                    # the halfway crossing lies somewhere between the departure and here (Milestone 2: the
                    # search used to stop 80 ms after the departure, so on a long slide the cut fell back to
                    # the very start of the slide)
                    cut = _crossing(raw, depart, (ref + new) / 2.0, hold, seg_start, until=i + hold)
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


def _crossing(raw: np.ndarray, near: int, level: float, reach: int, floor: int, until: int | None = None) -> int:
    """Frame from `near` - reach on where the raw pitch first crosses `level` (falls back to `near`)."""
    a = max(floor + 1, near - reach)
    b = min(len(raw) - 1, max(near + reach, until if until is not None else 0))
    side = np.sign(raw[a - 1] - level)
    for j in range(a, b + 1):
        if np.sign(raw[j] - level) != side and side != 0:
            return j
    return near


def _join_legato(pieces: list[tuple[int, int]], track: PitchTrack, voiced: np.ndarray,
                 p: NoteParams) -> list[tuple[int, int]]:
    """Close the gap between two notes when it is the pitch slide of a legato change, not a silence.

    pYIN usually marks the 30-80 ms where the pitch slides from one note to the next as unvoiced, which left a
    gap and started the next note late (the start of its slice then held the end of the slide). A gap counts as a
    legato slide when it is at most legato_gap_ms, loud (above the gate) all through, has a pitch reading on at
    least 80% of its frames, and that pitch moves from the first note towards the second. Both notes then meet
    where the pitch crosses halfway between them (or the middle of the gap).
    """
    if len(pieces) < 2:
        return pieces
    max_gap = int(round(p.legato_gap_ms / (track.hop_seconds * 1000.0)))
    with np.errstate(divide="ignore"):
        db = 20 * np.log10(np.maximum(track.rms, 1e-10))
    gate = max(db.max() - p.gate_below_peak_db, -70.0)
    out = [list(pieces[0])]
    for s, e in pieces[1:]:
        ps, pe = out[-1]
        gap = s - pe
        if 0 < gap <= max_gap and (db[pe:s] > gate).all():
            g = track.midi[pe:s]
            finite = np.isfinite(g)
            a = np.nanmedian(track.midi[max(ps, pe - 8):pe][voiced[max(ps, pe - 8):pe]]) \
                if voiced[max(ps, pe - 8):pe].any() else np.nan
            b = np.nanmedian(track.midi[s:min(e, s + 8)][voiced[s:min(e, s + 8)]]) \
                if voiced[s:min(e, s + 8)].any() else np.nan
            if finite.mean() >= 0.8 and np.isfinite(a) and np.isfinite(b) and abs(b - a) > p.change_semitones:
                level = (a + b) / 2
                gf = np.where(finite, g, np.nan)
                side = np.sign(gf - level)
                cross = [j for j in range(1, len(g)) if np.isfinite(side[j]) and np.isfinite(side[j - 1])
                         and side[j] != side[j - 1] and side[j] == np.sign(b - a)]
                cut = pe + (cross[0] if cross else gap // 2)
                out[-1][1] = cut
                out.append([cut, e])
                continue
        out.append([s, e])
    return [(a, b) for a, b in out]


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
    pieces = _join_legato(pieces, track, voiced, p)
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


def measure_range(track: PitchTrack, voiced: np.ndarray, s: int, e: int, p: NoteParams) -> Note | None:
    """A Note (pitch, spread, steadiness, voicedness) for frames [s, e); None if nothing in it is voiced.

    The note name, cents, confidence and flags come later from assign_note, once the tuning offset is known.
    """
    s, e = max(0, int(s)), min(track.n_frames, int(e))
    if e <= s:
        return None
    vmask = voiced[s:e]
    vals = track.midi[s:e][vmask]
    w = track.rms[s:e][vmask]
    if len(vals) == 0:
        return None
    pitch = measure_pitch(track, voiced, s, e, p.pitch_method)
    # Glide spread on the vibrato-smoothed pitch (Milestone 2): a slide moves the smoothed curve, vibrato doesn't.
    # On the raw pitch a wide vibrato (±0.8 st) spread over 1.5 st and was flagged Glide.
    frame_ms = track.hop_seconds * 1000.0
    filled = _fill(np.where(vmask, track.midi[s:e], np.nan), 0, e - s)
    sm = _vibrato_smooth(filled, max(1, int(round(p.smooth_ms / frame_ms))))[vmask]
    spread = weighted_percentile(sm, w, 90) - weighted_percentile(sm, w, 10)
    mad_cents = 100.0 * weighted_median(np.abs(vals - pitch), w)
    steadiness = float(np.clip((100.0 - mad_cents) / 80.0, 0.0, 1.0))
    voicedness = float(np.mean(np.where(vmask, track.voiced_prob[s:e], 0.0)))
    return Note(index=0, start=float(track.frame_time(s)), end=float(track.frame_time(e)), pitch=pitch,
                spread=spread, steadiness=steadiness, voicedness=voicedness, start_frame=s, end_frame=e)


def assign_note(n: Note, offset_cents: float, p: NoteParams) -> None:
    """Note name, cents, confidence and flags, once the song's tuning offset is known (SPEC §3.3)."""
    corrected = n.pitch - offset_cents / 100.0
    n.note = int(round(corrected))
    n.cents = int(round(100.0 * (corrected - n.note)))
    n.confidence = int(round(100.0 * np.clip(n.voicedness / 0.85, 0, 1) * n.steadiness))
    n.flags = []
    if n.spread > p.glide_semitones:
        n.flags.append("glide")
    if n.confidence < p.low_confidence:
        n.flags.append("low_confidence")


def analyze_notes(track: PitchTrack, p: NoteParams | None = None) -> NoteAnalysis:
    p = p or NoteParams()
    pieces, voiced = split_notes(track, p)
    notes: list[Note] = []
    for s, e in pieces:
        n = measure_range(track, voiced, s, e, p)
        if n is not None:
            notes.append(n)

    # Global tuning offset: measured first, from every non-glide note, weighted by how much steady voice it has.
    use = [n for n in notes if n.spread <= p.glide_semitones]
    devs = np.array([100.0 * (n.pitch - round(n.pitch)) for n in use])
    weights = np.array([n.length * n.steadiness * n.voicedness for n in use])
    offset = _circular_mean_cents(devs, weights)

    for i, n in enumerate(notes, start=1):
        n.index = i
        assign_note(n, offset, p)
    return NoteAnalysis(notes=notes, tuning_offset_cents=offset, voiced_mask=voiced)


def is_sustained(n: Note, p: NoteParams | None = None) -> bool:
    p = p or NoteParams()
    return n.length * 1000.0 >= p.sustained_ms and n.confidence >= p.low_confidence and "glide" not in n.flags
