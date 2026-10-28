import asyncio
import json
import os
import socket
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from comfy_fixture import comfy_server
from comfypilot.project import ExecutionTarget, ProjectError, initialize_project
from comfypilot.resources import get_resources, refresh_resources
from comfypilot.settings import get_project, update_project
from resource_fixture import recipe_record, write_recipe


@pytest.fixture
def workspace(tmp_path):
    from comfypilot.monitoring import TargetMonitor

    with comfy_server(busy=False) as (address, calls, _):
        project = initialize_project(tmp_path)
        write_recipe(tmp_path)
        refresh_resources(project, 0)
        config = get_project(project)["config"]
        update_project(
            project,
            1,
            {**config, "targets": [{"id": "render", "name": "可控目标", "base_url": address}]},
        )
        instance = uuid4()
        asyncio.run(
            TargetMonitor(
                project,
                instance,
                os.getpid(),
                2,
                ExecutionTarget.model_validate(get_project(project)["config"]["targets"][0]),
            ).poll()
        )
        yield project, instance, calls


def command(project, action, **fields):
    from comfypilot.execution import COMMAND_SCHEMA, apply_command, get_execution

    return apply_command(
        project,
        COMMAND_SCHEMA.validate_python(
            {
                "action": action,
                "command_id": str(uuid4()),
                "expected_revision": get_execution(project)["revision"],
                **fields,
            }
        ),
    )


def plan_body(project, task_id, *, count=2, limit=2):
    return {
        "task_id": task_id,
        "steps": [
            {
                "recipe_id": "swap",
                "recipe_hash": recipe_record(get_resources(project))["hash"],
                "inputs": {"video": "b" * 64},
                "parameters": {"seed": index, "prompt": ""},
                "target_id": "render",
                "required_nodes": ["FixtureRender"],
            }
            for index in range(count)
        ],
        "mandate": {
            "instruction": "用户允许制作这两个候选，失败停止",
            "source": "用户对话",
            "allowed_recipes": {"swap": recipe_record(get_resources(project))["hash"]},
            "allowed_targets": ["render"],
            "allowed_inputs": {"video": "b" * 64},
            "allowed_parameters": {"seed": list(range(count)), "prompt": [""]},
            "max_submissions": limit,
            "stop_conditions": ["on_failure", "after_limit"],
        },
    }


def frozen_hash(project, run_id):
    from comfypilot.execution import get_execution

    return next(
        run["document"]["resource_snapshot"]["graph_hash"]
        for run in get_execution(project)["runs"]
        if run["id"] == run_id
    )


def prepared(project, *, count=2, limit=2):
    task = command(project, "create_task", goal="人物替换", acceptance="人物一致，动作保留")
    plan = command(
        project,
        "create_plan",
        **plan_body(project, task["result"]["task_id"], count=count, limit=limit),
    )
    command(project, "activate_plan", plan_id=plan["result"]["plan_id"])
    return task["result"]["task_id"], plan["result"]["plan_id"]


def test_command_replay_is_idempotent_and_new_stale_command_conflicts(workspace):
    from comfypilot.execution import COMMAND_SCHEMA, apply_command, get_execution

    project, _, _ = workspace
    body = {
        "action": "create_task",
        "command_id": str(uuid4()),
        "expected_revision": 0,
        "goal": "人物替换",
        "acceptance": "保留动作",
    }
    original = apply_command(project, COMMAND_SCHEMA.validate_python(body))
    assert apply_command(project, COMMAND_SCHEMA.validate_python(body)) == original
    assert len(get_execution(project)["tasks"]) == 1
    with pytest.raises(ProjectError) as error:
        apply_command(project, COMMAND_SCHEMA.validate_python({**body, "command_id": str(uuid4())}))
    assert error.value.code == "conflict"
    with sqlite3.connect(project.database) as db:
        assert db.execute("SELECT COUNT(*) FROM business_events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1


def test_scope_validation_and_missing_mandate_do_not_create_plans(workspace):
    from comfypilot.execution import COMMAND_SCHEMA, get_execution
    from pydantic import ValidationError

    project, _, _ = workspace
    task = command(project, "create_task", goal="换人", acceptance="保留动作")
    body = plan_body(project, task["result"]["task_id"])
    with pytest.raises(ValidationError):
        COMMAND_SCHEMA.validate_python(
            {
                "action": "create_plan",
                "command_id": str(uuid4()),
                "expected_revision": 1,
                "task_id": body["task_id"],
                "steps": body["steps"],
            }
        )
    body["steps"][0]["parameters"]["seed"] = 999
    with pytest.raises(ProjectError) as error:
        command(project, "create_plan", **body)
    assert error.value.code == "mandate_exceeded"
    assert get_execution(project)["plans"] == []


def test_prompt_values_are_saved_exactly_as_authorized(workspace):
    from comfypilot.execution import get_execution

    project, _, _ = workspace
    task = command(project, "create_task", goal="换人", acceptance="保留动作")
    body = plan_body(project, task["result"]["task_id"])
    body["steps"][0]["parameters"]["prompt"] = "  用户指定的提示词  "
    body["mandate"]["allowed_parameters"]["prompt"] = ["", "  用户指定的提示词  "]
    command(project, "create_plan", **body)
    assert (
        get_execution(project)["plans"][0]["document"]["steps"][0]["parameters"]["prompt"]
        == "  用户指定的提示词  "
    )


def test_enqueue_is_unique_and_limited_and_draft_does_not_replace_active_plan(workspace):
    from comfypilot.execution import get_execution

    project, _, calls = workspace
    task_id, plan_id = prepared(project, count=3, limit=2)
    queued = command(project, "execute_plan", plan_id=plan_id, action_indices=[0, 1])
    duplicate = command(project, "execute_plan", plan_id=plan_id, action_indices=[0])
    assert duplicate["result"]["run_ids"] == queued["result"]["run_ids"][:1]
    with pytest.raises(ProjectError) as error:
        command(project, "execute_plan", plan_id=plan_id, action_indices=[2])
    assert error.value.code == "mandate_exceeded"
    command(project, "create_plan", **plan_body(project, task_id))
    state = get_execution(project)
    assert state["tasks"][0]["active_plan_id"] == plan_id
    assert all(run["status"] == "queued" for run in state["runs"])
    assert all(method == "GET" for method, _ in calls)


def test_two_claims_serialize_and_intent_quota_event_are_atomic(workspace):
    from comfypilot.execution import get_execution, reserve_submission

    project, instance, calls = workspace
    _, plan_id = prepared(project)
    queued = command(project, "execute_plan", plan_id=plan_id, action_indices=[0, 1])
    command(project, "resume_dispatch")

    def claim(run_id):
        try:
            return reserve_submission(
                project,
                run_id,
                instance,
                os.getpid(),
                frozen_hash(project, run_id),
            )
        except ProjectError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, queued["result"]["run_ids"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "dispatch_busy" in results
    state = get_execution(project)
    assert sum(run["status"] == "dispatching" for run in state["runs"]) == 1
    with sqlite3.connect(project.database) as db:
        assert db.execute("SELECT used_submissions FROM execution_plans").fetchall() == [(1,)]
        assert (
            db.execute("SELECT COUNT(*) FROM execution_runs WHERE intent IS NOT NULL").fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM business_events WHERE action='submission_reserved'"
            ).fetchone()[0]
            == 1
        )
    assert all(method == "GET" for method, _ in calls)


def test_activation_and_stop_cancel_only_unsubmitted_runs_and_preserve_intent(workspace):
    from comfypilot.execution import get_execution, reserve_submission

    project, instance, _ = workspace
    task_id, plan_id = prepared(project)
    queued = command(project, "execute_plan", plan_id=plan_id, action_indices=[0, 1])
    command(project, "resume_dispatch")
    run_id = queued["result"]["run_ids"][0]
    reserve_submission(
        project,
        run_id,
        instance,
        os.getpid(),
        frozen_hash(project, run_id),
    )
    replacement = command(project, "create_plan", **plan_body(project, task_id))
    command(project, "activate_plan", plan_id=replacement["result"]["plan_id"])
    state = get_execution(project)
    assert {run["status"] for run in state["runs"]} == {"dispatching", "cancelled"}
    with pytest.raises(ProjectError):
        command(project, "execute_plan", plan_id=plan_id, action_indices=[0])
    command(project, "execute_plan", plan_id=replacement["result"]["plan_id"], action_indices=[0])
    command(project, "stop_plan", plan_id=replacement["result"]["plan_id"])
    assert all(run["status"] != "queued" for run in get_execution(project)["runs"])


@pytest.mark.parametrize("reason", ["review_hold", "reconciliation_required"])
def test_resume_cannot_clear_other_dispatch_holds(workspace, reason):
    from comfypilot.execution import get_execution, reserve_submission

    project, instance, _ = workspace
    _, plan_id = prepared(project)
    queued = command(project, "execute_plan", plan_id=plan_id, action_indices=[0])
    with sqlite3.connect(project.database) as db:
        db.execute(f"UPDATE execution_control SET {reason}=1")
    command(project, "resume_dispatch")
    run_id = queued["result"]["run_ids"][0]
    assert get_execution(project)["dispatch"][reason] is True
    with pytest.raises(ProjectError) as error:
        reserve_submission(
            project,
            run_id,
            instance,
            os.getpid(),
            frozen_hash(project, run_id),
        )
    assert error.value.code == "dispatch_paused"


def test_claim_rechecks_target_revision_capability_and_external_queue(workspace):
    from comfypilot.execution import reserve_submission

    project, instance, _ = workspace
    _, plan_id = prepared(project)
    queued = command(project, "execute_plan", plan_id=plan_id, action_indices=[0])
    command(project, "resume_dispatch")
    run_id = queued["result"]["run_ids"][0]
    with sqlite3.connect(project.database) as db:
        row = db.execute("SELECT document FROM status_samples WHERE kind='http'").fetchone()
        value = json.loads(row[0])
        value["metrics"]["queue_running"]["value"] = 1
        db.execute("UPDATE status_samples SET document=? WHERE kind='http'", (json.dumps(value),))
    with pytest.raises(ProjectError) as error:
        reserve_submission(
            project,
            run_id,
            instance,
            os.getpid(),
            frozen_hash(project, run_id),
        )
    assert error.value.code == "external_queue_busy"
    config = get_project(project)["config"]
    update_project(project, 2, {**config, "targets": [{**config["targets"][0], "enabled": False}]})
    with pytest.raises(ProjectError) as error:
        reserve_submission(
            project,
            run_id,
            instance,
            os.getpid(),
            frozen_hash(project, run_id),
        )
    assert error.value.code == "target_changed"


@pytest.mark.parametrize("missing", ["nodes", "worker", "freshness"])
def test_missing_or_stale_capabilities_refuse_before_reserving_quota(workspace, missing):
    from comfypilot.execution import get_execution, reserve_submission

    project, instance, _ = workspace
    _, plan_id = prepared(project)
    queued = command(project, "execute_plan", plan_id=plan_id, action_indices=[0])
    command(project, "resume_dispatch")
    run_id = queued["result"]["run_ids"][0]
    before = get_execution(project)["event_cursor"]
    with sqlite3.connect(project.database) as db:
        if missing == "nodes":
            db.execute("DELETE FROM status_samples WHERE kind='catalog'")
        elif missing == "freshness":
            data = json.loads(
                db.execute(
                    "SELECT document FROM status_samples WHERE kind='connection'"
                ).fetchone()[0]
            )
            data["connection"]["expires_at"] = "2000-01-01T00:00:00+00:00"
            db.execute(
                "UPDATE status_samples SET document=? WHERE kind='connection'", (json.dumps(data),)
            )
    with pytest.raises(ProjectError) as error:
        reserve_submission(
            project,
            queued["result"]["run_ids"][0],
            instance,
            os.getpid() + (1 if missing == "worker" else 0),
            frozen_hash(project, run_id),
        )
    assert error.value.code == "capability_unavailable"
    state = get_execution(project)
    assert state["event_cursor"] == before
    assert state["plans"][0]["used_submissions"] == 0
    assert state["runs"][0]["intent"] is None


def test_failed_event_rolls_back_intent_and_quota_and_replay_does_not_consume_twice(workspace):
    from comfypilot.execution import get_execution, reserve_submission

    project, instance, _ = workspace
    _, plan_id = prepared(project)
    queued = command(project, "execute_plan", plan_id=plan_id, action_indices=[0])
    command(project, "resume_dispatch")
    run_id = queued["result"]["run_ids"][0]
    with sqlite3.connect(project.database) as db:
        db.execute(
            "CREATE TRIGGER reject_test_event BEFORE INSERT ON business_events "
            "WHEN NEW.action='submission_reserved' BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        reserve_submission(
            project,
            run_id,
            instance,
            os.getpid(),
            frozen_hash(project, run_id),
        )
    state = get_execution(project)
    assert state["plans"][0]["used_submissions"] == 0
    assert state["runs"][0]["intent"] is None
    with sqlite3.connect(project.database) as db:
        db.execute("DROP TRIGGER reject_test_event")
    first = reserve_submission(
        project,
        run_id,
        instance,
        os.getpid(),
        frozen_hash(project, run_id),
    )
    replay = reserve_submission(
        project,
        run_id,
        instance,
        os.getpid(),
        frozen_hash(project, run_id),
    )
    assert replay["intent"] == first["intent"]
    assert replay["reused"] is True
    assert get_execution(project)["plans"][0]["used_submissions"] == 1


def test_execution_schema_has_real_constraints_and_monitoring_does_not_advance_business_revision(
    workspace,
):
    from comfypilot.execution import get_execution
    from comfypilot.monitoring import TargetMonitor

    project, instance, _ = workspace
    before = get_execution(project)
    target = ExecutionTarget.model_validate(get_project(project)["config"]["targets"][0])
    asyncio.run(TargetMonitor(project, instance, os.getpid(), 2, target).poll())
    after = get_execution(project)
    assert (after["revision"], after["event_cursor"]) == (
        before["revision"],
        before["event_cursor"],
    )
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
        columns = {row[1] for row in db.execute("PRAGMA table_info(execution_runs)")}
        assert columns == {"id", "plan_id", "action_index", "status", "document", "intent"}
        indices = {row[1] for row in db.execute("PRAGMA index_list(execution_runs)")}
        assert "one_active_generation" in indices


def test_schema_four_upgrade_preserves_configuration_and_samples(workspace):
    from comfypilot.execution import get_execution
    from comfypilot.project import load_project

    project, _, _ = workspace
    with sqlite3.connect(project.database) as db:
        before = db.execute(
            "SELECT target_id,kind,document FROM status_samples ORDER BY kind"
        ).fetchall()
        for table in (
            "command_receipts",
            "business_events",
            "execution_runs",
            "execution_plans",
            "execution_tasks",
            "execution_control",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=4")
    upgraded = load_project(project.root)
    assert get_project(upgraded)["revision"] == 2
    assert get_execution(upgraded)["revision"] == 0
    with sqlite3.connect(project.database) as db:
        assert (
            before
            == db.execute(
                "SELECT target_id,kind,document FROM status_samples ORDER BY kind"
            ).fetchall()
        )


def test_real_stdio_and_http_share_command_receipts_and_control_boundary(workspace):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from comfypilot.lifecycle import start_service, stop_service
    from mcp import Client, StdioServerParameters

    project, _, calls = workspace
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    runtime = start_service(project, port)["runtime"]
    url = f"http://127.0.0.1:{port}"

    async def scenario():
        async with Client(
            StdioServerParameters(command=sys.executable, args=["-m", "comfypilot.mcp_bridge"]),
            read_timeout_seconds=20,
        ) as client:

            async def tool(name, **kwargs):
                response = await client.call_tool(
                    name, {"project_root": str(project.root), **kwargs}
                )
                assert not response.is_error, response
                return response.structured_content["data"]

            connection = await tool("connect_service")
            ref = {"connection_ref": connection["connection_ref"]}
            resources = await tool("list_resources", **ref)
            assert resources["revision"] == 1
            refreshed = await tool("refresh_resources", **ref, expected_revision=1)
            assert refreshed["hash"] == resources["hash"]
            resource_hash = recipe_record(resources)["files"]["project:modules/demo/canvas.json"]
            original = await tool("get_resource_file", **ref, resource_hash=resource_hash)
            assert json.loads(original["text"])["version"] == 0.4
            await tool("probe_target", **ref, target_id="render")
            report = await tool("preflight_recipe", **ref, recipe_id="swap", target_id="render")
            assert report["status"] == "passed" and report["real_test"] == "not_run"
            with urlopen(url + "/api/resources", timeout=3) as response:
                http_resources = json.load(response)["data"]
            assert http_resources == await tool("list_resources", **ref)
            state = await tool("get_execution", **ref)
            assert state["revision"] == 0
            created = await tool(
                "create_task",
                **ref,
                command_id=str(uuid4()),
                expected_revision=0,
                goal="换人",
                acceptance="保留动作",
            )
            plan = await tool(
                "create_plan",
                **ref,
                command_id=str(uuid4()),
                expected_revision=1,
                **plan_body(project, created["result"]["task_id"]),
            )
            plan_id = plan["result"]["plan_id"]
            await tool(
                "activate_plan",
                **ref,
                command_id=str(uuid4()),
                expected_revision=2,
                plan_id=plan_id,
            )
            body = {
                "command_id": str(uuid4()),
                "expected_revision": 3,
                "plan_id": plan_id,
                "action_indices": [0, 1],
            }
            queued = await tool("execute_plan", **ref, **body)
            assert await tool("execute_plan", **ref, **body) == queued
            await tool("pause_dispatch", **ref, command_id=str(uuid4()), expected_revision=4)
            await tool("resume_dispatch", **ref, command_id=str(uuid4()), expected_revision=5)
            stopped = await tool(
                "stop_plan", **ref, command_id=str(uuid4()), expected_revision=6, plan_id=plan_id
            )
            assert len(stopped["result"]["cancelled_run_ids"]) == 2
            with urlopen(url + "/api/execution", timeout=3) as response:
                http = json.load(response)["data"]
            assert http == await tool("get_execution", **ref)
            assert http["revision"] == 7
            assert all(run["status"] == "cancelled" for run in http["runs"])

    try:
        asyncio.run(scenario())
        body = json.dumps(
            {
                "action": "create_task",
                "command_id": str(uuid4()),
                "expected_revision": 0,
                "goal": "旧请求",
                "acceptance": "保留",
            }
        ).encode()
        for headers, status in [
            ({"X-ComfyPilot-Instance": runtime["instance_id"]}, 409),
            (
                {
                    "X-ComfyPilot-Instance": runtime["instance_id"],
                    "Origin": "https://other.example",
                },
                403,
            ),
            ({}, 409),
        ]:
            request = Request(
                url + "/api/execution/command",
                data=body,
                headers={"Content-Type": "application/json", **headers},
            )
            with pytest.raises(HTTPError) as error:
                urlopen(request, timeout=3)
            assert error.value.code == status
        assert all(method == "GET" for method, _ in calls)
    finally:
        stop_service(project)
