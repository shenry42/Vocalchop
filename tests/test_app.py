"""The Milestone 1 window, run without a screen (QT_QPA_PLATFORM=offscreen)."""
import json

import numpy as np
import pytest

from synth import SungNote, sing, write
from vocalchop.presets import parse_note
from vocalchop.project import analyze_file, remember_preset

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from vocalchop.app.main_window import MainWindow  # noqa: E402

MELODY = [parse_note(n) for n in ["E4", "G#4", "B4", "C#5", "B4", "A4", "F#4", "E4"]]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, vocalchop_home):
    path = vocalchop_home / "Acapellas" / "take.wav"
    write(path, sing([SungNote(m, 0.45) for m in MELODY], detune_cents=10), stereo=True)
    remember_preset(path, "custom:C3-C6")
    w = MainWindow()
    w.autoplay.setChecked(False)
    yield w, path
    w.close()


def test_window_lists_files_and_restores_the_saved_range(window):
    w, path = window
    assert w.file_combo.count() == 1 and w.current_file() == path
    assert w.preset_key() == "custom:C3-C6"
    assert w.custom_lo.isVisibleTo(w)


def test_analysis_fills_table_view_and_marks(window, vocalchop_home):
    w, path = window
    w.show_result(analyze_file(path, w.preset_key()))
    w.filter.setCurrentIndex(1)
    assert w.table.rowCount() == len(MELODY)
    assert [w.table.item(r, 3).text() for r in range(len(MELODY))] == ["E4", "G#4", "B4", "C#5", "B4", "A4", "F#4",
                                                                       "E4"]
    assert w.table.item(0, 4).text() == "E3"        # Live's name
    assert "Tuning offset: +" in w.info.text()
    w.table.selectRow(0)
    assert w.selected_note().index == 1 and w.view.selected == 1
    voice, sr = w.clip("voice")
    tone_, _ = w.clip("tone")
    both, _ = w.clip("both")
    assert sr == 44100 and voice.shape == tone_.shape == both.shape and voice.shape[1] == 2
    # pass moves on to the next note; fail with a reason
    w._mark("pass")
    assert w.selected_note().index == 2
    w.reason.setCurrentIndex(1)
    w._mark("fail")
    data = json.loads((vocalchop_home / "take" / "checks.json").read_text())
    results = sorted((m["index"], m["result"], m["reason"]) for m in data["marks"].values())
    assert results == [(1, "pass", ""), (2, "fail", "octave too high")]
    assert "2 checked" in w.tally.text()
    assert (vocalchop_home / "check_results.txt").exists()
    # marks come back after re-analysis
    w.show_result(analyze_file(path, w.preset_key()))
    w.filter.setCurrentIndex(1)
    assert w.marks == {1: "pass", 2: "fail"} and w.table.item(0, 10).text() == "PASS"


def test_export_slice_map_from_selected_note(window, vocalchop_home):
    w, path = window
    w.show_result(analyze_file(path, w.preset_key()))
    w.filter.setCurrentIndex(1)
    w.table.selectRow(3)
    w._export()
    m = json.loads((vocalchop_home / "take" / "slicemap.json").read_text())
    assert [s["note"] for s in m["slices"]] == MELODY[3:]


def test_clicking_a_hidden_note_shows_all_notes(window):
    w, path = window
    w.show_result(analyze_file(path, w.preset_key()))
    w._rows = w._rows[:-1]            # pretend the last note is filtered out
    w._select_note(len(MELODY))
    assert w.filter.currentData() == "all" and w.selected_note().index == len(MELODY)


def test_view_paints_without_errors(window, app):
    w, path = window
    w.show_result(analyze_file(path, w.preset_key()))
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    img = w.view.grab()
    assert img.width() > 100
    img.save(str(path.parent.parent / "view.png"))


def test_built_in_tuner_column_and_panel(window):
    w, path = window
    w.show_result(analyze_file(path, w.preset_key()))
    assert w.filter.currentData() == "check" and w.table.rowCount() == len(MELODY)   # fewer than 10 sustained: all
    assert [w.table.item(r, 9).text() for r in range(w.table.rowCount())] == ["match"] * len(MELODY)
    assert w.table.item(0, 8).text().startswith("E4")
    w.table.selectRow(0)
    assert "same note" in w.tuner_panel.text() and "Built-in tuner agrees on" in w.info.text()
