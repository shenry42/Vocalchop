"""The slice map JSON (SPEC §2.1): one file per sample, read by the Max for Live device."""
from __future__ import annotations

import json

import numpy as np
from pathlib import Path

from .notes import Note

MAP_VERSION = 1
MAX_SLICES = 32               # SPEC §6
RENDER_RANGE = (-12, 12)      # SPEC §4


def build_slice_map(source: Path, sample_rate: int, tuning_offset_cents: float, notes: list[Note]) -> dict:
    """Map for up to MAX_SLICES notes, numbered 1.. in the order given."""
    chosen = notes[:MAX_SLICES]
    slices = []
    for i, n in enumerate(chosen, start=1):
        slices.append({
            "index": i,
            "start": round(n.start, 3),
            "end": round(n.end, 3),
            "note": int(n.note),
            "cents": int(n.cents),
            "confidence": int(n.confidence),
            "flags": list(n.flags),
            "keepAsSung": False,
            "gain": 0.0,
            "renderFolder": f"renders/slice_{i:02d}",
        })
    return {
        "version": MAP_VERSION,
        "source": Path(source).resolve().as_posix(),
        "sampleRate": int(sample_rate),
        "tuningOffsetCents": int(round(tuning_offset_cents)),
        "renderRange": list(RENDER_RANGE),
        "slices": slices,
    }


def build_slice_map_from_slices(source: Path, sample_rate: int, tuning_offset_cents: float, slices: list,
                                fade_ms: float = 3.0) -> dict:
    """Map for the editor's slices (chop.Slice), numbered 1.. in time order.

    Besides the SPEC §2.1 fields each slice carries `renderHash` (boundaries + source + render settings, for the
    Milestone 3 render cache) and the map carries `fadeMs` (the device applies the same 3 ms edge fades).
    """
    from .chop import render_hash

    if len(slices) > MAX_SLICES:
        raise ValueError(f"A slice map holds at most {MAX_SLICES} slices ({len(slices)} given)")
    out = []
    for i, sl in enumerate(sorted(slices, key=lambda x: x.start), start=1):
        flags = list(sl.flags) + (["note_set_by_hand"] if sl.hand_note is not None else [])
        out.append({
            "index": i,
            "start": round(sl.start, 6),
            "end": round(sl.end, 6),
            "note": int(sl.note),
            "cents": int(np.clip(sl.cents, -50, 50)),
            "confidence": int(sl.confidence),
            "flags": flags,
            "keepAsSung": bool(sl.keep_as_sung),
            "gain": round(float(sl.gain), 2),
            "renderFolder": f"renders/slice_{i:02d}",
            "renderHash": render_hash(source, sample_rate, sl),
        })
    return {
        "version": MAP_VERSION,
        "source": Path(source).resolve().as_posix(),
        "sampleRate": int(sample_rate),
        "tuningOffsetCents": int(round(tuning_offset_cents)),
        "renderRange": list(RENDER_RANGE),
        "fadeMs": float(fade_ms),
        "slices": out,
    }


SLICE_KEYS = ("index", "start", "end", "note", "cents", "confidence", "flags", "keepAsSung", "gain", "renderFolder")
TOP_KEYS = ("version", "source", "sampleRate", "tuningOffsetCents", "renderRange", "slices")


def validate_slice_map(m: dict) -> list[str]:
    """Problems found (empty list = valid)."""
    errs = []
    for k in TOP_KEYS:
        if k not in m:
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    if m["version"] != MAP_VERSION:
        errs.append("version must be 1")
    if not isinstance(m["tuningOffsetCents"], int) or not -50 <= m["tuningOffsetCents"] <= 50:
        errs.append("tuningOffsetCents must be a whole number from -50 to 50")
    if len(m["slices"]) > MAX_SLICES:
        errs.append(f"more than {MAX_SLICES} slices")
    last_end = -1.0
    for i, s in enumerate(m["slices"], start=1):
        for k in SLICE_KEYS:
            if k not in s:
                errs.append(f"slice {i}: missing '{k}'")
        if any(k not in s for k in SLICE_KEYS):
            continue
        if s["index"] != i:
            errs.append(f"slice {i}: index is {s['index']}")
        if not 0 <= s["start"] < s["end"]:
            errs.append(f"slice {i}: start/end out of order")
        if s["start"] < last_end - 1e-9:
            errs.append(f"slice {i}: overlaps the slice before")
        last_end = s["end"]
        if not isinstance(s["note"], int) or not 0 <= s["note"] <= 127:
            errs.append(f"slice {i}: note must be a MIDI number 0-127")
        if not isinstance(s["cents"], int) or not -50 <= s["cents"] <= 50:
            errs.append(f"slice {i}: cents must be -50..50")
        if not isinstance(s["confidence"], int) or not 0 <= s["confidence"] <= 100:
            errs.append(f"slice {i}: confidence must be 0..100")
    return errs


def write_slice_map(m: dict, path: Path) -> None:
    errs = validate_slice_map(m)
    if errs:
        raise ValueError("Slice map is not valid: " + "; ".join(errs))
    Path(path).write_text(json.dumps(m, indent=2), encoding="utf-8")


def read_slice_map(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
