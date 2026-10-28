import asyncio
import json
import time
from urllib.request import urlopen
from uuid import uuid4

import httpx2
from comfypilot.execution import get_execution
from test_execution import command
from test_feedback import feedback
from test_generation import generation_workspace, media_files
from test_review import review_workspace
from test_service import running_service

__all__ = ["generation_workspace", "media_files", "review_workspace"]


def test_context_and_overview_show_effective_project_name(tmp_path):
    from comfypilot.project import read_project
    from comfypilot.settings import get_project, update_project

    with running_service(tmp_path / "renamed") as (address, _, _):
        project = read_project(tmp_path / "renamed")
        current = get_project(project)
        update_project(project, current["revision"], {**current["config"], "name": "审片项目"})
        with urlopen(address + "/api/project") as response:
            assert json.load(response)["name"] == "审片项目"
        with urlopen(address + "/api/context") as response:
            assert json.load(response)["data"]["project"]["name"] == "审片项目"


def test_fresh_stdio_session_recovers_facts_budget_and_feedback(review_workspace, tmp_path):
    from test_mcp_bridge import bridge, call

    workspace, version, _ = review_workspace
    project = workspace[0]
    item = feedback(project, version)["result"]["feedback_id"]
    with running_service(project.root):

        async def exercise():
            for _ in range(2):
                async with bridge(tmp_path) as client:
                    connected = await call(client, "connect_service", project.root)
                    context = await call(
                        client,
                        "get_context",
                        project.root,
                        connection_ref=connected["connection_ref"],
                    )
                    assert context["project"]["root"] == str(project.root)
                    assert context["revision"] == get_execution(project)["revision"]
                    assert context["authorizations"][0]["used_submissions"] == 1
                    assert context["unresolved_feedback"][0]["id"] == item
                    assert context["candidates"][0]["id"] == version["id"]
                    assert context["active_plans"] and context["active_runs"]
                    assert context["monitoring"]["targets"][0]["capabilities"]
                    assert "resource_snapshot" not in json.dumps(context)
                    assert context["references"]["log_path"].endswith("service.log")
                    sample = await call(
                        client,
                        "probe_target",
                        project.root,
                        target_id="render",
                        connection_ref=connected["connection_ref"],
                    )
                    assert "node_schemas" not in sample and "node_classes" not in sample

        asyncio.run(exercise())


def test_stdio_wait_timeout_pagination_and_reconnect_do_not_lose_events(tmp_path):
    from comfypilot.project import read_project
    from test_mcp_bridge import bridge, call

    with running_service(tmp_path / "wait"):
        project = read_project(tmp_path / "wait")

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                common = {"connection_ref": connected["connection_ref"]}
                initial = await call(client, "get_context", project.root, **common)
                started = time.monotonic()
                timeout = await call(
                    client,
                    "wait_for_events",
                    project.root,
                    after=initial["event_cursor"],
                    timeout_seconds=0,
                    **common,
                )
                assert timeout["timed_out"] and time.monotonic() - started < 2
                waiting = asyncio.create_task(
                    call(
                        client,
                        "wait_for_events",
                        project.root,
                        after=timeout["cursor"],
                        limit=1,
                        timeout_seconds=5,
                        **common,
                    )
                )
                await asyncio.sleep(0.2)
                responsive = time.monotonic()
                await call(client, "get_context", project.root, **common)
                assert time.monotonic() - responsive < 2
                receipts = [
                    command(project, "create_task", goal=str(i), acceptance="check")
                    for i in range(3)
                ]
                page = await waiting
                assert page["events"][0]["event_id"] == receipts[0]["event_id"]
                full = await call(
                    client,
                    "read_events",
                    project.root,
                    after=initial["event_cursor"],
                    limit=1,
                    **common,
                )
                assert full["has_more"]
                cursor = page["cursor"]
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                page = await call(
                    client,
                    "read_events",
                    project.root,
                    connection_ref=connected["connection_ref"],
                    after=cursor,
                    limit=2,
                )
                assert [item["event_id"] for item in page["events"]] == [
                    r["event_id"] for r in receipts[1:]
                ]
                await call(
                    client,
                    "wait_for_events",
                    project.root,
                    connection_ref=connected["connection_ref"],
                    timeout_seconds=26,
                    error="validation_error",
                )
                await call(
                    client,
                    "read_events",
                    project.root,
                    connection_ref=connected["connection_ref"],
                    limit=101,
                    error="validation_error",
                )

        asyncio.run(exercise())


def test_web_and_mcp_same_revision_have_only_one_success(tmp_path):
    from comfypilot.project import read_project
    from test_mcp_bridge import bridge, call

    with running_service(tmp_path / "conflict") as (address, _, _):
        project = read_project(tmp_path / "conflict")

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                current = await call(
                    client, "get_context", project.root, connection_ref=connected["connection_ref"]
                )
                body = dict(
                    command_id=str(uuid4()),
                    expected_revision=current["revision"],
                    goal="Agent draft",
                    acceptance="check",
                )
                webbody = {
                    **body,
                    "command_id": str(uuid4()),
                    "source": "web_user",
                    "action": "create_task",
                    "goal": "Web draft",
                }

                def post():
                    with httpx2.Client() as http:
                        return http.post(
                            address + "/api/execution/command",
                            json=webbody,
                            headers={"X-ComfyPilot-Instance": connected["instance_id"]},
                        )

                agent, web = await asyncio.gather(
                    client.call_tool(
                        "create_task",
                        {
                            "project_root": str(project.root),
                            "connection_ref": connected["connection_ref"],
                            **body,
                        },
                    ),
                    asyncio.to_thread(post),
                )
                assert int(agent.structured_content["ok"]) + int(web.status_code == 200) == 1
                assert body["goal"] == "Agent draft" and webbody["goal"] == "Web draft"
                assert len(get_execution(project)["tasks"]) == 1

        asyncio.run(exercise())


def test_open_mcp_wait_does_not_delay_platform_shutdown(tmp_path):
    from comfypilot.lifecycle import stop_service
    from comfypilot.project import read_project
    from test_mcp_bridge import bridge, call

    with running_service(tmp_path / "stop-wait"):
        project = read_project(tmp_path / "stop-wait")

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                common = {"connection_ref": connected["connection_ref"]}
                context = await call(client, "get_context", project.root, **common)
                waiting = asyncio.create_task(
                    call(
                        client,
                        "wait_for_events",
                        project.root,
                        after=context["event_cursor"],
                        timeout_seconds=25,
                        **common,
                    )
                )
                await asyncio.sleep(0.4)
                started = time.monotonic()
                result = await asyncio.to_thread(stop_service, project)
                assert result["status"] == "stopped" and time.monotonic() - started < 8
                assert (await waiting)["interrupted"]

        asyncio.run(exercise())


def test_context_media_and_run_evidence_are_references(review_workspace):
    workspace, version, _ = review_workspace
    project = workspace[0]
    with running_service(project.root) as (address, _, _):
        with httpx2.Client(base_url=address) as client:
            context = client.get("/api/context").json()["data"]
            run = next(run for run in context["recent_runs"] if run["id"] == version["run_id"])
            assert "materialized" not in run["document"]
            graph = client.get(run["references"]["graph"]).json()["data"]
            assert (
                graph["kind"] == "graph" and graph["evidence"]["1"]["class_type"] == "FixtureRender"
            )
            history = client.get(run["references"]["history"]).json()["data"]["evidence"]
            assert history["status"]["completed"]
