"""The editor window (SPEC §3.4, Milestone 2): analyze a song, chop a region, fix slices, audition, save the map."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import __version__, render, session
from ..audition import natural_clip, original_clip, shifted_clip, slice_for_key, tuner_on_clip, tuner_on_slice
from ..background import BackgroundRenderer
from ..chop import (CHOP_MODES, EQUAL_COUNTS, MAX_SLICES, MODE_LABELS, ChopSettings, EditError, SliceEditor,
                    SongContext, render_hash)
from ..paths import acapellas_dir, home_dir, is_audio_file
from ..presets import PRESETS, live_note_name, note_name, parse_note, preset_from_key
from ..project import AnalysisResult, analyze_file, preset_for, remember_preset
from ..tuner import compare
from .editor_view import GLIDE, LOWCONF, EditorView
from .midi_in import MidiInput, backend_error, list_ports
from .player import Player

COLUMNS = ["#", "Start", "Length", "Note", "Live name", "Cents", "Conf.", "Flags", "Keep as sung", "Natural"]
KEEP_COL = 8
NATURAL_COL = 9
PRESET_ORDER = ["male", "female", "instrument", "custom"]
OK = QColor("#4cc38a")
BAD = QColor("#ff5c5c")
TAP_SNAP = 0.060        # a tap within 60 ms of a detected note start lands exactly on it


class RenderBridge(QObject):
    """Carries the background renderer's events (worker thread) to the window (GUI thread)."""
    event = Signal(object)


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


def _fmt_time(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:06.3f}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Vocal Chop Sampler {__version__} - Editor")
        self.resize(1360, 880)
        self.result: AnalysisResult | None = None
        self.editor: SliceEditor | None = None
        self.player = Player()
        self.midi = MidiInput(self)
        self.thread: QThread | None = None
        self.tapping = False
        self._tap_region = (0.0, 0.0)
        self._filling = False
        self._play_offset: float | None = None   # source time where the playing clip starts (for the playhead)
        self._play_rate = 1.0
        self._ready: dict[str, Path] = {}       # renderHash -> finished render folder (this song)
        self._saved_hashes: set[str] = set()    # hashes in the saved slice map
        self._render_now: tuple[str, int] | None = None   # (hash, files done) of the slice rendering now
        self._render_bridge = RenderBridge()
        self._render_bridge.event.connect(self._render_event)
        self.renderer = BackgroundRenderer(on_event=self._render_bridge.event.emit)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- file, range, analyze
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
        self.render_label = QLabel("")
        self.render_label.setToolTip("Natural render (Rubber Band, formants kept): every slice at -12..+12 "
                                     "semitones, in renders\\slice_XX. Starts by itself when you save.")
        status.addWidget(self.render_label)
        root.addLayout(status)
        self.warning = QLabel("")
        self.warning.setStyleSheet(f"color: {LOWCONF.name()};")
        self.warning.setWordWrap(True)
        self.warning.setVisible(False)
        root.addWidget(self.warning)

        # --- chop bar
        chopbar = QHBoxLayout()
        chopbar.addWidget(QLabel("Chop mode:"))
        self.mode_combo = QComboBox()
        for m in CHOP_MODES:
            self.mode_combo.addItem(MODE_LABELS[m], m)
        self.mode_combo.setToolTip("Pitch: cut where the note changes or the voice stops\n"
                                   "Silence gate: cut at gaps quieter than -45 dBFS for 60 ms (words, phrases)\n"
                                   "Equal: N equal slices\nManual: click (or tap) to place slice points")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        chopbar.addWidget(self.mode_combo)
        self.count_label = QLabel("Slices:")
        self.count_combo = QComboBox()
        for n in EQUAL_COUNTS:
            self.count_combo.addItem(str(n), n)
        self.count_combo.setCurrentIndex(1)
        chopbar.addWidget(self.count_label)
        chopbar.addWidget(self.count_combo)
        self.chop_btn = QPushButton("Chop region")
        self.chop_btn.setToolTip("Chop the region with the chosen mode (replaces the slices). Shortcut: Ctrl+K")
        self.chop_btn.clicked.connect(self._chop)
        chopbar.addWidget(self.chop_btn)
        chopbar.addSpacing(14)
        self.region_label = QLabel("Region: whole song")
        self.region_label.setToolTip("Drag along the strip at the top of the view (or Shift + drag) to set the region")
        chopbar.addWidget(self.region_label)
        self.whole_btn = QPushButton("Whole song")
        self.whole_btn.clicked.connect(self._whole_song)
        chopbar.addWidget(self.whole_btn)
        chopbar.addSpacing(14)
        self.undo_btn = QPushButton("Undo")
        self.redo_btn = QPushButton("Redo")
        self.undo_btn.clicked.connect(self._undo)
        self.redo_btn.clicked.connect(self._redo)
        chopbar.addWidget(self.undo_btn)
        chopbar.addWidget(self.redo_btn)
        chopbar.addStretch(1)
        self.count_status = QLabel("")
        chopbar.addWidget(self.count_status)
        self.save_btn = QPushButton("Save slice map  [Ctrl+S]")
        self.save_btn.setStyleSheet("font-weight: bold;")
        self.save_btn.clicked.connect(self._save)
        chopbar.addWidget(self.save_btn)
        root.addLayout(chopbar)

        # --- view + slice panel
        split = QSplitter(Qt.Vertical)
        self.view = EditorView()
        self.view.sliceClicked.connect(self._select_slice)
        self.view.regionChosen.connect(self._region_chosen)
        self.view.splitAt.connect(self._split_at)
        self.view.edgeDragStarted.connect(self._edge_start)
        self.view.edgeDragged.connect(self._edge_move)
        self.view.edgeDragFinished.connect(self._edge_done)
        self.view.contextAction.connect(self._context_action)
        split.addWidget(self.view)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.tuner_panel = QLabel("Select a slice")
        self.tuner_panel.setTextFormat(Qt.RichText)
        self.tuner_panel.setWordWrap(True)
        self.tuner_panel.setStyleSheet("font-size: 16px; padding: 6px 4px;")
        bl.addWidget(self.tuner_panel)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(7, QHeaderView.Stretch)          # Flags takes the spare width
        self.table.itemSelectionChanged.connect(self._table_selection)
        self.table.itemChanged.connect(self._item_changed)
        bl.addWidget(self.table, 1)

        acts = QHBoxLayout()
        self.b_play = QPushButton("Play  [Space]")
        self.b_play_c = QPushButton("Play shifted to C  [C]")
        self.b_play_region = QPushButton("Play region  [R]")
        self.b_stop = QPushButton("Stop  [S]")
        self.b_split = QPushButton("Split  [/]")
        self.b_split.setToolTip("Split at the suggested point (the middle of a glide, or the note change)")
        self.b_merge = QPushButton("Merge with next  [M]")
        self.b_delete = QPushButton("Delete  [Del]")
        self.b_oct_dn = QPushButton("Octave -  [-]")
        self.b_oct_up = QPushButton("Octave +  [+]")
        self.b_note = QPushButton("Set note...  [N]")
        self.b_keep = QPushButton("Keep as sung  [K]")
        for b in (self.b_play, self.b_play_c, self.b_play_region, self.b_stop):
            acts.addWidget(b)
        acts.addSpacing(16)
        for b in (self.b_split, self.b_merge, self.b_delete, self.b_oct_dn, self.b_oct_up, self.b_note, self.b_keep):
            acts.addWidget(b)
        acts.addStretch(1)
        bl.addLayout(acts)

        midi_row = QHBoxLayout()
        midi_row.addWidget(QLabel("MIDI in:"))
        self.midi_combo = QComboBox()
        self.midi_combo.setMinimumWidth(260)
        self.midi_combo.setToolTip("Keys from Live's C1 (MIDI 36) up play slice 1, 2, 3...; during Tap to chop, "
                                   "any key adds a slice point")
        midi_row.addWidget(self.midi_combo)
        self.midi_refresh = QPushButton("Refresh")
        midi_row.addWidget(self.midi_refresh)
        self.midi_label = QLabel("")
        self.midi_label.setWordWrap(True)
        midi_row.addWidget(self.midi_label, 1)
        self.tap_btn = QPushButton("Tap to chop  [T]")
        self.tap_btn.setToolTip("Plays the region; press Space or any MIDI key where a slice should start. "
                                "Taps within 60 ms of a detected note start land on it. Press T or S to stop.")
        self.tap_btn.setCheckable(True)
        self.tap_btn.clicked.connect(self._toggle_tap)
        midi_row.addWidget(self.tap_btn)
        self.b_folder = QPushButton("Open project folder")
        self.b_folder.clicked.connect(self._open_folder)
        midi_row.addWidget(self.b_folder)
        bl.addLayout(midi_row)
        split.addWidget(bottom)
        split.setSizes([430, 420])
        root.addWidget(split, 1)

        self.b_play.clicked.connect(lambda: self._play("original"))
        self.b_play_c.clicked.connect(lambda: self._play("c"))
        self.b_play_region.clicked.connect(self._play_region)
        self.b_stop.clicked.connect(self._stop)
        self.b_split.clicked.connect(lambda: self._edit("split_suggested"))
        self.b_merge.clicked.connect(lambda: self._edit("merge_next"))
        self.b_delete.clicked.connect(lambda: self._edit("delete"))
        self.b_oct_dn.clicked.connect(lambda: self._edit("octave", -1))
        self.b_oct_up.clicked.connect(lambda: self._edit("octave", 1))
        self.b_note.clicked.connect(self._set_note)
        self.b_keep.clicked.connect(self._toggle_keep)
        self.midi_refresh.clicked.connect(self._fill_midi_ports)
        self.midi_combo.currentIndexChanged.connect(self._midi_port_changed)
        self.midi.noteOn.connect(self._midi_note)
        self.midi.status.connect(self.midi_label.setText)

        for key, fn in (("Space", self._space), ("C", lambda: self._play("c")), ("R", self._play_region),
                        ("S", self._stop), ("/", lambda: self._edit("split_suggested")),
                        ("M", lambda: self._edit("merge_next")), ("Delete", lambda: self._edit("delete")),
                        ("-", lambda: self._edit("octave", -1)), ("+", lambda: self._edit("octave", 1)),
                        ("=", lambda: self._edit("octave", 1)), ("N", self._set_note), ("K", self._toggle_keep),
                        ("T", self._toggle_tap_key), ("Up", lambda: self._step(-1)), ("Down", lambda: self._step(1))):
            # only while the view, the table or the buttons have focus, so typing "F#5" in the range box is safe
            sc = QShortcut(QKeySequence(key), split)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        for key, fn in (("Ctrl+S", self._save), ("Ctrl+Z", self._undo), ("Ctrl+Y", self._redo),
                        ("Ctrl+Shift+Z", self._redo), ("Ctrl+K", self._chop)):
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(fn)

        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._tick)

        self._range_changed()
        self._mode_changed()
        self._load_file_list()
        self._fill_midi_ports()
        self._refresh()
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
    def _confirm_discard(self) -> bool:
        if self.editor is None or not self.editor.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved slices", "Save the slice map before leaving this song?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            return self._save()
        return True

    def _analyze(self):
        f = self.current_file()
        if f is None:
            QMessageBox.information(self, "No file", f"Put acapellas in {acapellas_dir()} or use Browse.")
            return
        if not self._confirm_discard():
            return
        key = self.preset_key()
        try:
            preset_from_key(key)
        except Exception as exc:
            QMessageBox.warning(self, "Range", f"The custom range isn't valid: {exc}")
            return
        self._stop()
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
        self.editor = SliceEditor(SongContext.from_result(r))
        restored = session.load(r, self.editor)
        self.info.setText(
            f"<b>{r.source.name}</b> &nbsp;·&nbsp; "
            f"<b>Tuning offset: {r.tuning_offset_cents:+.1f} cents</b> &nbsp;·&nbsp; {len(r.notes)} notes detected "
            f"&nbsp;·&nbsp; {r.preset.label} &nbsp;·&nbsp; {r.audio.duration:.1f} s, {r.audio.sample_rate} Hz, "
            f"{r.audio.channels} ch &nbsp;·&nbsp; {r.seconds:.1f} s{' (pitch from cache)' if r.from_cache else ''}"
            + (" &nbsp;·&nbsp; <b>saved slices restored</b>" if restored else ""))
        self._load_render_state()
        if (r.folder / "slicemap.json").exists():
            self.renderer.request(r.folder)       # finish anything left from last time
        self.warning.setText("  ".join(r.warnings))
        self.warning.setVisible(bool(r.warnings))
        self.view.set_data(r.audio.mono(), r.audio.sample_rate, r.track, r.analysis.voiced_mask,
                           r.tuning_offset_cents)
        if restored:
            s = self.editor.settings
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(s.mode))
            self.count_combo.setCurrentIndex(max(0, self.count_combo.findData(s.equal_count)))
            if self.editor.slices:
                a, b = self.editor.region
                self.view.show_range(a, b)
        self._refresh()
        if not restored:
            self.statusBar().showMessage("Drag along the strip at the top of the view to choose a region (a phrase), "
                                         "pick a chop mode and press Chop region.", 15000)

    # ------------------------------------------------------------ state -> widgets
    def _refresh(self, select: int | None = None, keep_view: bool = True):
        ed = self.editor
        have = ed is not None
        for w in (self.mode_combo, self.count_combo, self.chop_btn, self.whole_btn, self.save_btn, self.tap_btn,
                  self.b_play_region):
            w.setEnabled(have)
        if not have:
            self._fill_table()
            self._set_slice_buttons(False)
            return
        if select is None:
            select = self.selected_index()
        if select is not None and not 0 <= select < len(ed.slices):
            select = len(ed.slices) - 1 if ed.slices else None
        whole = ed.region == (0.0, ed.ctx.duration)
        a, b = ed.region
        self.region_label.setText("Region: whole song" if whole else
                                  f"Region: {_fmt_time(a)} - {_fmt_time(b)} ({b - a:.2f} s)")
        self.view.manual_mode = self.mode_combo.currentData() == "manual"
        self.view.set_slices(ed.slices, None if whole else ed.region, select)
        self.undo_btn.setEnabled(ed.can_undo())
        self.redo_btn.setEnabled(ed.can_redo())
        n = len(ed.slices)
        flagged = sum(bool(set(s.flags) & {"glide", "low_confidence"}) for s in ed.slices)
        if n > MAX_SLICES:
            self.count_status.setText(f"<b style='color:{BAD.name()}'>{n} slices: a map holds {MAX_SLICES}. "
                                      f"Use a shorter region.</b>")
        else:
            dirty = " · <b>unsaved</b>" if ed.dirty else " · saved" if n else ""
            self.count_status.setText(f"{n} of {MAX_SLICES} slices · {flagged} flagged{dirty}")
        self.save_btn.setEnabled(not ed.problems())
        self._fill_table(select)

    def _fill_table(self, select: int | None = None):
        self._filling = True
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        slices = self.editor.slices if self.editor else []
        self.table.setRowCount(len(slices))
        for row, s in enumerate(slices):
            hand = s.hand_note is not None
            note_txt = (note_name(s.note) if (s.has_pitch or hand) else "-") + (" (set)" if hand else "") + \
                (f" ({s.octave:+d} oct)" if s.octave else "")
            vals = [str(row + 1), _fmt_time(s.start), f"{s.length * 1000:.0f} ms", note_txt,
                    live_note_name(s.note) if (s.has_pitch or hand) else "-",
                    f"{s.cents:+d}" if (s.has_pitch and not hand) else "", str(s.confidence),
                    ", ".join(f.replace("_", " ") for f in s.flags), "", ""]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if col in (0, 5, 6):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == KEEP_COL:
                    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    it.setCheckState(Qt.Checked if s.keep_as_sung else Qt.Unchecked)
                    it.setToolTip("Keep as sung: the device's Correction slider leaves this slice's cents alone")
                self.table.setItem(row, col, it)
            if "glide" in s.flags:
                self.table.item(row, 7).setForeground(GLIDE)
            elif s.flags:
                self.table.item(row, 7).setForeground(LOWCONF)
            if s.confidence < 60:
                self.table.item(row, 6).setForeground(LOWCONF)
        self._update_render_column()
        self.table.blockSignals(False)
        self._filling = False
        if select is not None and 0 <= select < len(slices):
            self.table.selectRow(select)
            self._show_slice(select)
        else:
            self._set_slice_buttons(False)
            self.tuner_panel.setText("Select a slice" if slices else "No slices yet")

    # ------------------------------------------------------------ Natural render state
    def _slice_hash(self, s) -> str:
        r = self.result
        return render_hash(r.source, r.audio.sample_rate, s, self.editor.settings.fade_ms)

    def _load_render_state(self):
        r = self.result
        self._ready = render.ready_hashes(r.folder) if r else {}
        self._saved_hashes = set()
        if r and (r.folder / "slicemap.json").exists():
            try:
                self._saved_hashes = {s["renderHash"] for s in session.read_slice_map(r.folder / "slicemap.json")
                                      ["slices"]}
            except (OSError, ValueError, KeyError):
                pass

    def slice_render_state(self, s) -> str:
        """'ready', 'rendering', 'waiting' (saved, in the queue) or 'unsaved'."""
        h = self._slice_hash(s)
        if h in self._ready:
            return "ready"
        if self._render_now and self._render_now[0] == h:
            return "rendering"
        return "waiting" if h in self._saved_hashes else "unsaved"

    def _update_render_column(self):
        if not self.editor or not self.result:
            return
        was = self.table.signalsBlocked()
        self.table.blockSignals(True)
        for row, s in enumerate(self.editor.slices):
            it = self.table.item(row, NATURAL_COL)
            if it is None:
                continue
            st = self.slice_render_state(s)
            if st == "ready":
                it.setText("ready")
                it.setForeground(OK)
                it.setToolTip("Natural renders done: 25 files, -12..+12 semitones")
            elif st == "rendering":
                it.setText(f"rendering {self._render_now[1]}/25")
                it.setForeground(LOWCONF)
                it.setToolTip("Rendering now")
            elif st == "waiting":
                it.setText("waiting")
                it.setForeground(QColor("#aaaaaa"))
                it.setToolTip("Saved; waiting for its turn in the Natural render")
            else:
                it.setText("save to render")
                it.setForeground(QColor("#888888"))
                it.setToolTip("This slice's audio isn't saved yet: Save starts its Natural render")
        self.table.blockSignals(was)

    def _render_event(self, e):
        if not self.result or Path(e.project) != Path(self.result.folder).resolve():
            if e.kind == "error":
                self.statusBar().showMessage(f"Natural render failed for {Path(e.project).name}: "
                                             f"{e.message.splitlines()[0]}", 15000)
            return
        if e.kind == "file":
            try:
                m = session.read_slice_map(self.result.folder / "slicemap.json")
                h = next(s["renderHash"] for s in m["slices"] if s["index"] == e.index)
                self._render_now = (h, e.files)
            except (OSError, ValueError, StopIteration, KeyError):
                pass
            self.render_label.setText(f"Natural render: {e.done} of {e.total} slices ready · "
                                      f"slice {e.index}: {e.files}/25")
        elif e.kind == "slice":
            self._render_now = None
            self._load_render_state()
            self.render_label.setText(f"Natural render: {e.done} of {e.total} slices ready")
        elif e.kind == "start":
            self._load_render_state()
            self.render_label.setText(f"Natural render: {e.done} of {e.total} slices ready"
                                      + (f" · {e.message}" if e.total != e.done else ""))
        elif e.kind == "idle":
            self._render_now = None
            self._load_render_state()
            col = OK if e.done == e.total else LOWCONF
            self.render_label.setText(f"<span style='color:{col.name()}'>Natural render: {e.done} of {e.total} "
                                      f"slices ready</span>")
        elif e.kind == "error":
            self._render_now = None
            self.render_label.setText(f"<span style='color:{BAD.name()}'>Natural render failed</span>")
            self.statusBar().showMessage(f"Natural render failed: {e.message.splitlines()[0]}", 20000)
        self._update_render_column()

    def selected_index(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else None

    def selected_slice(self):
        i = self.selected_index()
        return self.editor.slices[i] if (self.editor and i is not None and i < len(self.editor.slices)) else None

    def _table_selection(self):
        if self._filling:
            return
        i = self.selected_index()
        self._set_slice_buttons(i is not None)
        if i is None:
            return
        self._show_slice(i)

    def _show_slice(self, i: int):
        s = self.editor.slices[i]
        self.view.selected = i
        self.view.ensure_visible(s.start, s.end)
        self._set_slice_buttons(True)
        r = self.result
        tuner = tuner_on_slice(r.audio.mono(), r.audio.sample_rate, s, r.preset.low, r.preset.high)
        parts = [f"<b>Slice {i + 1}</b>: {_fmt_time(s.start)}, {s.length * 1000:.0f} ms"]
        if s.has_pitch or s.hand_note is not None:
            parts.append(f"App <b>{note_name(s.note)}</b> {s.cents:+d} c (Live {live_note_name(s.note)})")
        else:
            parts.append("no pitch found: set the note by hand if it has one")
        if tuner.ok and s.has_pitch and s.hand_note is None and s.octave == 0:
            status, apart = compare(tuner, s, r.tuning_offset_cents)
            col = OK if status == "match" else LOWCONF if status == "borderline" else BAD
            parts.append(f"built-in tuner <b>{note_name(tuner.note)}</b> {tuner.cents:+d} c → "
                         f"<b style='color:{col.name()}'>{'same note' if status == 'match' else status}</b>")
        flags = []
        if "glide" in s.flags:
            flags.append(f"<b style='color:{GLIDE.name()}'>Glide</b> (spread {s.spread:.1f} st: press Split)")
        if "low_confidence" in s.flags:
            flags.append(f"<b style='color:{LOWCONF.name()}'>Low confidence {s.confidence}</b> "
                         f"(voiced {s.voicedness * 100:.0f}%, steadiness {s.steadiness * 100:.0f}%)")
        self.tuner_panel.setText(" &nbsp;·&nbsp; ".join(parts + flags))

    def _set_slice_buttons(self, on: bool):
        for b in (self.b_play, self.b_play_c, self.b_split, self.b_merge, self.b_delete, self.b_oct_dn,
                  self.b_oct_up, self.b_note, self.b_keep):
            b.setEnabled(on)

    def _select_slice(self, i: int):
        self.table.selectRow(i)

    def _step(self, d: int):
        if not self.editor or not self.editor.slices:
            return
        i = self.selected_index()
        i = 0 if i is None else int(np.clip(i + d, 0, len(self.editor.slices) - 1))
        self.table.selectRow(i)

    def _item_changed(self, it: QTableWidgetItem):
        if self._filling or it.column() != KEEP_COL or not self.editor:
            return
        row = it.row()
        on = it.checkState() == Qt.Checked
        if self.editor.slices[row].keep_as_sung != on:
            self.editor.set_keep_as_sung(row, on)
            self._refresh(select=row)

    # ------------------------------------------------------------ chopping and editing
    def _mode_changed(self):
        equal = self.mode_combo.currentData() == "equal"
        self.count_label.setVisible(equal)
        self.count_combo.setVisible(equal)
        self.view.manual_mode = self.mode_combo.currentData() == "manual"
        self.chop_btn.setText("Start manual chop" if self.view.manual_mode else "Chop region")

    def chop_settings(self) -> ChopSettings:
        s = ChopSettings() if self.editor is None else ChopSettings(**vars(self.editor.settings))
        s.mode = self.mode_combo.currentData()
        s.equal_count = int(self.count_combo.currentData())
        return s

    def _chop(self):
        if not self.editor:
            return
        self.editor.chop(self.chop_settings())
        n = len(self.editor.slices)
        if self.view.manual_mode:
            self.statusBar().showMessage("Manual: click in the view (or use Tap to chop) where each slice should start.",
                                         10000)
        elif n > MAX_SLICES:
            self.statusBar().showMessage(f"{n} slices - a slice map holds {MAX_SLICES}. Drag a shorter region over "
                                         f"one phrase and chop again.", 12000)
        else:
            self.statusBar().showMessage(f"{n} slices.", 5000)
        self._refresh(select=0 if n else None)

    def _region_chosen(self, a: float, b: float):
        if not self.editor:
            return
        try:
            self.editor.set_region(a, b)
        except EditError as exc:
            self.statusBar().showMessage(str(exc), 5000)
            return
        self._refresh()

    def _whole_song(self):
        if self.editor:
            self.editor.clear_region()
            self._refresh()

    def _undo(self):
        if self.editor and self.editor.undo():
            self._refresh()

    def _redo(self):
        if self.editor and self.editor.redo():
            self._refresh()

    def _edit(self, action: str, arg=None):
        i = self.selected_index()
        if self.editor is None or i is None:
            return
        try:
            sel = i
            if action == "split_suggested":
                self.editor.split_suggested(i)
            elif action == "merge_next":
                self.editor.merge_next(i)
            elif action == "delete":
                self.editor.delete(i)
                sel = min(i, len(self.editor.slices) - 1)
            elif action == "octave":
                self.editor.shift_octave(i, arg)
        except EditError as exc:
            self.statusBar().showMessage(str(exc), 6000)
            return
        self._refresh(select=sel if self.editor.slices else None)

    def _split_at(self, t: float):
        if not self.editor:
            return
        i = self.editor.index_at(t)
        if i is None:
            self.statusBar().showMessage("Not inside a slice (chop the region first)", 5000)
            return
        try:
            j = self.editor.split(i, t)
        except EditError as exc:
            self.statusBar().showMessage(str(exc), 5000)
            return
        self._refresh(select=j)

    def _edge_start(self, i: int, edge: str):
        if self.editor:
            self.editor.begin_drag()
            self._drag_i = i

    def _edge_move(self, i: int, edge: str, t: float):
        if not self.editor:
            return
        try:
            self.editor.move_edge(i, edge, t, record=False)
        except EditError:
            return
        self.view.set_slices(self.editor.slices, self.view.region, i)

    def _edge_done(self):
        self._refresh(select=getattr(self, "_drag_i", None))

    def _context_action(self, action: str, t: float):
        if not self.editor:
            return
        i = self.editor.index_at(t)
        if i is None:
            return
        if action == "split":
            self._split_at(t)
        elif action == "merge":
            self.table.selectRow(i)
            self._edit("merge_next")
        elif action == "delete":
            self.table.selectRow(i)
            self._edit("delete")
        elif action == "region":
            s = self.editor.slices[i]
            self._region_chosen(s.start, s.end)

    def _set_note(self):
        i = self.selected_index()
        if self.editor is None or i is None:
            return
        s = self.editor.slices[i]
        text, ok = QInputDialog.getText(self, "Set the note by hand",
                                        "Note name (C4 = middle C, e.g. E4, F#3), or leave empty to go back to the "
                                        "detected note:", text=note_name(s.note))
        if not ok:
            return
        try:
            midi = None if not text.strip() else parse_note(text)
            self.editor.set_note(i, midi)
        except (ValueError, IndexError, EditError) as exc:
            self.statusBar().showMessage(f"Not a note name: {text} ({exc})", 6000)
            return
        self._refresh(select=i)

    def _toggle_keep(self):
        i = self.selected_index()
        if self.editor is None or i is None:
            return
        self.editor.set_keep_as_sung(i, not self.editor.slices[i].keep_as_sung)
        self._refresh(select=i)

    # ------------------------------------------------------------ saving
    def _save(self) -> bool:
        if not self.editor or not self.result:
            return False
        try:
            rep = session.save(self.result, self.editor)
        except ValueError as exc:
            QMessageBox.warning(self, "Can't save yet", str(exc))
            return False
        changed = (f"{len(rep.changed)} slice{'s' if len(rep.changed) != 1 else ''} changed"
                   if rep.changed else "no slice audio changed")
        self.statusBar().showMessage(f"Saved {rep.map_path.name}: {rep.count} slices, {changed}. The Natural "
                                     f"render runs in the background (Natural column).", 12000)
        self._load_render_state()
        self.renderer.request(self.result.folder)
        self._refresh()
        return True

    # ------------------------------------------------------------ listening
    def clip(self, kind: str, i: int | None = None):
        """(audio, sample rate, info) for the selected slice: 'original' or 'c' (shifted to the nearest C)."""
        i = self.selected_index() if i is None else i
        if self.editor is None or i is None:
            return None
        s = self.editor.slices[i]
        a = self.result.audio
        if kind == "c":
            folder = self._ready.get(self._slice_hash(s))
            if folder is not None:
                try:
                    x, target, st, k, rest = natural_clip(folder, a.sample_rate, s)
                    return x, a.sample_rate, (target, st, (k, rest))
                except (OSError, RuntimeError):
                    pass                              # a render file went missing: fall back to speed change
            x, target, st = shifted_clip(a.data, a.sample_rate, s)
            return x, a.sample_rate, (target, st, None)
        return original_clip(a.data, a.sample_rate, s), a.sample_rate, None

    def _play(self, kind: str, i: int | None = None):
        c = self.clip(kind, i)
        if c is None:
            return
        x, sr, extra = c
        i = self.selected_index() if i is None else i
        s = self.editor.slices[i]
        if kind == "c":
            target, st, nat = extra
            r = self.result
            reading = tuner_on_clip(x, sr, r.preset.low, r.preset.high)
            if reading.ok:
                got = reading.midi - r.tuning_offset_cents / 100.0
                ok = int(round(got)) == target
                col = OK if ok else BAD
                self.tuner_panel.setText(
                    f"<b>Slice {i + 1}</b> shifted {st:+.2f} st to <b>{note_name(target)}</b> "
                    f"(Live {live_note_name(target)}) · built-in tuner on what you hear: <b>{note_name(reading.note)}"
                    f"</b> {reading.cents:+d} c → <b style='color:{col.name()}'>"
                    f"{'it is a C' if ok else 'NOT a C'}</b> <span style='color:#888'>(the song's own tuning is "
                    f"{r.tuning_offset_cents:+.0f} c; " + (f"Natural render {render.shift_name(nat[0])[:-4]} + {nat[1] * 100:+.0f} c "
                    f"speed change, as the device plays it)" if nat else "not rendered yet, so this is a plain speed "
                    "change)") + "</span>")
            self._play_offset, self._play_rate = None, 1.0
        else:
            self._play_offset, self._play_rate = s.start, 1.0
        self._start(x, sr)

    def _play_region(self):
        if not self.editor or not self.result:
            return
        a, b = self.editor.region
        audio = self.result.audio
        x = audio.data[int(a * audio.sample_rate):int(b * audio.sample_rate)]
        self._play_offset = a
        self._start(x, audio.sample_rate)

    def _start(self, x, sr):
        if not self.player.available:
            self.statusBar().showMessage(self.player.error or "Audio playback is not available", 6000)
            return
        try:
            self.player.play(x, sr)
        except Exception as exc:
            self.statusBar().showMessage(f"Playback failed: {exc}", 8000)
            return
        if self._play_offset is not None:
            self.timer.start()

    def _tick(self):
        pos = self.player.position()
        if pos is None:
            self.timer.stop()
            self.view.set_playhead(None)
            if self.tapping:
                self._end_tap()
            return
        if self._play_offset is not None:
            self.view.set_playhead(self._play_offset + pos)

    def _stop(self):
        self.player.stop()
        self.timer.stop()
        self.view.set_playhead(None)
        if self.tapping:
            self._end_tap()

    def _space(self):
        if self.tapping:
            self._tap()
        else:
            self._play("original")

    # ------------------------------------------------------------ tap to chop
    def _toggle_tap_key(self):
        self.tap_btn.setChecked(not self.tapping)
        self._toggle_tap()

    def _toggle_tap(self):
        if self.tapping:
            self._stop()
            return
        if not self.editor:
            self.tap_btn.setChecked(False)
            return
        ed = self.editor
        a, b = ed.region
        if not any(s.start < b and s.end > a for s in ed.slices):
            ed.chop(ChopSettings(**{**vars(self.chop_settings()), "mode": "manual"}))
            self.mode_combo.setCurrentIndex(self.mode_combo.findData("manual"))
            self._refresh()
        self.tapping = True
        self.tap_btn.setChecked(True)
        self._tap_region = (a, b)
        self.statusBar().showMessage("Tapping: press Space or any MIDI key where a slice should start. "
                                     "T or S stops.", 0)
        self._play_region()
        if not self.player.available:
            self._end_tap()

    def _end_tap(self):
        self.tapping = False
        self.tap_btn.setChecked(False)
        self.statusBar().showMessage("Tap to chop finished. Undo removes the last tap.", 8000)
        self._refresh()

    def tap_at(self, t: float) -> int | None:
        """Add a slice point at source time t (snapped to a detected note start within 60 ms)."""
        if not self.editor:
            return None
        starts = [n.start for n in self.editor.ctx.notes]
        if starts:
            k = int(np.argmin(np.abs(np.array(starts) - t)))
            if abs(starts[k] - t) <= TAP_SNAP:
                t = starts[k]
        i = self.editor.index_at(t)
        if i is None:
            return None
        try:
            j = self.editor.split(i, t)
        except EditError:
            return None
        self._refresh(select=j)
        return j

    def _tap(self):
        pos = self.player.position()
        if pos is None:
            return
        self.tap_at(self._tap_region[0] + pos)

    # ------------------------------------------------------------ MIDI
    def _fill_midi_ports(self):
        cur = self.midi.name
        self.midi_combo.blockSignals(True)
        self.midi_combo.clear()
        self.midi_combo.addItem("(none)", None)
        err = backend_error()
        for name in list_ports():
            self.midi_combo.addItem(name, name)
        if cur and self.midi_combo.findData(cur) >= 0:
            self.midi_combo.setCurrentIndex(self.midi_combo.findData(cur))
        self.midi_combo.blockSignals(False)
        if err:
            self.midi_label.setText(err)
        elif self.midi_combo.count() == 1:
            self.midi_label.setText("No MIDI inputs found. Plug the keyboard in and press Refresh.")
        elif not cur:
            self.midi_label.setText("Pick your keyboard: keys from Live's C1 up play slices 1, 2, 3...")

    def _midi_port_changed(self):
        name = self.midi_combo.currentData()
        if name is None:
            self.midi.close()
            self.midi_label.setText("MIDI in: off")
            return
        if not self.midi.open(name):
            self.midi_combo.blockSignals(True)
            self.midi_combo.setCurrentIndex(0)
            self.midi_combo.blockSignals(False)

    def _midi_note(self, note: int, _vel: int):
        if not self.editor:
            return
        if self.tapping:
            self._tap()
            return
        i = slice_for_key(note, len(self.editor.slices))
        if i is None:
            return
        self.table.selectRow(i)
        self._play("original", i)

    # ------------------------------------------------------------ files
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
        if not self._confirm_discard():
            ev.ignore()
            return
        self._stop()
        self.midi.close()
        self.renderer.stop(wait=5.0)
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
