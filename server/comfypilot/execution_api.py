from typing import Annotated

from fastapi import Body, FastAPI
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

    @app.post("/api/execution/command")
    def command(body: Annotated[AnyCommand, Body(discriminator="action")]):
        return reply(apply_command(project, body))
