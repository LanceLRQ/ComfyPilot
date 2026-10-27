import os
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from comfypilot.project import Project


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


def create_app(project: Project) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker = subprocess.Popen(
            [sys.executable, "-m", "comfypilot.worker", "--project-root", str(project.root)],
            cwd=project.root,
        )
        try:
            yield
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=10)

    app = FastAPI(title="ComfyPilot", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str | int]:
        return {
            "status": "ok",
            "project_id": str(project.identity.project_id),
            "api_pid": os.getpid(),
        }

    @app.get("/api/project")
    def overview() -> ProjectOverview:
        with closing(sqlite3.connect(project.database)) as db:
            row = db.execute(
                "SELECT pid, last_seen_at, status FROM worker_status WHERE project_id=?",
                (str(project.identity.project_id),),
            ).fetchone()
        worker = WorkerStatus(status="not_started")
        if row:
            observed_at = datetime.fromisoformat(row[1])
            status = row[2]
            if status == "running" and (datetime.now(UTC) - observed_at).total_seconds() >= 15:
                status = "stale"
            worker = WorkerStatus(status=status, pid=row[0], last_seen_at=observed_at)
        return ProjectOverview(**project.summary(), worker=worker)

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
