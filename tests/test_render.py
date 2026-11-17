"""Milestone 3: Natural render (Rubber Band R3, formants kept), files, cache, background rendering."""
import json
import shutil
import time

import numpy as np
import pytest
import soundfile as sf

from synth import SR, SungNote, sing, write
from vocalchop import render, session
from vocalchop.audition import _Span, natural_clip, nearest_c, shift_to, varispeed
from vocalchop.background import BackgroundRenderer
from vocalchop.chop import ChopSettings, Slice, SliceEditor, SongContext, click_score, render_hash, slice_audio
from vocalchop.m3check import formant_fit, framewise_tuner, harmonicity_db
from vocalchop.presets import parse_note
from vocalchop.project import analyze_file
from vocalchop.slicemap import read_slice_map
from vocalchop.tuner import read_tuner

pytest.importorskip("pylibrb")

NOTES = [parse_note(n) for n in ["E4", "A4", "C#5"]]


def song(home, notes=NOTES, secs=0.3, name="take.wav"):
    path = home / "Acapellas" / name
    write(path, sing([SungNote(m, secs, gap_after=0.2) for m in notes], detune_cents=8), stereo=True)
    r = analyze_file(path, "female")
    ed = SliceEditor(SongContext.from_result(r))
    ed.chop(ChopSettings(mode="pitch"))
    return r, ed


def tune(x, sr, lo=36, hi=100):
    m = x.mean(axis=1) if x.ndim == 2 else x
    return read_tuner(m, sr, _Span(0, len(m) / sr), lo, hi)


def render_all(r, ed):
    rep = session.save(r, ed)
    m = read_slice_map(rep.map_path)
    for e in m["slices"]:
        render.render_slice(r.folder, r.audio.data, r.audio.sample_rate, e)
    return m


# ---------------------------------------------------------------- the engine

def test_engine_is_available_and_shifts_pitch_keeping_length():
    ok, msg = render.engine_available()
    assert ok, msg
    y = sing([SungNote(parse_note("A4"), 0.8)], lead_in=0.05)[:, None]
    t0 = tune(y, SR)
    for st in (-12, -5, 7, 12):
        z = render.shift_audio(y, SR, st)
        assert z.shape == y.shape
        assert abs(100 * (tune(z, SR).midi - t0.midi) - 100 * st) < 8


def test_formants_are_kept_unlike_a_speed_change():
    """A voice with fixed formants (700 and 1800 Hz) and a little vibrato: Natural keeps them, a plain speed change
    moves them with the pitch (the measurement used by check_m3 sees the difference)."""
    sr, f0 = SR, 220.0
    t = np.arange(int(0.8 * sr)) / sr
    phase = 2 * np.pi * np.cumsum(f0 * 2 ** (0.2 / 12 * np.sin(2 * np.pi * 5.5 * t))) / sr

    def amp(f):
        return 1 / (1 + ((f - 700) / 400) ** 2) + 0.5 / (1 + ((f - 1800) / 400) ** 2) + 0.02

    y = sum(amp(k * f0) * np.sin(k * phase) for k in range(1, 40))
    y = (0.2 * y / np.max(np.abs(y))).astype(np.float32)[:, None]
    for st in (-5, 5):
        nat = formant_fit(y, f0, render.shift_audio(y, sr, st), st, sr)
        spd = formant_fit(y, f0, varispeed(y, sr, st), st, sr)
        assert nat.kept and abs(nat.moved) <= 1.0
        assert not spd.kept and abs(spd.moved - st) <= 1.0


def test_natural_plan_nearest_semitone_and_leftover():
    assert render.natural_plan(4.3) == (4, pytest.approx(0.3))
    assert render.natural_plan(-0.5)[0] in (0, -1) and abs(render.natural_plan(-0.5)[1]) <= 0.5
    k, rest = render.natural_plan(14.2)          # beyond the range: the end file plus varispeed
    assert k == 12 and rest == pytest.approx(2.2)
    for x in np.linspace(-12, 12, 97):
        assert abs(render.natural_plan(x)[1]) <= 0.5 + 1e-9


def test_formant_guard_keeps_a_flute_like_tone_audible():
    """A near-pure tone: keeping its 'formant' (its own fundamental) would filter it away at +12."""
    t = np.arange(int(0.6 * SR)) / SR
    x = (0.3 * np.sin(2 * np.pi * 400 * t) + 0.003 * np.sin(2 * np.pi * 800 * t)).astype(np.float32)[:, None]
    y, info = render.render_one(x, SR, 0, len(x), 12)
    assert info["lossDb"] < -render.GUARD_DB and info["follow"] > 0
    assert abs(20 * np.log10(np.sqrt(np.mean(y ** 2)) / np.sqrt(np.mean(x[:len(y)] ** 2)))) < 1.0
    # an ordinary voice at +5 doesn't need the guard
    v = sing([SungNote(parse_note("A4"), 0.6)], lead_in=0.05)[:, None]
    _, info = render.render_one(v, SR, 0, len(v), 5)
    assert info["follow"] == 0.0


# ---------------------------------------------------------------- files

def test_render_slice_writes_25_files_24_bit_faded_and_level_matched(vocalchop_home):
    r, ed = song(vocalchop_home)
    m = render_all(r, ed)
    e = m["slices"][1]
    folder = r.folder / e["renderFolder"]
    names = sorted(p.name for p in folder.iterdir())
    assert names == sorted([render.shift_name(s) for s in render.SHIFTS] + [render.DONE_FILE])
    assert "+05.wav" in names and "-12.wav" in names and "+00.wav" in names
    info = json.loads((folder / render.DONE_FILE).read_text())
    assert info["renderHash"] == e["renderHash"] and len(info["files"]) == 25
    s = ed.slices[1]
    ref = slice_audio(r.audio.data, SR, s, gain=False)
    for st in render.SHIFTS:
        fi = sf.info(str(folder / render.shift_name(st)))
        assert fi.subtype == "PCM_24" and fi.samplerate == SR and fi.channels == 2 and fi.frames == len(ref)
        y, _ = sf.read(str(folder / render.shift_name(st)), always_2d=True)
        assert np.all(y[0] == 0) and np.all(y[-1] == 0)
        assert max(click_score(y, SR)) < 2.0
        assert abs(20 * np.log10(np.sqrt(np.mean(y ** 2)) / np.sqrt(np.mean(ref ** 2)))) < 1.5
    z, _ = sf.read(str(folder / "+00.wav"), always_2d=True, dtype="float32")
    assert np.max(np.abs(z - ref)) <= 2.0 ** -22                       # +00 is the slice itself
    y, _ = sf.read(str(folder / "-05.wav"), always_2d=True, dtype="float32")
    assert abs(framewise_tuner(ref, y, SR, 52, 84, -5)) < 10
    assert harmonicity_db(y, SR, tune(ref, SR).hz * 2 ** (-5 / 12)) > harmonicity_db(ref, SR, tune(ref, SR).hz) - 3
    assert render.is_ready(r.folder, e) and all(render.ready_state(r.folder, m).values())


def test_gain_and_note_edits_never_re_render_but_edges_do(vocalchop_home):
    r, ed = song(vocalchop_home)
    s = ed.slices[0]
    h = render_hash(r.source, SR, s)
    s.gain, s.keep_as_sung, s.octave, s.hand_note = -6.0, True, 1, 70
    assert render_hash(r.source, SR, s) == h
    s.end += 0.01
    assert render_hash(r.source, SR, s) != h
    m = render_all(r, ed)
    res = render.render_slice(r.folder, r.audio.data, SR, m["slices"][0])
    assert res.skipped                                                # cache: already there


def test_reconcile_follows_renumbering_parks_and_restores(vocalchop_home):
    r, ed = song(vocalchop_home)
    m = render_all(r, ed)
    stamps = {e["renderHash"]: json.loads((r.folder / e["renderFolder"] / render.DONE_FILE).read_text())["rendered"]
              for e in m["slices"]}
    first = m["slices"][0]["renderHash"]
    ed.delete(0)
    session.save(r, ed)
    m2 = read_slice_map(r.folder / "slicemap.json")
    rep = render.reconcile(r.folder, m2)
    assert len(rep["moved"]) == 2 and rep["parked"] == [first]
    assert all(render.ready_state(r.folder, m2).values())
    assert (r.folder / "renders" / render.UNUSED_DIR / first).is_dir()
    assert not (r.folder / "renders" / "slice_03").exists()
    ed.undo()
    session.save(r, ed)
    m3 = read_slice_map(r.folder / "slicemap.json")
    rep = render.reconcile(r.folder, m3)
    assert rep["restored"] and all(render.ready_state(r.folder, m3).values())
    for e in m3["slices"]:                                          # nothing was rendered again
        d = json.loads((r.folder / e["renderFolder"] / render.DONE_FILE).read_text())
        assert d["rendered"] == stamps[e["renderHash"]]


def test_interrupted_renders_are_cleaned_up(vocalchop_home):
    r, ed = song(vocalchop_home)
    m = render_all(r, ed)
    half = r.folder / m["slices"][2]["renderFolder"]
    (half / render.DONE_FILE).unlink()                              # as if the app closed mid-slice
    work = r.folder / "renders" / render.WORK_DIR / "abc"
    work.mkdir(parents=True)
    rep = render.reconcile(r.folder, m)
    assert "slice_03" in rep["removed"] and not half.exists()
    assert not render.is_ready(r.folder, m["slices"][2]) and render.is_ready(r.folder, m["slices"][0])


# ---------------------------------------------------------------- background

def wait(br):
    assert br.wait_idle(300)


@pytest.mark.parametrize("processes", [False, True])
def test_save_starts_the_background_render(vocalchop_home, processes):
    r, ed = song(vocalchop_home)
    events = []
    br = BackgroundRenderer(on_event=events.append, use_processes=processes)
    try:
        session.save(r, ed)
        q = json.loads((r.folder / "render_queue.json").read_text())
        assert len(q["pending"]) == 3 and q["ready"] == []
        br.request(r.folder)
        wait(br)
    finally:
        br.stop()
    m = read_slice_map(r.folder / "slicemap.json")
    assert all(render.ready_state(r.folder, m).values())
    q = json.loads((r.folder / "render_queue.json").read_text())
    assert q["pending"] == [] and q["ready"] == [1, 2, 3]
    kinds = [e.kind for e in events]
    assert kinds.count("slice") == 3 and kinds[-1] == "idle" and kinds.count("file") == 75
    assert not (r.folder / "renders" / render.WORK_DIR).exists()


def test_saving_again_during_a_render_is_safe(vocalchop_home):
    r, ed = song(vocalchop_home, notes=[parse_note(n) for n in ["E4", "A4", "C#5", "B4", "G#4"]])
    br = BackgroundRenderer(use_processes=False)
    try:
        session.save(r, ed)
        br.request(r.folder)
        time.sleep(0.3)                                             # mid-render
        ed.delete(1)                                                # renumbers slices 3.. and drops one
        session.save(r, ed)
        br.request(r.folder)
        wait(br)
    finally:
        br.stop()
    m = read_slice_map(r.folder / "slicemap.json")
    assert len(m["slices"]) == 4 and all(render.ready_state(r.folder, m).values())
    dirs = sorted(p.name for p in (r.folder / "renders").iterdir() if not p.name.startswith("_"))
    assert dirs == ["slice_01", "slice_02", "slice_03", "slice_04"]


def test_a_second_request_renders_nothing(vocalchop_home):
    r, ed = song(vocalchop_home)
    events = []
    br = BackgroundRenderer(on_event=events.append, use_processes=False)
    try:
        session.save(r, ed)
        br.request(r.folder)
        wait(br)
        events.clear()
        br.request(r.folder)
        wait(br)
    finally:
        br.stop()
    assert [e.kind for e in events] == ["start", "idle"]


def test_natural_audition_matches_the_device_formula(vocalchop_home):
    r, ed = song(vocalchop_home)
    m = render_all(r, ed)
    s = ed.slices[2]
    x, target, st, k, rest = natural_clip(r.folder / m["slices"][2]["renderFolder"], SR, s)
    assert target == nearest_c(s) and st == pytest.approx(shift_to(s, target))
    assert k == round(st) and abs(rest) <= 0.5
    got = tune(x, SR).midi - r.tuning_offset_cents / 100.0
    assert int(round(got)) == target                                  # it is a C
