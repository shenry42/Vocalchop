"""Sam's Pass/Fail marks for the Milestone 1 ear test.

Marks are kept per project in checks.json, and every change rewrites Documents\\Vocalchop\\check_results.txt, a
plain summary of all files that the next build chat reads.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .paths import check_results_path, home_dir
from .presets import note_name

FAIL_REASONS = ["", "octave too high", "octave too low", "a semitone too high", "a semitone too low",
                "other wrong note", "not one note (slide or noise)"]
# (Pass/Fail marks are optional since the built-in tuner was added: Sam can use them for his own tuner checks.)
GOAL_NOTES = 10  # SPEC §7 Milestone 1
GOAL_PASSES = 9


def checks_path(folder: Path) -> Path:
    return Path(folder) / "checks.json"


def load_checks(folder: Path) -> dict:
    p = checks_path(folder)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"marks": {}}


def set_mark(folder: Path, source: Path, note: dict, result: str | None, reason: str = "") -> dict:
    """result: 'pass', 'fail' or None (clears the mark). `note` holds index/start/end/note/cents/confidence."""
    data = load_checks(folder)
    data["source"] = Path(source).name
    key = f"{note['start']:.3f}"   # keyed by start time so a re-analysis with different numbering keeps marks
    if result is None:
        data["marks"].pop(key, None)
    else:
        data["marks"][key] = {
            "result": result, "reason": reason if result == "fail" else "",
            "index": note["index"], "start": round(note["start"], 3), "end": round(note["end"], 3),
            "note": note["note"], "cents": note["cents"], "confidence": note["confidence"],
            "time": time.strftime("%Y-%m-%d %H:%M"),
        }
    checks_path(folder).write_text(json.dumps(data, indent=2), encoding="utf-8")
    write_summary()
    return data


def counts(data: dict) -> tuple[int, int]:
    marks = data.get("marks", {}).values()
    return sum(m["result"] == "pass" for m in marks), sum(m["result"] == "fail" for m in marks)


def write_summary() -> Path:
    """Rewrite check_results.txt from every analyzed project: the built-in tuner's results (automatic) and any
    Pass/Fail marks Sam made."""
    home = home_dir()
    lines = [f"Vocal Chop Sampler - Milestone 1 note check (updated {time.strftime('%Y-%m-%d %H:%M')})",
             f"Goal: on 3 acapellas, at least {GOAL_PASSES} of {GOAL_NOTES} sustained notes match on a tuner.",
             "Built-in tuner = an independent spectral method (tuner.py), read automatically on every sustained note.",
             "'borderline' = sung near halfway between two notes; both names give the same pitch at 100% Correction.",
             ""]
    meeting = []
    folders = sorted({p.parent for p in home.glob("*/notes.json")} | {p.parent for p in home.glob("*/checks.json")})
    for folder in folders:
        info = {}
        nj = folder / "notes.json"
        if nj.exists():
            try:
                info = json.loads(nj.read_text(encoding="utf-8"))
            except ValueError:
                info = {}
        data = load_checks(folder)
        marks = sorted(data.get("marks", {}).values(), key=lambda m: m["start"])
        name = Path(info.get("source", "")).name or data.get("source", folder.name)
        lines.append(f"== {name}")
        if info:
            lines.append(f"   range preset {info.get('rangePreset')}, tuning offset {info.get('tuningOffsetCents')} c, "
                         f"{len(info.get('notes', []))} notes")
        tun = info.get("builtInTuner")
        if tun:
            cs, al = tun["checkSet"], tun["allSustained"]
            ok = cs["match"] + cs["borderline"]
            meets = cs["of"] >= GOAL_NOTES and ok >= GOAL_PASSES * cs["of"] / GOAL_NOTES
            if meets:
                meeting.append(name)
            lines.append(f"   built-in tuner, 10 check notes: {cs['match']} match, {cs['borderline']} borderline, "
                         f"{cs['different']} different" + (f", {cs['no reading']} no reading" if cs["no reading"] else "")
                         + (f"  -> MEETS GOAL ({ok}/{cs['of']})" if meets else f"  ({ok}/{cs['of']})"))
            lines.append(f"   built-in tuner, all {al['of']} sustained notes: {al['match']} match, "
                         f"{al['borderline']} borderline, {al['different']} different")
            for nd in info.get("notes", []):
                t = nd.get("tuner")
                if t and t["result"] in ("different", "no reading"):
                    lines.append(f"     different: #{nd['index']} at {nd['start']:.3f}s  app {nd['name']} {nd['cents']:+d}c,"
                                 f" tuner {t['name']} {t['cents']:+d}c ({t['centsApart']} c apart)"
                                 + (f"  [tuner_check #{nd['tunerCheck']}]" if nd.get("tunerCheck") else ""))
        if marks:
            p, f = counts(data)
            lines.append(f"   Sam's marks: {p + f} checked, {p} pass, {f} fail")
            for m in marks:
                r = "PASS" if m["result"] == "pass" else "FAIL"
                extra_txt = f" ({m['reason']})" if m.get("reason") else ""
                lines.append(f"     {r} #{m['index']:>3} {m['start']:8.3f}-{m['end']:8.3f}s  {note_name(m['note']):4} "
                             f"{m['cents']:+3d}c  conf {m['confidence']:3d}{extra_txt}")
        lines.append("")
    lines.append(f"Files meeting the goal (built-in tuner): {len(meeting)}" + (f" - {', '.join(meeting)}" if meeting else ""))
    out = check_results_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
