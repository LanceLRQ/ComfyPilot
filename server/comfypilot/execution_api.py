import json
import sqlite3
from contextlib import closing
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from comfypilot.events import state_snapshot
from comfypilot.execution import AnyCommand, Identifier, apply_command, get_execution
from comfypilot.project import Project, ProjectError
from comfypilot.runtime import RuntimeRecord


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: Identifier


def install_execution_routes(app: FastAPI, project: Project, runtime: RuntimeRecord) -> None:
    def reply(data):
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }

    @app.get("/api/execution")
    def status():
        return reply(get_execution(project))

    @app.get("/api/execution/runs")
    def runs():
        state = state_snapshot(project)
        return reply({key: state[key] for key in ("revision", "event_cursor", "runs")})

    @app.post("/api/execution/run")
    def run(body: RunRequest):
        state = state_snapshot(project)
        run = next((run for run in state["runs"] if run["id"] == body.run_id), None)
        if not run:
            raise ProjectError("run_unavailable", "运行不存在。")
        return reply(
            {"revision": state["revision"], "event_cursor": state["event_cursor"], "run": run}
        )

    @app.get("/api/execution/collections")
    def collections():
        state = state_snapshot(project)
        return reply({key: state[key] for key in ("revision", "event_cursor", "collections")})

    @app.get("/api/execution/runs/{run_id}/evidence")
    def evidence(run_id: UUID, kind: Literal["graph", "history", "checks"]):
        with closing(sqlite3.connect(project.database)) as db:
            row = db.execute(
                "SELECT document FROM execution_runs WHERE id=?", (str(run_id),)
            ).fetchone()
        if not row:
            raise ProjectError("run_unavailable", "运行不存在。")
        document = json.loads(row[0])
        data = {
            "graph": document.get("materialized", {}).get("graph"),
            "history": document.get("external_success", {}).get("history")
            or document.get("external_failure"),
            "checks": document.get("collection", {}).get("checks"),
        }[kind]
        if data is None:
            raise ProjectError("evidence_unavailable", "此运行尚无对应证据，请回读当前阶段。")
        return reply({"run_id": str(run_id), "kind": kind, "evidence": data})

    @app.post("/api/execution/command")
    def command(request: Request, body: Annotated[AnyCommand, Body(discriminator="action")]):
        if not request.state.local_control:
            if body.action not in {"save_feedback", "update_feedback", "record_user_decision"} or (
                body.action == "update_feedback" and body.status != "verified"
            ):
                return JSONResponse(
                    {
                        "error": {
                            "code": "control_forbidden",
                            "message": "LAN 只允许审片反馈和用户决定。",
                        }
                    },
                    status_code=403,
                )
            body = body.model_copy(update={"source": "web_user"})
        return reply(apply_command(project, body))
