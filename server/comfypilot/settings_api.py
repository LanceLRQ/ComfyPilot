from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from comfypilot.monitoring import TargetMonitor, get_target_status
from comfypilot.project import ExecutionTarget, Project, ProjectConfig, ProjectError
from comfypilot.runtime import RuntimeRecord
from comfypilot.settings import (
    get_project,
    refresh_project,
    resolve_target,
    update_project,
    update_target,
)


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


class ConfigRequest(RevisionRequest):
    config: ProjectConfig


class TargetRequest(RevisionRequest):
    target: ExecutionTarget


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_id: str | None = None


class TargetStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_id: str = Field(min_length=1, max_length=64)


def install_settings_routes(app: FastAPI, project: Project, runtime: RuntimeRecord) -> None:
    def reply(data=None, *, error: dict | None = None, status: int = 200):
        content = {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
        }
        content["error" if error else "data"] = error if error else data
        return JSONResponse(content, status_code=status)

    @app.exception_handler(ProjectError)
    async def project_error(_request: Request, error: ProjectError):
        return reply(
            error={"code": error.code, "message": str(error)},
            status=409 if error.code == "conflict" else 400,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError):
        return reply(
            error={"code": "validation_error", "message": "请求参数不符合定义，请检查字段和类型。"},
            status=422,
        )

    @app.middleware("http")
    async def control_boundary(request: Request, call_next):
        if (
            request.url.path.startswith(("/api/config", "/api/targets", "/api/execution"))
            and request.method != "GET"
        ):
            origin = request.headers.get("origin")
            hosts = {f"127.0.0.1:{runtime.port}", f"localhost:{runtime.port}"}
            if request.headers.get("host") not in hosts or (
                origin and origin not in {f"http://{host}" for host in hosts}
            ):
                return reply(
                    error={"code": "control_forbidden", "message": "配置操作仅接受本机同源控制。"},
                    status=403,
                )
            if request.headers.get("x-comfypilot-instance") != str(runtime.instance_id):
                return reply(
                    error={
                        "code": "connection_invalidated",
                        "message": "服务实例已变化，请重新读取并连接。",
                    },
                    status=409,
                )
        return await call_next(request)

    @app.get("/api/config")
    def configuration():
        return reply(get_project(project))

    @app.post("/api/config/update")
    def update(body: ConfigRequest):
        return reply(
            update_project(project, body.expected_revision, body.config.model_dump(mode="json"))
        )

    @app.post("/api/config/refresh")
    def refresh(body: RevisionRequest):
        return reply(refresh_project(project, body.expected_revision))

    @app.get("/api/targets")
    def targets():
        current = get_project(project)
        return reply(
            {
                "revision": current["revision"],
                "targets": current["config"]["targets"],
                "default_target_id": current["config"]["default_target_id"],
                "warnings": current["warnings"],
            }
        )

    @app.post("/api/targets/update")
    def target(body: TargetRequest):
        return reply(
            update_target(project, body.expected_revision, body.target.model_dump(mode="json"))
        )

    @app.post("/api/targets/resolve")
    def resolve(body: RouteRequest):
        return reply(resolve_target(project, body.target_id))

    @app.post("/api/targets/status")
    def status(body: TargetStatusRequest):
        return reply(
            get_target_status(
                project,
                runtime.instance_id,
                runtime.worker.pid if runtime.worker else 0,
                body.target_id,
            )
        )

    @app.post("/api/targets/probe")
    async def probe(body: TargetStatusRequest):
        selected = resolve_target(project, body.target_id)
        if not runtime.worker:
            raise ProjectError("worker_unavailable", "Worker 尚未就绪，请稍后重试。")
        monitor = TargetMonitor(
            project,
            runtime.instance_id,
            runtime.worker.pid,
            selected["config_revision"],
            ExecutionTarget.model_validate(selected["target"]),
        )
        await monitor.poll()
        return reply(
            get_target_status(project, runtime.instance_id, runtime.worker.pid, body.target_id)
        )
