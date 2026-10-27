import argparse
import asyncio
import os
import signal
import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from comfypilot.monitoring import monitor_targets
from comfypilot.project import Project, load_project, resolve_project_root
from comfypilot.runtime import project_lock


def run_worker(project: Project, instance_id: UUID) -> None:
    project_id = str(project.identity.project_id)

    async def run():
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopped.set)
        with closing(sqlite3.connect(project.database, timeout=5)) as db:
            try:
                async with asyncio.TaskGroup() as group:
                    monitors = group.create_task(monitor_targets(project, instance_id))
                    while not stopped.is_set():
                        with db:
                            db.execute(
                                "INSERT INTO worker_status "
                                "(project_id, pid, last_seen_at, status, instance_id) "
                                "VALUES (?, ?, ?, 'running', ?) "
                                "ON CONFLICT(project_id) DO UPDATE SET "
                                "pid=excluded.pid, last_seen_at=excluded.last_seen_at, "
                                "status=excluded.status, instance_id=excluded.instance_id",
                                (
                                    project_id,
                                    os.getpid(),
                                    datetime.now(UTC).isoformat(),
                                    str(instance_id),
                                ),
                            )
                        with suppress(TimeoutError):
                            await asyncio.wait_for(stopped.wait(), timeout=5)
                    monitors.cancel()
            finally:
                with db:
                    db.execute(
                        "UPDATE worker_status SET status='stopped', last_seen_at=? "
                        "WHERE project_id=? AND instance_id=? AND pid=?",
                        (datetime.now(UTC).isoformat(), project_id, str(instance_id), os.getpid()),
                    )

    asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(description="ComfyPilot 独立 Worker")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--instance-id", required=True, type=UUID)
    args = parser.parse_args()
    project = load_project(resolve_project_root(args.project_root))
    with project_lock(project, "worker", timeout=0):
        run_worker(project, args.instance_id)


if __name__ == "__main__":
    main()
