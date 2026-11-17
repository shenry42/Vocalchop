"""Milestone 2 automatic check (SPEC §7): the three "done when" items, measured without listening.

    python -m vocalchop check-m2            (scripts\\check_m2.bat)

For every song in Documents\\Vocalchop\\Acapellas (with its saved range preset) the whole song is chopped and
checked; results go to Documents\\Vocalchop\\m2_check_results.txt.

1. Pitch mode lands cuts on note changes
   a. every legato cut (two slices touching) is a real note change: the built-in tuner (a spectral method,
      independent of pYIN) reads both slices and they differ by at least half a semitone. Goal: 90%.
   b. no slice hides a note change: the tuner reads the first and second half of each slice of 300 ms or more;
      a change of a semitone or more inside a slice not flagged Glide is a missed cut. Goal: at most 10%.
      The goal counts slices without vibrato (notes.has_vibrato). On a vibrato note each half holds only a cycle
      or so of the wobble and the two readings often differ with no note change at all: on single synthetic
      notes the test false-alarms on 28 of 131 with detected vibrato and 16 of 148 without (those 16 are fast,
      wide vibratos the detector missed; steady notes and scoops gave none). Vibrato slices are still listed.
2. No clicks at slice edges: every slice of the Pitch, Silence gate and Equal chops is made as the device will
   play it (3 ms fades); its first and last sample must be 0 in every channel and its click score (chop.click_score)
   below 2. Goal: 0 clicks. For comparison the same slices are also cut 0.7 ms off the zero crossing with no fade,
   to show the detector does find clicks when they are there.
3. Glide and low-confidence flags on the right slices
   a. every flag matches its definition (Glide = pitch spread over 1.5 st, low confidence = under 60). Goal: 100%.
   b. Glide confirmed independently: the tuner reads the first and last third of each Glide slice of 200 ms or
      more; they should be at least a semitone apart. Goal: 80%. Borderline (Milestone 3): a Glide flag within
      0.1 st of the 1.5 st line whose ends the tuner reads 0.75-1 st apart is a fall or rise of about a semitone,
      the case the line is drawn through; it is listed but counts neither way (as M1's "borderline" notes).
   c. low confidence, for information: how often the tuner names the same note on low-confidence slices vs the
      others (low confidence should mean the app's note is less certain).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .audition import _Span
from .chop import CLICK_RATIO, ChopSettings, Slice, SongContext, chop, click_score, slice_audio
from .notes import NoteParams, has_vibrato
from .paths import acapellas_dir, home_dir, is_audio_file
from .presets import note_name
from .project import analyze_file, preset_for
from .tuner import compare, read_tuner

GOAL_CUTS = 0.90
GOAL_MISSED = 0.10
GOAL_GLIDE = 0.80
CHANGE_ST = 0.5        # 1a: a confirmed note change
INSIDE_ST = 1.0        # 1b: a change inside a slice
GLIDE_ST = 1.0         # 3b
GLIDE_BORDER_SPREAD = 0.1   # 3b borderline: spread within this of the Glide line...
GLIDE_BORDER_ST = 0.75      # ...and the tuner's ends at least this far apart


@dataclass
class FileCheck:
    name: str
    preset: str
    slices: int = 0
    legato_cuts: int = 0
    cuts_readable: int = 0
    cuts_confirmed: int = 0
    cut_failures: list[str] = field(default_factory=list)
    halves_checked: int = 0
    missed: list[str] = field(default_factory=list)
    wide_vibrato: list[str] = field(default_factory=list)   # vibrato slices whose halves differ (not counted)
    vibrato_checked: int = 0
    edges_checked: int = 0
    clicks: list[str] = field(default_factory=list)
    nonzero_ends: int = 0
    raw_clicks: int = 0
    flag_mismatch: list[str] = field(default_factory=list)
    glides: int = 0
    glides_readable: int = 0
    glides_confirmed: int = 0
    glide_failures: list[str] = field(default_factory=list)
    glide_borderline: list[str] = field(default_factory=list)
    low_conf: int = 0
    agree_low: float = float("nan")
    agree_ok: float = float("nan")

    @property
    def cut_rate(self) -> float:
        return self.cuts_confirmed / self.cuts_readable if self.cuts_readable else 1.0

    @property
    def missed_rate(self) -> float:
        return len(self.missed) / self.halves_checked if self.halves_checked else 0.0

    @property
    def glide_rate(self) -> float:
        return self.glides_confirmed / self.glides_readable if self.glides_readable else 1.0

    def goals(self) -> dict[str, bool]:
        return {"cuts": self.cut_rate >= GOAL_CUTS and self.missed_rate <= GOAL_MISSED,
                "clicks": not self.clicks and self.nonzero_ends == 0,
                "flags": not self.flag_mismatch and self.glide_rate >= GOAL_GLIDE}


def _t(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:06.3f}"


def _read(ctx: SongContext, lo: int, hi: int, a: float, b: float):
    r = read_tuner(ctx.mono, ctx.sample_rate, _Span(a, b), lo, hi)
    return r if r.ok else None


def check_song(source: Path, preset_key: str) -> FileCheck:
    r = analyze_file(source, preset_key, write_outputs=False)
    ctx = SongContext.from_result(r)
    lo, hi = r.preset.low, r.preset.high
    fc = FileCheck(name=source.name, preset=r.preset.label)
    whole = (0.0, ctx.duration)
    sl = chop(ctx, whole, ChopSettings(mode="pitch"))
    fc.slices = len(sl)
    p = NoteParams()
    readings: dict[int, object] = {}

    def reading(i: int):
        if i not in readings:
            s = sl[i]
            readings[i] = _read(ctx, lo, hi, s.start, s.end) if s.length >= 0.1 else None
        return readings[i]

    # 1a. legato cuts are real note changes
    for i in range(len(sl) - 1):
        if abs(sl[i].end - sl[i + 1].start) > 1e-6:
            continue
        fc.legato_cuts += 1
        ra, rb = reading(i), reading(i + 1)
        if ra is None or rb is None:
            continue
        fc.cuts_readable += 1
        if abs(rb.midi - ra.midi) >= CHANGE_ST:
            fc.cuts_confirmed += 1
        else:
            fc.cut_failures.append(f"cut at {_t(sl[i + 1].start)} (slices {i + 1}/{i + 2}): tuner reads "
                                   f"{note_name(ra.note)} {ra.cents:+d} c then {note_name(rb.note)} {rb.cents:+d} c")
    # 1b. no hidden note change inside a slice
    for i, s in enumerate(sl):
        if s.length < 0.3 or "glide" in s.flags:
            continue
        hop = ctx.track.hop_seconds
        vib = has_vibrato(ctx.track, ctx.voiced, int(round(s.start / hop)), int(round(s.end / hop)))
        mid = (s.start + s.end) / 2
        ra, rb = _read(ctx, lo, hi, s.start, mid), _read(ctx, lo, hi, mid, s.end)
        if ra is None or rb is None:
            continue
        if vib:
            fc.vibrato_checked += 1
            if abs(rb.midi - ra.midi) >= INSIDE_ST:
                fc.wide_vibrato.append(f"slice {i + 1} at {_t(s.start)} ({s.length * 1000:.0f} ms, "
                                       f"{note_name(s.note)}): halves {note_name(ra.note)} {ra.cents:+d} c / "
                                       f"{note_name(rb.note)} {rb.cents:+d} c")
            continue
        fc.halves_checked += 1
        if abs(rb.midi - ra.midi) >= INSIDE_ST:
            fc.missed.append(f"slice {i + 1} at {_t(s.start)} ({s.length * 1000:.0f} ms, {note_name(s.note)}): first "
                             f"half {note_name(ra.note)} {ra.cents:+d} c, second half {note_name(rb.note)} "
                             f"{rb.cents:+d} c")
    # 2. clicks: Pitch, Silence gate and Equal chops, as the device plays them, and the same edges cut raw
    data = r.audio.data
    sr = r.audio.sample_rate
    raw_settings = ChopSettings(snap_ms=0.0, fade_ms=0.0, preroll_ms=0.0)
    for mode in ("pitch", "silence", "equal"):
        cs = ChopSettings(mode=mode, equal_count=32)
        for s in (sl if mode == "pitch" else chop(ctx, whole, cs)):
            x = slice_audio(data, sr, s, cs.fade_ms)
            fc.edges_checked += 2
            if np.any(x[0] != 0) or np.any(x[-1] != 0):
                fc.nonzero_ends += 1
            a, b = click_score(x, sr)
            if max(a, b) >= CLICK_RATIO:
                fc.clicks.append(f"{mode} slice at {_t(s.start)}: click score start {a:.1f}, end {b:.1f}")
            raw = slice_audio(data, sr, Slice(s.start + 0.0007, s.end - 0.0007), raw_settings.fade_ms)
            fc.raw_clicks += sum(v >= CLICK_RATIO for v in click_score(raw, sr))
    # 3a. flags match their definitions
    for i, s in enumerate(sl):
        if not s.has_pitch:
            if "low_confidence" not in s.flags:
                fc.flag_mismatch.append(f"slice {i + 1}: no pitch but not flagged low confidence")
            continue
        if ("glide" in s.flags) != (s.spread > p.glide_semitones):
            fc.flag_mismatch.append(f"slice {i + 1}: glide flag {('glide' in s.flags)}, spread {s.spread:.2f}")
        if ("low_confidence" in s.flags) != (s.confidence < p.low_confidence):
            fc.flag_mismatch.append(f"slice {i + 1}: low-confidence flag vs confidence {s.confidence}")
    # 3b. glides confirmed by the tuner (first vs last third)
    for i, s in enumerate(sl):
        if "glide" not in s.flags:
            continue
        fc.glides += 1
        if s.length < 0.2:
            continue
        third = s.length / 3
        ra, rb = _read(ctx, lo, hi, s.start, s.start + third), _read(ctx, lo, hi, s.end - third, s.end)
        if ra is None or rb is None:
            continue
        apart = abs(rb.midi - ra.midi)
        if apart < GLIDE_ST and s.spread <= p.glide_semitones + GLIDE_BORDER_SPREAD and apart >= GLIDE_BORDER_ST:
            fc.glide_borderline.append(f"slice {i + 1} at {_t(s.start)} ({s.length * 1000:.0f} ms, spread "
                                       f"{s.spread:.2f} st): tuner ends {apart:.2f} st apart")
            continue
        fc.glides_readable += 1
        if apart >= GLIDE_ST:
            fc.glides_confirmed += 1
        else:
            fc.glide_failures.append(f"slice {i + 1} at {_t(s.start)} ({s.length * 1000:.0f} ms, spread "
                                     f"{s.spread:.2f} st): tuner start {note_name(ra.note)} {ra.cents:+d} c, end "
                                     f"{note_name(rb.note)} {rb.cents:+d} c")
    # 3c. low confidence vs agreement with the tuner
    agree = {True: [], False: []}
    for i, s in enumerate(sl):
        rd = reading(i)
        if rd is None or not s.has_pitch:
            continue
        status, _ = compare(rd, s, r.tuning_offset_cents)
        agree["low_confidence" in s.flags].append(status in ("match", "borderline"))
    fc.low_conf = sum("low_confidence" in s.flags for s in sl)
    fc.agree_low = float(np.mean(agree[True])) if agree[True] else float("nan")
    fc.agree_ok = float(np.mean(agree[False])) if agree[False] else float("nan")
    return fc


def report(checks: list[FileCheck], seconds: float) -> str:
    L = [f"Vocal Chop Sampler - Milestone 2 check ({time.strftime('%Y-%m-%d %H:%M')}, {seconds:.0f} s)",
         "Everything here is measured automatically; the built-in tuner is independent of the pitch analysis.",
         f"Goals: 1) >= {GOAL_CUTS:.0%} of legato cuts confirmed as note changes and <= {GOAL_MISSED:.0%} of slices "
         f"hiding a change; 2) 0 clicks; 3) flags match their definitions and >= {GOAL_GLIDE:.0%} of Glide flags "
         f"confirmed.", ""]
    for c in checks:
        g = c.goals()
        L += [f"== {c.name}", f"   range {c.preset}; whole song chopped in Pitch mode: {c.slices} slices"]
        L.append(f"   1. cuts on note changes: {'MET' if g['cuts'] else 'NOT MET'}")
        L.append(f"      a. legato cuts confirmed by the tuner: {c.cuts_confirmed} of {c.cuts_readable} readable "
                 f"({c.cut_rate:.0%}); {c.legato_cuts} legato cuts in all (the rest are where the voice stops)")
        L += [f"         not confirmed: {x}" for x in c.cut_failures[:12]]
        L.append(f"      b. slices without vibrato hiding a note change: {len(c.missed)} of {c.halves_checked} checked "
                 f"({c.missed_rate:.0%})")
        L += [f"         {x}" for x in c.missed[:12]]
        if c.vibrato_checked:
            L.append(f"         vibrato slices (not counted, the halves test false-alarms on vibrato): halves differ "
                     f"on {len(c.wide_vibrato)} of {c.vibrato_checked}")
            L += [f"           {x}" for x in c.wide_vibrato[:12]]
        L.append(f"   2. clicks: {'MET' if g['clicks'] else 'NOT MET'} - {len(c.clicks)} clicks and {c.nonzero_ends} "
                 f"slices not ending on 0, in {c.edges_checked} edges (Pitch, Silence gate, Equal 32)")
        L.append(f"      for comparison, the same slices cut 0.7 ms away from the zero crossing with no fade: {c.raw_clicks} edges would click")
        L += [f"         {x}" for x in c.clicks[:12]]
        L.append(f"   3. flags: {'MET' if g['flags'] else 'NOT MET'}")
        L.append(f"      a. flags not matching their definition: {len(c.flag_mismatch)}")
        L += [f"         {x}" for x in c.flag_mismatch[:12]]
        L.append(f"      b. Glide flags confirmed by the tuner: {c.glides_confirmed} of {c.glides_readable} readable "
                 f"({c.glide_rate:.0%}); {c.glides} Glide slices in all" if c.glides_readable else
                 f"      b. Glide flags confirmed by the tuner: none to judge; {c.glides} Glide slices in all"
                 + (f", {len(c.glide_borderline)} borderline" if c.glide_borderline else ""))
        L += [f"         not confirmed: {x}" for x in c.glide_failures[:12]]
        L += [f"         borderline (not counted): {x}" for x in c.glide_borderline[:12]]
        L.append(f"      c. {c.low_conf} low-confidence slices; the tuner names the same note on {c.agree_low:.0%} "
                 f"of them vs {c.agree_ok:.0%} of the confident ones")
        L.append("")
    met = [c.name for c in checks if all(c.goals().values())]
    L.append(f"Files meeting all three goals: {len(met)} of {len(checks)}" + (f" - {', '.join(met)}" if met else ""))
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    folder = acapellas_dir()
    files = sorted(f for f in folder.iterdir() if f.is_file() and is_audio_file(f)) if folder.exists() else []
    if not files:
        print(f"No audio files in {folder}")
        return 1
    t0 = time.perf_counter()
    checks = []
    for f in files:
        key = preset_for(f) or "instrument"
        print(f"Checking {f.name} [{key}]...", flush=True)
        checks.append(check_song(f, key))
        g = checks[-1].goals()
        print("  " + ", ".join(f"{k}: {'met' if v else 'NOT met'}" for k, v in g.items()), flush=True)
    text = report(checks, time.perf_counter() - t0)
    out = home_dir() / "m2_check_results.txt"
    out.write_text(text, encoding="utf-8")
    print(f"\nResults written to {out}")
    return 0
