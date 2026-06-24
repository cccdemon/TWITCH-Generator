"""Background job manager. Runs the pipeline serially (one heavy job at a time).

Job records are persisted to <data>/jobs.json so the Jobs table survives a
container restart. (Clips already survive — they live on disk under data/<vod>.)
A job that was running/queued when the process died is marked "interrupted" on
load, since its worker thread is gone.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import data_dir


@dataclass
class Job:
    id: str
    vod: str
    no_upload: bool
    status: str = "queued"            # queued | running | done | error | interrupted
    created: str = ""
    finished: str = ""
    title: str = ""
    error: str = ""
    _buf: io.StringIO = field(default_factory=io.StringIO)

    @property
    def log(self) -> str:
        return self._buf.getvalue()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "vod": self.vod, "no_upload": self.no_upload,
            "status": self.status, "created": self.created, "finished": self.finished,
            "title": self.title, "error": self.error, "log": self.log,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        buf = io.StringIO()
        buf.write(d.get("log", ""))
        status = d.get("status", "done")
        if status in ("running", "queued"):
            status = "interrupted"  # worker thread is gone after a restart
        return cls(
            id=d["id"], vod=d.get("vod", ""), no_upload=bool(d.get("no_upload")),
            status=status, created=d.get("created", ""), finished=d.get("finished", ""),
            title=d.get("title", ""), error=d.get("error", ""), _buf=buf,
        )


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # max_workers=1 -> serial; Whisper + ffmpeg are resource hungry.
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._load()

    # ---------------------------------------------------------- persistence
    def _store_path(self):
        return data_dir() / "jobs.json"

    def _load(self) -> None:
        p = self._store_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for d in data:
            try:
                job = Job.from_dict(d)
            except (KeyError, TypeError):
                continue
            self._jobs[job.id] = job
            self._order.append(job.id)

    def _persist(self) -> None:
        # Caller holds nothing; snapshot under lock then write outside it.
        with self._lock:
            snapshot = [self._jobs[i].to_dict() for i in self._order]
        try:
            self._store_path().write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ---------------------------------------------------------- operations
    def submit(self, vod: str, no_upload: bool, title: str = "") -> Job:
        job = Job(
            id=uuid.uuid4().hex[:8],
            vod=vod,
            no_upload=no_upload,
            title=title,
            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._persist()
        self._pool.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        """Run the pipeline as a separate, niced subprocess.

        Decoupling the CPU-heavy work (Whisper/ffmpeg) from the web process and
        running it at `nice -n 19` keeps uvicorn responsive: the scheduler gives
        the niced job whatever CPU is left after the web server, instead of the
        in-process worker starving the event loop.
        """
        job.status = "running"
        self._persist()
        cmd = ["nice", "-n", "19", "python", "-u", "-m", "src.main", "run", "--vod", job.vod]
        if job.no_upload:
            cmd.append("--no-upload")
        last_persist = 0.0
        try:
            proc = subprocess.Popen(
                cmd, cwd="/app", env={**os.environ},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                job._buf.write(line)
                now = time.monotonic()
                if now - last_persist > 3.0:   # throttle log persistence
                    last_persist = now
                    self._persist()
            rc = proc.wait()
            job.status = "done" if rc == 0 else "error"
            if rc != 0:
                job.error = f"pipeline exit code {rc}"
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = str(e)
            job._buf.write(f"EXCEPTION: {e}\n")
        finally:
            job.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._persist()  # final state + full log

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order)]


manager = JobManager()
