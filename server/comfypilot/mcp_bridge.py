import asyncio
import json
import sqlite3
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from comfypilot.execution import (
    ActivatePlan,
    CreatePlan,
    CreateTask,
    ExecutePlan,
    PauseDispatch,
    ResumeDispatch,
    StopPlan,
)
from comfypilot.lifecycle import inspect_service, start_service, stop_service
from comfypilot.project import (
    Project,
    ProjectError,
    initialize_project,
    load_project,
    read_project,
    resolve_project_root,
)
from comfypilot.runtime import RuntimeRecord, read_runtime, require_instance
from comfypilot.settings_api import (
    ConfigRequest,
    RevisionRequest,
    RouteRequest,
    TargetRequest,
    TargetStatusRequest,
)


class ProjectArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_root: str = Field(min_length=1, description="用户明确指定的项目绝对目录。")


class StartArguments(ProjectArguments):
    port: int | None = Field(default=None, ge=1, le=65535, description="默认读取项目配置。")


class ConnectionArguments(ProjectArguments):
    connection_ref: str | None = Field(
        default=None, min_length=1, description="本 MCP 进程 connect_service 返回的连接引用。"
    )


class DisconnectArguments(ConnectionArguments):
    connection_ref: str = Field(min_length=1, description="需要断开的本进程连接引用。")


class ConfigArguments(ConnectionArguments, ConfigRequest):
    pass


class RefreshArguments(ConnectionArguments, RevisionRequest):
    pass


class TargetArguments(ConnectionArguments, TargetRequest):
    pass


class RouteArguments(ConnectionArguments, RouteRequest):
    pass


class TargetStatusArguments(ConnectionArguments, TargetStatusRequest):
    pass


class CreateTaskArguments(ConnectionArguments, CreateTask):
    pass


class CreatePlanArguments(ConnectionArguments, CreatePlan):
    pass


class ActivatePlanArguments(ConnectionArguments, ActivatePlan):
    pass


class ExecutePlanArguments(ConnectionArguments, ExecutePlan):
    pass


class StopPlanArguments(ConnectionArguments, StopPlan):
    pass


class PauseArguments(ConnectionArguments, PauseDispatch):
    pass


class ResumeArguments(ConnectionArguments, ResumeDispatch):
    pass


class ToolError(BaseModel):
    code: str
    message: str


class ToolResponse(BaseModel):
    ok: bool
    data: dict | None = None
    error: ToolError | None = None


class ApplicationReply(BaseModel):
    project_id: UUID
    instance_id: UUID
    data: dict | None = None
    error: ToolError | None = None


PROJECT_API = {
    **{
        name: ("POST", "/api/execution/command")
        for name in (
            "create_task",
            "create_plan",
            "activate_plan",
            "execute_plan",
            "stop_plan",
            "pause_dispatch",
            "resume_dispatch",
        )
    },
    "get_execution": ("GET", "/api/execution"),
    "get_project": ("GET", "/api/config"),
    "update_project": ("POST", "/api/config/update"),
    "refresh_project": ("POST", "/api/config/refresh"),
    "list_targets": ("GET", "/api/targets"),
    "update_target": ("POST", "/api/targets/update"),
    "resolve_target": ("POST", "/api/targets/resolve"),
    "probe_target": ("POST", "/api/targets/probe"),
    "get_target_status": ("POST", "/api/targets/status"),
    "get_monitor_status": ("GET", "/api/status"),
}


# 仅定义静态协议元数据；不得在此探测项目、服务或 ComfyUI。
TOOL_DEFINITIONS = {
    "get_execution": (ConnectionArguments, "读取业务修订、方案、运行与独立派发限制。", True, True),
    "create_task": (
        CreateTaskArguments,
        "记录用户目标与验收要求；命令幂等并检查业务修订。",
        False,
        False,
    ),
    "create_plan": (
        CreatePlanArguments,
        "保存含用户执行范围的不可变方案草稿，不替换生效方案。",
        False,
        False,
    ),
    "activate_plan": (
        ActivatePlanArguments,
        "显式生效草稿，同事务取消旧方案未提交步骤。",
        False,
        False,
    ),
    "execute_plan": (
        ExecutePlanArguments,
        "在用户范围内幂等入队；当前阶段仅排队，尚不提交生成。",
        False,
        False,
    ),
    "stop_plan": (
        StopPlanArguments,
        "停止方案后续未提交步骤，保留在途记录，不中断外部队列。",
        False,
        False,
    ),
    "pause_dispatch": (PauseArguments, "保存手动派发暂停，不停止监测与在途工作。", False, False),
    "resume_dispatch": (
        ResumeArguments,
        "仅解除手动派发暂停，不清除审片挂起或待核对限制。",
        False,
        False,
    ),
    "probe_target": (
        TargetStatusArguments,
        "显式探测指定执行端的标准接口与节点清单，不提交任务。",
        True,
        True,
    ),
    "get_target_status": (
        TargetStatusArguments,
        "读取指定目标最新监测快照，包含来源、过期时间与缺项。",
        True,
        True,
    ),
    "get_monitor_status": (
        ConnectionArguments,
        "读取项目 Worker、调度状态与各目标的统一监测快照。",
        True,
        True,
    ),
    "inspect_project": (
        ProjectArguments,
        "只读检查指定目录是否初始化及项目身份，不连接或启动服务。",
        True,
        True,
    ),
    "init_project": (
        ProjectArguments,
        "显式初始化指定项目；保留已有配置与数据，不启动或连接服务。",
        False,
        True,
    ),
    "start_service": (
        StartArguments,
        "显式后台启动或复用指定项目服务；不会创建业务连接。",
        False,
        True,
    ),
    "connect_service": (
        ProjectArguments,
        "只连接已运行的指定项目，校验实例后返回本进程 connection_ref；不会启动服务。",
        True,
        False,
    ),
    "disconnect_service": (
        DisconnectArguments,
        "只释放指定连接引用，不请求、暂停或停止平台；其他连接继续可用。",
        True,
        False,
    ),
    "stop_service": (
        ConnectionArguments,
        "显式暂停并停止指定项目；给出 connection_ref 时只允许停止其绑定实例，"
        "不给出则停止该目录当前实例。",
        False,
        True,
    ),
    "get_project_status": (
        ConnectionArguments,
        "读取已连接项目的实际 API/Worker 与概览状态；"
        "必须先 connect_service 并传入 connection_ref。",
        True,
        True,
    ),
    "get_project": (
        ConnectionArguments,
        "读取已接受的项目配置修订与磁盘草稿状态，不显示凭据值。",
        True,
        True,
    ),
    "update_project": (
        ConfigArguments,
        "以 expected_revision 更新整个项目配置；冲突时保留草稿。",
        False,
        False,
    ),
    "refresh_project": (
        RefreshArguments,
        "显式验证并接纳磁盘 YAML；非法内容不替换有效修订。",
        False,
        False,
    ),
    "list_targets": (
        ConnectionArguments,
        "读取当前项目配置中的本机/远程执行端与默认目标。",
        True,
        True,
    ),
    "update_target": (
        TargetArguments,
        "以 expected_revision 新增或修改具名执行端，不改写历史快照。",
        False,
        False,
    ),
    "resolve_target": (
        RouteArguments,
        "只解析明确目标或配置默认值，缺项返回询问项；不探测、不隐式切换。",
        True,
        True,
    ),
}


class ProjectBridge:
    def __init__(self) -> None:
        self.connections: dict[str, RuntimeRecord] = {}

    def connection(self, root: Path, reference: str | None) -> RuntimeRecord:
        if reference is None or reference not in self.connections:
            raise ProjectError(
                "service_not_connected", "请先显式 connect_service，再传入其连接引用。"
            )
        expected = self.connections[reference]
        if expected.root != str(root):
            raise ProjectError(
                "project_mismatch", "连接引用属于其他项目目录，请使用对应项目的连接。"
            )
        return expected

    def execute(self, name: str, arguments: ProjectArguments) -> dict:
        root = resolve_project_root(Path(arguments.project_root))
        if name == "inspect_project":
            if not (root / "comfypilot.yaml").exists() and not (root / ".comfypilot").exists():
                return {"initialized": False, "root": str(root)}
            return {"initialized": True, **read_project(root).summary()}
        if name == "init_project":
            return initialize_project(root).summary()
        if isinstance(arguments, StartArguments):
            project = load_project(root)
            port = arguments.port if arguments.port is not None else project.config.server.port
            return start_service(project, port)
        expected = None
        if isinstance(arguments, ConnectionArguments):
            if name != "stop_service" or arguments.connection_ref is not None:
                expected = self.connection(root, arguments.connection_ref)
            if name == "disconnect_service":
                del self.connections[arguments.connection_ref]
                return {"status": "disconnected", "root": str(root)}
        project = read_project(root)
        if expected and expected.project_id != project.identity.project_id:
            raise ProjectError("project_mismatch", "项目 UUID 已变化，请检查目录并重新连接。")
        if name == "stop_service":
            return stop_service(project, expected=expected)
        runtime = read_runtime(project)
        if expected:
            require_instance(runtime, expected)
        if runtime is None:
            raise ProjectError("service_not_running", "服务尚未启动，请先显式 start_service。")
        health = inspect_service(project, runtime)
        if name == "connect_service":
            if health.runtime.phase != "running":
                raise ProjectError("service_busy", "实例正在启动或停止，请稍后再连接。")
            reference = str(uuid4())
            self.connections[reference] = health.runtime.model_copy(deep=True)
            return {
                "connection_ref": reference,
                "root": str(root),
                "project_id": str(project.identity.project_id),
                "instance_id": str(health.runtime.instance_id),
                "url": f"http://127.0.0.1:{runtime.port}",
            }
        if name in PROJECT_API:
            method, path = PROJECT_API[name]
            return self.request(
                project,
                runtime,
                method,
                path,
                arguments.model_dump(
                    mode="json",
                    exclude={"project_root", "connection_ref"},
                ),
            )
        return {
            "service": health.model_dump(mode="json"),
            "project": self.overview(project, runtime),
        }

    def request(
        self, project: Project, runtime: RuntimeRecord, method: str, path: str, data: dict
    ) -> dict:
        connection = HTTPConnection("127.0.0.1", runtime.port, timeout=3)
        try:
            connection.request(
                method,
                path,
                body=json.dumps(data) if method == "POST" else None,
                headers={
                    "Content-Type": "application/json",
                    "X-ComfyPilot-Instance": str(runtime.instance_id),
                },
            )
            response = connection.getresponse()
            result = ApplicationReply.model_validate_json(response.read(1048576))
        except (OSError, HTTPException) as error:
            raise ProjectError(
                "service_unavailable", "无法访问项目 API，请检查服务并重新连接。"
            ) from error
        except ValidationError as error:
            raise ProjectError(
                "identity_mismatch", "项目 API 响应不符合预期，拒绝使用。"
            ) from error
        finally:
            connection.close()
        if (
            result.project_id != project.identity.project_id
            or result.instance_id != runtime.instance_id
        ):
            raise ProjectError("identity_mismatch", "项目 API 响应来自其他项目或实例。")
        if result.error:
            raise ProjectError(result.error.code, result.error.message)
        if response.status != 200 or result.data is None:
            raise ProjectError("service_unavailable", "项目 API 未返回成功结果，请检查服务。")
        return result.data

    def overview(self, project: Project, runtime: RuntimeRecord) -> dict:
        from comfypilot.app import ProjectOverview

        connection = HTTPConnection("127.0.0.1", runtime.port, timeout=2)
        try:
            connection.request("GET", "/api/project")
            response = connection.getresponse()
            if response.status != 200:
                raise ProjectError("service_unavailable", "服务未返回项目概览，请检查项目日志。")
            overview = ProjectOverview.model_validate_json(response.read(65536))
        except (OSError, HTTPException) as error:
            raise ProjectError(
                "service_unavailable", "无法取得项目概览，请检查服务并重新连接。"
            ) from error
        except ValidationError as error:
            raise ProjectError(
                "identity_mismatch", "项目概览格式不符合预期，拒绝使用该响应。"
            ) from error
        finally:
            connection.close()
        if (
            overview.project_id != project.identity.project_id
            or overview.root != str(project.root)
            or overview.instance_id != runtime.instance_id
        ):
            raise ProjectError("identity_mismatch", "项目概览所属项目或实例不一致，请重新连接。")
        return overview.model_dump(mode="json")


def create_server() -> Server:
    bridge = ProjectBridge()

    async def list_tools(_context, _params) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name=name,
                    description=description,
                    input_schema=model.model_json_schema(),
                    output_schema=ToolResponse.model_json_schema(),
                    annotations=ToolAnnotations(
                        read_only_hint=read_only,
                        destructive_hint=name == "stop_service",
                        idempotent_hint=idempotent,
                        open_world_hint=False,
                    ),
                )
                for name, (model, description, read_only, idempotent) in TOOL_DEFINITIONS.items()
            ]
        )

    async def call_tool(_context, params) -> CallToolResult:
        try:
            if params.name not in TOOL_DEFINITIONS:
                raise ProjectError("unknown_tool", "未知工具，请重新枚举可用工具。")
            model = TOOL_DEFINITIONS[params.name][0]
            try:
                arguments = model.model_validate(params.arguments or {})
            except ValidationError as error:
                raise ProjectError(
                    "validation_error", "参数不符合工具定义，请检查必填项、类型和取值。"
                ) from error
            data = await asyncio.to_thread(bridge.execute, params.name, arguments)
            result = ToolResponse(ok=True, data=data)
        except ProjectError as error:
            result = ToolResponse(ok=False, error=ToolError(code=error.code, message=str(error)))
        except (OSError, sqlite3.Error, ValueError, yaml.YAMLError):
            result = ToolResponse(
                ok=False,
                error=ToolError(
                    code="project_unavailable",
                    message="项目无法读取或写入，请检查目录权限、配置和数据库完整性。",
                ),
            )
        payload = result.model_dump(exclude_none=True)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structured_content=payload,
            is_error=not result.ok,
        )

    return Server(
        "ComfyPilot", version="0.1.0.dev0", on_list_tools=list_tools, on_call_tool=call_tool
    )


def main() -> None:
    async def run():
        server = create_server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
