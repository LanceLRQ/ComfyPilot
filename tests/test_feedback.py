import asyncio
import json
import sqlite3
from uuid import uuid4

import httpx2
import pytest
from comfypilot.execution import COMMAND_SCHEMA, apply_command, get_execution
from comfypilot.project import ProjectError
from pydantic import ValidationError
from test_execution import command
from test_generation import generation_workspace, media_files, tick
from test_review import review_workspace
from test_service import running_service

__all__ = ["generation_workspace", "media_files", "review_workspace"]


def feedback(project, version, **fields):
    return command(
        project,
        "save_feedback",
        version_id=version["id"],
        kind="needs_changes",
        text="这里的人物需要修改",
        start_seconds=0.5,
        end_seconds=1.0,
        regions=[{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}],
        source="web_user",
        **fields,
    )


def test_feedback_and_task_hold_commit_together_and_resume_preserves_hold(review_workspace):
    from comfypilot.review import list_review_items

    workspace, version, _ = review_workspace
    project = workspace[0]
    before = get_execution(project)
    saved = feedback(project, version)
    items = list_review_items(project)["feedback"]
    assert len(items) == 1
    item = items[0]
    assert item["version_id"] == version["id"] and item["status"] == "open"
    assert item["original_range"] == {"start_seconds": 1.5, "end_seconds": 2.0}
    assert item["regions"][0]["x"] == 0.1 and item["source"] == "web_user"
    state = get_execution(project)
    assert state["event_cursor"] == saved["event_id"] == before["event_cursor"] + 1
    assert state["dispatch"]["review_hold"]
    command(project, "resume_dispatch")
    tick(workspace)
    assert len(workspace[2]["posts"]) == 1
    assert get_execution(project)["dispatch"]["review_hold"]
    command(
        project,
        "update_feedback",
        feedback_id=item["id"],
        status="addressed",
        note="已记录修改方案",
    )
    assert list_review_items(project)["feedback"][0]["status"] == "addressed"
    assert get_execution(project)["dispatch"]["review_hold"]


def test_feedback_does_not_block_an_unrelated_task(review_workspace):
    workspace, version, _ = review_workspace
    project = workspace[0]
    feedback(project, version)
    task = command(project, "create_task", goal="独立任务", acceptance="继续执行")["result"][
        "task_id"
    ]
    body = get_execution(project)["plans"][-1]["document"].copy()
    body["task_id"] = task
    plan = command(project, "create_plan", **body)["result"]["plan_id"]
    command(project, "activate_plan", plan_id=plan)
    queued = command(project, "execute_plan", plan_id=plan, action_indices=[0])["result"][
        "run_ids"
    ][0]
    state = tick(workspace)
    assert next(run for run in state["runs"] if run["id"] == queued)["intent"]
    assert len(workspace[2]["posts"]) == 2


def test_existing_intent_keeps_reconciling_after_feedback(review_workspace):
    workspace, version, _ = review_workspace
    state = tick(workspace)
    active = next(run for run in state["runs"] if run["status"] == "submitted")
    feedback(workspace[0], version)
    after = tick(workspace)
    assert next(run for run in after["runs"] if run["id"] == active["id"])["status"] == "running"
    assert len(workspace[2]["posts"]) == 2


def test_old_version_feedback_is_not_moved_to_new_plan(review_workspace):
    from comfypilot.review import list_review_items

    workspace, version, _ = review_workspace
    project = workspace[0]
    body = get_execution(project)["plans"][-1]["document"]
    new = command(project, "create_plan", **body)["result"]["plan_id"]
    command(project, "activate_plan", plan_id=new)
    feedback(project, version)
    assert not get_execution(project)["dispatch"]["review_hold"]
    assert list_review_items(project)["feedback"][0]["plan_id"] != new


@pytest.mark.parametrize(
    "fields",
    [
        {"start_seconds": -1.0},
        {"end_seconds": 0.1},
        {"end_seconds": 100.0},
        {"regions": [{"x": 0.9, "y": 0.1, "width": 0.5, "height": 0.5}]},
        {"screenshot_id": str(uuid4())},
    ],
)
def test_feedback_rejects_invalid_range_region_and_attachment(review_workspace, fields):
    workspace, version, _ = review_workspace
    project = workspace[0]
    body = dict(
        action="save_feedback",
        command_id=str(uuid4()),
        expected_revision=get_execution(project)["revision"],
        version_id=version["id"],
        kind="note",
        text="备注",
        start_seconds=0.5,
        end_seconds=1.0,
    )
    body.update(fields)
    before = get_execution(project)["revision"]
    with pytest.raises((ProjectError, ValidationError)):
        apply_command(project, COMMAND_SCHEMA.validate_python(body))
    assert get_execution(project)["revision"] == before


def test_screenshot_is_a_bounded_reference_and_must_match_version(review_workspace, media_files):
    workspace, version, _ = review_workspace
    project = workspace[0]
    with running_service(project.root) as (address, overview, _):
        with httpx2.Client(base_url=address, timeout=10) as client:
            headers = {"X-ComfyPilot-Instance": overview["instance_id"]}
            response = client.post(
                "/api/review/screenshots",
                params={"version_id": version["id"], "candidate_seconds": 0.5},
                headers=headers,
                content=media_files[1].read_bytes(),
            )
            assert response.status_code == 200, response.text
            attachment = response.json()["data"]
            assert "base64" not in json.dumps(attachment)
            assert client.get(attachment["url"]).headers["content-type"] == "image/png"
            saved = feedback(project, version, screenshot_id=attachment["id"])
            assert saved["result"]["screenshot_id"] == attachment["id"]
            assert (
                client.post(
                    "/api/review/screenshots",
                    params={"version_id": version["id"], "candidate_seconds": 0.5},
                    content=b"bad",
                ).status_code
                == 409
            )
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA table_info(review_feedback)").fetchall()


def test_stdio_feedback_is_idempotent_and_visible_in_web_snapshot(review_workspace, tmp_path):
    from test_mcp_bridge import bridge, call

    workspace, version, _ = review_workspace
    project = workspace[0]
    with running_service(project.root) as (address, _, _):

        async def exercise():
            async with bridge(tmp_path) as client:
                connection = await call(client, "connect_service", project.root)
                args = dict(
                    connection_ref=connection["connection_ref"],
                    command_id=str(uuid4()),
                    expected_revision=get_execution(project)["revision"],
                    version_id=version["id"],
                    kind="needs_changes",
                    text="用户要求修改人物",
                    start_seconds=0.5,
                    end_seconds=1.0,
                )
                receipt = await call(client, "save_feedback", project.root, **args)
                assert await call(client, "save_feedback", project.root, **args) == receipt
                items = await call(
                    client,
                    "list_review_items",
                    project.root,
                    connection_ref=connection["connection_ref"],
                )
                assert len(items["feedback"]) == 1
                await call(
                    client,
                    "save_feedback",
                    project.root,
                    error="conflict",
                    **{**args, "command_id": str(uuid4())},
                )
                return receipt

        receipt = asyncio.run(exercise())
        with httpx2.Client(base_url=address, timeout=5) as client:
            snapshot = client.get("/api/state").json()["data"]
            assert snapshot["event_cursor"] == receipt["event_id"]
            assert snapshot["feedback"][0]["id"] == receipt["result"]["feedback_id"]


def test_failed_event_rolls_back_feedback_and_hold(review_workspace, monkeypatch):
    import comfypilot.execution as execution
    from comfypilot.review import list_review_items

    workspace, version, _ = review_workspace
    project = workspace[0]
    before = get_execution(project)

    def fail(*args, **kwargs):
        raise ProjectError("event_failed", "模拟事件事务失败")

    monkeypatch.setattr(execution, "record_change", fail)
    with pytest.raises(ProjectError, match="模拟事件事务失败"):
        feedback(project, version)
    after = get_execution(project)
    assert not after["dispatch"]["review_hold"] and after["revision"] == before["revision"]
    assert list_review_items(project)["feedback"] == []


def test_agent_addressed_does_not_become_user_verified(review_workspace):
    from comfypilot.review import list_review_items

    workspace, version, _ = review_workspace
    project = workspace[0]
    item = feedback(project, version)["result"]["feedback_id"]
    with pytest.raises(ProjectError, match="用户验证需要"):
        command(
            project, "update_feedback", feedback_id=item, status="verified", note="技术检查通过"
        )
    assert get_execution(project)["dispatch"]["review_hold"]
    command(
        project,
        "update_feedback",
        feedback_id=item,
        status="verified",
        source="web_user",
        note="用户明确确认问题已解决",
    )
    assert list_review_items(project)["feedback"] == []
    assert not get_execution(project)["dispatch"]["review_hold"]
