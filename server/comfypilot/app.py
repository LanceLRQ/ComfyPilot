import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from comfypilot.events import install_event_routes
from comfypilot.execution_api import install_execution_routes
from comfypilot.media_api import install_media_routes
from comfypilot.monitoring import get_target_status, metric
from comfypilot.project import Project, read_dispatch, read_project, set_dispatch
from comfypilot.resources_api import install_resource_routes
from comfypilot.review_api import install_review_routes
from comfypilot.runtime import RuntimeRecord, ServiceHealth, process_identity, write_runtime
from comfypilot.settings import get_project
from comfypilot.settings_api import install_settings_routes


class WorkerStatus(BaseModel):
    status: Literal["not_started", "running", "stale", "stopped"]
    pid: int | None = None
    last_seen_at: datetime | None = None


class ProjectOverview(BaseModel):
    project_id: UUID
    root: str
    name: str
    created_at: datetime
    schema_version: int
    worker: WorkerStatus
    instance_id: UUID
    dispatch: dict[str, bool | str]


def create_app(
    project: Project, runtime: RuntimeRecord, stopping: Callable[[], bool] | None = None
) -> FastAPI:
    def spawn_worker() -> subprocess.Popen:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "comfypilot.worker",
                "--project-root",
                str(project.root),
                "--instance-id",
                str(runtime.instance_id),
            ],
            cwd=project.root,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        runtime.worker = process_identity(process.pid)
        write_runtime(project, runtime)
        return process

    worker: subprocess.Popen | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal worker
        set_dispatch(project, paused=True, reason="service_started")
        worker = spawn_worker()
        runtime.phase = "running"
        write_runtime(project, runtime)

        async def supervise():
            nonlocal worker
            restarts = 0
            born = time.monotonic()
            while True:
                await asyncio.sleep(1)
                if worker.poll() is None:
                    if time.monotonic() - born >= 5:
                        restarts = 0
                    continue
                if restarts >= 3:
                    logging.getLogger("comfypilot").error(
                        "Worker 连续失败，停止自动恢复；请检查项目日志。"
                    )
                    return
                restarts += 1
                worker = spawn_worker()
                born = time.monotonic()

        supervisor = asyncio.create_task(supervise())
        try:
            yield
        finally:
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor
            try:
                runtime.phase = "stopping"
                write_runtime(project, runtime)
                set_dispatch(project, paused=True, reason="service_stopped")
            finally:
                if worker.poll() is None:
                    worker.terminate()
                await asyncio.to_thread(worker.wait, timeout=8)
                runtime.phase = "stopped"
                write_runtime(project, runtime)

    app = FastAPI(title="ComfyPilot", docs_url=None, redoc_url=None, lifespan=lifespan)
    install_settings_routes(app, project, runtime)
    install_execution_routes(app, project, runtime)
    install_resource_routes(app, project, runtime)
    install_media_routes(app, project, runtime)
    install_review_routes(app, project, runtime)

    def worker_status() -> WorkerStatus:
        with closing(sqlite3.connect(project.database)) as db:
            row = db.execute(
                "SELECT pid, last_seen_at, status FROM worker_status "
                "WHERE project_id=? AND instance_id=?",
                (str(project.identity.project_id), str(runtime.instance_id)),
            ).fetchone()
        observed = WorkerStatus(status="not_started")
        if row and runtime.worker and row[0] == runtime.worker.pid:
            observed_at = datetime.fromisoformat(row[1])
            status = row[2]
            if worker and worker.poll() is not None:
                status = "stopped"
            elif status == "running" and (datetime.now(UTC) - observed_at).total_seconds() >= 15:
                status = "stale"
            observed = WorkerStatus(status=status, pid=row[0], last_seen_at=observed_at)
        return observed

    @app.get("/health")
    def health() -> ServiceHealth:
        return ServiceHealth(
            status="ok",
            project_id=project.identity.project_id,
            api_pid=os.getpid(),
            runtime=runtime,
            worker_ready=worker_status().status == "running",
        )

    @app.get("/api/project")
    def overview() -> ProjectOverview:
        return ProjectOverview(
            **read_project(project.root).summary(),
            worker=worker_status(),
            instance_id=runtime.instance_id,
            dispatch=read_dispatch(project),
        )

    monitored_at = 0.0
    monitored: dict = {}

    def monitor_view():
        nonlocal monitored, monitored_at
        if monitored and time.monotonic() - monitored_at < 5:
            return monitored
        current = get_project(project)
        targets = []
        for target in current["config"]["targets"]:
            sample = get_target_status(
                project,
                runtime.instance_id,
                runtime.worker.pid if runtime.worker else 0,
                target["id"],
                current=current,
            )
            sample.pop("node_schemas", None)
            sample.pop("node_classes", None)
            targets.append(sample)
        space = shutil.disk_usage(project.root)
        monitored = {
            "worker": worker_status().model_dump(mode="json"),
            "platform": {
                "api": metric(True, "platform.api"),
                "disk_free": metric(space.free, "platform.project_filesystem", "bytes"),
                "disk_total": metric(space.total, "platform.project_filesystem", "bytes"),
            },
            "targets": targets,
        }
        monitored_at = time.monotonic()
        return monitored

    install_event_routes(app, project, runtime, stopping, monitor_view)

    @app.get("/api/status")
    def monitoring():
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": {**monitor_view(), "dispatch": read_dispatch(project)},
        }

    web = Path(__file__).parent / "web"
    if (web / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    else:

        @app.get("/")
        def missing_web() -> JSONResponse:
            return JSONResponse(
                {
                    "code": "web_assets_missing",
                    "message": "请先运行 npm --prefix apps/web run build。",
                },
                status_code=503,
            )

    return app
