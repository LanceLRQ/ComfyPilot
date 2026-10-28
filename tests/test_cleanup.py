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
