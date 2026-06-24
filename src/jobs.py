"""Background job manager. Runs the pipeline serially (one heavy job at a time)."""
from __future__ import annotations

import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rich.console import Console

from . import main as pipeline
from .config import load_config
from .settings_store import apply_settings


@dataclass
class Job:
    id: str
    vod: str
    no_upload: bool
    status: str = "queued"            # queued | running | done | error
    created: str = ""
    finished: str = ""
    title: str = ""
    error: str = ""
    _buf: io.StringIO = field(default_factory=io.StringIO)

    @property
    def log(self) -> str:
        return self._buf.getvalue()


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # max_workers=1 -> serial; Whisper + ffmpeg are resource hungry.
        self._pool = ThreadPoolExecutor(max_workers=1)

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
        self._pool.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        job.status = "running"
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

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order)]


manager = JobManager()
