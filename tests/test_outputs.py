"""Slice map, exports, project folder, cache, check results and the command line."""
import json

import mido
import numpy as np
import pytest
import soundfile as sf

from synth import SungNote, sing, write
from vocalchop import checks
from vocalchop.cli import main as cli_main
from vocalchop.notes import Note
from vocalchop.presets import live_note_name, note_name, parse_note, preset_from_key
from vocalchop.project import analyze_file, export_slice_map, preset_for, remember_preset
from vocalchop.slicemap import MAX_SLICES, build_slice_map, read_slice_map, validate_slice_map

MELODY = [parse_note(n) for n in ["E4", "G#4", "B4", "C#5", "B4", "A4", "F#4", "E4"]]


@pytest.fixture
def take(vocalchop_home):
    path = vocalchop_home / "Acapellas" / "hook take.wav"
    write(path, sing([SungNote(m, 0.45) for m in MELODY], detune_cents=14), subtype="PCM_24", stereo=True)
    return path


def test_note_names():
    assert note_name(64) == "E4" and live_note_name(64) == "E3"
    assert parse_note("C#5") == 73 and parse_note("c2") == 36
    assert preset_from_key("custom:C2-F5").high == parse_note("F5")
    with pytest.raises(ValueError):
        preset_from_key("custom:G5-C2")


def test_project_folder_and_slice_map_match_the_spec(take, vocalchop_home):
    r = analyze_file(take, "female")
    folder = vocalchop_home / "hook take"
    assert r.folder == folder
    assert not (folder / "slicemap.json").exists()     # since Milestone 2 only the editor's Save writes it
    export_slice_map(r)
    for name in ("slicemap.json", "notes.json", "notes.mid", "guide.wav", "pitch_cache.npz", "analysis_log.txt"):
        assert (folder / name).exists(), name
    m = read_slice_map(folder / "slicemap.json")
    assert validate_slice_map(m) == []
    assert m["version"] == 1 and m["sampleRate"] == 44100 and m["renderRange"] == [-12, 12]
    assert abs(m["tuningOffsetCents"] - 14) <= 3
    assert m["source"].endswith("/hook take.wav") and "\\" not in m["source"]
    s = m["slices"][0]
    assert list(s) == ["index", "start", "end", "note", "cents", "confidence", "flags", "keepAsSung", "gain",
                       "renderFolder"]
    assert s["renderFolder"] == "renders/slice_01" and s["keepAsSung"] is False and s["gain"] == 0.0
    assert [x["note"] for x in m["slices"]] == MELODY


def test_slice_map_is_capped_at_32_and_can_start_later(take):
    r = analyze_file(take, "female")
    many = [Note(index=i, start=i * 0.5, end=i * 0.5 + 0.4, pitch=64.0, note=64) for i in range(1, 41)]
    m = build_slice_map(take, 44100, 0.0, many)
    assert len(m["slices"]) == MAX_SLICES and validate_slice_map(m) == []
    path = export_slice_map(r, first_index=5)
    m2 = read_slice_map(path)
    assert [x["note"] for x in m2["slices"]] == MELODY[4:]
    assert m2["slices"][0]["index"] == 1 and m2["slices"][0]["renderFolder"] == "renders/slice_01"


def test_validator_catches_problems():
    m = build_slice_map("x.wav", 48000, 3.0, [Note(index=1, start=0.1, end=0.5, pitch=60, note=60)])
    m["slices"][0]["cents"] = 70
    m["slices"].append(dict(m["slices"][0], index=2, start=0.2, end=0.3, cents=0))
    errs = validate_slice_map(m)
    assert any("cents" in e for e in errs) and any("overlaps" in e for e in errs)


def test_midi_and_guide_tone_line_up_with_the_notes(take, vocalchop_home):
    r = analyze_file(take, "female")
    mid = mido.MidiFile(vocalchop_home / "hook take" / "notes.mid")
    ons = []
    t = 0.0
    for msg in mid:              # iterating a MidiFile gives times in seconds
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            ons.append((round(t, 2), msg.note))
    assert [n for _, n in ons] == MELODY
    assert all(abs(t - n.start) < 0.02 for (t, _), n in zip(ons, r.notes))

    g, sr = sf.read(vocalchop_home / "hook take" / "guide.wav")
    assert sr == 44100 and abs(len(g) / sr - r.audio.duration) < 0.01
    # the guide tone's pitch inside the first note is the note + the global offset
    n = r.notes[0]
    seg = g[int((n.start + 0.05) * sr):int((n.end - 0.05) * sr)]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 8 * len(seg)))
    f = np.argmax(spec) * sr / (8 * len(seg))
    expect = 440 * 2 ** ((n.note + r.tuning_offset_cents / 100 - 69) / 12)
    assert abs(1200 * np.log2(f / expect)) < 5


def test_notes_json_has_names_and_sustained(take, vocalchop_home):
    analyze_file(take, "female")
    d = json.loads((vocalchop_home / "hook take" / "notes.json").read_text())
    assert d["rangePreset"] == "female" and len(d["notes"]) == len(MELODY)
    first = d["notes"][0]
    assert first["name"] == "E4" and first["liveName"] == "E3" and first["sustained"] is True


def test_pitch_cache_is_used_and_invalidated(take):
    a = analyze_file(take, "female")
    b = analyze_file(take, "female")
    assert not a.from_cache and b.from_cache
    assert [n.note for n in a.notes] == [n.note for n in b.notes]
    c = analyze_file(take, "instrument")
    assert not c.from_cache


def test_preset_is_remembered_per_file(take):
    assert preset_for(take) is None
    remember_preset(take, "custom:C2-F5")
    assert preset_for(take) == "custom:C2-F5"


def test_check_marks_and_summary(take, vocalchop_home):
    r = analyze_file(take, "female")
    notes = [dict(index=n.index, start=n.start, end=n.end, note=n.note, cents=n.cents, confidence=n.confidence)
             for n in r.notes]
    for nd in notes[:7]:
        checks.set_mark(r.folder, take, nd, "pass")
    checks.set_mark(r.folder, take, notes[7], "fail", "octave too high")
    text = (vocalchop_home / "check_results.txt").read_text()
    assert "Sam's marks: 8 checked, 7 pass, 1 fail" in text and "(octave too high)" in text
    checks.set_mark(r.folder, take, notes[7], None)
    assert checks.counts(checks.load_checks(r.folder)) == (7, 0)


def test_tuner_results_in_summary_and_goal(vocalchop_home):
    # 12 sustained notes, so the check set is 10 notes
    path = vocalchop_home / "Acapellas" / "long take.wav"
    mel = MELODY + MELODY[:4]
    write(path, sing([SungNote(m, 0.45) for m in mel], detune_cents=-8), stereo=True)
    r = analyze_file(path, "female")
    text = (vocalchop_home / "check_results.txt").read_text()
    assert "== long take.wav" in text
    assert "built-in tuner, 10 check notes: 10 match, 0 borderline, 0 different  -> MEETS GOAL (10/10)" in text
    assert "all 12 sustained notes: 12 match" in text
    assert "Files meeting the goal (built-in tuner): 1 - long take.wav" in text
    d = json.loads((r.folder / "notes.json").read_text())
    assert d["builtInTuner"]["checkSet"]["of"] == 10
    assert sum(1 for n in d["notes"] if n.get("tunerCheck")) == 10
    assert d["notes"][0]["tuner"]["result"] == "match"


def test_tuner_check_file(vocalchop_home):
    from vocalchop.exports import TUNER_HOLD, pick_check_notes, tuner_slot_start
    from vocalchop.presets import live_note_name

    path = vocalchop_home / "Acapellas" / "long take.wav"
    write(path, sing([SungNote(m, 0.4) for m in MELODY * 3]), stereo=True)
    r = analyze_file(path, "female")
    picks = pick_check_notes(r.notes)
    assert len(picks) == 10 and picks[-1].start > 0.7 * r.audio.duration      # spread across the song
    y, sr = sf.read(r.folder / "tuner_check.wav")
    assert abs(len(y) / sr - tuner_slot_start(11)) < 0.01
    rows = [l for l in (r.folder / "tuner_check.txt").read_text().splitlines() if l[:3].strip().isdigit()]
    assert len(rows) == 10
    for k, n in enumerate(picks[:3], start=1):
        a = int((tuner_slot_start(k) + 0.1) * sr)
        seg = y[a:a + int((TUNER_HOLD - 0.2) * sr)]
        assert np.sqrt(np.mean(seg ** 2)) > 0.05                                # held for the full 2 s
        assert live_note_name(n.note) in rows[k - 1]


def test_cli_analyzes_the_acapellas_folder(take, vocalchop_home, capsys):
    remember_preset(take, "female")
    (vocalchop_home / "Acapellas" / "notes.txt").write_text("not audio")
    assert cli_main([]) == 0
    s = (vocalchop_home / "analysis_summary.txt").read_text()
    assert "hook take.wav" in s and "Female vocal" in s and "notes: 8" in s


def test_cli_reports_a_broken_file_and_carries_on(take, vocalchop_home):
    (vocalchop_home / "Acapellas" / "broken.wav").write_bytes(b"RIFF nonsense")
    assert cli_main(["--preset", "female"]) == 1
    s = (vocalchop_home / "analysis_summary.txt").read_text()
    assert "broken.wav" in s and "FAILED" in s and "notes: 8" in s
