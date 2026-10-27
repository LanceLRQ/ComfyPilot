from typing import Annotated

from fastapi import Body, FastAPI

from comfypilot.execution import AnyCommand, apply_command, get_execution
from comfypilot.project import Project
from comfypilot.runtime import RuntimeRecord


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

    @app.post("/api/execution/command")
    def command(body: Annotated[AnyCommand, Body(discriminator="action")]):
        return reply(apply_command(project, body))
