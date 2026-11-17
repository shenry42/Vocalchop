"""Plays short clips through the default Windows audio output (shared mode, so Ableton can stay open)."""
from __future__ import annotations

import numpy as np


class Player:
    def __init__(self):
        self._sd = None
        self.error: str | None = None
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
        except Exception as exc:
            self.error = f"Playback failed: {exc}"
            raise

    def stop(self) -> None:
        if self._sd:
            try:
                self._sd.stop()
            except Exception:
                pass
