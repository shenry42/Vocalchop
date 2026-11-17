"""Milestone 3 automatic check: Natural renders measured instead of listened to.

For each song in Documents\\Vocalchop\\Acapellas the whole song is chopped in Pitch mode and 3 slices are picked by
fixed rules (before any render is looked at): a held vowel, a slice that starts on a consonant, and one with
vibrato. Each is rendered by the real background renderer (25 files, −12..+12) into Documents\\Vocalchop\\m3_check
(your own project folders are not touched), then every render is measured against the original slice:

  1. pitch    the built-in tuner reads the render and the original frame by frame (100 ms frames, same times);
              the median difference must be the shift (within 10 c). And the render's pitch curve (pYIN) minus
              the shift must follow the original's curve, vibrato included (median frame difference within 10 c)
  2. length   exactly as many samples as the original, and the loudness envelope lines up (within 5 ms)
  3. formants the spectral envelope (levels of the harmonics) fits the original's envelope better unmoved than
              moved with the pitch. A plain speed change of the same slice is measured the same way as a control:
              its formants do move, which shows the measurement can see it
  4. clicks   no click at either end (click_score), first and last sample exactly 0
  5. other    level within ±1.5 dB, no clipping, harmonicity kept (harmonics stand out from the gaps between
              them at least as clearly as in the original, to within 3 dB: roughness, noise and garble lower it),
              +00 identical to the slice. pYIN's voiced share is shown for information
  6. time     render time per second of slice (all 25 files), and a 32-slice Save timed from Save to the last
              slice ready, plus the cache: nothing re-renders on a second request or after renumbering

Goals (fixed before running on the real files):
  pitch: tuner within 10 c on ≥ 95% of readable renders and never a wrong note (50 c or more); curve within 10 c
         on ≥ 90% of renders with enough voiced frames; length: 100% exact, envelope lag ≤ 5 ms on 100%;
  formants: kept on ≥ 90% of the renders the formant guard left alone (the control must move on ≥ 90%, or the
  formant line reads "not measurable"); renders where the guard let the formants follow are listed apart;
  clicks: 0; level within ±1.5 dB on 100%; no clipping; harmonicity within 3 dB on ≥ 90%; +00 identical.
The headline table shows −12, −5, +5 and +12 (SPEC §7); the goals count all 24 shifted files of every slice.

Changed after the first run on the real files, with evidence (STATUS.md, Milestone 3): the tuner reads frame by
frame (read over the whole slice it was off by up to 65 c on vibrato even for a plain speed change, whose pitch is
exact by construction); harmonicity replaced pYIN's voiced share as the goal (pYIN's voicing fell to 0-30% on
formant-kept octave-down renders whose harmonics stood out as clearly as the original's); the formant guard was
added to the renderer (Crazy's flute-like notes lost up to 20 dB at +12 with formants kept).
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from . import render
from .audition import _Span, varispeed
from .background import BackgroundRenderer
from .chop import CLICK_RATIO, ChopSettings, SongContext, chop, click_score, slice_audio
from .notes import has_vibrato, vibrato_depth_cents
from .paths import acapellas_dir, home_dir, is_audio_file, project_name
from .pitch import to_analysis_rate, track_pitch
from .presets import note_name
from .project import analyze_file, preset_for
from .slicemap import build_slice_map_from_slices, write_slice_map
from .tuner import read_tuner

HEADLINE = (-12, -5, 5, 12)
GOAL_TUNER_C = 10.0
GOAL_TUNER_SHARE = 0.95
WRONG_NOTE_C = 50.0
GOAL_CURVE_C = 10.0
GOAL_CURVE_SHARE = 0.90
GOAL_LAG_MS = 5.0
GOAL_FORMANT_SHARE = 0.90
GOAL_LEVEL_DB = 1.5
GOAL_HARMONIC_DB = 3.0
GOAL_HARMONIC_SHARE = 0.90
TIMING_SLICES = 32


def check_dir() -> Path:
    return home_dir() / "m3_check"


def results_path() -> Path:
    return home_dir() / "m3_check_results.txt"


# ---------------------------------------------------------------- measurements

def mono(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=1) if x.ndim == 2 else x


def harmonic_levels(x: np.ndarray, sr: int, f0: float, fmax: float = 6000.0) -> tuple[np.ndarray, np.ndarray]:
    """(frequency, dB) of each harmonic of f0 up to fmax, from the slice's average spectrum."""
    m = mono(x).astype(np.float64)
    n = 8192 if len(m) >= 8192 else 1 << int(np.log2(max(len(m), 1024)))
    w = np.hanning(n)
    frames = [m[a:a + n] for a in range(0, max(1, len(m) - n + 1), n // 4)]
    p = np.mean([np.abs(np.fft.rfft(np.pad(f, (0, n - len(f))) * w)) ** 2 for f in frames], axis=0)
    db = 10 * np.log10(p + 1e-12)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    fk, lk = [], []
    k = 1
    while k * f0 <= fmax:
        a, b = np.searchsorted(freqs, k * f0 * 0.97), np.searchsorted(freqs, k * f0 * 1.03)
        if b > a:
            j = a + int(np.argmax(db[a:b]))
            fk.append(freqs[j])
            lk.append(db[j])
        k += 1
    return np.array(fk), np.array(lk)


@dataclass
class FormantFit:
    moved: float          # semitones the envelope moved (best fit), 0 = formants kept
    err_kept: float       # dB RMS misfit if the formants stayed where they were
    err_moved: float      # dB RMS misfit if they moved with the pitch (by the shift)

    @property
    def kept(self) -> bool:
        return self.err_kept < self.err_moved

    @property
    def measurable(self) -> bool:
        return np.isfinite(self.err_kept) and np.isfinite(self.err_moved)


def formant_fit(ref: np.ndarray, f0: float, y: np.ndarray, st: float, sr: int) -> FormantFit:
    """How far the render's spectral envelope moved compared with the original's.

    The render's harmonic levels (at its own pitch) are compared with the original's envelope (interpolated
    between the original's harmonics), with the render's frequencies moved back by `a` semitones; the misfit is
    the RMS dB difference after removing the overall level. a = 0 fitting best means the formants were kept;
    a = shift means they moved with the pitch (what a plain speed change does)."""
    fo, lo = harmonic_levels(ref, sr, f0)
    fr, lr = harmonic_levels(y, sr, f0 * 2 ** (st / 12.0))
    if len(fo) < 4:
        return FormantFit(float("nan"), float("inf"), float("inf"))

    def err(a: float) -> float:
        q = fr / 2 ** (a / 12.0)
        ok = (q >= fo[0]) & (q <= fo[-1]) & (fr >= 250)
        if ok.sum() < 6:
            return float("inf")
        d = lr[ok] - np.interp(np.log2(q[ok]), np.log2(fo), lo)
        d -= np.median(d)
        return float(np.sqrt(np.mean(d ** 2)))

    alphas = np.arange(-14, 14.01, 0.25)
    e = np.array([err(a) for a in alphas])
    best = float(alphas[int(np.argmin(e))]) if np.isfinite(e).any() else float("nan")
    return FormantFit(best, err(0.0), err(float(st)))


def envelope_lag_ms(ref: np.ndarray, y: np.ndarray, sr: int, hop_ms: float = 1.0, reach_ms: float = 20.0) -> float:
    """Lag (ms) of the render's loudness envelope against the original's (cross-correlation peak)."""
    h = max(1, int(sr * hop_ms / 1000))

    def env(x):
        m = mono(x)[: len(x) // h * h].astype(np.float64)
        e = np.sqrt(np.mean(m.reshape(-1, h) ** 2, axis=1))
        return np.log10(e + 1e-5)

    a, b = env(ref), env(y)
    a, b = a - a.mean(), b - b.mean()
    k = int(reach_ms / hop_ms)
    best, lag = -np.inf, 0
    for L in range(-k, k + 1):
        if L >= 0:
            c = float(np.dot(a[:len(a) - L], b[L:]))
        else:
            c = float(np.dot(a[-L:], b[:len(b) + L]))
        if c > best:
            best, lag = c, L
    return lag * hop_ms


def framewise_tuner(ref: np.ndarray, y: np.ndarray, sr: int, lo: int, hi: int, st: int,
                    win: float = 0.1, hop: float = 0.05) -> float:
    """Median (render − original − shift) in cents over 100 ms frames read at the same times (middle 70%)."""
    m0, m1 = mono(ref), mono(y)
    dur = len(m0) / sr
    starts = np.arange(0.15 * dur, 0.85 * dur - win + 1e-9, hop) if 0.7 * dur >= win else [max(0.0, (dur - win) / 2)]
    d = []
    for a in starts:
        r0 = read_tuner(m0, sr, _Span(a, a + win), lo, hi)
        r1 = read_tuner(m1, sr, _Span(a, a + win), max(0, lo + st - 2), min(127, hi + st + 2))
        if r0.ok and r1.ok and r0.clarity >= 0.2:
            d.append(100.0 * (r1.midi - r0.midi - st))
    return float(np.median(d)) if d else float("nan")


def harmonicity_db(x: np.ndarray, sr: int, f0: float, harmonics: int = 12) -> float:
    """How clearly the harmonics stand out: level at each harmonic minus the level halfway to the next (dB)."""
    m = mono(x).astype(np.float64)
    n = 8192
    w = np.hanning(n)
    if len(m) < n:
        m = np.pad(m, (0, n - len(m)))
    p = np.mean([np.abs(np.fft.rfft(m[a:a + n] * w)) ** 2 for a in range(0, len(m) - n + 1, n // 4)], axis=0)
    db = 10 * np.log10(p + 1e-12)
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    out = []
    for k in range(1, harmonics + 1):
        if (k + 0.5) * f0 >= sr / 2:
            break
        a, b = np.searchsorted(fr, k * f0 * 0.97), np.searchsorted(fr, k * f0 * 1.03)
        out.append(db[a:b + 1].max() - db[np.searchsorted(fr, (k + 0.5) * f0)])
    return float(np.mean(out)) if out else float("nan")


def rms_db(x: np.ndarray) -> float:
    return 20 * np.log10(float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) + 1e-12)


def pitch_curve(x: np.ndarray, sr: int, lo: int, hi: int):
    t = track_pitch(to_analysis_rate(mono(x).astype(np.float32), sr), max(0, lo), min(127, hi), workers=1)
    return t.midi, t.voiced_prob > 0.5


@dataclass
class RenderMeasure:
    shift: int
    tuner_err_c: float = float("nan")     # (render − original) − shift, cents; NaN = tuner couldn't read
    curve_c: float = float("nan")         # median |frame difference| after the shift, cents; NaN = too few frames
    same_length: bool = False
    lag_ms: float = 0.0
    formant: FormantFit | None = None
    control: FormantFit | None = None     # a plain speed change of the same slice
    clicks: int = 0
    ends_zero: bool = True
    level_db: float = 0.0
    clipped: int = 0
    voiced: float = float("nan")          # render's voiced frames / original's (pYIN, for information)
    harmonic_db: float = float("nan")     # render's harmonicity − original's (dB; negative = rougher)
    follow: float = 0.0                   # formant guard: how far the formants were let follow (0 = kept)
    fade_ms: float = 3.0                  # click safety: the fade used at a clicking edge


@dataclass
class SliceCheck:
    song: str
    role: str
    index: int
    start: float
    length: float
    note: str
    render_seconds: float = 0.0
    identical_zero: bool = False
    renders: list[RenderMeasure] = field(default_factory=list)


def measure_slice(sc: SliceCheck, folder: Path, ref: np.ndarray, sr: int, lo: int, hi: int) -> None:
    """Measure every render file of one slice against the original (faded, no gain) slice audio."""
    m0 = mono(ref)
    t0 = read_tuner(m0, sr, _Span(0, len(m0) / sr), lo, hi)
    c0, v0 = pitch_curve(ref, sr, lo, hi)
    z, _ = sf.read(str(folder / render.shift_name(0)), dtype="float32", always_2d=True)
    info = json.loads((folder / render.DONE_FILE).read_text(encoding="utf-8")).get("files", {})
    h0 = harmonicity_db(ref, sr, t0.hz) if t0.ok else float("nan")
    sc.identical_zero = z.shape == ref.shape and float(np.max(np.abs(z - ref))) <= 2.0 ** -22
    for st in render.SHIFTS:
        if st == 0:
            continue
        y, ysr = sf.read(str(folder / render.shift_name(st)), dtype="float32", always_2d=True)
        r = RenderMeasure(shift=st)
        fi = info.get(render.shift_name(st)[:-4], {})
        r.follow, r.fade_ms = float(fi.get("follow", 0.0)), float(fi.get("fadeMs", 3.0))
        r.same_length = ysr == sr and y.shape == ref.shape
        if r.same_length:
            r.tuner_err_c = framewise_tuner(ref, y, sr, lo, hi, st)
        if t0.ok:
            r.harmonic_db = harmonicity_db(y, sr, t0.hz * 2 ** (st / 12.0)) - h0
        c1, v1 = pitch_curve(y, sr, lo + st, hi + st)
        n = min(len(c0), len(c1))
        both = v0[:n] & v1[:n]
        if both.sum() >= 5:
            r.curve_c = float(np.median(np.abs((c1[:n][both] - st) - c0[:n][both]))) * 100.0
        if v0.sum() >= 5:
            r.voiced = float(v1[:n].sum() / v0[:n].sum())
        r.lag_ms = envelope_lag_ms(ref, y, sr) if r.same_length else float("nan")
        if t0.ok:
            r.formant = formant_fit(ref, t0.hz, y, st, sr)
            r.control = formant_fit(ref, t0.hz, varispeed(ref, sr, st), st, sr)
        cs = click_score(y, sr)
        r.clicks = sum(c >= CLICK_RATIO for c in cs)
        r.ends_zero = bool(np.all(y[0] == 0) and np.all(y[-1] == 0))
        r.level_db = rms_db(y) - rms_db(ref)
        r.clipped = int(np.sum(np.abs(y) >= 0.999)) - int(np.sum(np.abs(ref) >= 0.999))
        sc.renders.append(r)


# ---------------------------------------------------------------- picking slices (fixed rules)

def onset_brightness(ctx: SongContext, s) -> float:
    """Energy above 3 kHz in the first 40 ms relative to the slice's middle: high = starts on a consonant."""
    sr = ctx.sample_rate
    a, b = int(s.start * sr), int(s.end * sr)
    x = ctx.mono[a:b].astype(np.float64)
    k = int(0.04 * sr)
    if len(x) < 3 * k:
        return 0.0

    def hf(seg):
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        f = np.fft.rfftfreq(len(seg), 1.0 / sr)
        return float(spec[f >= 3000].sum() / (spec.sum() + 1e-12))

    mid = len(x) // 2
    return hf(x[:k]) / max(hf(x[mid - k // 2:mid + k // 2]), 1e-4)


def pick_slices(ctx: SongContext, slices: list) -> list[tuple[str, int]]:
    """(role, index into slices): held vowel, consonant start, vibrato. Rules fixed in Milestone 3."""
    hop = ctx.track.hop_seconds

    def fr(s):
        return int(round(s.start / hop)), int(round(s.end / hop))

    def vib(s):
        a, b = fr(s)
        return has_vibrato(ctx.track, ctx.voiced, a, b)

    picks: list[tuple[str, int]] = []
    held = [i for i, s in enumerate(slices) if s.length >= 0.4 and s.confidence >= 80 and not s.flags and not vib(s)]
    if held:
        picks.append(("held vowel", max(held, key=lambda i: (slices[i].steadiness, slices[i].length))))
    used = {i for _, i in picks}

    def starts_free(i):
        return i == 0 or slices[i - 1].end < slices[i].start - 1e-6

    cons = [i for i, s in enumerate(slices) if i not in used and s.length >= 0.3 and s.confidence >= 60
            and "glide" not in s.flags and starts_free(i)]
    if cons:
        picks.append(("consonant start", max(cons, key=lambda i: onset_brightness(ctx, slices[i]))))
    used = {i for _, i in picks}
    vibs = [i for i, s in enumerate(slices) if i not in used and s.length >= 0.4 and s.confidence >= 60
            and "glide" not in s.flags and vib(s)]
    if vibs:
        picks.append(("vibrato", max(vibs, key=lambda i: vibrato_depth_cents(ctx.track, ctx.voiced, *fr(slices[i])))))
    return picks


# ---------------------------------------------------------------- one song

@dataclass
class SongCheck:
    name: str
    preset: str
    sample_rate: int
    channels: int
    slices: list[SliceCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check_song(source: Path, preset_key: str, renderer: BackgroundRenderer) -> SongCheck:
    r = analyze_file(source, preset_key, write_outputs=False)
    ctx = SongContext.from_result(r)
    sl = chop(ctx, (0.0, ctx.duration), ChopSettings(mode="pitch"))
    sc = SongCheck(source.name, r.preset.label, r.audio.sample_rate, r.audio.channels)
    picks = pick_slices(ctx, sl)
    if len(picks) < 3:
        have = {p for p, _ in picks}
        sc.notes.append("no slice qualified as: " + ", ".join(x for x in ("held vowel", "consonant start", "vibrato")
                                                                 if x not in have))
    chosen = sorted(picks, key=lambda p: sl[p[1]].start)
    folder = check_dir() / project_name(source)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    m = build_slice_map_from_slices(source, r.audio.sample_rate, r.tuning_offset_cents, [sl[i] for _, i in chosen])
    write_slice_map(m, folder / "slicemap.json")
    renderer.request(folder)
    if not renderer.wait_idle(3600):
        raise RuntimeError("the renderer did not finish within an hour")
    errors = [e.message for e in getattr(renderer, "errors", []) if Path(e.project) == folder.resolve()]
    if errors:
        raise RuntimeError(f"rendering {source.name} failed: {errors[-1]}")
    for (role, i), entry in zip(chosen, m["slices"]):
        s = sl[i]
        info = json.loads((folder / entry["renderFolder"] / render.DONE_FILE).read_text(encoding="utf-8"))
        c = SliceCheck(source.name, role, i + 1, s.start, s.length, note_name(s.note), info["seconds"])
        ref = slice_audio(r.audio.data, r.audio.sample_rate, s, gain=False)
        measure_slice(c, folder / entry["renderFolder"], ref, r.audio.sample_rate, r.preset.low, r.preset.high)
        sc.slices.append(c)
    return sc


# ---------------------------------------------------------------- timing a 32-slice save, and the cache

@dataclass
class Timing:
    song: str = ""
    slices: int = 0
    audio_seconds: float = 0.0
    wall_seconds: float = 0.0
    first_ready: float = 0.0
    workers: int = 0
    again_seconds: float = 0.0
    again_rendered: int = -1
    renumber_rendered: int = -1
    renumber_moved: int = 0
    restore_rendered: int = -1


def time_full_save(source: Path, preset_key: str, workers: int | None = None) -> Timing:
    """Chop the first 32 Pitch slices of a song, 'save' them and time the background render; then the cache."""
    from .chop import SliceEditor
    from . import session

    r = analyze_file(source, preset_key, write_outputs=False)
    ctx = SongContext.from_result(r)
    sl = chop(ctx, (0.0, ctx.duration), ChopSettings(mode="pitch"))[:TIMING_SLICES]
    folder = check_dir() / (project_name(source) + " (32-slice timing)")
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    t = Timing(song=source.name, slices=len(sl), audio_seconds=sum(s.length for s in sl))
    events = []
    br = BackgroundRenderer(workers=workers, on_event=lambda e: events.append((time.perf_counter(), e)))
    t.workers = br.workers

    class _R:      # what session.save needs from an analysis result, pointing at the check folder
        pass
    res = _R()
    res.folder, res.source, res.audio, res.tuning_offset_cents, res.preset = (folder, r.source, r.audio,
                                                                              r.tuning_offset_cents, r.preset)
    ed = SliceEditor(ctx)
    ed.set_slices(sl, region=(sl[0].start, sl[-1].end))
    try:
        br._get_pool()                         # start the worker processes first, as the app does when it opens
        t0 = time.perf_counter()
        session.save(res, ed)
        br.request(folder)
        br.wait_idle(3600)
        t.wall_seconds = time.perf_counter() - t0
        failed = [e for _, e in events if e.kind == "error"]
        if failed:
            raise RuntimeError(f"the 32-slice render failed: {failed[-1].message}")
        firsts = [ts for ts, e in events if e.kind == "slice"]
        t.first_ready = (firsts[0] - t0) if firsts else float("nan")

        def rendered_after(action) -> tuple[int, int]:
            events.clear()
            action()
            br.request(folder)
            br.wait_idle(3600)
            n = sum(1 for _, e in events if e.kind == "slice")
            start = [e for _, e in events if e.kind == "start"]
            return n, (int(start[0].message.split(", ")[-1].split()[0]) if start and "kept" in start[0].message
                       else 0)

        t1 = time.perf_counter()
        t.again_rendered, _ = rendered_after(lambda: None)
        t.again_seconds = time.perf_counter() - t1
        ed.delete(4)                          # slice 5 goes: slices 6..32 become 5..31
        t.renumber_rendered, t.renumber_moved = rendered_after(lambda: session.save(res, ed))
        ed.undo()                             # and back: its renders come back from _unused
        t.restore_rendered, _ = rendered_after(lambda: session.save(res, ed))
    finally:
        br.stop()
    return t


# ---------------------------------------------------------------- report

def _share(xs, ok) -> tuple[int, int]:
    xs = list(xs)
    return sum(1 for x in xs if ok(x)), len(xs)


def _pct(a: int, b: int) -> str:
    return f"{a} of {b} ({100.0 * a / b:.0f}%)" if b else "none"


def evaluate(songs: list[SongCheck]) -> dict:
    rs = [r for s in songs for c in s.slices for r in c.renders]
    tun = [r.tuner_err_c for r in rs if np.isfinite(r.tuner_err_c)]
    cur = [r.curve_c for r in rs if np.isfinite(r.curve_c)]
    form = [r for r in rs if r.formant is not None and r.formant.measurable and r.follow == 0]
    guarded = [r for r in rs if r.follow > 0]
    ctrl = [r for r in rs if r.control is not None and r.control.measurable]
    voi = [r.voiced for r in rs if np.isfinite(r.voiced)]
    har = [r.harmonic_db for r in rs if np.isfinite(r.harmonic_db)]
    g = {}
    a, b = _share(tun, lambda x: abs(x) <= GOAL_TUNER_C)
    g["pitch (tuner)"] = (b > 0 and a / b >= GOAL_TUNER_SHARE and not any(abs(x) >= WRONG_NOTE_C for x in tun),
                          f"within {GOAL_TUNER_C:.0f} c: {_pct(a, b)}; wrong notes (50 c or more): "
                          f"{sum(abs(x) >= WRONG_NOTE_C for x in tun)}; median error {np.median(np.abs(tun)):.1f} c"
                          if tun else "nothing readable")
    a, b = _share(cur, lambda x: x <= GOAL_CURVE_C)
    g["pitch curve"] = (b > 0 and a / b >= GOAL_CURVE_SHARE,
                        f"within {GOAL_CURVE_C:.0f} c: {_pct(a, b)}; median {np.median(cur):.1f} c" if cur else "-")
    a, b = _share(rs, lambda r: r.same_length)
    a2, b2 = _share(rs, lambda r: np.isfinite(r.lag_ms) and abs(r.lag_ms) <= GOAL_LAG_MS)
    g["length kept"] = (a == b and a2 == b2, f"same length: {_pct(a, b)}; envelope lag ≤ {GOAL_LAG_MS:.0f} ms: "
                                             f"{_pct(a2, b2)}")
    ca, cb = _share(ctrl, lambda r: not r.control.kept)
    fa, fb = _share(form, lambda r: r.formant.kept)
    works = cb > 0 and ca / cb >= GOAL_FORMANT_SHARE
    g["formants kept"] = (works and fb > 0 and fa / fb >= GOAL_FORMANT_SHARE,
                          f"kept: {_pct(fa, fb)}; median movement {np.median([abs(r.formant.moved) for r in form]):.2f}"
                          f" st; control (plain speed change) moved on {_pct(ca, cb)}"
                          + ("" if works else " - NOT MEASURABLE: the control didn't move")
                          + f"; formant guard used on {len(guarded)} of {len(rs)} renders"
                          + (f" (formants followed the pitch by {min(r.follow for r in guarded):.2f}-"
                             f"{max(r.follow for r in guarded):.2f}; listed under 'other shifts' above)"
                             if guarded else ""))
    g["clicks"] = (sum(r.clicks for r in rs) == 0 and all(r.ends_zero for r in rs),
                   f"{sum(r.clicks for r in rs)} clicks in {2 * len(rs)} edges; "
                   f"{sum(not r.ends_zero for r in rs)} files not starting and ending on 0")
    a, b = _share(rs, lambda r: abs(r.level_db) <= GOAL_LEVEL_DB)
    clipped = sum(r.clipped > 0 for r in rs)
    va, vb = _share(har, lambda v: v >= -GOAL_HARMONIC_DB)
    ident = [c.identical_zero for s in songs for c in s.slices]
    g["other"] = (a == b and clipped == 0 and vb > 0 and va / vb >= GOAL_HARMONIC_SHARE and all(ident),
                  f"level within ±{GOAL_LEVEL_DB} dB: {_pct(a, b)}; files with new clipping: {clipped}; harmonicity "
                  f"within {GOAL_HARMONIC_DB:.0f} dB: {_pct(va, vb)} (median {np.median(har):+.1f} dB); +00 identical "
                  f"to the slice: {sum(ident)} of {len(ident)}; for information, pYIN voiced share ≥ 90%: "
                  f"{_pct(*_share(voi, lambda v: v >= 0.9))}")
    return g


def _fmt(x, f="{:+.0f}", none="-"):
    return none if x is None or not np.isfinite(x) else f.format(x)


def write_report(songs: list[SongCheck], timing: list[Timing], seconds: float) -> Path:
    from . import __version__

    lines = [f"Vocal Chop Sampler {__version__} - Milestone 3 check ({time.strftime('%Y-%m-%d %H:%M')}, "
             f"{seconds:.0f} s)",
             "Natural renders measured automatically, no listening. Engine: " + render.ENGINE_NAME + ".",
             "Columns per render: tuner = pitch error vs the exact shift, frame by frame (c); curve = median "
             "pitch-curve difference (c); len = same length; lag = envelope lag (ms); formants = movement in st "
             "(0 = kept; ! = fits moved better) / the plain speed change of the same slice; guard = how far the formant "
             "guard let the formants follow (0 = kept); clk = clicks; level (dB); harm = harmonicity vs the original "
             "(dB); voiced = pYIN voiced share vs the original (information).",
             ""]
    for s in songs:
        lines.append(f"== {s.name}  ({s.preset}, {s.sample_rate} Hz, {s.channels} ch)")
        lines += [f"   {n}" for n in s.notes]
        for c in s.slices:
            rate = c.render_seconds / c.length if c.length else 0
            lines.append(f"   {c.role}: slice {c.index} at {int(c.start // 60)}:{c.start % 60:06.3f}, "
                         f"{c.length * 1000:.0f} ms, {c.note}; 25 files in {c.render_seconds:.1f} s "
                         f"({rate:.1f} s per second of slice); +00 identical: {'yes' if c.identical_zero else 'NO'}")
            lines.append("      shift   tuner  curve  len  lag   formants (speed)  guard  clk  level   harm  voiced")
            for r in c.renders:
                if r.shift not in HEADLINE:
                    continue
                f = r.formant
                fm = (f"{f.moved:+5.2f}{'' if f.kept else '!'}" if f and f.measurable else "  -  ")
                ctl = f"{r.control.moved:+6.2f}" if r.control and r.control.measurable else "   -  "
                lines.append(f"      {r.shift:+4d}  {_fmt(r.tuner_err_c):>6}  {_fmt(r.curve_c, '{:.0f}'):>5}  "
                             f"{'yes' if r.same_length else 'NO':>3}  {_fmt(r.lag_ms, '{:+.0f}'):>3}   "
                             f"{fm} ({ctl})  {r.follow:4.2f}   {r.clicks:>2}  {r.level_db:+5.1f}  "
                             f"{_fmt(r.harmonic_db, '{:+5.1f}'):>5}  {_fmt(r.voiced * 100, '{:.0f}%')}")
            bad = [r for r in c.renders if r.shift not in HEADLINE and (
                (np.isfinite(r.tuner_err_c) and abs(r.tuner_err_c) > GOAL_TUNER_C) or r.clicks or
                (r.formant is not None and r.formant.measurable and not r.formant.kept and r.follow == 0) or
                abs(r.level_db) > GOAL_LEVEL_DB or (np.isfinite(r.curve_c) and r.curve_c > GOAL_CURVE_C) or
                (np.isfinite(r.harmonic_db) and r.harmonic_db < -GOAL_HARMONIC_DB) or r.follow > 0 or
                not (np.isfinite(r.lag_ms) and abs(r.lag_ms) <= GOAL_LAG_MS))]
            lines.append("      other shifts: " + ("all within the goals" if not bad else
                         "; ".join(f"{r.shift:+d} (tuner {_fmt(r.tuner_err_c)} c, curve {_fmt(r.curve_c, '{:.0f}')} c,"
                                   f" lag {_fmt(r.lag_ms, '{:+.0f}')} ms, formants "
                                   f"{_fmt(r.formant.moved if r.formant else None, '{:+.2f}')}"
                                   + (f", guard {r.follow:.2f}" if r.follow else "")
                                   + f", level {r.level_db:+.1f}, harm {_fmt(r.harmonic_db, '{:+.1f}')})"
                                   for r in bad)))
        lines.append("")
    g = evaluate(songs)
    lines.append("== Goals (all 24 shifted files of every checked slice)")
    for k, (ok, txt) in g.items():
        lines.append(f"   {k}: {'MET' if ok else 'NOT MET'} - {txt}")
    lines.append("")
    lines.append("== Render time")
    rates = [c.render_seconds / c.length for s in songs for c in s.slices if c.length]
    if rates:
        lines.append(f"   checked slices: {np.median(rates):.1f} s per second of slice for all 25 files (median; range "
                     f"{min(rates):.1f}-{max(rates):.1f})")
    for t in timing:
        lines.append(f"   {t.song}: Save of {t.slices} slices ({t.audio_seconds:.1f} s of audio) -> all ready in "
                     f"{t.wall_seconds:.1f} s with {t.workers} worker processes; first slice playable after "
                     f"{t.first_ready:.1f} s")
        lines.append(f"   cache: asking again re-rendered {t.again_rendered} slices ({t.again_seconds:.1f} s); deleting "
                     f"slice 5 (renumbering 27 slices) re-rendered {t.renumber_rendered}, kept {t.renumber_moved}; "
                     f"undo + Save re-rendered {t.restore_rendered}")
    cache_ok = all(t.again_rendered == 0 and t.renumber_rendered == 0 and t.restore_rendered == 0 for t in timing)
    lines.append(f"   cache: {'MET' if cache_ok else 'NOT MET'} (nothing re-renders unless its audio changed)")
    ok_all = all(ok for ok, _ in g.values()) and cache_ok
    lines.append("")
    lines.append("All Milestone 3 goals met." if ok_all else "Some goals NOT MET - see above.")
    p = results_path()
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    t0 = time.perf_counter()
    ok, msg = render.engine_available()
    if not ok:
        print("Rubber Band isn't available:", msg, "- run scripts\\setup.bat")
        return 1
    files = sorted(p for p in acapellas_dir().iterdir() if is_audio_file(p)) if acapellas_dir().exists() else []
    if not files:
        print("No audio files in", acapellas_dir())
        return 1
    songs, timing = [], []
    errors = []
    br = BackgroundRenderer(on_event=lambda e: errors.append(e) if e.kind == "error" else None)
    br.errors = errors
    try:
        for f in files:
            key = preset_for(f) or "female"
            print(f"Rendering and measuring 3 slices of {f.name}...", flush=True)
            songs.append(check_song(f, key, br))
    finally:
        br.stop()
    timed = next((f for f in files if f.suffix.lower() == ".wav"), files[0])
    print(f"Timing a {TIMING_SLICES}-slice Save of {timed.name}...", flush=True)
    timing.append(time_full_save(timed, preset_for(timed) or "female"))
    p = write_report(songs, timing, time.perf_counter() - t0)
    print(p.read_text(encoding="utf-8"))
    print("Results written to", p)
    return 0
