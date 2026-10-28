import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from comfypilot.collection import collect_run, list_versions, version_path
from comfypilot.execution import get_execution
from comfypilot.replicas import list_replicas
from test_collection import completed
from test_execution import command
from test_generation import generation_workspace, media_files

__all__ = ["generation_workspace", "media_files"]


def collected(workspace):
    run_id, path = completed(workspace)
    asyncio.run(collect_run(workspace[0], run_id))
    return run_id, path, list_versions(workspace[0])["versions"][0]


def preview(project, **fields):
    from comfypilot.cleanup import CleanupScope, inspect_storage

    return asyncio.run(inspect_storage(project, CleanupScope(target_id="render", **fields)))


def test_preview_never_deletes_and_protects_pending_review_and_future_inputs(generation_workspace):
    project = generation_workspace[0]
    _, path, version = collected(generation_workspace)
    original = path.read_bytes()
    before = list_replicas(project)
    result = preview(project)
    assert len(result["items"]) == 2
    by_role = {item["role"]: item for item in result["items"]}
    assert "pending_review" in by_role["output"]["protection_reasons"]
    assert "plan_reference" in by_role["input"]["protection_reasons"]
    assert result["estimated_bytes"] == 0
    assert path.read_bytes() == original
    assert list_replicas(project) == before
    assert version_path(project, version["id"], "original").is_file()


def test_reviewed_output_becomes_eligible_but_unverified_feedback_protects(generation_workspace):
    project = generation_workspace[0]
    _, _, version = collected(generation_workspace)
    command(
        project,
        "record_user_decision",
        version_id=version["id"],
        decision="adopt",
        user_statement="测试用户明确采用此合成验收结果",
    )
    output = next(x for x in preview(project)["items"] if x["role"] == "output")
    assert output["eligible"] and output["references"], output["protection_reasons"]
    assert output["fingerprint"]["sha256"] == version["content_hash"]
    command(
        project, "save_feedback", version_id=version["id"], kind="note", text="尚未确认的测试备注"
    )
    output = next(x for x in preview(project)["items"] if x["role"] == "output")
    assert "unresolved_feedback" in output["protection_reasons"]


def test_plan_freezes_scope_and_original_fingerprint(generation_workspace):
    from comfypilot.cleanup import PrepareCleanup, prepare_cleanup

    project = generation_workspace[0]
    _, path, version = collected(generation_workspace)
    command(
        project,
        "record_user_decision",
        version_id=version["id"],
        decision="reject",
        user_statement="测试拒绝该合成候选",
    )
    replica = next(x for x in list_replicas(project)["replicas"] if x["role"] == "output")
    request = PrepareCleanup(
        target_id="render",
        replica_ids=[replica["id"]],
        user_instruction="仅清理指定的已审阅测试输出副本",
    )
    plan = asyncio.run(prepare_cleanup(project, request))
    assert len(plan["items"]) == 1 and plan["estimated_bytes"] == path.stat().st_size, plan
    assert plan["items"][0]["replica_id"] == replica["id"]
    assert plan["reference_revision"] == get_execution(project)["revision"]
    path.write_bytes(b"new file at same name")
    with sqlite3.connect(project.database) as db:
        frozen = json.loads(
            db.execute("SELECT document FROM cleanup_plans WHERE id=?", (plan["id"],)).fetchone()[0]
        )
    assert frozen == plan
    with pytest.raises(Exception) as error:
        preview(project, replica_ids=[str(uuid4())])
    assert getattr(error.value, "code", "") == "replica_unavailable"


def test_cancelled_run_inputs_need_local_original_and_known_owner(generation_workspace):
    project = generation_workspace[0]
    _, _, _ = collected(generation_workspace)
    plan = get_execution(project)["plans"][0]["id"]
    command(project, "stop_plan", plan_id=plan)
    item = next(x for x in preview(project)["items"] if x["role"] == "input")
    assert item["eligible"]
    with sqlite3.connect(project.database) as db:
        db.execute(
            "INSERT INTO asset_references VALUES (?,?)", ("unknown-owner", item["replica_id"])
        )
    item = next(x for x in preview(project)["items"] if x["role"] == "input")
    assert "unknown_reference" in item["protection_reasons"]


def test_stdio_preview_matches_web_and_rejects_unknown_origins(generation_workspace, tmp_path):
    import httpx2
    from test_mcp_bridge import bridge, call
    from test_service import running_service

    project = generation_workspace[0]
    collected(generation_workspace)
    command(project, "pause_dispatch")
    with running_service(project.root) as (address, overview, _):

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                result = await call(
                    client,
                    "inspect_storage",
                    project.root,
                    connection_ref=connected["connection_ref"],
                    target_id="render",
                )
                with httpx2.Client() as http:
                    headers = {"X-ComfyPilot-Instance": overview["instance_id"]}
                    web = http.post(
                        address + "/api/cleanup/inspect",
                        json={"target_id": "render"},
                        headers=headers,
                    )
                    assert web.status_code == 200
                    assert web.json()["data"]["items"] == result["items"]
                    blocked = http.post(
                        address + "/api/cleanup/prepare",
                        json={"target_id": "render", "user_instruction": "clean"},
                        headers={**headers, "Origin": "https://untrusted.example"},
                    )
                    assert blocked.status_code == 403
                plan = await call(
                    client,
                    "prepare_cleanup",
                    project.root,
                    connection_ref=connected["connection_ref"],
                    target_id="render",
                    user_instruction="仅预览测试项目，不执行删除",
                )
                assert plan["id"] and plan["estimated_bytes"] == 0

        asyncio.run(exercise())


def ready_cleanup(workspace):
    from comfypilot.cleanup import PrepareCleanup, prepare_cleanup

    project = workspace[0]
    _, path, version = collected(workspace)
    command(
        project,
        "record_user_decision",
        version_id=version["id"],
        decision="adopt",
        user_statement="仅采用合成测试结果，用于验证清理保护",
    )
    replica = next(x for x in list_replicas(project)["replicas"] if x["role"] == "output")
    plan = asyncio.run(
        prepare_cleanup(
            project,
            PrepareCleanup(
                target_id="render",
                replica_ids=[replica["id"]],
                user_instruction="仅删除该测试输出副本",
            ),
        )
    )
    return plan, path, version


def execute_cleanup_for(project, plan_id, command_id=None):
    from comfypilot.cleanup import ExecuteCleanup, execute_cleanup

    return asyncio.run(
        execute_cleanup(
            project, ExecuteCleanup(plan_id=plan_id, command_id=command_id or str(uuid4()))
        )
    )


def test_cleanup_unlinks_only_replica_and_old_receipt_cannot_delete_replacement(
    generation_workspace,
):
    project = generation_workspace[0]
    plan, path, version = ready_cleanup(generation_workspace)
    original = version_path(project, version["id"], "original").read_bytes()
    cid = str(uuid4())
    result = execute_cleanup_for(project, plan["id"], cid)
    assert result["items"][0]["status"] == "deleted"
    assert result["deleted_bytes"] > 0 and not path.exists()
    assert version_path(project, version["id"], "original").read_bytes() == original
    path.write_bytes(b"new owner at same name")
    assert execute_cleanup_for(project, plan["id"], cid) == result
    assert path.read_bytes() == b"new owner at same name"
    retry = execute_cleanup_for(project, plan["id"])
    assert retry["items"][0]["status"] == "changed"
    assert path.read_bytes() == b"new owner at same name"


def test_new_feedback_after_preview_protects_remote_copy(generation_workspace):
    project = generation_workspace[0]
    plan, path, version = ready_cleanup(generation_workspace)
    command(
        project,
        "save_feedback",
        version_id=version["id"],
        kind="needs_changes",
        text="清理预览之后新增的测试修改意见",
    )
    result = execute_cleanup_for(project, plan["id"])
    assert result["items"][0]["status"] == "protected" and path.exists()


def test_deleting_claim_blocks_new_plan_and_partial_failure_can_retry(
    generation_workspace, monkeypatch
):
    from comfypilot import cleanup
    from comfypilot.project import ProjectError

    project = generation_workspace[0]
    plan, path, _ = ready_cleanup(generation_workspace)
    original = cleanup.unlink_replica
    active = get_execution(project)["plans"][0]["document"]

    async def failing(project, replica, operation_id):
        body = {k: active[k] for k in ("task_id", "steps", "mandate")}
        with pytest.raises(ProjectError) as error:
            command(project, "create_plan", **body)
        assert error.value.code == "cleanup_in_progress"
        raise OSError("simulated disk error")

    monkeypatch.setattr(cleanup, "unlink_replica", failing)
    failed = execute_cleanup_for(project, plan["id"])
    assert failed["items"][0]["status"] == "failed" and path.exists()
    monkeypatch.setattr(cleanup, "unlink_replica", original)
    done = execute_cleanup_for(project, plan["id"])
    assert done["items"][0]["status"] == "deleted" and not path.exists()
    missing = execute_cleanup_for(project, plan["id"])
    assert missing["items"][0]["status"] == "already_missing"


def test_cleaned_input_can_upload_again_without_old_plan_deleting_it(generation_workspace):
    from comfypilot.cleanup import PrepareCleanup, prepare_cleanup
    from comfypilot.replicas import local_path, put_input

    project = generation_workspace[0]
    run_id, _, _ = collected(generation_workspace)
    command(project, "stop_plan", plan_id=get_execution(project)["plans"][0]["id"])
    replica = next(x for x in list_replicas(project)["replicas"] if x["role"] == "input")
    plan = asyncio.run(
        prepare_cleanup(
            project,
            PrepareCleanup(
                target_id="render", replica_ids=[replica["id"]], user_instruction="测试输入副本重传"
            ),
        )
    )
    result = execute_cleanup_for(project, plan["id"])
    assert result["items"][0]["status"] == "deleted"
    uploaded = asyncio.run(
        put_input(project, replica["target_snapshot"], replica["asset_id"], run_id)
    )
    assert uploaded["fingerprint"] != replica["fingerprint"]
    assert execute_cleanup_for(project, plan["id"])["items"][0]["status"] == "changed"
    assert local_path(
        replica["target_snapshot"],
        str(project.identity.project_id),
        "input",
        replica["relative_path"],
    ).is_file()


def test_stdio_executes_fixed_cleanup_and_returns_same_receipt(generation_workspace, tmp_path):
    import httpx2
    from test_mcp_bridge import bridge, call
    from test_service import running_service

    project = generation_workspace[0]
    plan, path, version = ready_cleanup(generation_workspace)
    command(project, "pause_dispatch")
    with running_service(project.root) as (address, _, _):

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                cid = str(uuid4())
                result = await call(
                    client,
                    "execute_cleanup",
                    project.root,
                    connection_ref=connected["connection_ref"],
                    plan_id=plan["id"],
                    command_id=cid,
                )
                assert result["items"][0]["status"] == "deleted", result
                repeated = await call(
                    client,
                    "execute_cleanup",
                    project.root,
                    connection_ref=connected["connection_ref"],
                    plan_id=plan["id"],
                    command_id=cid,
                )
                assert repeated == result
                history = await call(
                    client, "list_cleanup", project.root, connection_ref=connected["connection_ref"]
                )
                assert history["receipts"] == [result]
                with httpx2.Client() as http:
                    assert http.get(address + "/api/cleanup").json()["data"] == history
                assert (
                    not path.exists() and version_path(project, version["id"], "original").is_file()
                )

        asyncio.run(exercise())
