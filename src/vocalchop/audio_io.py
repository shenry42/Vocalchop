"""Loading WAV / AIFF / FLAC / MP3 (SPEC §3.1)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .paths import AUDIO_EXTENSIONS


@dataclass
class Audio:
    path: Path
    data: np.ndarray      # float32, shape (frames, channels)
    sample_rate: int

    @property
    def channels(self) -> int:
        return self.data.shape[1]

    @property
    def duration(self) -> float:
        return self.data.shape[0] / self.sample_rate

    def mono(self) -> np.ndarray:
        return self.data.mean(axis=1).astype(np.float32)


class AudioLoadError(Exception):
    pass


def load_audio(path: Path | str) -> Audio:
    path = Path(path)
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise AudioLoadError(f"{path.name}: not a WAV, AIFF, FLAC or MP3 file")
    if not path.exists():
        raise AudioLoadError(f"{path}: file not found")
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as exc:  # soundfile raises several types
        raise AudioLoadError(f"{path.name}: could not read the audio ({exc})") from exc
    if data.shape[0] == 0:
        raise AudioLoadError(f"{path.name}: the file has no audio")
    return Audio(path=path, data=data, sample_rate=int(sr))
