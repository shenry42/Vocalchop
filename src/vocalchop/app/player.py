"""Plays clips through the default Windows audio output (shared mode, so Ableton can stay open).

It also knows where playback is (position()), for the playhead and for tap-chopping: the time since play()
minus the output latency the audio device reports, so a tap lines up with what you hear.
"""
from __future__ import annotations

import time

import numpy as np


class Player:
    def __init__(self):
        self._sd = None
        self.error: str | None = None
        self._t_start: float | None = None
        self._length = 0.0
        self.latency = 0.0
        try:
            import sounddevice as sd

            self._sd = sd
        except Exception as exc:  # PortAudio missing etc.
            self.error = f"Audio playback is not available: {exc}"

    @property
    def available(self) -> bool:
        return self._sd is not None

    def play(self, data: np.ndarray, sample_rate: int) -> None:
        if not self._sd:
            return
        data = np.asarray(data, dtype=np.float32)
        peak = float(np.abs(data).max()) if data.size else 0.0
        if peak > 0.98:
            data = data * (0.98 / peak)
        try:
            self._sd.stop()
            self._sd.play(data, sample_rate)
            self._t_start = time.perf_counter()
            self._length = len(data) / sample_rate
            try:
                lat = self._sd.get_stream().latency
                self.latency = float(lat[1] if isinstance(lat, (tuple, list)) else lat)
            except Exception:
                self.latency = 0.0
        except Exception as exc:
            self.error = f"Playback failed: {exc}"
            self._t_start = None
            raise

    def position(self) -> float | None:
        """Seconds into the clip that is being heard now, or None when nothing is playing."""
        if self._t_start is None:
            return None
        pos = time.perf_counter() - self._t_start - self.latency
        if pos > self._length:
            self._t_start = None
            return None
        return max(0.0, pos)

    def stop(self) -> None:
        self._t_start = None
        if self._sd:
            try:
                self._sd.stop()
            except Exception:
                pass
