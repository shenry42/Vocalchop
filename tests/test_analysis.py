"""Pitch analysis on synthetic voices with known notes (SPEC §3.3, Milestone 1)."""
import numpy as np
import pytest

from synth import SR, SungNote, sing, write
from vocalchop.notes import analyze_notes, is_sustained
from vocalchop.pitch import to_analysis_rate, track_pitch
from vocalchop.presets import PRESETS, parse_note
from vocalchop.project import analyze_file


def notes_of(y, preset="female", workers=1, sr=SR):
    p = PRESETS[preset]
    track = track_pitch(to_analysis_rate(y, sr), p.low, p.high, workers=workers)
    return analyze_notes(track)


MELODY = [parse_note(n) for n in ["E4", "G#4", "B4", "C#5", "B4", "A4", "F#4", "E4"]]


def test_female_melody_notes_and_offset():
    y = sing([SungNote(m, 0.5) for m in MELODY], detune_cents=14)
    a = notes_of(y)
    assert [n.note for n in a.notes] == MELODY
    assert abs(a.tuning_offset_cents - 14) <= 3
    assert all(abs(n.cents) <= 4 for n in a.notes)
    assert all(n.confidence >= 85 for n in a.notes)


@pytest.mark.parametrize("detune", [-30, 0, 45, -48])
def test_offset_is_found_for_any_detune(detune):
    y = sing([SungNote(m, 0.4) for m in MELODY], detune_cents=detune)
    a = notes_of(y)
    # an offset of ±48 can come out as ∓52 equivalent; compare on the circle
    diff = (a.tuning_offset_cents - detune + 50) % 100 - 50
    assert abs(diff) <= 3
    names = [n.note for n in a.notes]
    if abs(detune) < 45:
        assert names == MELODY
    else:
        # near ±50 the whole melody may be named a semitone off, but consistently
        shift = names[0] - MELODY[0]
        assert abs(shift) <= 1 and names == [m + shift for m in MELODY]


def test_male_low_notes_no_octave_errors():
    low = [parse_note(n) for n in ["C2", "E2", "G2", "C3", "A2", "F2", "D3", "G3"]]
    a = notes_of(sing([SungNote(m, 0.5) for m in low]), preset="male")
    assert [n.note for n in a.notes] == low


def test_vibrato_does_not_split_notes():
    notes = [SungNote(parse_note("A4"), 1.2), SungNote(parse_note("D5"), 1.2), SungNote(parse_note("F#4"), 1.2)]
    a = notes_of(sing(notes, vibrato_semitones=0.6))
    assert [n.note for n in a.notes] == [n.midi for n in notes]
    assert all(n.confidence >= 60 for n in a.notes)


def test_scoops_do_not_move_the_note():
    notes = [SungNote(m, 0.5, scoop=1.5) for m in MELODY]
    a = notes_of(sing(notes))
    assert [n.note for n in a.notes] == MELODY


def test_legato_note_changes_are_split():
    """Notes sung with no gap between them are still found as separate notes."""
    notes = [SungNote(m, 0.35, gap_after=0.0) for m in MELODY]
    a = notes_of(sing(notes))
    assert [n.note for n in a.notes] == MELODY


def test_single_note_cents_kept_after_offset():
    notes = [SungNote(m, 0.5) for m in MELODY]
    notes[3] = SungNote(MELODY[3] + 0.30, 0.5)   # one note sung 30 cents sharp
    a = notes_of(sing(notes))
    assert a.notes[3].note == MELODY[3]
    assert abs(a.notes[3].cents - 30) <= 4
    assert all(abs(n.cents) <= 5 for i, n in enumerate(a.notes) if i != 3)


def test_glide_is_flagged():
    notes = [SungNote(parse_note("A4"), 0.6), SungNote(parse_note("C5"), 0.8, glide_to=parse_note("F5")),
             SungNote(parse_note("E4"), 0.6)]
    a = notes_of(sing(notes))
    glides = [n for n in a.notes if "glide" in n.flags]
    assert len(glides) >= 1
    assert all(not is_sustained(n) for n in glides)
    assert a.notes[0].note == parse_note("A4") and "glide" not in a.notes[0].flags


def test_noise_lowers_confidence_but_keeps_notes():
    clean = notes_of(sing([SungNote(m, 0.5) for m in MELODY]))
    noisy = notes_of(sing([SungNote(m, 0.5) for m in MELODY], noise_db=-35, seed=3))
    assert [n.note for n in noisy.notes] == MELODY
    assert np.mean([n.confidence for n in noisy.notes]) <= np.mean([n.confidence for n in clean.notes])


def test_refined_pitch_is_within_two_cents():
    y = sing([SungNote(64.13, 1.0), SungNote(71.57, 1.0)])
    a = notes_of(y)
    assert abs(a.notes[0].pitch - 64.13) <= 0.02
    assert abs(a.notes[1].pitch - 71.57) <= 0.02


def test_chunked_parallel_matches_single_and_long_notes_survive_chunk_edges():
    # 45 s of audio so there are three 20 s chunks; one note (38 s to 42 s) is held across the 40 s edge
    notes = ([SungNote(m, 1.5, gap_after=1.0) for m in MELODY] * 2)[:15]
    notes.append(SungNote(parse_note("B4"), 4.0, gap_after=0.5))
    notes += [SungNote(m, 1.5, gap_after=1.0) for m in MELODY]
    y = sing(notes, lead_in=0.5)
    assert len(y) / SR > 45
    one = notes_of(y, workers=1)
    two = notes_of(y, workers=2)
    assert [n.note for n in one.notes] == [n.note for n in two.notes] == [round(n.midi) for n in notes]
    held = [n for n in one.notes if n.length > 3.5]
    assert len(held) == 1 and held[0].start < 40.0 < held[0].end


def test_silence_gives_no_notes():
    a = notes_of(np.zeros(SR * 3, dtype=np.float32) + 1e-5 * np.random.default_rng(0).standard_normal(SR * 3)
                 .astype(np.float32))
    assert a.notes == []


@pytest.mark.parametrize("ext,subtype", [("wav", "PCM_24"), ("aiff", "PCM_16"), ("flac", "PCM_16"),
                                         ("mp3", None)])
def test_every_format_loads_and_gives_the_same_notes(vocalchop_home, ext, subtype):
    path = vocalchop_home / "Acapellas" / f"take.{ext}"
    write(path, sing([SungNote(m, 0.45) for m in MELODY], detune_cents=-12), subtype=subtype, stereo=True)
    r = analyze_file(path, "female")
    assert [n.note for n in r.notes] == MELODY
    assert abs(r.tuning_offset_cents + 12) <= 4
    assert r.audio.channels == 2


def test_48k_source():
    y = sing([SungNote(m, 0.45) for m in MELODY], sr=48000)
    a = notes_of(y, sr=48000)
    assert [n.note for n in a.notes] == MELODY
