import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from comfypilot.execution import get_execution
from comfypilot.project import ProjectError
from test_execution import command
from test_generation import generation_workspace, media_files, tick

__all__ = ["generation_workspace", "media_files"]


def test_plan_pause_survives_restart_and_resume_keeps_other_gates(generation_workspace):
    project, _, external, _ = generation_workspace
    state = get_execution(project)
    plan = state["plans"][0]["id"]
    paused = command(project, "pause_plan", plan_id=plan)
    assert paused["control"]["paused_plan_ids"] == [plan]
    tick(generation_workspace)
    assert external.get("posts", []) == []
    from comfypilot.project import read_project

    assert get_execution(read_project(project.root))["paused_plan_ids"] == [plan]
    command(project, "pause_dispatch")
    resumed = command(project, "resume_plan", plan_id=plan)
    assert resumed["control"]["dispatch"]["manual_pause"]
    assert not resumed["control"]["paused_plan_ids"]
    tick(generation_workspace)
    assert external.get("posts", []) == []
    command(project, "resume_dispatch")
    tick(generation_workspace)
    assert len(external["posts"]) == 1


def test_cancel_accepts_only_named_unsubmitted_runs_atomically(generation_workspace):
    project, _, _, _ = generation_workspace
    state = get_execution(project)
    run = state["runs"][0]
    plan = run["plan_id"]
    more = command(project, "execute_plan", plan_id=plan, action_indices=[1])["result"]["run_ids"][
        0
    ]
    tick(generation_workspace)
    with pytest.raises(ProjectError, match="等待|意图"):
        command(project, "cancel_queued_runs", plan_id=plan, run_ids=[run["id"], more])
    after = get_execution(project)
    assert next(item for item in after["runs"] if item["id"] == more)["status"] == "queued"
    receipt = command(project, "cancel_queued_runs", plan_id=plan, run_ids=[more])
    assert receipt["affected_steps"] == [{"run_id": more, "plan_id": plan, "action_index": 1}]
    current = next(item for item in get_execution(project)["runs"] if item["id"] == run["id"])
    assert current["intent"] == after["runs"][0]["intent"]
    assert current["status"] != "cancelled"


def test_cancel_replay_and_stale_revision_do_not_change_results(generation_workspace):
    from comfypilot.execution import COMMAND_SCHEMA, apply_command

    project = generation_workspace[0]
    state = get_execution(project)
    run = state["runs"][0]
    body = dict(
        action="cancel_queued_runs",
        plan_id=run["plan_id"],
        run_ids=[run["id"]],
        expected_revision=state["revision"],
        command_id=str(uuid4()),
    )
    receipt = apply_command(project, COMMAND_SCHEMA.validate_python(body))
    assert apply_command(project, COMMAND_SCHEMA.validate_python(body)) == receipt
    with pytest.raises(ProjectError, match="修订"):
        apply_command(project, COMMAND_SCHEMA.validate_python({**body, "command_id": str(uuid4())}))
    assert receipt["event_id"] and receipt["revision"] > state["revision"]
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA table_info(plan_pauses)").fetchall()


def test_stdio_control_and_run_queries_share_application_state(generation_workspace, tmp_path):
    from test_mcp_bridge import bridge, call
    from test_service import running_service

    project = generation_workspace[0]
    with running_service(project.root):

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                common = {"connection_ref": connected["connection_ref"]}
                state = await call(client, "get_system_status", project.root, **common)
                runs = await call(client, "list_runs", project.root, **common)
                run = runs["runs"][0]
                assert (
                    run
                    == (await call(client, "get_run", project.root, run_id=run["id"], **common))[
                        "run"
                    ]
                )
                assert "resource_snapshot" not in json.dumps(run)
                args = {
                    **common,
                    "command_id": str(uuid4()),
                    "expected_revision": state["revision"],
                    "plan_id": run["plan_id"],
                }
                paused = await call(client, "pause_plan", project.root, **args)
                assert await call(client, "pause_plan", project.root, **args) == paused
                await call(
                    client,
                    "resume_plan",
                    project.root,
                    error="conflict",
                    **{**args, "command_id": str(uuid4())},
                )
                assert (await call(client, "get_collection_status", project.root, **common))[
                    "collections"
                ] == []
                assert (await call(client, "get_system_status", project.root, **common))[
                    "event_cursor"
                ] == paused["event_id"]
                cancelled = await call(
                    client,
                    "cancel_queued_runs",
                    project.root,
                    **{
                        **args,
                        "command_id": str(uuid4()),
                        "expected_revision": paused["revision"],
                        "run_ids": [run["id"]],
                    },
                )
                resumed = await call(
                    client,
                    "resume_plan",
                    project.root,
                    **{
                        **args,
                        "command_id": str(uuid4()),
                        "expected_revision": cancelled["revision"],
                    },
                )
                body = get_execution(project)["plans"][0]["document"]
                proposed = await call(
                    client,
                    "propose_plan",
                    project.root,
                    **common,
                    **body,
                    command_id=str(uuid4()),
                    expected_revision=resumed["revision"],
                )
                assert proposed["result"]["status"] == "draft"

        asyncio.run(exercise())


def test_plan_pause_is_inherited_by_revision_without_blocking_other_tasks(generation_workspace):
    project = generation_workspace[0]
    original = get_execution(project)["plans"][0]
    command(project, "pause_plan", plan_id=original["id"])
    body = original["document"]
    replacement = command(project, "create_plan", **body)["result"]["plan_id"]
    command(project, "activate_plan", plan_id=replacement)
    command(project, "execute_plan", plan_id=replacement, action_indices=[0])
    assert get_execution(project)["paused_plan_ids"] == [replacement]
    task = command(project, "create_task", goal="独立任务", acceptance="独立派发")["result"][
        "task_id"
    ]
    other = command(project, "create_plan", **{**body, "task_id": task})["result"]["plan_id"]
    command(project, "activate_plan", plan_id=other)
    command(project, "execute_plan", plan_id=other, action_indices=[0])
    state = tick(generation_workspace)
    assert next(run for run in state["runs"] if run["plan_id"] == other)["intent"]
    assert next(run for run in state["runs"] if run["plan_id"] == replacement)["intent"] is None


def test_stdio_resume_collection_does_not_submit_or_consume_budget(generation_workspace, tmp_path):
    from comfypilot.generation import save_run
    from test_collection import completed
    from test_mcp_bridge import bridge, call
    from test_service import running_service

    project, _, external, _ = generation_workspace
    run_id, _ = completed(generation_workspace)
    save_run(
        project,
        run_id,
        "collecting",
        {"collection": {"phase": "blocked", "error": {"code": "disk_full"}}},
    )
    with running_service(project.root):

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                args = dict(
                    connection_ref=connected["connection_ref"],
                    command_id=str(uuid4()),
                    expected_revision=get_execution(project)["revision"],
                    run_id=run_id,
                )
                receipt = await call(client, "resume_collection", project.root, **args)
                assert await call(client, "resume_collection", project.root, **args) == receipt
                assert receipt["affected_steps"][0]["run_id"] == run_id

        asyncio.run(exercise())
    state = get_execution(project)
    assert state["plans"][0]["used_submissions"] == 1 and len(external["posts"]) == 1


def test_cancel_racing_intent_reservation_has_only_one_winner(generation_workspace):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from comfypilot.execution import reserve_submission
    from comfypilot.monitoring import TargetMonitor
    from comfypilot.project import ExecutionTarget
    from comfypilot.replicas import materialize_run
    from comfypilot.settings import get_project

    project, instance, _, _ = generation_workspace
    run = get_execution(project)["runs"][0]
    final = asyncio.run(materialize_run(project, run["id"]))
    asyncio.run(
        TargetMonitor(
            project,
            instance,
            os.getpid(),
            get_project(project)["revision"],
            ExecutionTarget.model_validate(final["target_snapshot"]["target"]),
        ).poll()
    )
    revision = get_execution(project)["revision"]
    barrier = Barrier(2)

    def act(cancel):
        barrier.wait()
        try:
            if cancel:
                return command(
                    project,
                    "cancel_queued_runs",
                    plan_id=run["plan_id"],
                    run_ids=[run["id"]],
                    expected_revision=revision,
                )
            return reserve_submission(
                project, run["id"], instance, os.getpid(), final["graph_hash"]
            )
        except ProjectError as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(act, [True, False]))
    assert sum(isinstance(item, dict) for item in results) == 1
    current = get_execution(project)["runs"][0]
    assert (current["status"] == "cancelled") != (current["intent"] is not None)
