"""MIDI keyboard / pad input for the editor (mido + python-rtmidi).

Note-ons arrive on rtmidi's own thread; they are passed to the window through a Qt signal (queued, so the
window handles them on its own thread).

On Windows a MIDI input can usually be open in only one program at a time. If Ableton is using the keyboard,
opening it here fails with a message: turn the keyboard's Track/Remote off in Live's Preferences > Link, Tempo &
MIDI (or close Live) while chopping.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


def backend_error() -> str | None:
    try:
        import mido  # noqa: F401
        import rtmidi  # noqa: F401
    except Exception as exc:
        return f"MIDI input is not available ({exc}). Run scripts\\setup.bat again."
    return None


def list_ports() -> list[str]:
    if backend_error():
        return []
    import mido

    try:
        return list(mido.get_input_names())
    except Exception:
        return []


class MidiInput(QObject):
    noteOn = Signal(int, int)          # note, velocity
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._port = None
        self.name: str | None = None

    def open(self, name: str) -> bool:
        self.close()
        err = backend_error()
        if err:
            self.status.emit(err)
            return False
        import mido

        try:
            self._port = mido.open_input(name, callback=self._on_message)
        except Exception as exc:
            self._port = None
            self.status.emit(f"Couldn't open '{name}' ({exc}). If Ableton is open and using this keyboard, turn "
                             f"its Track/Remote input off in Live's MIDI preferences, or close Live, then try again.")
            return False
        self.name = name
        self.status.emit(f"MIDI in: {name}")
        return True

    def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None
        self.name = None

    def _on_message(self, msg) -> None:
        if msg.type == "note_on" and msg.velocity > 0:
            self.noteOn.emit(int(msg.note), int(msg.velocity))
