# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background jobs: one per action, with a log, a status and a history on disk."""

from __future__ import annotations

import asyncio
import contextvars
import datetime
import json
import logging
import traceback
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

_logger = logging.getLogger(__name__)

QUEUED, RUNNING, SUCCESS, FAILED, INTERRUPTED, STOPPED = (
    "queued",
    "running",
    "success",
    "failed",
    "interrupted",
    # an administrator stopped it (a job that hangs or loops)
    "stopped",
)
FINISHED = (SUCCESS, FAILED, INTERRUPTED, STOPPED)
# the ids of the jobs the request being served started (app.py: its entry in
# the access log names them)
STARTED: contextvars.ContextVar[list | None] = contextvars.ContextVar("omniacli_jobs_started", default=None)


class JobFailed(Exception):
    """An action failed for a reason it already explained in the job log."""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    title: str
    instances: list[str]
    source: str
    status: str = QUEUED
    created: str = field(default_factory=_now)
    started: str | None = None
    finished: str | None = None
    error: str | None = None
    lines: list[str] = field(default_factory=list)
    # what the action produced, e.g. {"download": url} for a backup
    result: dict = field(default_factory=dict)
    # the workspace of a job on an instance that may not be its workspace's
    # server before or after (an instance created or removed): its users see
    # the job by it
    workspace: str = ""
    # what the action tells the watchers of its end (deploys.py: the backup made
    # before an update, the deploy gone back to); not written in the job's file
    notes: dict = field(default_factory=dict, repr=False, compare=False)

    def log(self, text: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        for line in str(text).splitlines() or [""]:
            self.lines.append(f"{stamp} {line}")

    def warn(self, text: str) -> None:
        """What did not come though the job's main work did (a certificate,
        names left out): in its log, and in result["warnings"], so the job
        ends "success with warnings". Kept in result: an older OmniaCLI reads
        the job file and the job API as before (its status stays success)."""
        self.log(f"WARNING: {text}")
        self.result.setdefault("warnings", []).append(str(text))

    @property
    def warnings(self) -> list[str]:
        found = self.result.get("warnings") if isinstance(self.result, dict) else None
        return [str(w) for w in found] if isinstance(found, list) else []

    @property
    def outcome(self) -> str:
        """The status as the pages show it: success with warnings is "warned"."""
        status = self.notes.get("status", self.status)  # while the watchers of its end run (JobManager._run)
        return "warned" if status == SUCCESS and self.warnings else status

    @property
    def servers(self) -> list[str]:
        """The servers of the instances (server/role) the job ran on."""
        return list(dict.fromkeys(key.split("/", 1)[0] for key in self.instances))

    @property
    def finished_flag(self) -> bool:
        return self.status in FINISHED

    @property
    def duration(self) -> str:
        """How long it ran: from its start to its end, or to now while it
        runs; empty when it never started."""
        if not self.started:
            return ""
        start = datetime.datetime.fromisoformat(self.started)
        end = datetime.datetime.fromisoformat(self.finished) if self.finished else datetime.datetime.now()
        return duration_text((end - start).total_seconds())


def duration_text(seconds: float) -> str:
    """12 s, 3 min 05 s, 1 h 02 min."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {seconds:02d} s"
    return f"{seconds} s"


def filter_jobs(jobs: list[Job], server: str = "", query: str = "") -> list[Job]:
    """The jobs of a server and containing every word of query (title,
    started by, error, server/role), case insensitive."""
    if server:
        jobs = [j for j in jobs if server in j.servers]
    words = query.lower().split()
    if words:
        def text(job):
            return " ".join([job.title, job.source, job.error or "", *job.instances]).lower()
        jobs = [j for j in jobs if all(w in text(j) for w in words)]
    return jobs


class JobManager:
    def __init__(self, jobs_dir: Path, keep: int = 500):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.keep = keep
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        # the task of each job not finished, and who asked to stop it
        self._running: dict[str, asyncio.Task] = {}
        self._stop_by: dict[str, str] = {}
        # called with each job that ends (the access log: its outcome)
        self.listeners: list[Callable[[Job], None]] = []
        # awaited when a job starts (its locks held) and when it ends, before
        # its locks are released: begin(job), end(job) (deploys.Recorder)
        self.watchers: list = []
        self._load()

    def _load(self) -> None:
        files = sorted(self.jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in files[-self.keep :]:
            try:
                job = Job(**json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, TypeError) as exc:
                _logger.warning("Skipping unreadable job file %s: %s", path, exc)
                continue
            if not job.finished_flag:
                # OmniaCLI stopped while the job was running
                job.status = INTERRUPTED
                job.log("OmniaCLI was stopped before this job finished")
                self._save(job)
            self.jobs[job.id] = job

    def _save(self, job: Job) -> None:
        path = self.jobs_dir / f"{job.id}.json"
        tmp = path.with_suffix(".tmp")
        data = asdict(job)
        if not data["workspace"]:
            # the file as before: an older OmniaCLI still reads it
            del data["workspace"]
        del data["notes"]
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def is_busy(self, key: str) -> bool:
        return self.lock(key).locked()

    def running_on(self, key: str) -> Job | None:
        """The job running on the instance key, None when none is."""
        return next((j for j in reversed(self.jobs.values()) if j.status == RUNNING and key in j.instances), None)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def recent(self, limit: int = 100) -> list[Job]:
        return list(reversed(self.jobs.values()))[:limit]

    def submit(
        self,
        title: str,
        instances: list[str],
        source: str,
        action: Callable[[Job], Awaitable[None]],
        workspace: str = "",
    ) -> Job:
        job = Job(
            id=datetime.datetime.now().strftime("%Y%m%d-%H%M%S-")
            + uuid.uuid4().hex[:6],
            title=title,
            instances=sorted(instances),
            source=source,
            workspace=workspace,
        )
        self.jobs[job.id] = job
        while len(self.jobs) > self.keep:
            old_id, _ = self.jobs.popitem(last=False)
            (self.jobs_dir / f"{old_id}.json").unlink(missing_ok=True)
        self._save(job)
        started = STARTED.get()
        if started is not None:
            started.append(job.id)
        task = asyncio.get_running_loop().create_task(self._run(job, action))
        self._tasks.add(task)
        self._running[job.id] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._running.pop(job.id, None))
        return job

    def stop(self, job_id: str, by: str) -> bool:
        """Stop a queued or running job: its task is cancelled, which ends the
        program it runs (ops.run: SIGTERM to its process group, SIGKILL after
        KILL_WAIT). False when the job is not running any more."""
        task = self._running.get(job_id)
        job = self.jobs.get(job_id)
        if task is None or task.done() or job is None or job.finished_flag:
            return False
        if job_id not in self._stop_by:
            self._stop_by[job_id] = by
            job.log(f"--- stop asked by {by}")
        task.cancel()
        return True

    async def _watch(self, moment: str, job: Job) -> None:
        for watcher in self.watchers:
            try:
                await getattr(watcher, moment)(job)
            except Exception:  # noqa: BLE001 - a watcher must not change the job
                _logger.exception("job %s: a watcher of its %s failed", job.id, moment)

    async def _run(self, job: Job, action: Callable[[Job], Awaitable[None]]) -> None:
        locks = [self.lock(key) for key in job.instances]  # sorted: no deadlock
        if any(lock.locked() for lock in locks):
            job.log("Waiting for another job on the same instance to finish")
        held = []
        final = None
        try:
            for lock in locks:
                await lock.acquire()
                held.append(lock)
            job.status, job.started = RUNNING, _now()
            self._save(job)
            await self._watch("begin", job)
            await action(job)
            final = SUCCESS
        except asyncio.CancelledError:
            by = self._stop_by.pop(job.id, None)
            if by is None:
                # OmniaCLI is stopping: the job is interrupted (see _load)
                raise
            final, job.error = STOPPED, f"stopped by {by}"
            job.log(f"STOPPED by {by}: what the job did before stays done")
        except JobFailed as exc:
            final, job.error = FAILED, str(exc)
            job.log(f"FAILED: {exc}")
        except Exception as exc:  # noqa: BLE001 - every error must reach the job log
            final, job.error = FAILED, f"{type(exc).__name__}: {exc}"
            job.log(traceback.format_exc())
            _logger.exception("Job %s crashed", job.id)
        finally:
            job.finished = _now()
            if final is not None and job.started:
                # the watchers of its end write what it did (deploys.py: its deploy) before the job reads finished:
                # a client that waits for the job and then reads the deploys finds it (GitHub's runner read two
                # deploys of three, 23/11/2026). They read its outcome in its notes (Job.outcome)
                job.notes["status"] = final
                await self._watch("end", job)
            if final is not None:
                job.status = final
            self._save(job)
            for lock in reversed(held):
                lock.release()
            if job.finished_flag:
                for listener in self.listeners:
                    try:
                        listener(job)
                    except Exception:  # noqa: BLE001 - a listener must not change the job's end
                        _logger.exception("job %s: a listener of its end failed", job.id)
