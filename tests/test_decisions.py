import json
import sqlite3

import pytest
from comfypilot.execution import get_execution
from comfypilot.project import ProjectError
from pydantic import ValidationError
from test_execution import command
from test_feedback import feedback
from test_generation import generation_workspace, media_files, tick
from test_review import review_workspace

__all__ = ["generation_workspace", "media_files", "review_workspace"]


def test_rework_reuses_remaining_budget_and_preserves_previous_intent(review_workspace):
    workspace, version, _ = review_workspace
    project = workspace[0]
    before = get_execution(project)
    old = next(run for run in before["runs"] if run["id"] == version["run_id"])
    body = before["plans"][-1]["document"]
    replacement = command(project, "create_plan", **body)["result"]["plan_id"]
    command(project, "activate_plan", plan_id=replacement)
    with pytest.raises(ProjectError, match="次数|额度"):
        command(project, "execute_plan", plan_id=replacement, action_indices=[0, 1])
    command(project, "execute_plan", plan_id=replacement, action_indices=[0])
    state = tick(workspace)
    assert len(workspace[2]["posts"]) == 2
    historical = next(run for run in state["runs"] if run["id"] == old["id"])
    assert historical["intent"] == old["intent"] and historical["document"] == old["document"]
    assert sum(plan["used_submissions"] for plan in state["plans"]) == 2
    again = command(project, "create_plan", **body)["result"]["plan_id"]
    command(project, "activate_plan", plan_id=again)
    with pytest.raises(ProjectError, match="次数|额度"):
        command(project, "execute_plan", plan_id=again, action_indices=[1])


def test_feedback_only_releases_hold_when_linked_revision_is_activated(review_workspace):
    from comfypilot.review import list_review_items

    workspace, version, _ = review_workspace
    project = workspace[0]
    item = feedback(project, version)["result"]["feedback_id"]
    body = get_execution(project)["plans"][-1]["document"]
    unlinked = command(project, "create_plan", **body)["result"]["plan_id"]
    with pytest.raises(ProjectError, match="反馈"):
        command(project, "activate_plan", plan_id=unlinked)
    linked = command(project, "create_plan", **{**body, "feedback_ids": [item]})["result"][
        "plan_id"
    ]
    assert get_execution(project)["dispatch"]["review_hold"]
    command(project, "activate_plan", plan_id=linked)
    assert not get_execution(project)["dispatch"]["review_hold"]
    stored = list_review_items(project)["feedback"][0]
    assert stored["status"] == "addressed" and stored["plan_id"] != linked
    assert stored["addressed_by_plan_id"] == linked and stored["version_id"] == version["id"]


def test_changed_mandate_requires_explicit_new_user_instruction(review_workspace):
    workspace, _, _ = review_workspace
    project = workspace[0]
    body = json.loads(json.dumps(get_execution(project)["plans"][-1]["document"]))
    body["mandate"]["max_submissions"] = 10
    with pytest.raises(ProjectError, match="新.*指示"):
        command(project, "create_plan", **body)
    body["mandate"]["instruction"] = "用户明确允许另做十次测试候选"
    created = command(project, "create_plan", **body, new_instruction=True)
    assert created["result"]["authorization_id"] == created["result"]["plan_id"]


def test_failure_stop_condition_survives_rework_with_same_instruction(review_workspace):
    workspace, version, _ = review_workspace
    project = workspace[0]
    with sqlite3.connect(project.database) as db:
        db.execute("UPDATE execution_runs SET status='failed' WHERE id=?", (version["run_id"],))
    body = get_execution(project)["plans"][-1]["document"]
    replacement = command(project, "create_plan", **body)["result"]["plan_id"]
    command(project, "activate_plan", plan_id=replacement)
    command(project, "execute_plan", plan_id=replacement, action_indices=[0])
    state = tick(workspace)
    assert len(workspace[2]["posts"]) == 1
    queued = next(run for run in state["runs"] if run["plan_id"] == replacement)
    assert queued["status"] == "queued" and queued["intent"] is None


def test_user_adoption_is_distinct_from_agent_evaluation_and_survives_failure(review_workspace):
    from comfypilot.review import delivery_manifest

    workspace, version, _ = review_workspace
    project = workspace[0]
    command(
        project, "record_evaluation", version_id=version["id"], text="技术检查通过，尚未人工采用"
    )
    assert delivery_manifest(project)["items"] == []
    with pytest.raises(ValidationError):
        command(project, "record_user_decision", version_id=version["id"], decision="adopt")
    decision = command(
        project,
        "record_user_decision",
        version_id=version["id"],
        decision="adopt",
        user_statement="用户说：采用这个片段版本",
        source="agent",
    )
    delivered = delivery_manifest(project)
    assert [item["version_id"] for item in delivered["items"]] == [version["id"]]
    assert delivered["items"][0]["decision_id"] == decision["result"]["decision_id"]
    assert delivered["items"][0]["content_hash"] == version["content_hash"]
    with sqlite3.connect(project.database) as db:
        db.execute("UPDATE execution_runs SET status='failed' WHERE status='queued'")
    assert delivery_manifest(project)["items"][0]["version_id"] == version["id"]
    assert delivery_manifest(project)["items"][0]["user_statement"] == "用户说：采用这个片段版本"


def test_technically_unqualified_version_never_enters_delivery(review_workspace):
    workspace, version, _ = review_workspace
    project = workspace[0]
    version["checks"]["status"] = "failed"
    with sqlite3.connect(project.database) as db:
        db.execute(
            "UPDATE output_versions SET document=? WHERE id=?", (json.dumps(version), version["id"])
        )
    with pytest.raises(ProjectError, match="技术|检查"):
        command(
            project,
            "record_user_decision",
            version_id=version["id"],
            decision="adopt",
            user_statement="采用",
            source="web_user",
        )


def test_stdio_decisions_replay_and_delivery_share_web_state(review_workspace, tmp_path):
    import asyncio
    from uuid import uuid4

    from test_mcp_bridge import bridge, call
    from test_service import running_service

    workspace, version, _ = review_workspace
    project = workspace[0]
    with running_service(project.root):

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", project.root)
                connection = {"connection_ref": connected["connection_ref"]}
                evaluation = await call(
                    client,
                    "record_evaluation",
                    project.root,
                    command_id=str(uuid4()),
                    expected_revision=get_execution(project)["revision"],
                    version_id=version["id"],
                    text="技术合格尚待用户决定",
                    **connection,
                )
                assert evaluation["result"]["adopted"] is False
                assert (await call(client, "get_delivery_manifest", project.root, **connection))[
                    "items"
                ] == []
                args = dict(
                    command_id=str(uuid4()),
                    expected_revision=get_execution(project)["revision"],
                    version_id=version["id"],
                    decision="adopt",
                    user_statement="测试用户明确采用本候选",
                    **connection,
                )
                receipt = await call(client, "record_user_decision", project.root, **args)
                assert await call(client, "record_user_decision", project.root, **args) == receipt
                manifest = await call(client, "get_delivery_manifest", project.root, **connection)
                assert manifest["items"][0]["decision_id"] == receipt["result"]["decision_id"]
                await call(
                    client,
                    "record_user_decision",
                    project.root,
                    error="conflict",
                    **{**args, "command_id": str(uuid4())},
                )

        asyncio.run(exercise())
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA table_info(adopted_versions)").fetchall()
