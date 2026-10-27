import argparse
import os
import signal
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from comfypilot.project import Project, load_project, resolve_project_root


def run_worker(project: Project) -> None:
    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    project_id = str(project.identity.project_id)
    with closing(sqlite3.connect(project.database, timeout=5)) as db:
        try:
            while not stopped.is_set():
                with db:
                    db.execute(
                        "INSERT INTO worker_status (project_id, pid, last_seen_at, status) "
                        "VALUES (?, ?, ?, 'running') ON CONFLICT(project_id) DO UPDATE SET "
                        "pid=excluded.pid, last_seen_at=excluded.last_seen_at, "
                        "status=excluded.status",
                        (project_id, os.getpid(), datetime.now(UTC).isoformat()),
                    )
                stopped.wait(5)
        finally:
            with db:
                db.execute(
                    "UPDATE worker_status SET status='stopped', last_seen_at=? WHERE project_id=?",
                    (datetime.now(UTC).isoformat(), project_id),
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="ComfyPilot 独立 Worker")
    parser.add_argument("--project-root", required=True, type=Path)
    args = parser.parse_args()
    run_worker(load_project(resolve_project_root(args.project_root)))


if __name__ == "__main__":
    main()
