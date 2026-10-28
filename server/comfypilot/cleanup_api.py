from fastapi import FastAPI

from comfypilot.cleanup import (
    CleanupScope,
    ExecuteCleanup,
    PrepareCleanup,
    execute_cleanup,
    inspect_storage,
    list_cleanup,
    prepare_cleanup,
)
from comfypilot.project import Project
from comfypilot.runtime import RuntimeRecord


def install_cleanup_routes(app: FastAPI, project: Project, runtime: RuntimeRecord) -> None:
    def reply(data):
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }

    @app.post("/api/cleanup/inspect")
    async def inspect(body: CleanupScope):
        return reply(await inspect_storage(project, body))

    @app.post("/api/cleanup/prepare")
    async def prepare(body: PrepareCleanup):
        return reply(await prepare_cleanup(project, body))

    @app.post("/api/cleanup/execute")
    async def execute(body: ExecuteCleanup):
        return reply(await execute_cleanup(project, body))

    @app.get("/api/cleanup")
    def history():
        return reply(list_cleanup(project))
