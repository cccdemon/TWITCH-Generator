"""SQLite job store. One row per post; per-platform results in JSON columns."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created REAL NOT NULL,
  run_at REAL NOT NULL,
  clip_name TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  caption TEXT NOT NULL,
  platforms TEXT NOT NULL,          -- ["tiktok","instagram"]
  options TEXT NOT NULL DEFAULT '{}',
  dry_run INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,             -- queued|running|done|partial|failed|cancelled
  results TEXT NOT NULL DEFAULT '{}', -- {platform: {ok, msg, at, shot}}
  log TEXT NOT NULL DEFAULT '',
  finished REAL
);
"""

COLS = ("id", "created", "run_at", "clip_name", "title", "source", "caption", "platforms",
        "options", "dry_run", "status", "results", "log", "finished")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute(_SCHEMA)
        self.db.commit()
        self.lock = threading.Lock()
        # A crash mid-run leaves rows "running"; they are not retried blindly.
        with self.lock:
            self.db.execute("UPDATE posts SET status='failed', log=log || ? WHERE status='running'",
                            ("\n[Dienst neu gestartet während des Laufs – bitte prüfen und ggf. erneut senden]",))
            self.db.commit()

    def _row(self, r) -> dict:
        d = dict(zip(COLS, r))
        for k in ("platforms", "options", "results"):
            d[k] = json.loads(d[k] or "null")
        d["dry_run"] = bool(d["dry_run"])
        return d

    def add(self, *, run_at: float, clip_name: str, title: str, source: str, caption: str,
            platforms: list[str], options: dict, dry_run: bool) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO posts (created, run_at, clip_name, title, source, caption, platforms, options,"
                " dry_run, status) VALUES (?,?,?,?,?,?,?,?,?, 'queued')",
                (time.time(), run_at, clip_name, title, source, caption, json.dumps(platforms),
                 json.dumps(options), int(dry_run)))
            self.db.commit()
            return int(cur.lastrowid)

    def get(self, pid: int) -> dict | None:
        with self.lock:
            r = self.db.execute(f"SELECT {','.join(COLS)} FROM posts WHERE id=?", (pid,)).fetchone()
        return self._row(r) if r else None

    def list(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                f"SELECT {','.join(COLS)} FROM posts ORDER BY "
                "CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                "CASE WHEN status IN ('running','queued') THEN run_at ELSE -COALESCE(finished, created) END "
                "LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def next_due(self, now: float) -> dict | None:
        with self.lock:
            r = self.db.execute(
                f"SELECT {','.join(COLS)} FROM posts WHERE status='queued' AND run_at<=? "
                "ORDER BY run_at, id LIMIT 1", (now,)).fetchone()
        return self._row(r) if r else None

    def update(self, pid: int, **fields) -> None:
        if not fields:
            return
        sets, vals = [], []
        for k, v in fields.items():
            if k in ("platforms", "options", "results"):
                v = json.dumps(v)
            if k == "dry_run":
                v = int(v)
            sets.append(f"{k}=?")
            vals.append(v)
        with self.lock:
            self.db.execute(f"UPDATE posts SET {','.join(sets)} WHERE id=?", (*vals, pid))
            self.db.commit()

    def append_log(self, pid: int, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.db.execute("UPDATE posts SET log = log || ? WHERE id=?", (f"{stamp} {line}\n", pid))
            self.db.commit()

    def delete(self, pid: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM posts WHERE id=?", (pid,))
            self.db.commit()

    def old_finished(self, before: float) -> list[int]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM posts WHERE status IN ('done','partial','failed','cancelled') AND finished<?",
                (before,)).fetchall()
        return [r[0] for r in rows]
