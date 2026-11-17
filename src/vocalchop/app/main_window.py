"""Milestone 1 window: analyze a file, look at the notes, listen, and mark each note Pass or Fail."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import __version__, checks
from ..exports import note_tone_hz, pick_check_notes, tone
from ..notes import is_sustained
from ..paths import acapellas_dir, home_dir, is_audio_file
from ..presets import PRESETS, live_note_name, note_name, preset_from_key
from ..project import AnalysisResult, analyze_file, export_slice_map, preset_for, remember_preset
from ..tuner import compare, read_tuner
from .pitch_view import FAIL, LOWCONF, PASS, PitchView
from .player import Player

COLUMNS = ["#", "Start", "Length", "Note", "Live name", "Cents", "Conf.", "Flags", "Tuner", "Match", "Check"]
CHECK_COL = 10
FILTERS = [("check", "The 10 check notes"), ("sustained", "All sustained notes"), ("all", "All notes")]
MATCH_TEXT = {"match": "match", "borderline": "borderline", "different": "DIFFERENT", "no reading": "no reading"}
PRESET_ORDER = ["male", "female", "instrument", "custom"]


class AnalysisWorker(QObject):
    progress = Signal(str, float)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, preset_key: str):
        super().__init__()
        self.path, self.preset_key = path, preset_key

    def run(self):
        try:
            r = analyze_file(self.path, self.preset_key, progress=lambda m, f: self.progress.emit(m, f))
            self.finished.emit(r)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Vocal Chop Sampler {__version__} - Pitch analysis")
        self.resize(1280, 820)
        self.result: AnalysisResult | None = None
        self.marks: dict[int, str] = {}
        self.player = Player()
        self.thread: QThread | None = None
        self._rows: list[int] = []    # table row -> note index

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- top bar: file, range, analyze
        top = QHBoxLayout()
        top.addWidget(QLabel("File:"))
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(420)
        self.file_combo.currentIndexChanged.connect(self._file_changed)
        top.addWidget(self.file_combo, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        top.addWidget(browse)
        top.addSpacing(16)
        top.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        for k in PRESET_ORDER[:3]:
            self.range_combo.addItem(PRESETS[k].label, k)
        self.range_combo.addItem("Custom", "custom")
        self.range_combo.currentIndexChanged.connect(self._range_changed)
        top.addWidget(self.range_combo)
        self.custom_lo = QLineEdit("C2")
        self.custom_hi = QLineEdit("F5")
        for w in (self.custom_lo, self.custom_hi):
            w.setFixedWidth(46)
            w.setToolTip("Note name, e.g. C2, E3, F#5 (C4 = middle C)")
        self.custom_to = QLabel("to")
        top.addWidget(self.custom_lo)
        top.addWidget(self.custom_to)
        top.addWidget(self.custom_hi)
        self.analyze_btn = QPushButton("Analyze")
        self.analyze_btn.setDefault(True)
        self.analyze_btn.clicked.connect(self._analyze)
        top.addWidget(self.analyze_btn)
        root.addLayout(top)

        status = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setVisible(False)
        status.addWidget(self.progress)
        self.info = QLabel("")
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        status.addWidget(self.info, 1)
        root.addLayout(status)
        self.warning = QLabel("")
        self.warning.setStyleSheet(f"color: {LOWCONF.name()};")
        self.warning.setWordWrap(True)
        self.warning.setVisible(False)
        root.addWidget(self.warning)

        # --- view + table
        split = QSplitter(Qt.Vertical)
        self.view = PitchView()
        self.view.noteClicked.connect(self._select_note)
        split.addWidget(self.view)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.tuner_panel = QLabel("Built-in tuner: select a note")
        self.tuner_panel.setTextFormat(Qt.RichText)
        self.tuner_panel.setStyleSheet("font-size: 17px; padding: 6px 4px;")
        bl.addWidget(self.tuner_panel)
        filt = QHBoxLayout()
        filt.addWidget(QLabel("Show:"))
        self.filter = QComboBox()
        for key, label in FILTERS:
            self.filter.addItem(label, key)
        self.filter.setToolTip("The 10 check notes are sustained notes (250 ms+, confidence 60+, no glide) picked "
                               "evenly across the song; they are also in tuner_check.wav")
        self.filter.currentIndexChanged.connect(self._fill_table)
        filt.addWidget(self.filter)
        self.autoplay = QCheckBox("Play voice + tone when a note is selected")
        self.autoplay.setChecked(False)
        filt.addWidget(self.autoplay)
        filt.addStretch(1)
        self.tally = QLabel("")
        filt.addWidget(self.tally)
        bl.addLayout(filt)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._table_selection)
        bl.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.b_voice = QPushButton("Voice  [V]")
        self.b_tone = QPushButton("Tone  [T]")
        self.b_both = QPushButton("Voice + tone  [Space]")
        self.b_stop = QPushButton("Stop  [S]")
        self.b_pass = QPushButton("Pass  [P]")
        self.b_fail = QPushButton("Fail  [F]")
        self.reason = QComboBox()
        self.reason.addItem("(why it failed - optional)", "")
        for r in checks.FAIL_REASONS[1:]:
            self.reason.addItem(r, r)
        self.b_clear = QPushButton("Clear mark")
        self.b_export = QPushButton("Export slice map from this note")
        self.b_folder = QPushButton("Open project folder")
        self.b_pass.setStyleSheet(f"color: {PASS.name()}; font-weight: bold;")
        self.b_fail.setStyleSheet(f"color: {FAIL.name()}; font-weight: bold;")
        for b in (self.b_voice, self.b_tone, self.b_both, self.b_stop):
            btns.addWidget(b)
        btns.addSpacing(20)
        for b in (self.b_pass, self.b_fail, self.reason, self.b_clear):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.b_export)
        btns.addWidget(self.b_folder)
        bl.addLayout(btns)
        split.addWidget(bottom)
        split.setSizes([380, 420])
        root.addWidget(split, 1)

        self.b_voice.clicked.connect(lambda: self._play("voice"))
        self.b_tone.clicked.connect(lambda: self._play("tone"))
        self.b_both.clicked.connect(lambda: self._play("both"))
        self.b_stop.clicked.connect(self.player.stop)
        self.b_pass.clicked.connect(lambda: self._mark("pass"))
        self.b_fail.clicked.connect(lambda: self._mark("fail"))
        self.b_clear.clicked.connect(lambda: self._mark(None))
        self.b_export.clicked.connect(self._export)
        self.b_folder.clicked.connect(self._open_folder)
        for key, fn in (("V", lambda: self._play("voice")), ("T", lambda: self._play("tone")),
                        ("Space", lambda: self._play("both")), ("S", self.player.stop),
                        ("P", lambda: self._mark("pass")), ("F", lambda: self._mark("fail"))):
            # only while the note view, the table or its buttons have focus, so typing "F#5" in the range box
            # doesn't mark a note as failed
            sc = QShortcut(QKeySequence(key), split)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

        self._set_note_buttons(False)
        self._range_changed()
        self._load_file_list()
        if not self.player.available:
            self.statusBar().showMessage(self.player.error or "Audio playback is not available")

    # ------------------------------------------------------------ files and range
    def _load_file_list(self, select: Path | None = None):
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        folder = acapellas_dir()
        files = sorted(f for f in folder.iterdir() if f.is_file() and is_audio_file(f)) if folder.exists() else []
        for f in files:
            self.file_combo.addItem(f.name, str(f))
        if select is not None and self.file_combo.findData(str(select)) < 0:
            self.file_combo.addItem(select.name, str(select))
        if select is not None:
            self.file_combo.setCurrentIndex(self.file_combo.findData(str(select)))
        self.file_combo.blockSignals(False)
        self._file_changed()

    def _browse(self):
        start = str(acapellas_dir() if acapellas_dir().exists() else home_dir())
        path, _ = QFileDialog.getOpenFileName(self, "Open an acapella or stem", start,
                                              "Audio (*.wav *.aif *.aiff *.flac *.mp3)")
        if path:
            self._load_file_list(select=Path(path))

    def current_file(self) -> Path | None:
        d = self.file_combo.currentData()
        return Path(d) if d else None

    def _file_changed(self):
        f = self.current_file()
        if f is None:
            return
        key = preset_for(f)
        if key:
            self.set_preset_key(key)

    def set_preset_key(self, key: str):
        if key.startswith("custom:"):
            lo, hi = key[len("custom:"):].split("-")
            self.custom_lo.setText(lo)
            self.custom_hi.setText(hi)
            key = "custom"
        i = self.range_combo.findData(key)
        if i >= 0:
            self.range_combo.setCurrentIndex(i)

    def preset_key(self) -> str:
        k = self.range_combo.currentData()
        if k == "custom":
            return f"custom:{self.custom_lo.text().strip().upper()}-{self.custom_hi.text().strip().upper()}"
        return k

    def _range_changed(self):
        custom = self.range_combo.currentData() == "custom"
        for w in (self.custom_lo, self.custom_to, self.custom_hi):
            w.setVisible(custom)

    # ------------------------------------------------------------ analysis
    def _analyze(self):
        f = self.current_file()
        if f is None:
            QMessageBox.information(self, "No file", f"Put acapellas in {acapellas_dir()} or use Browse.")
            return
        key = self.preset_key()
        try:
            preset_from_key(key)
        except Exception as exc:
            QMessageBox.warning(self, "Range", f"The custom range isn't valid: {exc}")
            return
        remember_preset(f, key)
        self.analyze_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.info.setText(f"Analyzing {f.name}...")
        self.thread = QThread(self)
        self.worker = AnalysisWorker(f, key)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._analysis_done)
        self.worker.failed.connect(self._analysis_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.start()

    def _progress(self, msg: str, frac: float):
        self.progress.setValue(int(frac * 100))
        self.info.setText(msg + "...")

    def _analysis_failed(self, msg: str):
        self.analyze_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.info.setText("Analysis failed.")
        QMessageBox.warning(self, "Analysis failed", msg)

    def _analysis_done(self, r: AnalysisResult):
        self.analyze_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.show_result(r)

    def show_result(self, r: AnalysisResult):
        self.result = r
        sus = sum(is_sustained(n) for n in r.notes)
        self.info.setText(
            f"<b>Tuning offset: {r.tuning_offset_cents:+.1f} cents</b> &nbsp;·&nbsp; {len(r.notes)} notes, "
            f"{sus} sustained &nbsp;·&nbsp; {r.preset.label} &nbsp;·&nbsp; "
            f"{r.seconds:.1f} s{' (pitch from cache)' if r.from_cache else ''}"
            + self._tuner_tally(r))
        self.warning.setText("  ".join(r.warnings))
        self.warning.setVisible(bool(r.warnings))
        self.view.set_data(r.audio.mono(), r.audio.sample_rate, r.track, r.analysis)
        self._load_marks()
        self._fill_table()

    def _tuner_tally(self, r) -> str:
        picks = [n.index for n in pick_check_notes(r.notes)]
        st = [r.tuner[i][1] for i in picks if i in r.tuner]
        if not st:
            return ""
        ok = st.count("match") + st.count("borderline")
        goal = ok >= 0.9 * len(st) and len(st) >= checks.GOAL_NOTES
        col = PASS if goal else FAIL
        return (f" &nbsp;·&nbsp; <b style='color:{col.name()}'>Built-in tuner agrees on {ok} of {len(st)} check notes"
                f"{' (goal met)' if goal else ''}</b>")

    # ------------------------------------------------------------ table
    def _load_marks(self):
        self.marks = {}
        if not self.result:
            return
        data = checks.load_checks(self.result.folder).get("marks", {})
        for n in self.result.notes:
            m = data.get(f"{n.start:.3f}")
            if m:
                self.marks[n.index] = m["result"]
        self.view.set_marks(self.marks)
        self._update_tally()

    def _fill_table(self):
        self.table.setRowCount(0)
        self._rows = []
        if not self.result:
            return
        mode = self.filter.currentData()
        if mode == "check":
            notes = pick_check_notes(self.result.notes)
        elif mode == "sustained":
            notes = [n for n in self.result.notes if is_sustained(n)]
        else:
            notes = list(self.result.notes)
        self.table.setRowCount(len(notes))
        for row, n in enumerate(notes):
            self._rows.append(n.index)
            vals = [str(n.index), f"{int(n.start // 60)}:{n.start % 60:06.3f}", f"{n.length * 1000:.0f} ms",
                    note_name(n.note), live_note_name(n.note), f"{n.cents:+d}", str(n.confidence),
                    ", ".join(f.replace("_", " ") for f in n.flags), *self._tuner_cells(n), ""]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if col in (0, 5, 6):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, it)
            if n.flags:
                self.table.item(row, 7).setForeground(LOWCONF)
            status = self.result.tuner.get(n.index, (None, ""))[1]
            if status:
                self.table.item(row, 9).setForeground(QColor(PASS if status == "match" else LOWCONF
                                                             if status == "borderline" else FAIL))
            self._paint_mark(row, n.index)
        self._set_note_buttons(False)

    def _tuner_cells(self, n) -> list[str]:
        t = self.result.tuner.get(n.index)
        if not t:
            return ["", ""]
        r, status, _apart = t
        return [f"{note_name(r.note)} {r.cents:+d}" if r.ok else "-", MATCH_TEXT.get(status, status)]

    def _tuner_for(self, n):
        """(reading, status, cents apart) for any note; sustained notes come precomputed."""
        if n.index in self.result.tuner:
            return self.result.tuner[n.index]
        r = read_tuner(self.result.audio.mono(), self.result.audio.sample_rate, n, self.result.preset.low,
                       self.result.preset.high)
        return (r, *compare(r, n, self.result.tuning_offset_cents))

    def _show_tuner(self, n):
        r, status, apart = self._tuner_for(n)
        if not r.ok:
            self.tuner_panel.setText("Built-in tuner: no clear pitch in this note")
            return
        col = PASS if status == "match" else LOWCONF if status == "borderline" else FAIL
        verdict = {"match": "same note", "borderline": "borderline: sung near halfway between the two notes",
                   "different": "DIFFERENT note"}.get(status, status)
        self.tuner_panel.setText(
            f"Built-in tuner: <b>{note_name(r.note)}</b> {r.cents:+d} c (Live {live_note_name(r.note)}) "
            f"&nbsp;&nbsp;|&nbsp;&nbsp; App: <b>{note_name(n.note)}</b> {n.cents:+d} c (Live {live_note_name(n.note)})"
            f" &nbsp;&nbsp;→&nbsp;&nbsp; <b style='color:{col.name()}'>{verdict}</b>"
            f" <span style='color:#888'>({apart:.0f} c apart; tuner cents are before the song's "
            f"{self.result.tuning_offset_cents:+.0f} c offset)</span>")

    def _paint_mark(self, row: int, index: int):
        m = self.marks.get(index)
        it = self.table.item(row, CHECK_COL)
        it.setText({"pass": "PASS", "fail": "FAIL"}.get(m, ""))
        it.setForeground(QColor(PASS if m == "pass" else FAIL if m == "fail" else "#999"))

    def _update_tally(self):
        p = sum(v == "pass" for v in self.marks.values())
        f = sum(v == "fail" for v in self.marks.values())
        goal = "goal met" if p + f >= checks.GOAL_NOTES and p >= 0.9 * (p + f) else \
            f"goal: {checks.GOAL_PASSES} of {checks.GOAL_NOTES} pass"
        self.tally.setText(f"This file: {p + f} checked, <b style='color:{PASS.name()}'>{p} pass</b>, "
                           f"<b style='color:{FAIL.name()}'>{f} fail</b> ({goal})")

    def selected_note(self):
        if not self.result:
            return None
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.result.notes[self._rows[rows[0].row()] - 1]

    def _table_selection(self):
        n = self.selected_note()
        self._set_note_buttons(n is not None)
        if n is None:
            return
        self.view.select(n.index)
        self._show_tuner(n)
        if self.autoplay.isChecked():
            self._play("both")

    def _select_note(self, index: int):
        if index not in self._rows:
            self.filter.setCurrentIndex(len(FILTERS) - 1)
        self.table.selectRow(self._rows.index(index))

    def _set_note_buttons(self, on: bool):
        for b in (self.b_voice, self.b_tone, self.b_both, self.b_pass, self.b_fail, self.b_clear, self.b_export):
            b.setEnabled(on)

    # ------------------------------------------------------------ listening
    def clip(self, kind: str) -> tuple[np.ndarray, int] | None:
        n = self.selected_note()
        if n is None or not self.result:
            return None
        a = self.result.audio
        sr = a.sample_rate
        s = max(0, int((n.start - 0.03) * sr))
        e = min(len(a.data), int((n.end + 0.03) * sr))
        voice = a.data[s:e]
        t = tone(note_tone_hz(n, self.result.tuning_offset_cents), (e - s) / sr, sr)
        t = t[:len(voice)]
        if kind == "voice":
            out = voice
        elif kind == "tone":
            out = np.repeat(t[:, None], max(1, a.channels), axis=1)
        else:
            vpk = float(np.abs(voice).max()) or 1.0
            out = voice * (0.5 / vpk) + 0.8 * t[:, None]
        return out.astype(np.float32), sr

    def _play(self, kind: str):
        c = self.clip(kind)
        if c is None:
            return
        if not self.player.available:
            self.statusBar().showMessage(self.player.error or "Audio playback is not available", 6000)
            return
        try:
            self.player.play(*c)
        except Exception as exc:
            self.statusBar().showMessage(f"Playback failed: {exc}", 8000)

    # ------------------------------------------------------------ marking
    def _mark(self, result: str | None):
        n = self.selected_note()
        if n is None or not self.result:
            return
        reason = self.reason.currentData() if result == "fail" else ""
        checks.set_mark(self.result.folder, self.result.source,
                        dict(index=n.index, start=n.start, end=n.end, note=n.note, cents=n.cents,
                             confidence=n.confidence), result, reason)
        if result is None:
            self.marks.pop(n.index, None)
        else:
            self.marks[n.index] = result
        self.reason.setCurrentIndex(0)
        row = self._rows.index(n.index)
        self._paint_mark(row, n.index)
        self.view.set_marks(self.marks)
        self._update_tally()
        if result is not None and row + 1 < len(self._rows):
            self.table.selectRow(row + 1)        # straight on to the next note

    # ------------------------------------------------------------ files
    def _export(self):
        n = self.selected_note()
        if n is None or not self.result:
            return
        path = export_slice_map(self.result, first_index=n.index)
        count = min(32, len(self.result.notes) - n.index + 1)
        self.statusBar().showMessage(f"Slice map with {count} slices (notes {n.index} to {n.index + count - 1}) "
                                     f"written to {path}", 10000)

    def _open_folder(self):
        folder = self.result.folder if self.result else home_dir()
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # noqa: S606 - opens Explorer
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)], check=False)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, ev):
        self.player.stop()
        if self.thread and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(3000)
        super().closeEvent(ev)


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    return app.exec()
