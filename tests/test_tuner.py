"""The built-in tuner (an independent spectral method) against known pitches."""
import numpy as np
import pytest

from synth import SR, SungNote, sing
from vocalchop.notes import Note
from vocalchop.tuner import TunerReading, compare, read_tuner


def note_at(start=0.3, length=0.6):
    return Note(index=1, start=start, end=start + length, pitch=0.0)


@pytest.mark.parametrize("midi", [36, 40, 45, 52, 57, 64, 69, 76, 83])
def test_steady_notes_read_within_3_cents(midi):
    y = sing([SungNote(midi + 0.12, 0.6)])
    r = read_tuner(y, SR, note_at(), 36, 84)
    assert r.note == midi and abs(r.cents - 12) <= 3


@pytest.mark.parametrize("midi", [40, 57, 64, 76])
def test_vibrato_keeps_the_note(midi):
    y = sing([SungNote(midi, 0.8)], vibrato_semitones=0.5)
    r = read_tuner(y, SR, note_at(length=0.8), 36, 84)
    assert r.note == midi


def test_weak_fundamental_is_not_read_an_octave_off():
    # a 'telephone' voice: the fundamental removed, harmonics 2..10 kept
    t = np.arange(int(0.6 * SR)) / SR
    y = sum(np.sin(2 * np.pi * h * 110.0 * t) / h for h in range(2, 11)).astype(np.float32)
    y = np.concatenate([np.zeros(int(0.3 * SR), np.float32), y])
    r = read_tuner(y, SR, note_at(), 36, 84)
    assert r.note == 45


def test_silence_has_no_reading():
    y = np.zeros(SR, dtype=np.float32)
    assert not read_tuner(y, SR, note_at(), 36, 84).ok


def test_compare_verdicts():
    n = Note(index=1, start=0, end=1, pitch=0, note=67, cents=-25)       # app: G4 -25

    def reading(midi):
        return TunerReading(hz=440 * 2 ** ((midi - 69) / 12), midi=midi, note=round(midi), cents=0, clarity=1)

    assert compare(reading(66.9), n, 0)[0] == "match"
    assert compare(reading(66.43), n, 0)[0] == "borderline"               # F#4 +43: both near the halfway point
    assert compare(reading(66.0), n, 0)[0] == "different"                 # a clear F#4
    assert compare(reading(67.3), n, 20)[0] == "match"                   # the song's offset is removed first
    assert compare(TunerReading(0, 0, 0, 0, 0), n, 0)[0] == "no reading"
