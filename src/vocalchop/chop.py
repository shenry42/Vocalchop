"""Chopping (SPEC §3.2) and the slice edits behind the editor (SPEC §3.4).

Everything here works without the window, so it can be tested directly:

  chop()          the four chop modes inside a region, followed by the rules every mode shares
  SliceEditor     the slices of one song plus split / merge / delete / drag / octave / set note / undo
  slice_audio()   a slice's audio as the device and the renders will play it (3 ms fades, gain)

Rules shared by every mode (SPEC §3.2, with the Milestone 2 interpretation noted in SPEC §9):
  - pieces under 60 ms merge into a neighbour
  - each slice start moves 20 ms earlier to keep consonants, but never into the slice before it: between legato
    notes there is usually no consonant, only the previous note at the wrong pitch. Where a legato cut (Pitch or
    Silence gate mode) does have a consonant burst in the 20 ms before it, the cut moves back to the burst, so the
    consonant opens the next slice instead of ticking at the end of this one
  - every edge snaps to the nearest zero crossing (within 5 ms) of the mono mix
  - 3 ms fades at both ends, applied whenever slice audio is made (slice_audio), so the first and last sample of
    every slice is exactly zero in every channel
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .notes import Note, NoteParams, assign_note, measure_range
from .pitch import PitchTrack

CHOP_MODES = ("pitch", "silence", "equal", "manual")
MODE_LABELS = {"pitch": "Pitch", "silence": "Silence gate", "equal": "Equal", "manual": "Manual"}
EQUAL_COUNTS = (4, 8, 16, 32)
MAX_SLICES = 32
RENDER_SETTINGS = "rubberband-r3-fine-formants|range-12..12|24bit|v1"   # part of each slice's render hash (SPEC §4)


@dataclass
class ChopSettings:
    mode: str = "pitch"
    equal_count: int = 8
    gate_db: float = -45.0       # silence gate threshold, dBFS (SPEC §3.2)
    gate_ms: float = 60.0        # a gap must be at least this long
    preroll_ms: float = 20.0
    snap_ms: float = 5.0         # zero-crossing search reach
    fade_ms: float = 3.0
    min_slice_ms: float = 60.0


@dataclass
class Slice:
    start: float                 # seconds in the source, after the chop rules
    end: float
    pitch: float = float("nan")  # measured pitch, fractional MIDI, before the tuning offset is removed
    det_note: int = 60           # detected note (after the offset is removed)
    det_cents: int = 0
    confidence: int = 0
    flags: list[str] = field(default_factory=list)
    spread: float = 0.0
    steadiness: float = 0.0
    voicedness: float = 0.0
    keep_as_sung: bool = False
    gain: float = 0.0            # dB
    hand_note: int | None = None  # note set by hand (SPEC §3.4 "set the note by hand")
    octave: int = 0              # ±1 octave edits

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def has_pitch(self) -> bool:
        return bool(np.isfinite(self.pitch))

    @property
    def note(self) -> int:
        """The note the device uses: detected (or set by hand), plus octave edits."""
        base = self.hand_note if self.hand_note is not None else self.det_note
        return int(np.clip(base + 12 * self.octave, 0, 127))

    @property
    def cents(self) -> int:
        """Offset from `note`; 0 when the note was set by hand (the true pitch is then unknown)."""
        return 0 if self.hand_note is not None else self.det_cents

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pitch"] = None if not self.has_pitch else round(float(self.pitch), 4)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Slice":
        d = dict(d)
        if d.get("pitch") is None:
            d["pitch"] = float("nan")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------- the four modes (raw ranges, in seconds)

def pitch_ranges(notes: list[Note], region: tuple[float, float]) -> list[tuple[float, float]]:
    """Pitch mode: the detected notes (cut on note changes or where the voice stops), clipped to the region."""
    r0, r1 = region
    out = []
    for n in notes:
        a, b = max(n.start, r0), min(n.end, r1)
        if b > a:
            out.append((a, b))
    return out


def frame_db(mono: np.ndarray, sr: int, hop_s: float = 0.005, win_s: float = 0.010) -> tuple[np.ndarray, float]:
    """Loudness in dBFS (RMS) per hop, and the hop in seconds."""
    hop = max(1, int(round(hop_s * sr)))
    win = max(hop, int(round(win_s * sr)))
    x = np.asarray(mono, dtype=np.float64)
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    sq = np.concatenate([[0.0], np.cumsum(x * x)])
    starts = np.arange(0, len(x) - win + 1, hop)
    ms = (sq[starts + win] - sq[starts]) / win
    with np.errstate(divide="ignore"):
        db = 10 * np.log10(np.maximum(ms, 1e-14))
    return db, hop / sr


def silence_ranges(mono: np.ndarray, sr: int, region: tuple[float, float], gate_db: float = -45.0,
                   gate_ms: float = 60.0) -> list[tuple[float, float]]:
    """Silence gate: the stretches between gaps quieter than gate_db lasting at least gate_ms."""
    r0, r1 = region
    a, b = int(r0 * sr), int(r1 * sr)
    db, hop = frame_db(mono[a:b], sr)
    if len(db) == 0:
        return []
    loud = db >= gate_db
    # quiet runs shorter than gate_ms don't split
    min_gap = max(1, int(round(gate_ms / 1000.0 / hop)))
    runs = _runs(~loud)
    for s, e in runs:
        if e - s < min_gap and s > 0 and e < len(loud):
            loud[s:e] = True
    out = []
    win = 0.010
    for s, e in _runs(loud):
        t0 = r0 + s * hop
        t1 = min(r1, r0 + (e - 1) * hop + win)
        if t1 > t0:
            out.append((t0, t1))
    return out


def equal_ranges(region: tuple[float, float], count: int) -> list[tuple[float, float]]:
    r0, r1 = region
    edges = np.linspace(r0, r1, int(count) + 1)
    return [(float(a), float(b)) for a, b in zip(edges, edges[1:])]


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


# ---------------------------------------------------------------- shared rules

def zero_crossing(mono: np.ndarray, sr: int, t: float, reach_ms: float = 5.0) -> float:
    """The nearest zero crossing to time t (within reach), else t unchanged."""
    i = int(round(t * sr))
    r = max(1, int(round(reach_ms / 1000.0 * sr)))
    a, b = max(1, i - r), min(len(mono) - 1, i + r)
    if b <= a:
        return t
    seg = mono[a - 1:b + 1]
    s = np.signbit(seg)
    cross = np.flatnonzero(s[1:] != s[:-1]) + a      # sample after a sign change
    zeros = np.flatnonzero(seg[1:-1] == 0) + a
    cand = np.concatenate([cross, zeros])
    if len(cand) == 0:
        return t
    j = int(cand[np.argmin(np.abs(cand - i))])
    return j / sr


MERGE_REACH = 0.100   # a short piece only merges into a neighbour this close; a lone one is widened instead


def merge_short(ranges: list[tuple[float, float]], min_s: float,
                limits: tuple[float, float] = (0.0, float("inf"))) -> list[tuple[float, float]]:
    """Pieces shorter than min_s merge into the neighbour across the smaller gap (SPEC §3.2).

    A piece with no neighbour within 100 ms (a lone short syllable between silences) is widened to min_s
    instead, so a merge never swallows a long silence.
    """
    rs = [list(r) for r in ranges]
    while True:
        short = [k for k, (a, b) in enumerate(rs) if b - a < min_s - 1e-9]
        if not short:
            break
        k = min(short, key=lambda j: rs[j][1] - rs[j][0])
        gl = rs[k][0] - rs[k - 1][1] if k > 0 else np.inf
        gr = rs[k + 1][0] - rs[k][1] if k + 1 < len(rs) else np.inf
        if min(gl, gr) <= MERGE_REACH:
            if gl <= gr:
                rs[k - 1][1] = rs[k][1]
            else:
                rs[k + 1][0] = rs[k][0]
            del rs[k]
            continue
        lo = rs[k - 1][1] if k > 0 else limits[0]
        hi = rs[k + 1][0] if k + 1 < len(rs) else limits[1]
        if hi - lo < min_s:
            del rs[k]                       # no room at all (can't happen with gaps > 100 ms)
            continue
        need = min_s - (rs[k][1] - rs[k][0])
        a = max(lo, rs[k][0] - need / 2)
        b = min(hi, a + min_s)
        rs[k] = [max(lo, b - min_s), b]
    return [(a, b) for a, b in rs]


ONSET_RATIO = 4.0     # a 1 ms bin with this many times the high-frequency energy of the 20 ms before = a burst


def consonant_onset(mono: np.ndarray, sr: int, t: float, reach_s: float = 0.020) -> float | None:
    """Start of a consonant burst in the `reach_s` before time t, or None.

    Works on the high-frequency energy (sample-to-sample steps) in 1 ms bins: the first bin in the window with
    ONSET_RATIO times the median of the 20 ms before the window marks the burst.
    """
    b = int(round(t * sr))
    a = b - int(round(reach_s * sr))
    ref_a = a - int(round(0.020 * sr))
    if ref_a < 1 or b > len(mono):
        return None
    d = np.abs(np.diff(np.asarray(mono[ref_a - 1:b], dtype=np.float64)))
    bin_ = max(1, int(round(0.001 * sr)))
    n_ref = (a - ref_a) // bin_
    n_win = (b - a) // bin_
    e = np.array([np.mean(d[k * bin_:(k + 1) * bin_] ** 2) for k in range(n_ref + n_win)])
    ref = float(np.median(e[:n_ref])) if n_ref else 0.0
    if ref <= 0:
        return None
    hits = np.flatnonzero(e[n_ref:] > ONSET_RATIO * ref)
    if len(hits) == 0:
        return None
    return (a + int(hits[0]) * bin_) / sr


def apply_rules(ranges: list[tuple[float, float]], mono: np.ndarray, sr: int,
                s: ChopSettings) -> list[tuple[float, float]]:
    """Merge short pieces, pre-roll (never into the slice before), snap to zero crossings, keep order."""
    dur = len(mono) / sr
    rs = merge_short(sorted(ranges), s.min_slice_ms / 1000.0, (0.0, dur))
    out: list[list[float]] = []
    pre = s.preroll_ms / 1000.0
    for k, (a, b) in enumerate(rs):
        floor = rs[k - 1][1] if k > 0 else 0.0
        out.append([max(floor, a - pre, 0.0), min(b, dur)])
    # snap: shared edges (end == next start) move together
    snapped: dict[float, float] = {}

    def snap(t):
        key = round(t, 6)
        if key not in snapped:
            snapped[key] = zero_crossing(mono, sr, t, s.snap_ms)
        return snapped[key]

    # legato cuts (no gap) get no pre-roll, except when a consonant burst starts just before the cut: then the
    # cut moves back to the burst, so the consonant opens the next slice instead of ticking at the end of this one
    detected = s.mode in ("pitch", "silence")       # Equal and Manual cuts stay exactly where they were put
    for k in range(1, len(out)):
        if detected and abs(out[k][0] - out[k - 1][1]) < 1e-9:
            on = consonant_onset(mono, sr, out[k][0], pre)
            if on is not None and on - out[k - 1][0] >= s.min_slice_ms / 1000.0:
                on = max(on - 0.001, out[k - 1][0] + s.min_slice_ms / 1000.0)
                out[k][0] = out[k - 1][1] = on
    for r in out:
        r[0], r[1] = snap(r[0]), snap(r[1])
    for k in range(1, len(out)):
        out[k][0] = max(out[k][0], out[k - 1][1])
    return [(a, b) for a, b in out if b - a > 1.0 / sr]


# ---------------------------------------------------------------- measuring a slice

@dataclass
class SongContext:
    """What chopping and measuring need from the analysis of one song."""
    mono: np.ndarray             # source sample rate
    sample_rate: int
    track: PitchTrack
    voiced: np.ndarray
    tuning_offset_cents: float
    notes: list[Note]
    params: NoteParams = field(default_factory=NoteParams)

    @property
    def duration(self) -> float:
        return len(self.mono) / self.sample_rate

    @classmethod
    def from_result(cls, r) -> "SongContext":
        return cls(mono=r.audio.mono(), sample_rate=r.audio.sample_rate, track=r.track, voiced=r.analysis.voiced_mask,
                   tuning_offset_cents=r.tuning_offset_cents, notes=r.notes)


def measure(sl: Slice, ctx: SongContext) -> Slice:
    """Fill a slice's pitch, note, cents, confidence and flags from its current boundaries."""
    hop = ctx.track.hop_seconds
    n = measure_range(ctx.track, ctx.voiced, int(round(sl.start / hop)), int(round(sl.end / hop)), ctx.params)
    if n is None:
        sl.pitch, sl.det_note, sl.det_cents = float("nan"), 60, 0
        sl.confidence, sl.spread, sl.steadiness, sl.voicedness = 0, 0.0, 0.0, 0.0
        sl.flags = ["no_pitch", "low_confidence"]
        return sl
    assign_note(n, ctx.tuning_offset_cents, ctx.params)
    sl.pitch, sl.det_note, sl.det_cents = n.pitch, n.note, n.cents
    sl.confidence, sl.spread, sl.steadiness, sl.voicedness = n.confidence, n.spread, n.steadiness, n.voicedness
    sl.flags = list(n.flags)
    return sl


def chop(ctx: SongContext, region: tuple[float, float], s: ChopSettings) -> list[Slice]:
    """Chop the region with the chosen mode and the shared rules. Manual starts as one slice (the region)."""
    r0, r1 = max(0.0, region[0]), min(ctx.duration, region[1])
    if r1 <= r0:
        return []
    if s.mode == "pitch":
        raw = pitch_ranges(ctx.notes, (r0, r1))
    elif s.mode == "silence":
        raw = silence_ranges(ctx.mono, ctx.sample_rate, (r0, r1), s.gate_db, s.gate_ms)
    elif s.mode == "equal":
        raw = equal_ranges((r0, r1), s.equal_count)
    elif s.mode == "manual":
        raw = [(r0, r1)]
    else:
        raise ValueError(f"unknown chop mode {s.mode}")
    return [measure(Slice(a, b), ctx) for a, b in apply_rules(raw, ctx.mono, ctx.sample_rate, s)]


def suggest_split(sl: Slice, ctx: SongContext) -> float | None:
    """Where to split a slice: where its smoothed pitch crosses halfway between its first and last third.

    For a Glide that is the middle of the slide; for two notes run together, the change between them.
    """
    hop = ctx.track.hop_seconds
    s, e = int(round(sl.start / hop)), int(round(sl.end / hop))
    raw = ctx.track.midi[s:e]
    if ctx.voiced[s:e].sum() < 6:
        return None
    # every frame with a pitch reading, voiced or not: pYIN marks the slide between legato notes unvoiced, and
    # skipping those frames put the suggestion at the end of the slide instead of its middle
    ok = np.isfinite(raw)
    idx = np.flatnonzero(ok)
    m = raw[ok]
    vm = ctx.voiced[s:e][ok]
    k = max(1, int(vm.sum()) // 3)
    vidx = np.flatnonzero(vm)
    lo = float(np.median(m[vidx[:k]]))
    hi = float(np.median(m[vidx[-k:]]))
    if abs(hi - lo) < 0.5:
        t = (sl.start + sl.end) / 2
    else:
        level = (lo + hi) / 2
        above = m > level
        ch = np.flatnonzero(above[1:] != above[:-1])
        if len(ch) == 0:
            t = (sl.start + sl.end) / 2
        else:
            mid = (len(raw)) / 2
            j = int(ch[np.argmin(np.abs(idx[ch] - mid))])
            t = (s + idx[j + 1]) * hop
    min_s = 0.06
    if t - sl.start < min_s or sl.end - t < min_s:
        return None
    return float(t)


# ---------------------------------------------------------------- audio of a slice

def fade_ramp(n: int) -> np.ndarray:
    """Raised-cosine ramp 0 -> 1 over n samples, starting at exactly 0."""
    if n <= 0:
        return np.ones(0)
    return 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)


def slice_audio(data: np.ndarray, sr: int, sl: Slice, fade_ms: float = 3.0, gain: bool = True) -> np.ndarray:
    """The slice as played: source channels, 3 ms fades so the first and last sample are 0, per-slice gain."""
    a, b = int(round(sl.start * sr)), int(round(sl.end * sr))
    x = np.array(data[a:b], dtype=np.float32, copy=True)
    if x.ndim == 1:
        x = x[:, None]
    f = min(len(x) // 2, int(round(fade_ms / 1000.0 * sr)))
    if f > 0:
        r = fade_ramp(f)[:, None]
        x[:f] *= r
        x[-f:] *= r[::-1]
    if len(x):
        x[-1] = 0.0
    if gain and sl.gain:
        x *= 10 ** (sl.gain / 20.0)
    return x


CLICK_RATIO = 2.0         # an edge step this many times the sharpest step in the audio next to it = a click
CLICK_FLOOR = 10 ** (-60 / 20)   # steps smaller than −60 dBFS are inaudible whatever the ratio


def click_score(x: np.ndarray, sr: int, edge_ms: float = 2.0, ref_ms: float = 20.0) -> tuple[float, float]:
    """How click-like each end of a slice is: (start score, end score); above CLICK_RATIO = a click.

    Playback starts from and returns to silence, so the step from 0 to the first sample (and from the last
    sample back to 0) counts. The score is the sharpest sample-to-sample step within `edge_ms` of the end,
    divided by the sharpest step in the `ref_ms` of audio just inside it (the slice's own sound). Steps under
    −60 dBFS score 0. With fades the edge is gentler than the sound itself (score below 1).
    """
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    e = max(1, int(round(edge_ms / 1000.0 * sr)))
    r = max(2, int(round(ref_ms / 1000.0 * sr)))
    out = []
    for seg_edge, seg_ref in (
            (np.vstack([np.zeros((1, x.shape[1])), x[:e + 1]]), x[e:e + r]),
            (np.vstack([x[-(e + 1):], np.zeros((1, x.shape[1]))]), x[-(e + r):-e] if len(x) > e + r else x)):
        jump = float(np.abs(np.diff(seg_edge, axis=0)).max()) if len(seg_edge) > 1 else 0.0
        ref = float(np.abs(np.diff(seg_ref, axis=0)).max()) if len(seg_ref) > 1 else 0.0
        out.append(0.0 if jump < CLICK_FLOOR else jump / max(ref, CLICK_FLOOR))
    return out[0], out[1]


def render_hash(source: Path, sample_rate: int, sl: Slice) -> str:
    """Changes when the slice's boundaries, its source file or the render settings change (SPEC §4 cache)."""
    p = Path(source)
    try:
        st = p.stat()
        ident = f"{p.resolve().as_posix()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        ident = p.as_posix()
    a, b = int(round(sl.start * sample_rate)), int(round(sl.end * sample_rate))
    raw = f"{ident}|{sample_rate}|{a}|{b}|{RENDER_SETTINGS}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- editing with undo

class EditError(Exception):
    pass


class SliceEditor:
    """The slices of one song and the edit actions of SPEC §3.4, each undoable."""

    UNDO_LIMIT = 100

    def __init__(self, ctx: SongContext, settings: ChopSettings | None = None):
        self.ctx = ctx
        self.settings = settings or ChopSettings()
        self.region: tuple[float, float] = (0.0, ctx.duration)
        self.slices: list[Slice] = []
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self.dirty = False

    # ---- state
    def _state(self):
        return (copy.deepcopy(self.slices), self.region, copy.deepcopy(self.settings))

    def _push(self):
        self._undo.append(self._state())
        del self._undo[:-self.UNDO_LIMIT]
        self._redo.clear()
        self.dirty = True

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._state())
        self.slices, self.region, self.settings = self._undo.pop()
        self.dirty = True
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._state())
        self.slices, self.region, self.settings = self._redo.pop()
        self.dirty = True
        return True

    @property
    def min_s(self) -> float:
        return self.settings.min_slice_ms / 1000.0

    def _snap(self, t: float) -> float:
        return zero_crossing(self.ctx.mono, self.ctx.sample_rate, t, self.settings.snap_ms)

    def _check(self, i: int):
        if not 0 <= i < len(self.slices):
            raise EditError("No slice selected")

    def index_at(self, t: float) -> int | None:
        for i, sl in enumerate(self.slices):
            if sl.start <= t < sl.end:
                return i
        return None

    # ---- whole-region actions
    def set_region(self, a: float, b: float) -> None:
        a, b = sorted((a, b))
        a, b = max(0.0, a), min(self.ctx.duration, b)
        if b - a < self.min_s:
            raise EditError("The region is too short")
        self._push()
        self.region = (a, b)

    def clear_region(self) -> None:
        self._push()
        self.region = (0.0, self.ctx.duration)

    def chop(self, settings: ChopSettings | None = None) -> list[Slice]:
        self._push()
        if settings is not None:
            self.settings = settings
        self.slices = chop(self.ctx, self.region, self.settings)
        return self.slices

    def set_slices(self, slices: list[Slice], region: tuple[float, float] | None = None) -> None:
        """Load saved slices (re-measured, keeping the hand edits)."""
        self.slices = [measure(s, self.ctx) for s in slices]
        if region:
            self.region = region
        self._undo.clear()
        self._redo.clear()
        self.dirty = False

    # ---- slice actions
    def split(self, i: int, t: float) -> int:
        """Split slice i at time t (snapped). Returns the index of the new right-hand slice."""
        self._check(i)
        sl = self.slices[i]
        t = self._snap(t)
        if t - sl.start < self.min_s or sl.end - t < self.min_s:
            raise EditError("Both halves must be at least 60 ms")
        self._push()
        left = Slice(sl.start, t, keep_as_sung=sl.keep_as_sung, gain=sl.gain)
        right = Slice(t, sl.end, keep_as_sung=sl.keep_as_sung, gain=sl.gain)
        self.slices[i:i + 1] = [measure(left, self.ctx), measure(right, self.ctx)]
        return i + 1

    def split_suggested(self, i: int) -> int:
        self._check(i)
        t = suggest_split(self.slices[i], self.ctx)
        if t is None:
            raise EditError("No good split point in this slice")
        return self.split(i, t)

    def merge_next(self, i: int) -> None:
        self._check(i)
        if i + 1 >= len(self.slices):
            raise EditError("There is no slice after this one")
        self._push()
        a, b = self.slices[i], self.slices[i + 1]
        m = Slice(a.start, b.end, keep_as_sung=a.keep_as_sung, gain=a.gain)
        self.slices[i:i + 2] = [measure(m, self.ctx)]

    def delete(self, i: int) -> None:
        self._check(i)
        self._push()
        del self.slices[i]

    def move_edge(self, i: int, edge: str, t: float, record: bool = True) -> None:
        """Drag a slice's start or end. A boundary shared with the neighbour moves both slices."""
        self._check(i)
        sl = self.slices[i]
        prev = self.slices[i - 1] if i > 0 else None
        nxt = self.slices[i + 1] if i + 1 < len(self.slices) else None
        if edge == "start":
            shared = prev is not None and abs(prev.end - sl.start) < 1e-6
            lo = (prev.start + self.min_s) if shared else (prev.end if prev else 0.0)
            hi = sl.end - self.min_s
        elif edge == "end":
            shared = nxt is not None and abs(nxt.start - sl.end) < 1e-6
            lo = sl.start + self.min_s
            hi = (nxt.end - self.min_s) if shared else (nxt.start if nxt else self.ctx.duration)
        else:
            raise ValueError(edge)
        if hi < lo:
            raise EditError("No room to move this edge")
        t = float(np.clip(self._snap(float(np.clip(t, lo, hi))), lo, hi))
        if record:
            self._push()
        if edge == "start":
            sl.start = t
            if shared:
                prev.end = t
                measure(prev, self.ctx)
        else:
            sl.end = t
            if shared:
                nxt.start = t
                measure(nxt, self.ctx)
        measure(sl, self.ctx)
        self.dirty = True

    def begin_drag(self) -> None:
        """Call once before a series of move_edge(record=False) calls from one mouse drag."""
        self._push()

    def shift_octave(self, i: int, step: int) -> None:
        self._check(i)
        sl = self.slices[i]
        if not 0 <= sl.note + 12 * step <= 127:
            raise EditError("That octave is out of the MIDI range")
        self._push()
        sl.octave += step

    def set_note(self, i: int, midi: int | None) -> None:
        """Set the note by hand (None = back to the detected note). Clears octave edits."""
        self._check(i)
        if midi is not None and not 0 <= int(midi) <= 127:
            raise EditError("The note must be a MIDI number 0-127")
        self._push()
        sl = self.slices[i]
        sl.hand_note = None if midi is None else int(midi)
        sl.octave = 0

    def set_keep_as_sung(self, i: int, on: bool) -> None:
        self._check(i)
        self._push()
        self.slices[i].keep_as_sung = bool(on)

    def set_gain(self, i: int, db: float) -> None:
        self._check(i)
        self._push()
        self.slices[i].gain = float(np.clip(db, -24.0, 12.0))

    # ---- saving
    def problems(self) -> list[str]:
        """Reasons the slice map can't be saved yet."""
        out = []
        if not self.slices:
            out.append("There are no slices yet: choose a mode and press Chop.")
        if len(self.slices) > MAX_SLICES:
            out.append(f"{len(self.slices)} slices: a slice map holds {MAX_SLICES}. Drag a shorter region over a "
                       f"phrase and chop again, or merge/delete slices.")
        return out
