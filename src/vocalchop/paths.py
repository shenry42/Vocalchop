"""Where things live on disk (SPEC §2.2)."""
from __future__ import annotations

import os
import re
from pathlib import Path


def home_dir() -> Path:
    """Documents\\Vocalchop (or VOCALCHOP_HOME if set, used by the tests)."""
    env = os.environ.get("VOCALCHOP_HOME")
    if env:
        return Path(env)
    return Path.home() / "Documents" / "Vocalchop"


def acapellas_dir() -> Path:
    return home_dir() / "Acapellas"


def settings_path() -> Path:
    return home_dir() / "settings.json"


def check_results_path() -> Path:
    return home_dir() / "check_results.txt"


def project_name(source: Path) -> str:
    """Folder name for a source file: its name without extension, made safe for Windows."""
    name = re.sub(r'[<>:"/\\|?*]', "_", Path(source).stem).strip(" .")
    return name or "untitled"


def project_dir(source: Path) -> Path:
    return home_dir() / project_name(source)


AUDIO_EXTENSIONS = (".wav", ".aif", ".aiff", ".flac", ".mp3")


def is_audio_file(path: Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS
