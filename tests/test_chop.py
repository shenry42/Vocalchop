"""Chopping and slice editing (SPEC §3.2, §3.4, Milestone 2) on synthetic voices with known answers."""
import json

import numpy as np
import pytest

from synth import SR, SungNote, sing, write
from vocalchop.chop import (CLICK_RATIO, ChopSettings, EditError, Slice, SliceEditor, SongContext, apply_rules,
                            chop, click_score, equal_ranges, merge_short, silence_ranges, slice_audio, suggest_split,
                            zero_crossing)
from vocalchop.notes import NoteParams, analyze_notes
from vocalchop.pitch import to_analysis_rate, track_pitch
from vocalchop.presets import PRESETS, parse_note
from vocalchop.project import analyze_file
from vocalchop.session import load, save
from vocalchop.slicemap import read_slice_map, validate_slice_map

MELODY = [parse_note(n) for n in ["E4", "G#4", "B4", "C#5", "B4", "A4", "F#4", "E4"]]


def harmonic(path: np.ndarray, sr: int = SR) -> np.ndarray:
    f = 440.0 * 2 ** ((path - 69) / 12)
    ph = 2 * np.pi * np.cumsum(f) / sr
    return sum((1.0 / h) * (1.6 if h in (2, 3) else 1.0) * np.sin(h * ph) for h in range(1, 16)) / 3.0


def legato(notes: list[int], secs: float = 0.4, slide: float = 0.03, sr: int = SR, lead: float = 0.3):
    """One continuous voice (no gaps, constant level) that steps through `notes`; returns audio and change times."""
    k = int(secs * sr)
    path = np.concatenate([np.full(k, float(m)) for m in notes])
    s = int(slide * sr)
    changes = []
    for i in range(1, len(notes)):
        c = i * k
        path[c - s // 2:c + s // 2] = np.linspace(notes[i - 1], notes[i], 2 * (s // 2))
        changes.append(lead + c / sr)
    y = 0.25 * harmonic(path, sr)
    env = np.ones(len(y))
    a = int(0.03 * sr)
    env[:a] = np.linspace(0, 1, a)
    env[-a:] = np.linspace(1, 0, a)
    y = np.concatenate([np.zeros(int(lead * sr)), y * env, np.zeros(int(lead * sr))])
    return y.astype(np.float32), changes


def context(y: np.ndarray, preset: str = "female", sr: int = SR) -> SongContext:
    p = PRESETS[preset]
    track = track_pitch(to_analysis_rate(y, sr), p.low, p.high, workers=1)
    a = analyze_notes(track)
    return SongContext(mono=y.astype(np.float32), sample_rate=sr, track=track, voiced=a.voiced_mask,
                       tuning_offset_cents=a.tuning_offset_cents, notes=a.notes)


# ---------------------------------------------------------------- Pitch mode lands cuts on note changes

def test_pitch_mode_cuts_land_on_legato_note_changes():
    y, changes = legato(MELODY)
    ctx = context(y)
    sl = chop(ctx, (0, ctx.duration), ChopSettings(mode="pitch"))
    assert [s.note for s in sl] == MELODY
    cuts = [s.start for s in sl[1:]]
    for got, want in zip(cuts, changes):
        assert abs(got - want) <= 0.025, (got, want)        # within 25 ms of the true change
    for a, b in zip(sl, sl[1:]):
        assert a.end == b.start                            # legato: no pre-roll into the previous note, no gap


def test_pitch_mode_preroll_keeps_the_consonant_before_a_gapped_note():
    y = sing([SungNote(m, 0.4, gap_after=0.25) for m in MELODY[:4]])
    ctx = context(y)
    sl = chop(ctx, (0, ctx.duration), ChopSettings(mode="pitch"))
    assert [s.note for s in sl] == MELODY[:4]
    notes = ctx.notes
    for s, n in zip(sl, notes):
        assert 0.014 <= n.start - s.start <= 0.026, (n.start, s.start)   # 20 ms earlier, ±5 ms zero-crossing snap
    no_pre = chop(ctx, (0, ctx.duration), ChopSettings(mode="pitch", preroll_ms=0))
    assert all(abs(a.start - n.start) <= 0.006 for a, n in zip(no_pre, notes))


def test_region_limits_the_chop_and_clips_notes_at_its_edges():
    y, changes = legato(MELODY)
    ctx = context(y)
    region = (changes[1] + 0.1, changes[4] + 0.1)        # from inside note 3 to inside note 6
    sl = chop(ctx, region, ChopSettings(mode="pitch"))
    assert [s.note for s in sl] == MELODY[2:6]
    assert sl[0].start >= region[0] - 0.026 and sl[-1].end <= region[1] + 0.006


# ---------------------------------------------------------------- shared rules

def test_every_edge_is_a_zero_crossing_and_every_slice_starts_and_ends_at_zero():
    y = sing([SungNote(m, 0.3, gap_after=0.1) for m in MELODY])
    stereo = np.stack([y, -0.7 * y + 0.01 * np.sin(np.arange(len(y)) * 0.01)], axis=1)
    ctx = context(y)
    for mode in ("pitch", "silence", "equal"):
        for s in chop(ctx, (0, ctx.duration), ChopSettings(mode=mode)):
            for t in (s.start, s.end):
                i = int(round(t * SR))
                assert np.signbit(y[i - 1]) != np.signbit(y[i]) or y[i] == 0 or abs(y[i]) < 1e-3, (mode, t)
            x = slice_audio(stereo, SR, s)
            assert x.shape[1] == 2
            assert np.all(x[0] == 0) and np.all(x[-1] == 0)
            assert max(click_score(x, SR)) < CLICK_RATIO


def test_click_detector_finds_a_hard_cut_and_passes_a_faded_one():
    t = np.arange(SR) / SR
    y = 0.5 * np.sin(2 * np.pi * 220 * t)
    peak = int(SR / 220 / 4) + 2000                       # cut right at a waveform peak
    hard = y[peak:peak + 8000]
    assert max(click_score(hard, SR)) > 5
    s = Slice(peak / SR, (peak + 8000) / SR)
    faded = slice_audio(y, SR, s)
    assert max(click_score(faded, SR)) < 1.0


def test_short_pieces_merge_into_a_close_neighbour_or_are_widened_when_alone():
    assert merge_short([(0.0, 0.5), (0.5, 0.54), (0.54, 1.0)], 0.06) == [(0.0, 0.54), (0.54, 1.0)]
    assert merge_short([(0.0, 0.5), (0.55, 0.58)], 0.06) == [(0.0, 0.58)]          # 50 ms gap: merges
    out = merge_short([(0.0, 0.5), (1.0, 1.03), (2.0, 2.5)], 0.06)                   # lone 30 ms syllable
    assert len(out) == 3 and abs((out[1][1] - out[1][0]) - 0.06) < 1e-9 and out[1][0] < 1.0 < out[1][1]


def test_zero_crossing_moves_at_most_the_reach():
    y = np.sin(2 * np.pi * 100 * np.arange(SR) / SR)       # crossings every 5 ms
    t = zero_crossing(y, SR, 0.0123)
    assert abs(t - 0.0123) <= 0.0026 and abs(y[int(round(t * SR))]) < 0.02
    flat = np.ones(SR)
    assert zero_crossing(flat, SR, 0.5) == 0.5             # none in reach: unchanged


# ---------------------------------------------------------------- the other modes

def test_silence_gate_cuts_at_gaps_of_60_ms_or_more_only():
    notes = [SungNote(64, 0.4, gap_after=0.2), SungNote(67, 0.4, gap_after=0.04),   # 40 ms gap: no cut
             SungNote(69, 0.4, gap_after=0.3), SungNote(71, 0.4)]
    y = sing(notes, noise_db=-55)                         # noise floor below the −45 dBFS gate
    r = silence_ranges(y, SR, (0, len(y) / SR))
    assert len(r) == 3
    ctx = context(y)
    sl = chop(ctx, (0, ctx.duration), ChopSettings(mode="silence"))
    assert len(sl) == 3 and sl[1].length > 0.8           # notes 2 and 3 stay together


def test_equal_mode_gives_n_equal_slices():
    y = sing([SungNote(m, 0.3) for m in MELODY])
    ctx = context(y)
    for n in (4, 8, 16, 32):
        sl = chop(ctx, (0.5, 2.5), ChopSettings(mode="equal", equal_count=n))
        assert len(sl) == n
        lens = np.array([s.length for s in sl[1:-1]])
        assert np.all(np.abs(lens - 2.0 / n) <= 0.011)
    assert equal_ranges((0, 1), 4)[1] == (0.25, 0.5)


def test_manual_mode_splits_where_you_click_and_refuses_tiny_pieces():
    y = sing([SungNote(m, 0.3) for m in MELODY])
    ctx = context(y)
    ed = SliceEditor(ctx)
    ed.set_region(0.3, 2.0)
    ed.chop(ChopSettings(mode="manual"))
    assert len(ed.slices) == 1
    j = ed.split(0, 1.0)
    assert len(ed.slices) == 2 and abs(ed.slices[j].start - 1.0) <= 0.006
    with pytest.raises(EditError):
        ed.split(1, ed.slices[1].start + 0.03)


# ---------------------------------------------------------------- flags on the right slices

def test_glide_flag_is_on_the_glide_slice_and_not_on_the_steady_notes():
    notes = [SungNote(64, 0.5, gap_after=0.2), SungNote(64, 0.6, glide_to=69, gap_after=0.2), SungNote(67, 0.5)]
    ctx = context(sing(notes))
    sl = chop(ctx, (0, ctx.duration), ChopSettings(mode="pitch"))
    glide_mid = 0.3 + 0.5 + 0.2 + 0.3
    steady = [s for s in sl if s.end < 0.95 or s.start > 1.75]
    assert len(steady) == 2 and not any("glide" in s.flags for s in steady)
    at_mid = next(s for s in sl if s.start <= glide_mid < s.end)
    assert "glide" in at_mid.flags
    t = suggest_split(at_mid, ctx)
    assert t is not None and at_mid.start < t < at_mid.end


def test_low_confidence_flag_is_on_the_breath_slice_only():
    """Sung note, 0.4 s of breath noise, sung note; chopped by hand at the joins."""
    rng = np.random.default_rng(3)
    n1 = sing([SungNote(64, 0.6, gap_after=0.0)], lead_in=0.0)
    breath = (0.05 * rng.standard_normal(int(0.4 * SR))).astype(np.float32)
    n2 = sing([SungNote(67, 0.6, gap_after=0.0)], lead_in=0.0)
    lead = np.zeros(int(0.3 * SR), dtype=np.float32)
    y = np.concatenate([lead, n1, breath, n2, lead])
    j1, j2 = (len(lead) + len(n1)) / SR, (len(lead) + len(n1) + len(breath)) / SR
    ctx = context(y)
    ed = SliceEditor(ctx)
    ed.set_region(0.3, j2 + 0.6)
    ed.chop(ChopSettings(mode="manual"))
    ed.split(0, j1)
    ed.split(1, j2)
    assert [("low_confidence" in s.flags) for s in ed.slices] == [False, True, False]
    assert [s.note for s in (ed.slices[0], ed.slices[2])] == [64, 67]


def test_flags_follow_their_definitions():
    y = sing([SungNote(m, 0.35, scoop=1.0) for m in MELODY], vibrato_semitones=0.4)
    ctx = context(y)
    p = NoteParams()
    for s in chop(ctx, (0, ctx.duration), ChopSettings(mode="pitch")):
        assert ("glide" in s.flags) == (s.spread > p.glide_semitones)
        assert ("low_confidence" in s.flags) == (s.confidence < p.low_confidence)


# ---------------------------------------------------------------- editing

def test_edit_actions_and_undo():
    y, changes = legato(MELODY)
    ctx = context(y)
    ed = SliceEditor(ctx)
    ed.chop()
    n0 = len(ed.slices)
    before = [(s.start, s.end) for s in ed.slices]
    ed.merge_next(2)
    assert len(ed.slices) == n0 - 1 and "glide" in ed.slices[2].flags
    ed.split_suggested(2)
    assert len(ed.slices) == n0 and abs(ed.slices[3].start - changes[2]) <= 0.03
    assert [s.note for s in ed.slices] == MELODY
    ed.undo()
    ed.undo()
    assert [(s.start, s.end) for s in ed.slices] == before
    ed.redo()
    assert len(ed.slices) == n0 - 1
    ed.undo()
    # drag a shared boundary: both neighbours move, notes are re-measured
    ed.move_edge(3, "start", changes[2] + 0.05)
    assert ed.slices[2].end == ed.slices[3].start and abs(ed.slices[3].start - changes[2] - 0.05) <= 0.006
    with pytest.raises(EditError):
        ed.move_edge(3, "end", 99.0) or ed.split(3, ed.slices[3].start + 0.01)
    ed.shift_octave(0, 1)
    assert ed.slices[0].note == MELODY[0] + 12 and ed.slices[0].cents == ed.slices[0].det_cents
    ed.set_note(1, 70)
    assert ed.slices[1].note == 70 and ed.slices[1].cents == 0
    ed.set_note(1, None)
    assert ed.slices[1].note == MELODY[1]
    ed.set_keep_as_sung(2, True)
    ed.delete(4)
    assert len(ed.slices) == n0 - 1 and ed.slices[2].keep_as_sung


def test_more_than_32_slices_cannot_be_saved():
    y = sing([SungNote(m, 0.3) for m in MELODY] * 5)
    ctx = context(y)
    ed = SliceEditor(ctx)
    ed.chop(ChopSettings(mode="pitch"))
    assert len(ed.slices) == 40 and ed.problems()
    ed.set_region(0, ed.slices[31].end + 0.01)
    ed.chop()
    assert len(ed.slices) == 32 and not ed.problems()


# ---------------------------------------------------------------- saving

def test_save_writes_a_valid_map_queues_changed_slices_and_reopens(vocalchop_home):
    path = vocalchop_home / "Acapellas" / "take.wav"
    y, _ = legato(MELODY)
    write(path, y, stereo=True)
    r = analyze_file(path, "female")
    ctx = SongContext.from_result(r)
    ed = SliceEditor(ctx)
    ed.chop()
    ed.set_keep_as_sung(1, True)
    ed.shift_octave(2, -1)
    rep = save(r, ed)
    assert rep.count == 8 and rep.changed == list(range(1, 9)) and not ed.dirty
    m = read_slice_map(rep.map_path)
    assert validate_slice_map(m) == [] and m["fadeMs"] == 3.0
    assert [s["note"] for s in m["slices"]] == [MELODY[0], MELODY[1], MELODY[2] - 12] + MELODY[3:]
    assert m["slices"][1]["keepAsSung"] is True
    q = json.loads((r.folder / "render_queue.json").read_text())
    assert len(q["pending"]) == 8
    # move one boundary: only the two slices that share it need rendering again
    ed.move_edge(5, "start", ed.slices[5].start + 0.04)
    rep2 = save(r, ed)
    assert rep2.changed == [5, 6]
    # reopening restores the slices and the hand edits
    ed2 = SliceEditor(ctx)
    assert load(r, ed2)
    assert [(round(s.start, 6), round(s.end, 6)) for s in ed2.slices] == \
           [(round(s.start, 6), round(s.end, 6)) for s in ed.slices]
    assert ed2.slices[1].keep_as_sung and ed2.slices[2].octave == -1 and not ed2.dirty


def test_apply_rules_never_overlaps():
    y = sing([SungNote(m, 0.2, gap_after=0.005) for m in MELODY])
    rs = apply_rules([(0.3 + 0.205 * i, 0.3 + 0.205 * i + 0.2) for i in range(8)], y, SR, ChopSettings())
    assert all(a[1] <= b[0] for a, b in zip(rs, rs[1:]))


def test_a_consonant_just_before_a_legato_cut_goes_to_the_next_slice():
    y, changes = legato(MELODY[:3])
    rng = np.random.default_rng(2)
    c = int(round((changes[0] - 0.008) * SR))                # a 'k' burst starting 8 ms before the change
    y[c:c + int(0.012 * SR)] += (0.3 * rng.standard_normal(int(0.012 * SR))).astype(np.float32)
    ctx = context(y)
    sl = chop(ctx, (0, ctx.duration), ChopSettings(mode="pitch"))
    assert [s.note for s in sl] == MELODY[:3]
    assert sl[1].start <= changes[0] - 0.008 + 0.002          # the burst opens slice 2
    assert sl[0].end == sl[1].start
    x = slice_audio(y, SR, sl[0])
    assert click_score(x, SR)[1] < CLICK_RATIO


def test_wide_vibrato_is_not_a_glide_but_a_slide_is():
    """The Glide spread is measured on the vibrato-smoothed pitch (a constructed pitch track, so pYIN's own
    voicing drop-outs on wide synthetic vibrato don't get in the way)."""
    from vocalchop.notes import assign_note, measure_range
    from vocalchop.pitch import PitchTrack

    hop = 256 / 22050
    t = np.arange(int(0.8 / hop)) * hop
    p = NoteParams()

    def flags(midi):
        tr = PitchTrack(midi=midi, voiced_prob=np.ones(len(midi)), rms=np.ones(len(midi)), hop_seconds=hop)
        n = measure_range(tr, np.ones(len(midi), bool), 0, len(midi), p)
        assign_note(n, 0.0, p)
        return n

    vib = flags(64 + 0.9 * np.sin(2 * np.pi * 5.0 * t))
    raw_spread = np.percentile(64 + 0.9 * np.sin(2 * np.pi * 5.0 * t), 90) - \
        np.percentile(64 + 0.9 * np.sin(2 * np.pi * 5.0 * t), 10)
    assert raw_spread > 1.5                                   # Milestone 1 would have called this a Glide
    assert "glide" not in vib.flags and vib.note == 64
    slide = flags(np.linspace(62, 67, len(t)))
    assert "glide" in slide.flags
