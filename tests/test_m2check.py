"""The Milestone 2 automatic check runs end to end and reports its three goals."""
from synth import SungNote, sing, write
from vocalchop.m2check import main
from vocalchop.presets import parse_note
from vocalchop.project import remember_preset

MELODY = [parse_note(n) for n in ["E4", "G#4", "B4", "C#5", "B4", "A4", "F#4", "E4"]]


def test_check_m2_writes_a_report_with_all_goals_met(vocalchop_home):
    path = vocalchop_home / "Acapellas" / "song.wav"
    notes = [SungNote(m, 0.4, gap_after=0.0 if i % 3 else 0.2) for i, m in enumerate(MELODY * 2)]
    write(path, sing(notes, vibrato_semitones=0.2), stereo=True)
    remember_preset(path, "female")
    assert main([]) == 0
    text = (vocalchop_home / "m2_check_results.txt").read_text()
    assert "1. cuts on note changes: MET" in text
    assert "2. clicks: MET" in text
    assert "3. flags: MET" in text
    assert "Files meeting all three goals: 1 of 1" in text
