"""Natural mode rendering (SPEC §4): each slice pitch-shifted to every whole semitone from −12 to +12.

Engine: Rubber Band Library, R3 ("finer") engine with formants preserved, through the `pylibrb` package (the
library itself, installed with pip; SPEC §9 2026-11-16, Milestone 3). Offline mode, so the whole slice is studied
first and the output has exactly the input's length.

What a render is made from (decided in Milestone 3 by click and edge measurements, see STATUS.md):
the raw slice plus up to CONTEXT_MS of the source on each side is shifted, then trimmed back to the slice's exact
edges and given the same 3 ms fades as slice_audio(); then its loudness (RMS) is matched to the original slice
(the engine loses about 1 dB). The engine sees real sound at the edges instead of a fade from silence, and the
first and last sample of every file is exactly 0. Per-slice gain is NOT in the renders:
the device applies it live (SPEC §5.4), so changing a slice's gain never re-renders.

Files (SPEC §4): renders/slice_XX/±NN.wav, 24-bit, the source's sample rate and channels. +00.wav is the slice
itself (trimmed and faded the same way, no processing). Each file is written under a temporary name and renamed
when complete; render.json is written last and says which renderHash the folder holds. A folder without a matching
render.json is not ready, so the device never plays a half-written slice. A slice renders in
renders/_rendering/<hash> and moves to its slice_XX folder when all 25 files are done; reconcile() moves finished
folders when slices are renumbered and parks removed ones in renders/_unused (reused by undo + Save).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .chop import fade_ramp

RENDER_RANGE = (-12, 12)
SHIFTS = tuple(range(RENDER_RANGE[0], RENDER_RANGE[1] + 1))
CONTEXT_MS = 0.0                  # source audio given to the engine beyond each edge (0: the slice alone, see below)
GUARD_DB = 3.0                    # formant guard: a kept-formant render this much quieter than the slice...
GUARD_FOLLOW = (0.25, 0.5, 0.75, 1.0)   # ...lets its formants follow the pitch this far, the least that's enough
SAFETY_FADES_MS = (6.0, 10.0)     # longer fades tried at an edge that still measures as a click
SUBTYPE = "PCM_24"
DONE_FILE = "render.json"
ENGINE_NAME = "Rubber Band R3 (finer), formants preserved, pylibrb"
UNUSED_DIR = "_unused"
UNUSED_LIMIT_MB = 500             # renders/_unused is trimmed (oldest first) above this


def shift_name(st: int) -> str:
    return f"{st:+03d}.wav" if st else "+00.wav"


# ---------------------------------------------------------------- the engine

def engine_available() -> tuple[bool, str]:
    try:
        import pylibrb  # noqa: F401
    except Exception as exc:  # ImportError, or a DLL problem on Windows
        return False, f"pylibrb could not be loaded ({exc})"
    return True, "pylibrb " + _pylibrb_version()


def _pylibrb_version() -> str:
    try:
        import importlib.metadata
        return importlib.metadata.version("pylibrb")
    except Exception:
        return "?"


def shift_audio(x: np.ndarray, sr: int, semitones: float, formants: bool = True, follow: float = 0.0) -> np.ndarray:
    """Pitch-shift x (frames, channels) by `semitones`, keeping its length (and formants). float32 out.

    follow: how far the formants move with the pitch, 0 = kept (Natural), 1 = moved all the way (like a speed
    change). In between is used only by the formant guard (render_file)."""
    import pylibrb as rb

    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    n, ch = x.shape
    if n == 0 or semitones == 0:
        return x.copy()
    opts = rb.Option.PROCESS_OFFLINE | rb.Option.ENGINE_FINER
    opts |= rb.Option.FORMANT_PRESERVED if formants else rb.Option.FORMANT_SHIFTED
    if ch > 1:
        opts |= rb.Option.CHANNELS_TOGETHER
    s = rb.RubberBandStretcher(sample_rate=int(sr), channels=ch, options=opts)
    s.time_ratio = 1.0
    s.pitch_scale = float(2.0 ** (semitones / 12.0))
    if formants and follow:
        # with formants preserved, the formant scale is how far the envelope moves: 1/pitch scale = kept (the
        # default), 1.0 = moved with the pitch
        s.formant_scale = float(2.0 ** ((follow - 1.0) * semitones / 12.0))
    s.set_expected_input_duration(n)
    planar = np.ascontiguousarray(x.T)                 # pylibrb wants (channels, samples)
    block = 1 << 16
    s.set_max_process_size(block)
    for a in range(0, n, block):
        s.study(planar[:, a:a + block], final=a + block >= n)
    out = []
    for a in range(0, n, block):
        s.process(planar[:, a:a + block], final=a + block >= n)
        y = s.retrieve_available()
        if y.shape[1]:
            out.append(y)
    while True:
        y = s.retrieve_available()
        if y.shape[1] == 0:
            break
        out.append(y)
    y = np.concatenate(out, axis=1).T if out else np.zeros((0, ch), np.float32)
    if len(y) < n:                                       # offline mode gives exactly n; be safe anyway
        y = np.vstack([y, np.zeros((n - len(y), ch), np.float32)])
    return np.ascontiguousarray(y[:n], dtype=np.float32)


# ---------------------------------------------------------------- what is rendered

def edges(sl, sr: int) -> tuple[int, int]:
    """The slice's sample range, rounded exactly as slice_audio() and render_hash() round it."""
    return int(round(sl.start * sr)), int(round(sl.end * sr))


def apply_fades(x: np.ndarray, sr: int, fade_ms: float = 3.0, start_ms: float | None = None,
                end_ms: float | None = None) -> np.ndarray:
    """slice_audio()'s fades: raised-cosine in and out, first and last sample exactly 0 (each end's length can be
    set on its own, for the click safety)."""
    x = np.array(x, dtype=np.float32, copy=True)
    if x.ndim == 1:
        x = x[:, None]
    for ms, at_start in ((fade_ms if start_ms is None else start_ms, True),
                         (fade_ms if end_ms is None else end_ms, False)):
        f = min(len(x) // 2, int(round(ms / 1000.0 * sr)))
        if f > 0:
            r = fade_ramp(f)[:, None]
            if at_start:
                x[:f] *= r
            else:
                x[-f:] *= r[::-1]
    if len(x):
        x[-1] = 0.0
    return x


def render_slice_shift(data: np.ndarray, sr: int, a: int, b: int, semitones: int, fade_ms: float = 3.0,
                       context_ms: float = CONTEXT_MS, match_level: bool = False) -> np.ndarray:
    """A plain render in memory (no guard, no level match unless asked): source samples [a, b) shifted by
    `semitones`, with `context_ms` of the source around it during the shift, trimmed and faded. For studies."""
    c = int(round(context_ms / 1000.0 * sr))
    pa, pb = max(0, a - c), min(len(data), b + c)
    seg = data[pa:pb]
    y = seg if semitones == 0 else shift_audio(seg, sr, semitones)
    y = apply_fades(y[a - pa:a - pa + (b - a)], sr, fade_ms)
    if match_level and semitones:
        y = level_match(y, apply_fades(seg[a - pa:a - pa + (b - a)], sr, fade_ms))
    return y


# ---------------------------------------------------------------- folders

WORK_DIR = "_rendering"           # renders/_rendering/<hash>: a slice being rendered (moved into place when done)


def folder_hash(folder: Path) -> str | None:
    """The renderHash a finished render folder holds, or None if it isn't complete."""
    folder = Path(folder)
    try:
        d = json.loads((folder / DONE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not all((folder / shift_name(st)).exists() for st in SHIFTS):
        return None
    return d.get("renderHash")


def is_ready(project: Path, slice_entry: dict) -> bool:
    return folder_hash(Path(project) / slice_entry["renderFolder"]) == slice_entry["renderHash"]


def ready_hashes(project: Path) -> dict[str, Path]:
    """renderHash -> folder, for every finished render folder of the project (renders/slice_XX)."""
    out = {}
    renders = Path(project) / "renders"
    if renders.exists():
        for d in renders.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                h = folder_hash(d)
                if h:
                    out[h] = d
    return out


def natural_plan(shift: float) -> tuple[int, float]:
    """How the device plays a shift (SPEC §4): the nearest rendered semitone, plus the leftover as a tiny speed
    change (never more than ±50 cents inside ±12; beyond ±12 the end file plus varispeed)."""
    k = int(np.clip(np.floor(shift + 0.5), RENDER_RANGE[0], RENDER_RANGE[1]))
    return k, float(shift - k)


def ready_state(project: Path, slicemap: dict) -> dict[int, bool]:
    """Slice number -> its Natural renders are complete and match its current renderHash."""
    return {s["index"]: is_ready(project, s) for s in slicemap["slices"]}


def _retry(fn, what: str, seconds: float = 10.0):
    """Run a file operation, retrying for a while: on Windows a file that was just written can stay locked for a
    moment (virus scanner, search indexer), and renaming its folder then fails with "access denied"."""
    t0 = time.monotonic()
    delay = 0.02
    while True:
        try:
            return fn()
        except OSError as exc:
            if time.monotonic() - t0 > seconds:
                raise OSError(f"{what}: {exc}") from exc
            time.sleep(delay)
            delay = min(0.5, delay * 2)


def _write_wav_atomic(path: Path, y: np.ndarray, sr: int) -> None:
    tmp = path.with_name(path.stem + ".part.wav")
    sf.write(str(tmp), y, sr, subtype=SUBTYPE)
    _retry(lambda: os.replace(tmp, path), f"renaming {tmp.name}")


def _rmtree(p: Path) -> None:
    p = Path(p)
    if not p.exists():
        return

    def go():
        shutil.rmtree(p, ignore_errors=True)
        if p.exists():
            raise OSError(f"{p} is still there")
    try:
        _retry(go, f"removing {p.name}", seconds=5.0)
    except OSError:
        pass                     # left behind; the next reconcile() tries again


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _retry(lambda: os.replace(src, dst), f"moving {Path(src).name} to {Path(dst).name}")


def reconcile(project: Path, slicemap: dict) -> dict[str, list]:
    """Line render folders up with the slice map (the SPEC §4 cache: renders follow the renderHash, not the number).

    - a finished folder whose hash now belongs to another slice number (a split, merge or delete renumbered the
      slices) is moved to that number instead of being rendered again
    - a finished folder whose hash is no longer in the map goes to renders/_unused/<hash>, so undo + Save can
      bring it back without rendering; a finished render found there is moved back when its slice returns
    - incomplete folders (an interrupted render) are removed
    Returns what happened: {"moved": [...], "parked": [...], "restored": [...], "removed": [...]}.
    """
    project = Path(project)
    renders = project / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    unused = renders / UNUSED_DIR
    want = {s["renderHash"]: s["renderFolder"] for s in slicemap["slices"]}
    report: dict[str, list] = {"moved": [], "parked": [], "restored": [], "removed": []}

    # 1. what is on disk now (folders starting with "_" belong to the renderer)
    have: dict[str, Path] = {}
    for d in sorted(p for p in renders.iterdir() if p.is_dir() and not p.name.startswith("_")):
        h = folder_hash(d)
        if h is None:
            _rmtree(d)
            report["removed"].append(d.name)
            continue
        if h in have:                # the same audio twice: keep the copy already in the right place
            if h in want and d.resolve() == (project / want[h]).resolve():
                have[h], d = d, have[h]
            _rmtree(d)
            report["removed"].append(d.name)
            continue
        have[h] = d
    # 2. everything not in its place moves to a temporary name first (slice numbers can swap)
    staging = renders / "_moving"
    _rmtree(staging)
    for h, d in list(have.items()):
        if h in want and d.resolve() == (project / want[h]).resolve():
            continue
        _move(d, staging / h)
        have[h] = staging / h
    # 3. then into place, or parked in _unused
    for h, d in list(have.items()):
        if d.parent != staging:
            continue
        if h in want:
            target = project / want[h]
            _rmtree(target)
            _move(d, target)
            report["moved"].append(f"{h} -> {Path(want[h]).name}")
        else:
            _rmtree(unused / h)
            _move(d, unused / h)
            report["parked"].append(h)
    _rmtree(staging)
    # 4. renders that come back from _unused
    if unused.exists():
        for d in sorted(unused.iterdir()):
            h = d.name
            if h in want and folder_hash(d) == h and folder_hash(project / want[h]) != h:
                target = project / want[h]
                _rmtree(target)
                _move(d, target)
                report["restored"].append(f"{h} -> {Path(want[h]).name}")
        trim_unused(unused)
    return report


def place(project: Path, done: Path, slicemap: dict) -> str:
    """Move a finished render (renders/_rendering/<hash>) to the folder the map now gives its hash.

    The map is the one on disk at the moment the slice finished, so a Save during the render is respected.
    Returns the folder name it went to ("_unused/<hash>" if the slice is no longer in the map)."""
    project = Path(project)
    h = folder_hash(done)
    want = {s["renderHash"]: s["renderFolder"] for s in slicemap["slices"]}
    if h in want:
        target = project / want[h]
        # whatever holds that number now belongs to another hash: let reconcile() sort it out next pass
        if target.exists() and folder_hash(target) not in (None, h):
            _move(target, project / "renders" / UNUSED_DIR / folder_hash(target))
        _rmtree(target)
        _move(done, target)
        return Path(want[h]).name
    dst = project / "renders" / UNUSED_DIR / str(h)
    _rmtree(dst)
    _move(done, dst)
    return f"{UNUSED_DIR}/{h}"


def trim_unused(unused: Path, limit_mb: float = UNUSED_LIMIT_MB) -> None:
    dirs = [d for d in Path(unused).iterdir() if d.is_dir()]
    sizes = {d: sum(f.stat().st_size for f in d.iterdir() if f.is_file()) for d in dirs}
    total = sum(sizes.values())
    for d in sorted(dirs, key=lambda p: p.stat().st_mtime):
        if total <= limit_mb * 1e6:
            break
        total -= sizes[d]
        _rmtree(d)


# ---------------------------------------------------------------- rendering one slice

class RenderCancelled(Exception):
    pass


@dataclass
class SliceRenderResult:
    index: int
    render_hash: str
    seconds: float              # wall time for all 25 files
    audio_seconds: float        # length of the slice
    files: int
    skipped: bool = False       # already rendered (cache)
    folder: str = ""


def segment_for(data: np.ndarray, sr: int, entry: dict, context_ms: float = CONTEXT_MS):
    """(segment, offset, length): the slice plus its context, and where the slice sits inside it.
    Only this goes to a worker process, not the whole song."""
    a, b = int(round(entry["start"] * sr)), int(round(entry["end"] * sr))
    c = int(round(context_ms / 1000.0 * sr))
    pa, pb = max(0, a - c), min(len(data), b + c)
    return np.ascontiguousarray(data[pa:pb], dtype=np.float32), a - pa, b - a


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def level_match(y: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Give the render the original slice's loudness (RMS), so a slice plays equally loud on every key.
    At most ±6 dB, and never above 0.999 peak."""
    ry, rr = _rms(y), _rms(ref)
    if ry < 1e-7 or rr < 1e-7:
        return y
    g = float(np.clip(rr / ry, 0.5, 2.0))
    peak = float(np.max(np.abs(y))) * g
    if peak > 0.999:
        g *= 0.999 / peak
    return (y * g).astype(np.float32)


def _db(a: float, b: float) -> float:
    return 20.0 * np.log10(max(a, 1e-9) / max(b, 1e-9))


def render_one(seg: np.ndarray, sr: int, offset: int, length: int, st: int, fade_ms: float = 3.0) -> tuple:
    """One render in memory: (audio, info). The steps, each decided by measurement in Milestone 3 (STATUS.md):

    1. shift the slice with formants kept
    2. formant guard: if that made it more than GUARD_DB quieter, the voice's strongest "formant" was really its
       own fundamental (a pure, flute-like tone; Crazy has many), which formant keeping filters away when the
       pitch moves far. Then the formants follow the pitch a quarter, half, three quarters or all the way,
       whichever is the first that keeps the level within GUARD_DB
    3. trim to the slice (already exact), 3 ms fades, level matched to the original slice
    4. click safety: an edge that still measures as a click (chop.click_score) gets a 6 ms, then 10 ms fade
    """
    from .chop import CLICK_RATIO, click_score

    ref = apply_fades(seg[offset:offset + length], sr, fade_ms)
    info = {"follow": 0.0, "lossDb": 0.0, "fadeMs": fade_ms}
    if st == 0:
        return ref, info

    last = {}

    def make(follow: float) -> np.ndarray:
        last["raw"] = shift_audio(seg, sr, st, follow=follow)[offset:offset + length]
        return apply_fades(last["raw"], sr, fade_ms)

    y = make(0.0)
    loss = _db(_rms(y), _rms(ref))
    info["lossDb"] = round(float(loss), 2)
    if loss < -GUARD_DB:
        for f in GUARD_FOLLOW:
            y = make(f)
            loss = _db(_rms(y), _rms(ref))
            info["follow"], info["guardLossDb"] = f, round(float(loss), 2)
            if loss >= -GUARD_DB:
                break
    faded = y
    y = level_match(y, ref)
    cs = click_score(y, sr)
    if max(cs) >= CLICK_RATIO:
        raw = last["raw"] * (_rms(y) / max(_rms(faded), 1e-9))       # with the level match just applied
        for fm in SAFETY_FADES_MS:
            y = apply_fades(raw, sr, fade_ms, start_ms=fm if cs[0] >= CLICK_RATIO else fade_ms,
                            end_ms=fm if cs[1] >= CLICK_RATIO else fade_ms)
            info["fadeMs"] = fm
            if max(click_score(y, sr)) < CLICK_RATIO:
                break
    return y.astype(np.float32), info


def render_file(seg: np.ndarray, sr: int, offset: int, length: int, st: int, fade_ms: float, out: str) -> tuple:
    """One file (runs in a worker process): render_one(), written as 24-bit WAV. Returns (shift, info)."""
    y, info = render_one(seg, sr, offset, length, st, fade_ms)
    _write_wav_atomic(Path(out), y, sr)
    return st, info


def shift_order() -> list[int]:
    """Nearest shifts first (the ones Nearest mode and small moves use most)."""
    return sorted(SHIFTS, key=lambda st: (abs(st), st))


def render_to(work: Path, data: np.ndarray, sr: int, entry: dict, fade_ms: float = 3.0, pool=None, cancel=None,
              on_file=None) -> SliceRenderResult:
    """Render the 25 files of one slice-map entry into the folder `work`, render.json last.

    pool: a concurrent.futures executor (files render in parallel) or None (one after another).
    cancel(): checked between files; True stops the slice (RenderCancelled, the folder is removed)."""
    work = Path(work)
    t0 = time.perf_counter()
    _rmtree(work)
    work.mkdir(parents=True)
    seg, off, n = segment_for(data, sr, entry)
    order = shift_order()
    files: dict[str, dict] = {}
    try:
        if pool is None:
            for st in order:
                if cancel is not None and cancel():
                    raise RenderCancelled()
                _, inf = render_file(seg, sr, off, n, st, fade_ms, str(work / shift_name(st)))
                files[shift_name(st)[:-4]] = inf
                if on_file:
                    on_file(st)
        else:
            from concurrent.futures import FIRST_COMPLETED, wait
            futs = {pool.submit(render_file, seg, sr, off, n, st, fade_ms, str(work / shift_name(st))): st
                    for st in order}
            remaining = set(futs)
            while remaining:
                done, remaining = wait(remaining, timeout=0.25, return_when=FIRST_COMPLETED)
                for f in done:
                    _, inf = f.result()              # re-raises a worker's error
                    files[shift_name(futs[f])[:-4]] = inf
                    if on_file:
                        on_file(futs[f])
                if cancel is not None and cancel():
                    for f in remaining:
                        f.cancel()
                    wait(remaining)
                    raise RenderCancelled()
    except BaseException:
        _rmtree(work)
        raise
    info = {"renderHash": entry["renderHash"], "start": entry["start"], "end": entry["end"],
            "sampleRate": int(sr), "channels": int(data.shape[1] if data.ndim == 2 else 1),
            "renderRange": list(RENDER_RANGE), "contextMs": CONTEXT_MS, "fadeMs": float(fade_ms),
            "engine": ENGINE_NAME, "pylibrb": _pylibrb_version(),
            "seconds": round(time.perf_counter() - t0, 3), "rendered": time.strftime("%Y-%m-%d %H:%M:%S"),
            "guardDb": GUARD_DB,
            "files": {k: files[k] for k in sorted(files, key=lambda k: int(k))}}
    (work / DONE_FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return SliceRenderResult(entry["index"], entry["renderHash"], time.perf_counter() - t0, n / sr, len(SHIFTS))


def render_slice(project: Path, data: np.ndarray, sr: int, entry: dict, fade_ms: float = 3.0, pool=None,
                 cancel=None, on_file=None) -> SliceRenderResult:
    """Render one slice into its renderFolder, unless it is already there (cache). No reconcile, no map reread."""
    project = Path(project)
    if is_ready(project, entry):
        return SliceRenderResult(entry["index"], entry["renderHash"], 0.0, entry["end"] - entry["start"], 0,
                                 skipped=True, folder=entry["renderFolder"])
    work = project / "renders" / WORK_DIR / entry["renderHash"]
    res = render_to(work, data, sr, entry, fade_ms, pool, cancel, on_file)
    target = project / entry["renderFolder"]
    _rmtree(target)
    _move(work, target)
    res.folder = entry["renderFolder"]
    return res
