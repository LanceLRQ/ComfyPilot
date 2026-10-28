from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from comfypilot.project import Project
from comfypilot.resource_preflight import list_preflights, preflight_recipe
from comfypilot.resources import get_resource_file, get_resources, refresh_resources
from comfypilot.runtime import RuntimeRecord


class ResourceRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)


class ResourceFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class PreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recipe_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=64)


def install_resource_routes(app: FastAPI, project: Project, runtime: RuntimeRecord) -> None:
    def reply(data):
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }

    def catalog(data):
        return reply(
            {
                **data,
                "preflights": list_preflights(
                    project, runtime.instance_id, runtime.worker.pid if runtime.worker else 0
                ),
            }
        )

    @app.get("/api/resources")
    def resources():
        return catalog(get_resources(project))

    @app.post("/api/resources/refresh")
    def refresh(body: ResourceRefreshRequest):
        return catalog(refresh_resources(project, body.expected_revision))

    @app.post("/api/resources/file")
    def file(body: ResourceFileRequest):
        return reply(get_resource_file(project, body.resource_hash))

    @app.post("/api/resources/preflight")
    def preflight(body: PreflightRequest):
        return reply(
            preflight_recipe(
                project,
                body.recipe_id,
                body.target_id,
                runtime.instance_id,
                runtime.worker.pid if runtime.worker else 0,
            )
        )
