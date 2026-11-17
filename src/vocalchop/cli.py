"""Command line: analyze one file or every file in the Acapellas folder (used by analyze_acapellas.bat)."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .notes import is_sustained
from .paths import acapellas_dir, home_dir, is_audio_file
from .presets import note_name
from .project import analyze_file, preset_for, remember_preset


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vocalchop analyze")
    ap.add_argument("paths", nargs="*", help="audio files or folders (default: Documents\\Vocalchop\\Acapellas)")
    ap.add_argument("--preset", help="male / female / instrument / custom:C2-G5 (default: the one saved per file)")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    targets: list[Path] = []
    for p in (args.paths or [str(acapellas_dir())]):
        p = Path(p)
        if p.is_dir():
            targets += sorted(f for f in p.iterdir() if f.is_file() and is_audio_file(f))
        elif p.is_file():
            targets.append(p)
        else:
            print(f"Not found: {p}")
    if not targets:
        print("No audio files found.")
        return 1

    lines = [f"Vocal Chop Sampler - analysis summary {time.strftime('%Y-%m-%d %H:%M')}", ""]
    failed = 0
    for src in targets:
        key = args.preset or preset_for(src)
        note = ""
        if key is None:
            key, note = "instrument", " (no voice type saved for this file, so the wide Instrument range was used)"
        print(f"Analyzing {src.name} [{key}]...", flush=True)
        try:
            r = analyze_file(src, key, use_cache=not args.no_cache)
        except Exception as exc:  # report and carry on with the next file
            failed += 1
            print(f"  FAILED: {exc}")
            lines += [f"== {src.name}", f"   FAILED: {exc}", ""]
            continue
        if args.preset:
            remember_preset(src, key)
        sus = [n for n in r.notes if is_sustained(n)]
        print(f"  {len(r.notes)} notes ({len(sus)} sustained), tuning offset {r.tuning_offset_cents:+.1f} c, "
              f"{r.seconds:.1f} s{' (cached pitch)' if r.from_cache else ''}")
        lines += [f"== {src.name}",
                  f"   range: {r.preset.label}{note}",
                  f"   tuning offset: {r.tuning_offset_cents:+.1f} cents",
                  f"   notes: {len(r.notes)}, sustained: {len(sus)}, "
                  f"glide: {sum('glide' in n.flags for n in r.notes)}, "
                  f"low confidence: {sum('low_confidence' in n.flags for n in r.notes)}",
                  f"   most common sustained notes: " + ", ".join(
                      f"{name} x{c}" for name, c in _top([note_name(n.note) for n in sus])),
                  f"   time: {r.seconds:.1f} s{' (cached pitch)' if r.from_cache else ''}",
                  f"   files in: {r.folder}"]
        lines += [f"   WARNING: {w}" for w in r.warnings] + [""]
        for w in r.warnings:
            print(f"  WARNING: {w}")
    out = home_dir() / "analysis_summary.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSummary written to {out}")
    return 1 if failed else 0


def _top(names: list[str], k: int = 8):
    from collections import Counter

    return Counter(names).most_common(k)


if __name__ == "__main__":
    sys.exit(main())
