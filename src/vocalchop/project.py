"""Analyzing one source file into its project folder (Documents\\Vocalchop\\<name>\\).

Files written there:
  slicemap.json     the test slice map (SPEC §2.1), up to 32 notes
  notes.json        every detected note, with the same fields plus names and times
  notes.mid         the detected notes as MIDI (120 BPM)
  guide.wav         reference tones at the detected notes, lined up with the source
  tuner_check.wav   10 sustained notes spread across the song, each held 2 s, one every 3 s (read with a tuner)
  tuner_check.txt   the expected note for each of those 10
  pitch_cache.npz   the pitch track, so reopening the file is instant
  analysis_log.txt  what was run and what was found
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import __version__
from .audio_io import Audio, load_audio
from .exports import pick_check_notes, write_guide_wav, write_notes_midi, write_tuner_check
from .notes import Note, NoteAnalysis, NoteParams, analyze_notes, is_sustained
from .paths import project_dir, settings_path
from .pitch import ANALYSIS_SR, HOP_LENGTH, RESOLUTION, PitchTrack, to_analysis_rate, track_pitch
from .presets import RangePreset, live_note_name, note_name, preset_from_key
from .slicemap import build_slice_map, write_slice_map
from .tuner import TunerReading, compare, read_tuner

PITCH_CACHE_VERSION = 2   # bump when the pitch tracking changes, so old caches are ignored


# ---------------------------------------------------------------- settings (range preset per file)

def load_settings() -> dict:
    p = settings_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_settings(s: dict) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2), encoding="utf-8")


def preset_for(source: Path) -> str | None:
    return load_settings().get("voicePresets", {}).get(Path(source).name)


def remember_preset(source: Path, key: str) -> None:
    s = load_settings()
    s.setdefault("voicePresets", {})[Path(source).name] = key
    save_settings(s)


# ---------------------------------------------------------------- analysis

@dataclass
class AnalysisResult:
    source: Path
    audio: Audio
    preset: RangePreset
    track: PitchTrack
    analysis: NoteAnalysis
    folder: Path
    seconds: float
    from_cache: bool
    warnings: list[str] = field(default_factory=list)
    tuner: dict[int, tuple[TunerReading, str, float]] = field(default_factory=dict)  # sustained notes only

    @property
    def notes(self) -> list[Note]:
        return self.analysis.notes

    @property
    def tuning_offset_cents(self) -> float:
        return self.analysis.tuning_offset_cents


def _cache_key(source: Path, preset: RangePreset) -> str:
    st = source.stat()
    raw = f"{PITCH_CACHE_VERSION}|{source.resolve()}|{st.st_size}|{int(st.st_mtime)}|{preset.low}|{preset.high}"
    raw += f"|{ANALYSIS_SR}|{HOP_LENGTH}|{RESOLUTION}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _load_cache(path: Path, key: str) -> PitchTrack | None:
    if not path.exists():
        return None
    try:
        d = np.load(path, allow_pickle=False)
        if str(d["key"]) != key:
            return None
        return PitchTrack(midi=d["midi"], voiced_prob=d["vp"], rms=d["rms"], hop_seconds=float(d["hop"]))
    except Exception:
        return None


def range_warnings(analysis: NoteAnalysis, preset: RangePreset) -> list[str]:
    """Hints when the voice seems to run into the edge of the search range."""
    out = []
    notes = analysis.notes
    if len(notes) < 5:
        return out
    pitches = np.array([n.pitch for n in notes])
    if (pitches < preset.low + 0.5).mean() > 0.15:
        out.append(f"Many notes sit at the very bottom of the range ({note_name(preset.low)}). If they sound wrong, "
                   f"the voice may go lower: try a lower range preset or Custom.")
    if (pitches > preset.high - 0.5).mean() > 0.15:
        out.append(f"Many notes sit at the very top of the range ({note_name(preset.high)}). If they sound wrong, "
                   f"the voice may go higher: try a higher range preset or Custom.")
    return out


def analyze_file(source: Path | str, preset_key: str, progress: Callable[[str, float], None] | None = None,
                 use_cache: bool = True, workers: int | None = None, write_outputs: bool = True,
                 params: NoteParams | None = None) -> AnalysisResult:
    source = Path(source)
    preset = preset_from_key(preset_key)
    t0 = time.perf_counter()

    def report(msg, frac):
        if progress:
            progress(msg, frac)

    report("Loading audio", 0.0)
    audio = load_audio(source)
    folder = project_dir(source)
    folder.mkdir(parents=True, exist_ok=True)
    cache_path = folder / "pitch_cache.npz"
    key = _cache_key(source, preset)
    track = _load_cache(cache_path, key) if use_cache else None
    from_cache = track is not None
    if track is None:
        report("Preparing audio", 0.05)
        y = to_analysis_rate(audio.mono(), audio.sample_rate)
        report("Detecting pitch (pYIN)", 0.1)
        track = track_pitch(y, preset.low, preset.high, workers=workers,
                            progress=lambda f: report("Detecting pitch (pYIN)", 0.1 + 0.8 * f))
        np.savez_compressed(cache_path, key=np.array(key), midi=track.midi, vp=track.voiced_prob, rms=track.rms,
                            hop=np.array(track.hop_seconds))
    report("Finding notes", 0.92)
    analysis = analyze_notes(track, params)
    result = AnalysisResult(source=source, audio=audio, preset=preset, track=track, analysis=analysis, folder=folder,
                            seconds=time.perf_counter() - t0, from_cache=from_cache,
                            warnings=range_warnings(analysis, preset))
    report("Built-in tuner", 0.94)
    result.tuner = tuner_readings(result)
    if write_outputs:
        report("Writing files", 0.96)
        write_outputs_for(result)
    report("Done", 1.0)
    return result


def tuner_readings(result: AnalysisResult) -> dict[int, tuple[TunerReading, str, float]]:
    """The built-in tuner (tuner.py, an independent method) on every sustained note."""
    mono = result.audio.mono()
    out = {}
    for n in result.notes:
        if is_sustained(n):
            r = read_tuner(mono, result.audio.sample_rate, n, result.preset.low, result.preset.high)
            status, apart = compare(r, n, result.tuning_offset_cents)
            out[n.index] = (r, status, apart)
    return out


def tuner_summary(result: AnalysisResult) -> dict:
    picks = pick_check_notes(result.notes)
    def tally(idx):
        st = [result.tuner[i][1] for i in idx if i in result.tuner]
        return {k: st.count(k) for k in ("match", "borderline", "different", "no reading")} | {"of": len(st)}
    return {"checkSet": tally([n.index for n in picks]), "allSustained": tally(list(result.tuner))}


def notes_json(result: AnalysisResult) -> dict:
    p = NoteParams()
    picked = {n.index: k for k, n in enumerate(pick_check_notes(result.notes), start=1)}
    return {
        "app": f"Vocal Chop Sampler {__version__}",
        "source": result.source.resolve().as_posix(),
        "sampleRate": result.audio.sample_rate,
        "duration": round(result.audio.duration, 3),
        "rangePreset": result.preset.key,
        "tuningOffsetCents": round(result.tuning_offset_cents, 1),
        "notes": [{
            "index": n.index, "start": round(n.start, 3), "end": round(n.end, 3), "note": n.note,
            "name": note_name(n.note), "liveName": live_note_name(n.note), "cents": n.cents,
            "confidence": n.confidence, "flags": n.flags, "sustained": is_sustained(n, p),
            "spreadSemitones": round(n.spread, 2), "tunerCheck": picked.get(n.index),
            **({"tuner": {"note": result.tuner[n.index][0].note, "name": note_name(result.tuner[n.index][0].note)
                          if result.tuner[n.index][0].ok else None,
                          "cents": result.tuner[n.index][0].cents, "result": result.tuner[n.index][1],
                          "centsApart": None if result.tuner[n.index][2] != result.tuner[n.index][2]
                          else round(result.tuner[n.index][2])}} if n.index in result.tuner else {}),
        } for n in result.notes],
        "builtInTuner": tuner_summary(result),
    }


def export_slice_map(result: AnalysisResult, first_index: int = 1) -> Path:
    """Slice map of up to 32 notes starting at note `first_index`."""
    chosen = [n for n in result.notes if n.index >= first_index]
    m = build_slice_map(result.source, result.audio.sample_rate, result.tuning_offset_cents, chosen)
    path = result.folder / "slicemap.json"
    write_slice_map(m, path)
    return path


def write_outputs_for(result: AnalysisResult) -> None:
    f = result.folder
    export_slice_map(result)
    (f / "notes.json").write_text(json.dumps(notes_json(result), indent=2), encoding="utf-8")
    write_notes_midi(f / "notes.mid", result.notes, result.source.stem)
    write_guide_wav(f / "guide.wav", result.notes, result.tuning_offset_cents, result.audio.duration,
                    result.audio.sample_rate)
    picks = pick_check_notes(result.notes)
    extra = [n for n in result.notes if result.tuner.get(n.index, (None, ""))[1] == "different" and n not in picks]
    write_tuner_check(f, result.audio.mono(), result.audio.sample_rate, picks, result.source.name, extra=extra)
    sus = [n for n in result.notes if is_sustained(n)]
    lines = [
        f"Vocal Chop Sampler {__version__} - analysis {time.strftime('%Y-%m-%d %H:%M')}",
        f"Source: {result.source}",
        f"Audio: {result.audio.sample_rate} Hz, {result.audio.channels} ch, {result.audio.duration:.1f} s",
        f"Range preset: {result.preset.label}",
        f"Pitch: {'from cache' if result.from_cache else 'pYIN'}; total {result.seconds:.1f} s",
        f"Tuning offset: {result.tuning_offset_cents:+.1f} cents",
        f"Notes: {len(result.notes)}; sustained (>=250 ms, confidence >=60, no glide): {len(sus)}; "
        f"glide: {sum('glide' in n.flags for n in result.notes)}; "
        f"low confidence: {sum('low_confidence' in n.flags for n in result.notes)}",
    ] + [f"Warning: {w}" for w in result.warnings]
    ts = tuner_summary(result)
    for label, key in (("10 check notes", "checkSet"), ("all sustained notes", "allSustained")):
        t = ts[key]
        lines.append(f"Built-in tuner, {label}: {t['match']} match, {t['borderline']} borderline, "
                     f"{t['different']} different, {t['no reading']} no reading (of {t['of']})")
    (f / "analysis_log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    from .checks import write_summary

    write_summary()
