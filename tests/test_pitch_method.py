"""The Milestone 2 answer to "whole note or steadiest part?" (see study_note_pitch.py and STATUS.md).

A fast subset of the study: the vibrato-aware default must beat the Milestone 1 whole-note median on vibrato and
not be worse on scoops, fall-offs and short dips; the "steadiest part" idea must stay the worst on vibrato.
"""
import numpy as np

from study_note_pitch import SR, voice
from vocalchop.notes import NoteParams, measure_pitch, voiced_frames
from vocalchop.pitch import to_analysis_rate, track_pitch


def errors(paths, target):
    out = {m: [] for m in ("whole", "steadiest", "auto")}
    for p in paths:
        track = track_pitch(to_analysis_rate(voice(p), SR), 40, 84, workers=1)
        v = voiced_frames(track, NoteParams())
        idx = np.flatnonzero(v)
        for m in out:
            out[m].append(abs(100 * (measure_pitch(track, v, idx[0], idx[-1] + 1, m) - target)))
    return {m: float(np.sqrt(np.mean(np.square(e)))) for m, e in out.items()}


def test_vibrato_aware_pitch_beats_whole_note_on_vibrato():
    rng = np.random.default_rng(11)
    paths = []
    for secs in (0.6, 1.2):
        t = np.arange(int(secs * SR)) / SR
        for depth, rate in ((0.6, 4.5), (0.7, 6.5), (0.5, 5.5)):
            paths.append(64.0 + depth * np.clip(t / 0.15, 0, 1) * np.sin(2 * np.pi * rate * t + rng.uniform(0, 6)))
    e = errors(paths, 64.0)
    assert e["auto"] < e["whole"] and e["auto"] <= 6.0
    assert e["steadiest"] > e["auto"]


def test_vibrato_aware_pitch_keeps_scoops_falls_and_dips_out():
    k = int(0.6 * SR)
    paths = []
    p = np.full(k, 64.0)
    p[:int(0.12 * SR)] -= 2.0 * (1 - np.linspace(0, 1, int(0.12 * SR))) ** 1.5
    paths.append(p)
    p = np.full(k, 64.0)
    p[-int(0.15 * SR):] -= 2.5 * np.linspace(0, 1, int(0.15 * SR)) ** 2
    paths.append(p)
    p = np.full(k, 64.0)
    for c in (0.35, 0.7):
        a, b = int(c * k), int(c * k + 0.07 * SR)
        p[a:b] -= np.sin(np.linspace(0, np.pi, b - a))
    paths.append(p)
    e = errors(paths, 64.0)
    assert e["auto"] <= 3.0 and e["auto"] <= e["whole"] + 1.0
