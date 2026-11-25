"""Run and cancel one speech-fetch operation at a time."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
from uuid import uuid4


OPERATION = "spoken_player_names"
ACTIVE = {"queued", "running"}
COMPLETION_DISPLAY_SECONDS = 0.45


def visible_activity(job, now=None):
    """Keep verified completions visible until polling displays can read them."""
    now = time.monotonic() if now is None else now
    for deadline, activity in job.get("name_completions", ()):
        if now < deadline:
            return activity, True
    return job.get("name_activity", {}), False


class NameFetchJobs:
    def __init__(self, jobs, lock, activity):
        self.jobs = jobs
        self.lock = lock
        self.activity = activity
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="name-fetch")
        self.stops = {}
        self.closed = False

    def submit(self, data, run):
        character = data.get("character_id")
        mode = "fetch" if data.get("submit") is True else "coverage"
        scope = (data.get("provider_id"), data.get("coverage"), data.get("source_keys")) if mode == "coverage" else None
        with self.lock:
            if self.closed:
                raise ValueError("Player name fetching is shutting down.")
            for active in self.jobs.values():
                if (active["operation"] == OPERATION and active["status"] in ACTIVE
                        and active.get("character_id") == character and active["request_mode"] == mode
                        and active["coverage_scope"] == scope):
                    return dict(active)
            ident = uuid4().hex
            stop = Event()
            job = {"id": ident, "status": "queued", "operation": OPERATION,
                   "created": time.time(), "character_id": character,
                   "request_mode": mode, "coverage_scope": scope}
            self.stops[ident] = stop
            self.jobs[ident] = job
            self.pool.submit(self._work, ident, run, data, stop)
            return dict(job)

    def _update(self, ident, **values):
        with self.lock:
            self.jobs[ident].update(values)

    def _show_activity(self, ident, details):
        with self.lock:
            job = self.jobs[ident]
            job["name_activity"] = details
            if details.get("instance_progress") == 100:
                now = time.monotonic()
                pending = [(deadline, activity) for deadline, activity in job.get("name_completions", ())
                           if deadline > now]
                start = pending[-1][0] if pending else now
                job["name_completions"] = (*pending, (start + COMPLETION_DISPLAY_SECONDS, details))

    def _log(self, level, summary, data, **fields):
        try:
            self.activity.append(level, summary, {**data, "operation": OPERATION, **fields})
        except OSError:
            # Progress and job state must remain available if the journal cannot be written.
            pass

    def _work(self, ident, run, data, stop):
        self._update(ident, status="running")
        try:
            self._log("info", "Started " + OPERATION, data)
            result = run(
                data, lambda message: self._update(ident, message=message),
                should_stop=stop.is_set,
                on_progress=lambda ready, total: self._update(ident, progress={"ready": ready, "total": total}),
                on_activity=lambda details: self._show_activity(ident, details),
            )
            self._update(ident, status="complete", result=result, finished_at_ns=time.time_ns())
            asset_id = result.get("id") if isinstance(result, dict) else None
            self._log("info", "Completed " + OPERATION, data, asset_id=asset_id)
        except Exception as error:
            self._update(ident, status="failed", error=str(error), finished_at_ns=time.time_ns())
            self._log("error", OPERATION + ": " + str(error), data)
        finally:
            with self.lock:
                self.stops.pop(ident, None)

    def pause(self, ident):
        with self.lock:
            if ident not in self.jobs:
                raise ValueError("Unknown job.")
            stop = self.stops.get(ident)
            if stop is not None:
                stop.set()
            return dict(self.jobs[ident])

    def close(self):
        with self.lock:
            self.closed = True
            for stop in self.stops.values():
                stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
