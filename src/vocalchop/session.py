"""Saving and reopening the editor's work for one song (one slice map per song, SPEC §2.1 and §3.4).

Save writes, in the song's project folder:
  slicemap.json       the slice map the device loads (up to 32 slices)
  editor.json         region, chop settings and every slice with its hand edits, so reopening restores them
  render_queue.json   the slices whose audio changed since the last save: Milestone 3's background render
                      picks these up (SPEC §3.4 "starts the Natural render for any slice that changed")
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__
from .chop import ChopSettings, Slice, SliceEditor
from .slicemap import build_slice_map_from_slices, read_slice_map, write_slice_map

EDITOR_VERSION = 1


@dataclass
class SaveReport:
    map_path: Path
    count: int
    changed: list[int]          # 1-based slice numbers whose audio changed since the last save


def editor_path(folder: Path) -> Path:
    return Path(folder) / "editor.json"


def save(result, editor: SliceEditor) -> SaveReport:
    problems = editor.problems()
    if problems:
        raise ValueError(" ".join(problems))
    folder = Path(result.folder)
    folder.mkdir(parents=True, exist_ok=True)
    map_path = folder / "slicemap.json"
    before: set[str] = set()
    if map_path.exists():
        try:
            before = {s.get("renderHash", "") for s in read_slice_map(map_path).get("slices", [])}
        except (OSError, ValueError):
            before = set()
    slices = sorted(editor.slices, key=lambda s: s.start)
    m = build_slice_map_from_slices(result.source, result.audio.sample_rate, result.tuning_offset_cents, slices,
                                    editor.settings.fade_ms)
    write_slice_map(m, map_path)
    changed = [s["index"] for s in m["slices"] if s["renderHash"] not in before]
    queue_path = folder / "render_queue.json"
    queue = []
    if queue_path.exists():
        try:
            queue = json.loads(queue_path.read_text(encoding="utf-8")).get("pending", [])
        except (OSError, ValueError):
            queue = []
    live = {s["renderHash"] for s in m["slices"]}
    pending = {q["renderHash"]: q for q in queue if q.get("renderHash") in live}
    for s in m["slices"]:
        if s["index"] in changed:
            pending[s["renderHash"]] = {k: s[k] for k in ("index", "start", "end", "renderHash", "renderFolder")}
    for s in m["slices"]:          # numbering may have moved since the slice was queued
        if s["renderHash"] in pending:
            pending[s["renderHash"]].update(index=s["index"], renderFolder=s["renderFolder"])
    queue_path.write_text(json.dumps({"note": "Slices waiting for the Natural render (Milestone 3).",
                                      "pending": sorted(pending.values(), key=lambda q: q["index"])}, indent=2),
                          encoding="utf-8")
    state = {
        "version": EDITOR_VERSION,
        "app": f"Vocal Chop Sampler {__version__}",
        "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": Path(result.source).resolve().as_posix(),
        "rangePreset": result.preset.key,
        "region": list(editor.region),
        "settings": asdict(editor.settings),
        "slices": [s.to_dict() for s in slices],
    }
    editor_path(folder).write_text(json.dumps(state, indent=2), encoding="utf-8")
    editor.dirty = False
    return SaveReport(map_path=map_path, count=len(slices), changed=changed)


def load(result, editor: SliceEditor) -> bool:
    """Restore the saved region, settings and slices for this song. False if there is nothing (valid) saved."""
    p = editor_path(result.folder)
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if d.get("version") != EDITOR_VERSION or d.get("source") != Path(result.source).resolve().as_posix():
        return False
    known = ChopSettings.__dataclass_fields__
    editor.settings = ChopSettings(**{k: v for k, v in d.get("settings", {}).items() if k in known})
    region = tuple(d.get("region") or (0.0, editor.ctx.duration))
    editor.set_slices([Slice.from_dict(s) for s in d.get("slices", [])], region=region)
    return True
