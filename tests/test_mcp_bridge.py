import asyncio
import json
import socket
import sys
from pathlib import Path
from uuid import uuid4

import psutil
import pytest
from comfypilot.lifecycle import start_service, status_service, stop_service
from comfypilot.project import initialize_project, resolve_project_root
from mcp import Client, StdioServerParameters

TOOLS = {
    "get_system_status",
    "list_runs",
    "get_run",
    "get_collection_status",
    "pause_plan",
    "resume_plan",
    "cancel_queued_runs",
    "propose_plan",
    "record_user_decision",
    "record_evaluation",
    "get_delivery_manifest",
    "save_feedback",
    "update_feedback",
    "list_review_items",
    "resume_collection",
    "list_versions",
    "list_replicas",
    "list_assets",
    "get_asset",
    "get_media_job",
    "import_asset",
    "extract_clip",
    "list_resources",
    "refresh_resources",
    "get_resource_file",
    "preflight_recipe",
    "get_execution",
    "create_task",
    "create_plan",
    "activate_plan",
    "execute_plan",
    "stop_plan",
    "pause_dispatch",
    "resume_dispatch",
    "probe_target",
    "get_target_status",
    "get_monitor_status",
    "inspect_project",
    "init_project",
    "start_service",
    "connect_service",
    "disconnect_service",
    "stop_service",
    "get_project_status",
    "get_project",
    "update_project",
    "refresh_project",
    "list_targets",
    "update_target",
    "resolve_target",
}

# 审计在桥接进程内安装，避免把独立 API/Worker 自己的心跳误算为 MCP 写入。
IDLE_GUARD = """
import atexit, json, os, sys
from pathlib import Path
root, home, report = map(Path, sys.argv[1:])
events = []
def audit(event, args):
    if event in ('socket.connect', 'socket.bind'):
        events.append(event)
    if event in ('open', 'sqlite3.connect', 'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename'):
        for item in args[:2] if event == 'os.rename' else args[:1]:
            if isinstance(item, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(item)).absolute()
                if path.is_relative_to(root) or path.is_relative_to(home):
                    events.append(event + ':' + str(path))
def finish():
    report.write_text(json.dumps(events))
atexit.register(finish)
sys.addaudithook(audit)
from comfypilot.mcp_bridge import main
main()
"""


def bridge(
    cwd: Path, *, args: list[str] | None = None, home: Path | None = None, mode: str = "auto"
) -> Client:
    return Client(
        StdioServerParameters(
            command=sys.executable,
            args=args if args is not None else ["-m", "comfypilot.mcp_bridge"],
            cwd=cwd,
            env={"PYTHONDONTWRITEBYTECODE": "1", "HOME": str(home or cwd)},
        ),
        read_timeout_seconds=30,
        mode=mode,
    )


async def call(client: Client, name: str, root: Path, *, error: str | None = None, **args):
    result = await client.call_tool(name, {"project_root": str(root), **args})
    data = result.structured_content
    assert result.is_error == (error is not None), result
    assert data["ok"] == (error is None), result
    assert json.loads(result.content[0].text) == data
    if error:
        assert data["error"]["code"] == error, data
        return data["error"]
    return data["data"]


@pytest.fixture
def projects(tmp_path: Path):
    created = []

    def create(name: str):
        project = initialize_project(resolve_project_root(tmp_path / name))
        created.append(project)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        return project, port

    yield create
    for project in reversed(created):
        stop_service(project)


@pytest.mark.parametrize("state", ["absent", "stopped", "running"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_stdio_initialize_list_and_idle_have_no_project_or_network_io(
    tmp_path: Path, projects, state: str, mode: str
) -> None:
    root = tmp_path / "创作 project"
    if state != "absent":
        project, port = projects(root.name)
        if state == "running":
            start_service(project, port)
    home = tmp_path / "host-home"
    home.mkdir()
    runner = tmp_path / "idle_guard.py"
    runner.write_text(IDLE_GUARD)
    report = tmp_path / "audit.json"

    async def scenario():
        async with bridge(
            root if root.exists() else tmp_path,
            args=[str(runner), str(root), str(home), str(report)],
            home=home,
            mode=mode,
        ) as client:
            assert client.server_info.name == "ComfyPilot"
            assert client.server_capabilities.tools is not None
            for _ in range(2):
                listing = await client.list_tools(cache_mode="bypass")
                assert {tool.name for tool in listing.tools} == TOOLS
                for tool in listing.tools:
                    assert "project_root" in tool.input_schema["required"]
                    assert tool.output_schema is not None
            await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert json.loads(report.read_text()) == []
    assert list(home.iterdir()) == []
    if state == "absent":
        assert not root.exists()
    elif state == "running":
        assert status_service(project)["worker_ready"] is True


def test_explicit_initialization_start_connection_and_input_errors(
    tmp_path: Path, projects
) -> None:
    project, port = projects("project")
    missing = tmp_path / "not-initialized"

    async def scenario():
        async with bridge(tmp_path) as client:
            assert await call(client, "inspect_project", missing) == {
                "initialized": False,
                "root": str(missing),
            }
            assert not missing.exists()
            initialized = await call(client, "init_project", project.root)
            assert initialized["project_id"] == str(project.identity.project_id)
            assert await call(client, "init_project", project.root) == initialized
            await call(client, "get_project_status", project.root, error="service_not_connected")
            await call(client, "connect_service", project.root, error="service_not_running")
            await call(client, "init_project", Path("relative"), error="validation_error")
            await call(client, "start_service", project.root, port=0, error="validation_error")
            await call(client, "start_service", project.root, port="8765", error="validation_error")
            await call(
                client, "inspect_project", project.root, surprise=True, error="validation_error"
            )
            await call(client, "not_a_tool", project.root, error="unknown_tool")
            invalid = await client.call_tool("init_project", {})
            assert (
                invalid.is_error
                and invalid.structured_content["error"]["code"] == "validation_error"
            )
            started = await call(client, "start_service", project.root, port=port)
            assert started["status"] == "running" and "connection_ref" not in started
            await call(client, "get_project_status", project.root, error="service_not_connected")
            connected = await call(client, "connect_service", project.root)
            ref = connected["connection_ref"]
            overview = await call(client, "get_project_status", project.root, connection_ref=ref)
            assert overview["project"]["project_id"] == initialized["project_id"]
            assert overview["service"]["worker_ready"] is True
            assert overview["project"]["dispatch"]["paused"] is True
            await call(client, "disconnect_service", project.root, connection_ref=ref)
            await call(
                client,
                "get_project_status",
                project.root,
                connection_ref=ref,
                error="service_not_connected",
            )
            assert status_service(project)["status"] == "running"

    asyncio.run(scenario())


def test_two_projects_and_two_bridges_do_not_share_connections(tmp_path: Path, projects) -> None:
    first, port_a = projects("first")
    second, port_b = projects("second")

    async def scenario():
        async with bridge(tmp_path) as one, bridge(tmp_path) as two:
            a = await call(one, "start_service", first.root, port=port_a)
            b = await call(one, "start_service", second.root, port=port_b)
            reused = await call(two, "start_service", first.root, port=port_a)
            assert reused["reused"] is True
            assert reused["runtime"]["api"] == a["runtime"]["api"]
            refs = [
                await call(one, "connect_service", first.root),
                await call(one, "connect_service", second.root),
                await call(two, "connect_service", first.root),
            ]
            ref_a, ref_b, ref_other = [item["connection_ref"] for item in refs]
            assert len({ref_a, ref_b, ref_other}) == 3
            await call(
                one, "stop_service", second.root, connection_ref=ref_a, error="project_mismatch"
            )
            await call(
                one,
                "disconnect_service",
                second.root,
                connection_ref=ref_a,
                error="project_mismatch",
            )
            await call(
                two,
                "get_project_status",
                first.root,
                connection_ref=ref_a,
                error="service_not_connected",
            )
            await call(one, "disconnect_service", first.root, connection_ref=ref_a)
            await call(two, "get_project_status", first.root, connection_ref=ref_other)
            await call(two, "stop_service", first.root, connection_ref=ref_other)
            result = await call(one, "get_project_status", second.root, connection_ref=ref_b)
            assert result["service"]["runtime"]["api"] == b["runtime"]["api"]
            assert status_service(first)["status"] == "stopped"
            assert status_service(second)["worker_ready"] is True
            # 生命周期工具允许不建立连接，明确停止给出的项目。
            await call(two, "stop_service", second.root)

    asyncio.run(scenario())


def test_old_connection_cannot_read_or_stop_a_restarted_instance(tmp_path: Path, projects) -> None:
    project, port = projects("project")
    start_service(project, port)

    async def scenario():
        async with bridge(tmp_path) as client:
            previous = await call(client, "connect_service", project.root)
            stop_service(project)
            current = start_service(project, port)
            assert current["runtime"]["instance_id"] != previous["instance_id"]
            for tool in ("get_project_status", "stop_service"):
                await call(
                    client,
                    tool,
                    project.root,
                    connection_ref=previous["connection_ref"],
                    error="connection_invalidated",
                )
            assert status_service(project)["runtime"]["api"] == current["runtime"]["api"]
            fresh = await call(client, "connect_service", project.root)
            await call(
                client, "get_project_status", project.root, connection_ref=fresh["connection_ref"]
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["instance_id", "started_at"])
def test_connection_checks_current_instance_and_process_identity(
    tmp_path: Path, projects, field: str
) -> None:
    project, port = projects("project")
    runtime = start_service(project, port)["runtime"]

    async def scenario():
        async with bridge(tmp_path) as client:
            connected = await call(client, "connect_service", project.root)
            path = project.root / ".comfypilot/runtime.json"
            original = path.read_bytes()
            changed = json.loads(original)
            if field == "instance_id":
                changed[field] = str(uuid4())
            else:
                changed["api"][field] += 1
            path.write_text(json.dumps(changed))
            try:
                for tool in ("get_project_status", "stop_service"):
                    await call(
                        client,
                        tool,
                        project.root,
                        connection_ref=connected["connection_ref"],
                        error="connection_invalidated",
                    )
                assert psutil.pid_exists(runtime["api"]["pid"])
            finally:
                path.write_bytes(original)

    asyncio.run(scenario())


@pytest.mark.parametrize("terminate", [False, True])
def test_host_disconnect_or_bridge_termination_leaves_service_running(
    tmp_path: Path, projects, terminate: bool
) -> None:
    project, port = projects("project")

    async def scenario():
        async with bridge(tmp_path) as client:
            started = await call(client, "start_service", project.root, port=port)
            connected = await call(client, "connect_service", project.root)
            await call(
                client,
                "get_project_status",
                project.root,
                connection_ref=connected["connection_ref"],
            )
            if terminate:
                children = [
                    child
                    for child in psutil.Process().children()
                    if "comfypilot.mcp_bridge" in child.cmdline() and child.cwd() == str(tmp_path)
                ]
                assert len(children) == 1
                children[0].terminate()
                await asyncio.to_thread(children[0].wait, timeout=5)
        status = status_service(project)
        assert status["runtime"]["api"] == started["runtime"]["api"]
        assert status["runtime"]["worker"] == started["runtime"]["worker"]
        assert status["worker_ready"] is True
        assert all(psutil.pid_exists(status["runtime"][role]["pid"]) for role in ("api", "worker"))

    asyncio.run(scenario())
