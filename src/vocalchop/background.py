"""Background Natural rendering (SPEC §3.4 "starts the Natural render ... automatically, in the background", §4).

One BackgroundRenderer per app. Save calls request(project folder); a worker thread then

  1. reads the project's slicemap.json and loads the source audio
  2. reconcile(): renumbered slices keep their renders, removed ones are parked in renders/_unused
  3. renders every slice that isn't ready, in slice order, its 25 files spread over worker processes; each
     finished slice is moved into place at once (it becomes playable then), using the map as it is on disk at
     that moment, so saving again during a render is safe
  4. keeps render_queue.json up to date (pending / ready), for the device and for reopening the app

A new Save during a render restarts the pass after the current slice; if the current slice was removed or changed
by that Save it is cancelled straight away. Closing the app stops between files; whatever is unfinished is picked
up the next time that song is saved or opened.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import render
from .slicemap import read_slice_map


def default_workers() -> int:
    """Worker processes: all cores but one (the editor stays responsive). VOCALCHOP_WORKERS overrides (tests)."""
    env = os.environ.get("VOCALCHOP_WORKERS")
    if env:
        return max(1, int(env))
    return max(1, (os.cpu_count() or 2) - 1)


def write_queue(project: Path, slicemap: dict, current: int | None = None, error: str = "") -> dict:
    """render_queue.json: what is still to render and what is ready (slice numbers and hashes)."""
    project = Path(project)
    ready = render.ready_state(project, slicemap)
    pending = [{k: s[k] for k in ("index", "start", "end", "renderHash", "renderFolder")}
               for s in slicemap["slices"] if not ready[s["index"]]]
    d = {"note": "Natural render status (Milestone 3). A slice is playable when its renders/slice_XX/render.json "
                 "holds the slice's renderHash.",
         "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
         "ready": [i for i, ok in ready.items() if ok],
         "rendering": current,
         "pending": pending}
    if error:
        d["error"] = error
    tmp = project / "render_queue.json.part"
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, project / "render_queue.json")
    return d


@dataclass
class Event:
    """What the renderer tells the window (via on_event, from the worker thread)."""
    kind: str                     # "start" | "file" | "slice" | "idle" | "error"
    project: Path
    index: int | None = None      # slice number
    files: int = 0                # files done for this slice (of 25)
    done: int = 0                 # slices ready in this project
    total: int = 0                # slices in the map
    message: str = ""
    seconds: float = 0.0
    timings: list = field(default_factory=list)   # (slice seconds, render seconds) per slice rendered this pass


class BackgroundRenderer:
    def __init__(self, workers: int | None = None, on_event=None, use_processes: bool = True):
        self.workers = workers or default_workers()
        self.on_event = on_event or (lambda e: None)
        self.use_processes = use_processes
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._gen: dict[Path, int] = {}         # bumped by request()
        self._done_gen: dict[Path, int] = {}
        self._stop = False
        self._thread: threading.Thread | None = None
        self._pool = None
        self.busy = False

    # ---------------------------------------------------------------- called from the window
    def request(self, project: Path) -> None:
        project = Path(project).resolve()
        with self._lock:
            self._gen[project] = self._gen.get(project, 0) + 1
            if self._thread is None or not self._thread.is_alive():
                self._stop = False
                self._thread = threading.Thread(target=self._run, name="natural-render", daemon=True)
                self._thread.start()
        self._wake.set()

    def stop(self, wait: float = 10.0) -> None:
        self._stop = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(wait)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def wait_idle(self, timeout: float = 600.0) -> bool:
        """For tests and the check script: wait until every requested project is rendered."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                idle = not self.busy and all(self._done_gen.get(p) == g for p, g in self._gen.items())
            if idle:
                return True
            time.sleep(0.05)
        return False

    # ---------------------------------------------------------------- the worker thread
    def _next_project(self) -> Path | None:
        with self._lock:
            for p, g in self._gen.items():
                if self._done_gen.get(p) != g:
                    return p
        return None

    def _get_pool(self):
        if not self.use_processes:
            return None
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=self.workers)
        return self._pool

    def _run(self) -> None:
        while not self._stop:
            project = self._next_project()
            if project is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            with self._lock:
                gen = self._gen[project]
                self.busy = True
            try:
                self._render_project(project, gen)
            except Exception as exc:  # a broken map, a missing source, a full disk...
                self.on_event(Event("error", project, message=f"{exc}\n{traceback.format_exc(limit=3)}"))
                with self._lock:
                    self._done_gen[project] = gen   # don't spin; the next Save retries
            finally:
                with self._lock:
                    self.busy = False

    def _changed(self, project: Path, gen: int) -> bool:
        with self._lock:
            return self._stop or self._gen.get(project) != gen

    def _render_project(self, project: Path, gen: int) -> None:
        from .audio_io import load_audio

        map_path = project / "slicemap.json"
        if not map_path.exists():
            with self._lock:
                self._done_gen[project] = gen
            return
        m = read_slice_map(map_path)
        render._rmtree(project / "renders" / render.WORK_DIR)
        rep = render.reconcile(project, m)
        ready = render.ready_state(project, m)
        total = len(m["slices"])
        pending = [s for s in m["slices"] if not ready[s["index"]]]
        write_queue(project, m)
        self.on_event(Event("start", project, done=total - len(pending), total=total,
                            message=f"{len(pending)} to render" + (
                                f", {len(rep['moved']) + len(rep['restored'])} kept from before"
                                if rep["moved"] or rep["restored"] else "")))
        if not pending:
            with self._lock:
                self._done_gen[project] = gen
            self.on_event(Event("idle", project, done=total, total=total))
            return
        audio = load_audio(Path(m["source"]))
        if audio.sample_rate != m["sampleRate"]:
            raise ValueError(f"{Path(m['source']).name}: sample rate is {audio.sample_rate}, the map says "
                             f"{m['sampleRate']} (the file changed; open it and save again)")
        fade = float(m.get("fadeMs", 3.0))
        pool = self._get_pool()
        timings = []
        t_pass = time.perf_counter()
        for entry in pending:
            if self._changed(project, gen):
                return                                   # a newer Save: start the pass again
            h = entry["renderHash"]
            files = [0]

            def cancel() -> bool:
                if self._stop:
                    return True
                if self._changed(project, gen):          # still wanted by the newest map?
                    try:
                        now = read_slice_map(map_path)
                    except (OSError, ValueError):
                        return False
                    return h not in {s["renderHash"] for s in now["slices"]}
                return False

            def on_file(_st, e=entry):
                files[0] += 1
                self.on_event(Event("file", project, index=e["index"], files=files[0],
                                    done=sum(ready.values()), total=total))

            write_queue(project, m, current=entry["index"])
            try:
                res = render.render_to(project / "renders" / render.WORK_DIR / h, audio.data, audio.sample_rate,
                                       entry, fade, pool, cancel, on_file)
            except render.RenderCancelled:
                continue
            now = read_slice_map(map_path)
            folder = render.place(project, project / "renders" / render.WORK_DIR / h, now)
            ready = render.ready_state(project, now)
            timings.append((res.audio_seconds, res.seconds))
            write_queue(project, now)
            idx = next((s["index"] for s in now["slices"] if s["renderHash"] == h), entry["index"])
            self.on_event(Event("slice", project, index=idx, done=sum(ready.values()), total=len(now["slices"]),
                                seconds=res.seconds, message=folder, timings=list(timings)))
        render._rmtree(project / "renders" / render.WORK_DIR)
        if not self._changed(project, gen):
            with self._lock:
                self._done_gen[project] = gen
            self.on_event(Event("idle", project, done=sum(ready.values()), total=total,
                                seconds=time.perf_counter() - t_pass, timings=timings))
