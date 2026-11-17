"""The editor's main view (SPEC §3.4): waveform, slice lines and the pitch curve drawn over it, plus the region.

Mouse:
  wheel                         zoom in/out around the pointer
  drag on empty space           scroll
  drag in the top strip         set the region (or Shift + drag anywhere)
  drag a slice line             move that boundary (a line shared by two slices moves both)
  click a slice                 select it (in Manual mode a click splits there instead)
  double-click                  split the slice there (any mode)
  right-click                   menu: split here / merge with next / delete / region = this slice
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QMenu, QWidget

from ..presets import NOTE_NAMES

BG = QColor("#1e1f24")
OUTSIDE = QColor("#16171b")
WAVE = QColor("#3b4150")
WAVE_IN = QColor("#4a5266")
GRID = QColor("#2a2d35")
GRID_C = QColor("#3a3e48")
CURVE = QColor("#7fb3ff")
NOTE = QColor("#c9ccd4")
SELECTED = QColor("#ffcc4d")
LOWCONF = QColor("#ff9f43")
GLIDE = QColor("#d68cff")
LINE = QColor("#9aa3b5")
REGION = QColor("#5ad1c4")
PLAYHEAD = QColor("#ff5c5c")
SLICE_A = QColor(255, 255, 255, 14)
SLICE_B = QColor(255, 255, 255, 28)

STRIP = 18          # top strip for the region and slice numbers
EDGE_PX = 5         # how close the pointer must be to grab a slice line


class EditorView(QWidget):
    sliceClicked = Signal(int)                 # 0-based slice index
    regionChosen = Signal(float, float)
    splitAt = Signal(float)                    # seconds
    edgeDragStarted = Signal(int, str)         # slice index, "start" / "end"
    edgeDragged = Signal(int, str, float)
    edgeDragFinished = Signal()
    contextAction = Signal(str, float)         # "split" / "merge" / "delete" / "region", seconds

    LEFT = 44

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
        self.slices = []
        self.offset = 0.0
        self.region = None
        self.selected: int | None = None
        self.playhead: float | None = None
        self.manual_mode = False
        self.duration = 1.0
        self.t0, self.t1 = 0.0, 8.0
        self.lo, self.hi = 55.0, 80.0
        self._drag = None              # ("pan", x, t0) | ("region", t) | ("edge", i, edge)
        self._press = None

    # ------------------------------------------------------------ data
    def set_data(self, mono: np.ndarray, sample_rate: int, track, voiced, tuning_offset_cents: float):
        self.duration = len(mono) / sample_rate
        step = max(1, sample_rate // 100)
        n = len(mono) // step
        self.env = np.abs(mono[:n * step]).reshape(n, step).max(axis=1) if n else np.zeros(1)
        self.env_t = np.arange(len(self.env)) * step / sample_rate
        self.times = track.times()
        self.midi = track.midi
        self.voiced = voiced
        self.offset = tuning_offset_cents
        vals = self.midi[self.voiced]
        if len(vals):
            self.lo = float(np.floor(np.percentile(vals, 1))) - 2
            self.hi = float(np.ceil(np.percentile(vals, 99))) + 2
        self.t0, self.t1 = 0.0, min(self.duration, 12.0)
        self.update()

    def set_slices(self, slices, region, selected=None):
        self.slices = slices
        self.region = region
        self.selected = selected
        self.update()

    def clear(self):
        self.times = None
        self.slices = []
        self.update()

    def show_range(self, a: float, b: float, margin: float = 0.08):
        span = max(0.5, (b - a) * (1 + 2 * margin))
        mid = (a + b) / 2
        self.t0 = float(np.clip(mid - span / 2, 0, max(0.0, self.duration - span)))
        self.t1 = min(self.duration, self.t0 + span)
        self.update()

    def ensure_visible(self, a: float, b: float):
        if a < self.t0 or b > self.t1:
            span = max(self.t1 - self.t0, (b - a) + 1.0)
            mid = (a + b) / 2
            self.t0 = float(np.clip(mid - span / 2, 0, max(0.0, self.duration - span)))
            self.t1 = min(self.duration, self.t0 + span)
        self.update()

    def set_playhead(self, t: float | None):
        self.playhead = t
        if t is not None and not (self.t0 <= t <= self.t1):
            span = self.t1 - self.t0
            self.t0 = float(np.clip(t - 0.1 * span, 0, max(0.0, self.duration - span)))
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
        return h - 8 - (m - self.lo) / max(1e-9, self.hi - self.lo) * (h - 16 - STRIP)

    def edge_at(self, x: float) -> tuple[int, str] | None:
        """The slice line under the pointer, as (slice index, edge). Shared lines report the right-hand slice."""
        best = None
        for i, s in enumerate(self.slices):
            for edge, t in (("start", s.start), ("end", s.end)):
                d = abs(self._x(t) - x)
                if d <= EDGE_PX and (best is None or d < best[0] or (d == best[0] and edge == "start")):
                    best = (d, i, edge)
        return None if best is None else (best[1], best[2])

    def slice_at(self, t: float) -> int | None:
        for i, s in enumerate(self.slices):
            if s.start <= t < s.end:
                return i
        return None

    # ------------------------------------------------------------ painting
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), BG)
        if self.times is None:
            p.setPen(NOTE)
            p.drawText(self.rect(), Qt.AlignCenter, "Pick a file and press Analyze")
            return
        h, w = self.height(), self.width()
        small = QFont(self.font())
        small.setPointSize(8)
        p.setFont(small)
        # outside the region: darker
        if self.region:
            r0, r1 = self.region
            for a, b in ((self.t0, r0), (r1, self.t1)):
                if b > a:
                    p.fillRect(QRectF(self._x(max(a, self.t0)), STRIP, self._x(min(b, self.t1)) -
                                      self._x(max(a, self.t0)), h - STRIP), OUTSIDE)
        # semitone grid
        for m in range(int(np.floor(self.lo)), int(np.ceil(self.hi)) + 1):
            y = self._y(m + self.offset / 100.0)
            is_c = m % 12 == 0
            p.setPen(QPen(GRID_C if is_c else GRID, 1))
            p.drawLine(QPointF(self.LEFT, y), QPointF(w, y))
            if is_c or (self.hi - self.lo) < 30:
                p.setPen(NOTE if is_c else QColor("#80848f"))
                p.drawText(QRectF(2, y - 7, self.LEFT - 6, 14), Qt.AlignRight | Qt.AlignVCenter,
                           f"{NOTE_NAMES[m % 12]}{m // 12 - 1}")
        # slice backgrounds
        for i, s in enumerate(self.slices):
            if s.end < self.t0 or s.start > self.t1:
                continue
            x0, x1 = self._x(s.start), self._x(s.end)
            p.fillRect(QRectF(x0, STRIP, x1 - x0, h - STRIP), SLICE_B if i % 2 else SLICE_A)
        # waveform
        sel = (self.env_t >= self.t0) & (self.env_t <= self.t1)
        if sel.any():
            mid = STRIP + (h - STRIP) / 2
            scale = ((h - STRIP) / 2 - 6) / max(1e-6, float(self.env.max()))
            xs = self._x(self.env_t[sel])
            amps = self.env[sel] * scale
            ts = self.env_t[sel]
            stride = max(1, len(xs) // (w * 2))
            for x, a, t in zip(xs[::stride], amps[::stride], ts[::stride]):
                inside = self.region is None or self.region[0] <= t <= self.region[1]
                p.setPen(QPen(WAVE_IN if inside else WAVE, 1))
                p.drawLine(QPointF(x, mid - a), QPointF(x, mid + a))
        # slice note bars and lines
        for i, s in enumerate(self.slices):
            if s.end < self.t0 or s.start > self.t1:
                continue
            x0, x1 = self._x(s.start), self._x(s.end)
            if s.has_pitch or s.hand_note is not None:
                y = self._y(s.note + s.cents / 100.0 + self.offset / 100.0) if s.hand_note is None else \
                    self._y(s.note + self.offset / 100.0)
                col = SELECTED if i == self.selected else NOTE
                c = QColor(col)
                c.setAlpha(170 if i == self.selected else 110)
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawRoundedRect(QRectF(x0 + 1, y - 5, max(2.0, x1 - x0 - 2), 10), 3, 3)
                if "glide" in s.flags or "low_confidence" in s.flags:
                    p.setPen(QPen(GLIDE if "glide" in s.flags else LOWCONF, 1.8))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(QRectF(x0 + 1, y - 5, max(2.0, x1 - x0 - 2), 10), 3, 3)
                if x1 - x0 > 26:
                    p.setPen(QColor("#111"))
                    p.drawText(QRectF(x0 + 4, y - 6, x1 - x0, 12), Qt.AlignLeft | Qt.AlignVCenter,
                               f"{NOTE_NAMES[s.note % 12]}{s.note // 12 - 1}")
            for t in (s.start, s.end):
                p.setPen(QPen(SELECTED if i == self.selected else LINE, 1.2))
                p.drawLine(QPointF(self._x(t), STRIP), QPointF(self._x(t), h))
            # number + flag letters in the strip
            if x1 - x0 > 14:
                flags = ("G" if "glide" in s.flags else "") + ("?" if "low_confidence" in s.flags else "")
                p.setPen(SELECTED if i == self.selected else NOTE)
                p.drawText(QRectF(x0 + 2, 2, x1 - x0 - 2, STRIP - 3), Qt.AlignLeft | Qt.AlignVCenter,
                           f"{i + 1}{' ' + flags if flags else ''}")
        # pitch curve
        if len(self.times) > 1:
            dt = self.times[1] - self.times[0]
            i0 = max(0, int(self.t0 / dt) - 1)
            i1 = min(len(self.times), int(self.t1 / dt) + 2)
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
        # region bar in the strip
        if self.region:
            r0, r1 = self.region
            p.setPen(Qt.NoPen)
            c = QColor(REGION)
            c.setAlpha(70)
            p.setBrush(c)
            p.drawRect(QRectF(self._x(r0), 0, self._x(r1) - self._x(r0), STRIP))
            p.setPen(QPen(REGION, 1.5))
            for t in (r0, r1):
                p.drawLine(QPointF(self._x(t), 0), QPointF(self._x(t), h))
        if self._drag and self._drag[0] == "region":
            a, b = sorted((self._drag[1], self._drag[2]))
            c = QColor(REGION)
            c.setAlpha(60)
            p.fillRect(QRectF(self._x(a), 0, self._x(b) - self._x(a), h), c)
        # playhead
        if self.playhead is not None:
            p.setPen(QPen(PLAYHEAD, 1.5))
            x = self._x(self.playhead)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
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
        span = float(np.clip((self.t1 - self.t0) * factor, 0.3, self.duration))
        frac = (at - self.t0) / max(1e-9, self.t1 - self.t0)
        self.t0 = float(np.clip(at - frac * span, 0, max(0.0, self.duration - span)))
        self.t1 = self.t0 + span
        self.update()

    def mousePressEvent(self, ev):
        if self.times is None:
            return
        x, y = ev.position().x(), ev.position().y()
        t = float(np.clip(self._t(x), 0, self.duration))
        if ev.button() == Qt.RightButton:
            self._context_menu(ev, t)
            return
        if ev.button() != Qt.LeftButton:
            return
        self._press = (x, y)
        if y < STRIP or ev.modifiers() & Qt.ShiftModifier:
            self._drag = ["region", t, t]
            return
        e = self.edge_at(x)
        if e is not None:
            self._drag = ["edge", e[0], e[1]]
            self.edgeDragStarted.emit(e[0], e[1])
            return
        self._drag = ["pan", x, self.t0]

    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        if self._drag is None:
            if self.times is not None:
                over_edge = ev.position().y() >= STRIP and self.edge_at(x) is not None
                self.setCursor(Qt.SizeHorCursor if over_edge else Qt.ArrowCursor)
            return
        kind = self._drag[0]
        if kind == "pan":
            x0, t0 = self._drag[1], self._drag[2]
            dt = (x - x0) / max(1, self.width() - self.LEFT) * (self.t1 - self.t0)
            span = self.t1 - self.t0
            self.t0 = float(np.clip(t0 - dt, 0, max(0.0, self.duration - span)))
            self.t1 = self.t0 + span
            self.update()
        elif kind == "region":
            self._drag[2] = float(np.clip(self._t(x), 0, self.duration))
            self.update()
        elif kind == "edge":
            self.edgeDragged.emit(self._drag[1], self._drag[2], float(np.clip(self._t(x), 0, self.duration)))

    def mouseReleaseEvent(self, ev):
        drag, press = self._drag, self._press
        self._drag, self._press = None, None
        if drag is None:
            return
        moved = press is not None and abs(ev.position().x() - press[0]) > 3
        t = float(np.clip(self._t(ev.position().x()), 0, self.duration))
        if drag[0] == "region":
            if moved:
                a, b = sorted((drag[1], drag[2]))
                self.regionChosen.emit(a, b)
            self.update()
        elif drag[0] == "edge":
            self.edgeDragFinished.emit()
        elif drag[0] == "pan" and not moved:
            if self.manual_mode and self.region and self.region[0] < t < self.region[1]:
                self.splitAt.emit(t)
                return
            i = self.slice_at(t)
            if i is not None:
                self.sliceClicked.emit(i)

    def mouseDoubleClickEvent(self, ev):
        if self.times is None or ev.button() != Qt.LeftButton or self.manual_mode:
            return
        self.splitAt.emit(float(np.clip(self._t(ev.position().x()), 0, self.duration)))

    def _context_menu(self, ev, t: float):
        i = self.slice_at(t)
        m = QMenu(self)
        acts = {}
        if i is not None:
            acts[m.addAction(f"Split slice {i + 1} here")] = "split"
            if i + 1 < len(self.slices):
                acts[m.addAction(f"Merge slice {i + 1} with {i + 2}")] = "merge"
            acts[m.addAction(f"Delete slice {i + 1}")] = "delete"
            m.addSeparator()
            acts[m.addAction("Set the region to this slice")] = "region"
        else:
            m.addAction("(no slice here)").setEnabled(False)
        chosen = m.exec(ev.globalPosition().toPoint())
        if chosen in acts:
            self.contextAction.emit(acts[chosen], t)
