"""Waveform with the pitch curve and the detected notes drawn over it (SPEC §3.4, Milestone 1 version)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..presets import NOTE_NAMES

BG = QColor("#1e1f24")
WAVE = QColor("#3b4150")
GRID = QColor("#2a2d35")
GRID_C = QColor("#3a3e48")
CURVE = QColor("#7fb3ff")
NOTE = QColor("#c9ccd4")
SELECTED = QColor("#ffcc4d")
PASS = QColor("#4cc38a")
FAIL = QColor("#ff5c5c")
LOWCONF = QColor("#ff9f43")


class PitchView(QWidget):
    noteClicked = Signal(int)          # note index (1-based)

    LEFT = 44                          # room for note labels

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.times = None
        self.midi = None
        self.voiced = None
        self.env_t = None
        self.env = None
        self.notes = []
        self.offset = 0.0
        self.marks: dict[int, str] = {}
        self.selected: int | None = None
        self.duration = 1.0
        self.t0, self.t1 = 0.0, 8.0
        self.lo, self.hi = 55.0, 80.0
        self._drag = None

    # ------------------------------------------------------------ data
    def set_data(self, mono: np.ndarray, sample_rate: int, track, analysis):
        self.duration = len(mono) / sample_rate
        # waveform envelope: peak per 10 ms
        step = max(1, sample_rate // 100)
        n = len(mono) // step
        self.env = np.abs(mono[:n * step]).reshape(n, step).max(axis=1) if n else np.zeros(1)
        self.env_t = np.arange(len(self.env)) * step / sample_rate
        self.times = track.times()
        self.midi = track.midi
        self.voiced = analysis.voiced_mask
        self.notes = analysis.notes
        self.offset = analysis.tuning_offset_cents
        vals = self.midi[self.voiced]
        if len(vals):
            self.lo = float(np.floor(np.percentile(vals, 1))) - 2
            self.hi = float(np.ceil(np.percentile(vals, 99))) + 2
        self.t0, self.t1 = 0.0, min(self.duration, 10.0)
        self.update()

    def clear(self):
        self.times = None
        self.notes = []
        self.update()

    def set_marks(self, marks: dict[int, str]):
        self.marks = marks
        self.update()

    def select(self, index: int | None, center: bool = True):
        self.selected = index
        if center and index is not None:
            n = self.notes[index - 1]
            span = max(self.t1 - self.t0, n.length + 1.0)
            mid = (n.start + n.end) / 2
            if not (self.t0 + 0.1 * span < n.start and n.end < self.t1 - 0.1 * span):
                self.t0 = max(0.0, min(self.duration - span, mid - span / 2))
                self.t1 = self.t0 + span
        self.update()

    # ------------------------------------------------------------ geometry
    def _x(self, t):
        w = self.width() - self.LEFT
        return self.LEFT + (t - self.t0) / max(1e-9, self.t1 - self.t0) * w

    def _t(self, x):
        w = self.width() - self.LEFT
        return self.t0 + (x - self.LEFT) / max(1, w) * (self.t1 - self.t0)

    def _y(self, m):
        h = self.height()
        return h - 8 - (m - self.lo) / max(1e-9, self.hi - self.lo) * (h - 16)

    # ------------------------------------------------------------ painting
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), BG)
        if self.times is None:
            p.setPen(NOTE)
            p.drawText(self.rect(), Qt.AlignCenter, "Pick a file and press Analyze")
            return
        h = self.height()
        # semitone grid with labels on C and on every note when zoomed enough
        small = QFont(self.font())
        small.setPointSize(8)
        p.setFont(small)
        for m in range(int(np.floor(self.lo)), int(np.ceil(self.hi)) + 1):
            y = self._y(m + self.offset / 100.0)
            is_c = m % 12 == 0
            p.setPen(QPen(GRID_C if is_c else GRID, 1))
            p.drawLine(QPointF(self.LEFT, y), QPointF(self.width(), y))
            if is_c or (self.hi - self.lo) < 30:
                p.setPen(NOTE if is_c else QColor("#80848f"))
                p.drawText(QRectF(2, y - 7, self.LEFT - 6, 14), Qt.AlignRight | Qt.AlignVCenter,
                           f"{NOTE_NAMES[m % 12]}{m // 12 - 1}")
        # waveform
        sel = (self.env_t >= self.t0) & (self.env_t <= self.t1)
        if sel.any():
            mid = h / 2
            scale = (h / 2 - 6) / max(1e-6, float(self.env.max()))
            p.setPen(QPen(WAVE, 1))
            xs = self._x(self.env_t[sel])
            amps = self.env[sel] * scale
            stride = max(1, len(xs) // (self.width() * 2))
            for x, a in zip(xs[::stride], amps[::stride]):
                p.drawLine(QPointF(x, mid - a), QPointF(x, mid + a))
        # notes
        for n in self.notes:
            if n.end < self.t0 or n.start > self.t1:
                continue
            y = self._y(n.note + self.offset / 100.0)
            mark = self.marks.get(n.index)
            col = PASS if mark == "pass" else FAIL if mark == "fail" else NOTE
            if n.index == self.selected:
                col = SELECTED
            x0, x1 = self._x(n.start), self._x(n.end)
            p.setPen(Qt.NoPen)
            c = QColor(col)
            c.setAlpha(110 if n.index != self.selected else 170)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(x0, y - 5, max(2.0, x1 - x0), 10), 3, 3)
            if "low_confidence" in n.flags or "glide" in n.flags:
                p.setPen(QPen(LOWCONF, 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(x0, y - 5, max(2.0, x1 - x0), 10), 3, 3)
            if x1 - x0 > 26:
                p.setPen(QColor("#111"))
                p.drawText(QRectF(x0 + 3, y - 6, x1 - x0, 12), Qt.AlignLeft | Qt.AlignVCenter,
                           f"{NOTE_NAMES[n.note % 12]}{n.note // 12 - 1}")
        # pitch curve
        i0 = max(0, int(self.t0 / (self.times[1] - self.times[0])) - 1) if len(self.times) > 1 else 0
        i1 = min(len(self.times), int(self.t1 / (self.times[1] - self.times[0])) + 2) if len(self.times) > 1 else 0
        p.setPen(QPen(CURVE, 1.6))
        prev = None
        for i in range(i0, i1):
            if self.voiced[i] and np.isfinite(self.midi[i]):
                pt = QPointF(self._x(self.times[i]), self._y(self.midi[i]))
                if prev is not None:
                    p.drawLine(prev, pt)
                prev = pt
            else:
                prev = None
        # time ruler
        p.setPen(QColor("#80848f"))
        span = self.t1 - self.t0
        step = next(s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60) if span / s <= 12)
        t = np.ceil(self.t0 / step) * step
        while t <= self.t1:
            x = self._x(t)
            p.drawLine(QPointF(x, h - 4), QPointF(x, h))
            p.drawText(QRectF(x + 2, h - 16, 60, 12), Qt.AlignLeft, f"{int(t // 60)}:{t % 60:05.2f}")
            t += step

    # ------------------------------------------------------------ mouse
    def wheelEvent(self, ev):
        if self.times is None:
            return
        factor = 0.8 if ev.angleDelta().y() > 0 else 1.25
        at = self._t(ev.position().x())
        span = float(np.clip((self.t1 - self.t0) * factor, 0.5, self.duration))
        frac = (at - self.t0) / max(1e-9, self.t1 - self.t0)
        self.t0 = float(np.clip(at - frac * span, 0, max(0.0, self.duration - span)))
        self.t1 = self.t0 + span
        self.update()

    def mousePressEvent(self, ev):
        if self.times is None:
            return
        x, y = ev.position().x(), ev.position().y()
        t = self._t(x)
        for n in self.notes:
            if n.start <= t <= n.end and abs(self._y(n.note + self.offset / 100.0) - y) <= 8:
                self.noteClicked.emit(n.index)
                return
        self._drag = (x, self.t0)

    def mouseMoveEvent(self, ev):
        if self._drag is None:
            return
        x0, t0 = self._drag
        dt = (ev.position().x() - x0) / max(1, self.width() - self.LEFT) * (self.t1 - self.t0)
        span = self.t1 - self.t0
        self.t0 = float(np.clip(t0 - dt, 0, max(0.0, self.duration - span)))
        self.t1 = self.t0 + span
        self.update()

    def mouseReleaseEvent(self, _ev):
        self._drag = None
