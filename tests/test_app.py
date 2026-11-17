"""The editor window (Milestone 2), run without a screen (QT_QPA_PLATFORM=offscreen)."""
import json

import numpy as np
import pytest

from synth import SR, SungNote, sing, write
from vocalchop.audition import nearest_c, shift_to, slice_for_key, tuner_on_clip
from vocalchop.presets import parse_note
from vocalchop.project import analyze_file, remember_preset

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402

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
    yield w, path
    if w.editor:
        w.editor.dirty = False
    w.close()


def analyzed(w, path):
    w.show_result(analyze_file(path, w.preset_key()))
    return w, path


def test_window_lists_files_and_restores_the_saved_range(window):
    w, path = window
    assert w.file_combo.count() == 1 and w.current_file() == path
    assert w.preset_key() == "custom:C3-C6"
    assert w.custom_lo.isVisibleTo(w)
    assert not w.chop_btn.isEnabled()          # nothing analyzed yet


def test_chop_fills_the_slice_list_and_view(window):
    w, path = analyzed(*window)
    assert w.table.rowCount() == 0 and "Tuning offset: +" in w.info.text()
    w.mode_combo.setCurrentIndex(w.mode_combo.findData("pitch"))
    w._chop()
    assert w.table.rowCount() == len(MELODY)
    assert [w.table.item(r, 3).text() for r in range(len(MELODY))] == \
           ["E4", "G#4", "B4", "C#5", "B4", "A4", "F#4", "E4"]
    assert w.table.item(0, 4).text() == "E3"                    # Live's name
    assert w.view.slices is w.editor.slices
    assert "8 of 32 slices" in w.count_status.text() and w.save_btn.isEnabled()
    assert w.selected_index() == 0 and "Slice 1" in w.tuner_panel.text()
    assert "same note" in w.tuner_panel.text()                 # built-in tuner agrees on slice 1


def test_region_equal_mode_and_too_many_slices(window):
    w, path = analyzed(*window)
    w._region_chosen(0.5, 2.5)
    assert "Region: 0:00.500" in w.region_label.text()
    w.mode_combo.setCurrentIndex(w.mode_combo.findData("equal"))
    assert w.count_combo.isVisibleTo(w)
    w.count_combo.setCurrentIndex(w.count_combo.findData(16))
    w._chop()
    assert w.table.rowCount() == 16
    w._whole_song()
    w.count_combo.setCurrentIndex(w.count_combo.findData(32))
    w._chop()
    w.editor.split(31, (w.editor.slices[31].start + w.editor.slices[31].end) / 2)   # 33 slices
    w._refresh()
    assert not w.save_btn.isEnabled() and "a map holds 32" in w.count_status.text()


def test_audition_original_and_shifted_to_c(window):
    w, path = analyzed(*window)
    w._chop()
    w.table.selectRow(2)                                       # B4
    x, sr, _ = w.clip("original")
    assert sr == SR and x.shape[1] == 2 and np.all(x[0] == 0) and np.all(x[-1] == 0)
    s = w.editor.slices[2]
    assert nearest_c(s) == parse_note("C5") and abs(shift_to(s, parse_note("C5")) - (1 - s.cents / 100)) < 1e-9
    xc, sr, (target, st) = w.clip("c")
    r = tuner_on_clip(xc, sr, 48, 84)
    got = r.midi - w.result.tuning_offset_cents / 100
    assert round(got) == target == parse_note("C5")
    w._play("c")                                               # no audio device in the tests: still updates the panel
    assert "it is a C" in w.tuner_panel.text() or not w.player.available


def test_edit_buttons_undo_and_keep_as_sung(window, monkeypatch):
    w, path = analyzed(*window)
    w._chop()
    w.table.selectRow(3)
    w._edit("merge_next")
    assert w.table.rowCount() == 7 and "glide" in w.table.item(3, 7).text()
    w._edit("split_suggested")
    assert w.table.rowCount() == 8 and w.table.item(4, 3).text() == "B4"
    w._undo()
    assert w.table.rowCount() == 7
    w._redo()
    w.table.selectRow(0)
    w._edit("octave", 1)
    assert w.table.item(0, 3).text() == "E5 (+1 oct)"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("F#4", True))
    w.table.selectRow(1)
    w._set_note()
    assert w.table.item(1, 3).text() == "F#4 (set)" and w.table.item(1, 5).text() == ""
    w.table.item(2, 8).setCheckState(Qt.Checked)               # the Keep as sung checkbox
    assert w.editor.slices[2].keep_as_sung
    w.table.selectRow(5)
    w._edit("delete")
    assert w.table.rowCount() == 7 and w.selected_index() == 5


def test_dragging_a_slice_line_and_splitting_by_click(window):
    w, path = analyzed(*window)
    w._chop()
    a = w.editor.slices[1].start
    w._edge_start(1, "start")
    w._edge_move(1, "start", a - 0.03)
    w._edge_move(1, "start", a - 0.05)
    w._edge_done()
    assert abs(w.editor.slices[1].start - (a - 0.05)) <= 0.006
    w._undo()                                                  # one drag = one undo step
    assert w.editor.slices[1].start == pytest.approx(a)
    s = w.editor.slices[0]
    w._split_at((s.start + s.end) / 2)
    assert w.table.rowCount() == 9


def test_tap_and_midi_keys(window):
    w, path = analyzed(*window)
    w.mode_combo.setCurrentIndex(w.mode_combo.findData("manual"))
    w._chop()
    assert w.table.rowCount() == 1
    note2 = w.editor.ctx.notes[1].start
    j = w.tap_at(note2 + 0.04)                                 # sloppy tap: snaps to the note start
    assert j == 1 and abs(w.editor.slices[1].start - note2) <= 0.006
    assert w.tap_at(w.editor.ctx.duration + 1) is None         # outside every slice: ignored
    assert slice_for_key(36, 2) == 0 and slice_for_key(37, 2) == 1 and slice_for_key(38, 2) is None
    w._midi_note(37, 100)                                      # Live's C#1 = slice 2
    assert w.selected_index() == 1


def test_save_and_reopen_restores_the_slices(window, vocalchop_home):
    w, path = analyzed(*window)
    w._region_chosen(0.2, 2.6)
    w._chop()
    w.table.selectRow(0)
    w._toggle_keep()
    assert w._save()
    folder = vocalchop_home / "take"
    m = json.loads((folder / "slicemap.json").read_text())
    assert len(m["slices"]) == w.table.rowCount() and m["slices"][0]["keepAsSung"] is True
    assert (folder / "editor.json").exists() and (folder / "render_queue.json").exists()
    assert "saved" in w.count_status.text()
    w2 = MainWindow()
    w2.show_result(analyze_file(path, w2.preset_key()))
    assert w2.table.rowCount() == w.table.rowCount() and "restored" in w2.info.text()
    assert w2.editor.region == pytest.approx(w.editor.region)
    w2.close()
