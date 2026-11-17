"""Checks that everything setup.bat installed works. Output goes to setup_log.txt."""
import sys
import tempfile
from pathlib import Path

ok = True


def check(name, fn):
    global ok
    try:
        info = fn()
        print(f"OK   {name}{': ' + str(info) if info else ''}")
    except Exception as exc:
        ok = False
        print(f"FAIL {name}: {exc}")


def _mp3():
    import numpy as np
    import soundfile as sf

    p = Path(tempfile.gettempdir()) / "vocalchop_mp3_check.mp3"
    sf.write(p, (0.1 * np.sin(np.arange(44100) * 0.06)).astype("float32"), 44100, format="MP3")
    y, _ = sf.read(p)
    p.unlink(missing_ok=True)
    return f"MP3 read/write works (libsndfile {sf.__libsndfile_version__})"


def _pyin():
    import librosa
    import numpy as np

    y = np.sin(2 * np.pi * 440 * np.arange(22050) / 22050).astype("float32")
    f0, _, _ = librosa.pyin(y, fmin=100, fmax=1000, sr=22050)
    return f"librosa {librosa.__version__}, 440 Hz test -> {np.nanmedian(f0):.1f} Hz"


def _qt():
    import PySide6

    return f"PySide6 {PySide6.__version__}"


def _audio():
    import sounddevice as sd

    d = sd.query_devices(kind="output")
    return f"default output: {d['name']}"


print(f"Python {sys.version.split()[0]}")
check("pitch detection", _pyin)
check("audio files", _mp3)
check("window toolkit", _qt)
check("audio output", _audio)
sys.exit(0 if ok else 1)
