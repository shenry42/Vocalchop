import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def vocalchop_home(tmp_path, monkeypatch):
    """Every test gets its own empty Documents\\Vocalchop."""
    home = tmp_path / "Vocalchop"
    (home / "Acapellas").mkdir(parents=True)
    monkeypatch.setenv("VOCALCHOP_HOME", str(home))
    monkeypatch.setenv("VOCALCHOP_WORKERS", "1")
    return home
