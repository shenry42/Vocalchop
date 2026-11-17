"""Study for the Milestone 2 open question: should a slice's pitch come from the whole note or its steadiest part?

Synthetic sung notes with a known intended pitch go through the real pipeline (pYIN + refinement + voicing), then
each candidate method names the pitch. Error = cents away from the intended pitch. Run:

    python tests/study_note_pitch.py         (prints a table; also used by test_pitch_method.py)

Cases with a clear answer in the listening research are scored:
  scoop     a slide up into the note (perceived pitch = the note)
  falloff   a drop at the end of the note (perceived pitch = the note)
  vibrato   symmetric vibrato, any rate/depth/length (perceived pitch = the centre / mean)
  dips      a held note with two short dips (an ornament, like Crazy #254)
"Bent" notes (start flat, rise to the note) have no agreed answer and are only shown, not scored.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vocalchop.notes import NoteParams, PITCH_METHODS, measure_pitch, voiced_frames  # noqa: E402
from vocalchop.pitch import ANALYSIS_SR, to_analysis_rate, track_pitch  # noqa: E402

SR = 44100


def voice(path: np.ndarray, sr: int = SR, seed: int = 0) -> np.ndarray:
    """Harmonic 'voice' following a pitch path (MIDI per sample), with 30/60 ms fades and a little noise."""
    rng = np.random.default_rng(seed)
    f = 440.0 * 2 ** ((path - 69) / 12)
    phase = 2 * np.pi * np.cumsum(f) / sr
    y = sum((1.0 / h) * (1.6 if h in (2, 3) else 1.0) * np.sin(h * phase) for h in range(1, 16))
    k = len(path)
    env = np.ones(k)
    a, r = min(k // 2, int(0.03 * sr)), min(k // 2, int(0.06 * sr))
    env[:a] = np.linspace(0, 1, a)
    env[-r:] = np.linspace(1, 0, r)
    out = np.concatenate([np.zeros(int(0.3 * sr)), 0.08 * y * env, np.zeros(int(0.3 * sr))])
    return (out + 1e-3 * rng.standard_normal(len(out))).astype(np.float32)


def cases(rng: np.random.Generator):
    """(group, label, intended MIDI, pitch path)."""
    out = []
    for target in (52.0, 57.3, 64.0, 69.6, 74.0):
        for secs in (0.3, 0.6, 1.2):
            k = int(secs * SR)
            t = np.arange(k) / SR
            for depth, dur in ((1.0, 0.06), (2.0, 0.12), (3.0, 0.15)):
                p = np.full(k, target)
                s = int(dur * SR)
                p[:s] -= depth * (1 - np.linspace(0, 1, s)) ** 1.5
                out.append(("scoop", f"{target} {secs}s -{depth}st/{dur * 1000:.0f}ms", target, p))
            for depth, dur in ((1.0, 0.08), (2.5, 0.15)):
                p = np.full(k, target)
                s = int(dur * SR)
                p[-s:] -= depth * np.linspace(0, 1, s) ** 2
                out.append(("falloff", f"{target} {secs}s -{depth}st/{dur * 1000:.0f}ms", target, p))
            for vd, vr in ((0.3, 5.5), (0.6, 4.5), (0.7, 6.5)):
                ph = rng.uniform(0, 2 * np.pi)
                p = target + vd * np.clip(t / 0.15, 0, 1) * np.sin(2 * np.pi * vr * t + ph)
                out.append(("vibrato", f"{target} {secs}s ±{vd}st {vr}Hz", target, p))
            if secs >= 0.6:
                p = np.full(k, target)
                for c in (0.35, 0.7):
                    a, b = int(c * k), int(c * k + 0.07 * SR)
                    p[a:b] -= np.sin(np.linspace(0, np.pi, b - a)) * 1.0
                out.append(("dips", f"{target} {secs}s two -1st dips", target, p))
                p = np.full(k, target)
                s = int(0.4 * k)
                p[:s] -= 0.45 * (1 - np.linspace(0, 1, s))
                out.append(("bent", f"{target} {secs}s starts -45c, reaches the note at 40%", target, p))
    return out


def run(verbose: bool = True) -> dict[str, dict[str, float]]:
    rng = np.random.default_rng(7)
    p = NoteParams()
    errs: dict[str, dict[str, list[float]]] = {}
    for group, label, target, path in cases(rng):
        y = to_analysis_rate(voice(path), SR)
        track = track_pitch(y, 40, 84, workers=1)
        voiced = voiced_frames(track, p)
        idx = np.flatnonzero(voiced)
        s, e = int(idx[0]), int(idx[-1]) + 1
        row = []
        for m in PITCH_METHODS:
            got = measure_pitch(track, voiced, s, e, method=m)
            err = 100 * (got - target)
            errs.setdefault(group, {}).setdefault(m, []).append(err)
            row.append(f"{err:+6.1f}")
        if verbose:
            print(f"{group:8s} {label:42s} " + " ".join(row))
    summary = {g: {m: float(np.sqrt(np.mean(np.square(v)))) for m, v in d.items()} for g, d in errs.items()}
    worst = {g: {m: float(np.max(np.abs(v))) for m, v in d.items()} for g, d in errs.items()}
    if verbose:
        print("\nRMS error in cents (worst case in brackets):")
        print(f"{'':8s} " + " ".join(f"{m:>18s}" for m in PITCH_METHODS))
        for g in summary:
            print(f"{g:8s} " + " ".join(f"{summary[g][m]:8.1f} ({worst[g][m]:5.1f})   " for m in PITCH_METHODS))
    return {"rms": summary, "worst": worst}


if __name__ == "__main__":
    run()
