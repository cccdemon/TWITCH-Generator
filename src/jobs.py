"""Background job manager. Runs the pipeline serially (one heavy job at a time).

Job records are persisted to <data>/jobs.json so the Jobs table survives a
container restart. (Clips already survive — they live on disk under data/<vod>.)
A job that was running/queued when the process died is marked "interrupted" on
load, since its worker thread is gone.
"""
from __future__ import annotations

import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rich.console import Console

from . import main as pipeline
from .config import data_dir, load_config
from .settings_store import apply_settings


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
        job.status = "running"
        self._persist()
        console = Console(file=job._buf, force_terminal=False, width=100)
        try:
            apply_settings()                 # pull latest UI settings into env
            cfg = load_config()
            rc = pipeline.run_pipeline(
                job.vod, cfg, no_upload=job.no_upload, console=console
            )
            job.status = "done" if rc == 0 else "error"
            if rc != 0:
                job.error = f"pipeline exit code {rc}"
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = str(e)
            console.print(f"[red]EXCEPTION: {e}")
        finally:
            job.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._persist()  # final state + full log

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order)]


manager = JobManager()
