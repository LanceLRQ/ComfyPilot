import asyncio
import os
from uuid import uuid4

import pytest
from comfy_fixture import comfy_server
from comfypilot.execution import get_execution
from comfypilot.project import initialize_project
from comfypilot.resources import refresh_resources
from comfypilot.settings import get_project, update_project
from media_fixture import media_files
from resource_fixture import write_recipe
from test_execution import command, plan_body
from test_media import imported

__all__ = ["media_files"]


@pytest.fixture
def generation_workspace(tmp_path, media_files):
    state = {}
    with comfy_server(busy=False, generation=state) as (address, calls, _):
        project = initialize_project(tmp_path / "project")
        write_recipe(project.root)
        refresh_resources(project, 0)
        for role in ("input", "output"):
            (tmp_path / role).mkdir()
        config = get_project(project)
        update_project(
            project,
            config["revision"],
            {
                **config["config"],
                "targets": [
                    {
                        "id": "render",
                        "name": "测试执行端",
                        "base_url": address,
                        "assets": {
                            "mode": "local",
                            "input_dir": str(tmp_path / "input"),
                            "output_dir": str(tmp_path / "output"),
                        },
                    }
                ],
            },
        )
        asset_id = imported(project, media_files[0], "video")
        from comfypilot.media import get_asset

        digest = get_asset(project, asset_id)["content_hash"]
        task = command(project, "create_task", goal="生成验证", acceptance="外部成功有凭据")
        body = plan_body(project, task["result"]["task_id"])
        for step in body["steps"]:
            step.update(asset_ids={"video": asset_id}, inputs={"video": digest})
        body["mandate"]["allowed_inputs"] = {"video": digest}
        plan = command(project, "create_plan", **body)["result"]["plan_id"]
        command(project, "activate_plan", plan_id=plan)
        command(project, "execute_plan", plan_id=plan, action_indices=[0, 1])
        command(project, "resume_dispatch")
        yield project, uuid4(), state, calls


def tick(workspace):
    from comfypilot.generation import advance_generation

    project, instance, _, _ = workspace
    asyncio.run(advance_generation(project, instance, os.getpid()))
    return get_execution(project)


def test_submission_freezes_final_graph_and_reconciles_after_restart(generation_workspace):
    project, _, state, calls = generation_workspace
    first = tick(generation_workspace)
    run = first["runs"][0]
    assert run["status"] == "submitted" and first["plans"][0]["used_submissions"] == 1
    assert run["intent"]["graph_hash"] == run["document"]["materialized"]["graph_hash"]
    assert state["posts"][0]["prompt"] == run["document"]["materialized"]["graph"]
    assert "ComfyPilot/" in state["posts"][0]["prompt"]["1"]["inputs"]["video"]
    tick(generation_workspace)
    assert len(state["posts"]) == 1
    assert get_execution(project)["runs"][0]["status"] == "running"
    assert not any(method == "POST" and path != "/prompt" for method, path in calls)


def test_lost_receipt_is_matched_only_by_project_run_and_intent(generation_workspace):
    _, _, state, _ = generation_workspace
    state["lost_receipt"] = True
    first = tick(generation_workspace)
    assert first["runs"][0]["status"] == "reconciling"
    assert first["dispatch"]["reconciliation_required"]
    row = state["running"][0]
    row[3]["comfypilot"]["project_id"] = "foreign"
    assert tick(generation_workspace)["runs"][0]["status"] == "reconciling"
    row[3]["comfypilot"]["project_id"] = str(generation_workspace[0].identity.project_id)
    recovered = tick(generation_workspace)
    assert recovered["runs"][0]["status"] == "running"
    assert not recovered["dispatch"]["reconciliation_required"]
    assert len(state["posts"]) == 1 and recovered["plans"][0]["used_submissions"] == 1


def test_history_queue_history_race_needs_completed_success(generation_workspace):
    _, _, state, _ = generation_workspace
    tick(generation_workspace)
    state["running"] = []
    state["race"] = True
    state["history"] = {
        "fixture-prompt": {
            "prompt": state["prompt"],
            "outputs": {},
            "status": {"status_str": "success", "completed": False},
        }
    }
    assert tick(generation_workspace)["runs"][0]["status"] == "reconciling"
    state["history"]["fixture-prompt"]["status"]["completed"] = True
    result = tick(generation_workspace)
    assert result["runs"][0]["status"] == "collecting"
    assert (
        result["runs"][0]["document"]["external_success"]["history"]
        == state["history"]["fixture-prompt"]
    )
    assert len(state["posts"]) == 1


def test_definite_rejection_stops_on_failure_without_retry(generation_workspace):
    _, _, state, _ = generation_workspace
    state["reject"] = True
    result = tick(generation_workspace)
    assert result["runs"][0]["status"] == "failed"
    assert result["plans"][0]["used_submissions"] == 1
    tick(generation_workspace)
    assert len(state["posts"]) == 1


def test_reserved_but_unacknowledged_intent_is_never_posted_again(generation_workspace):
    from comfypilot.execution import reserve_submission
    from comfypilot.monitoring import TargetMonitor
    from comfypilot.project import ExecutionTarget
    from comfypilot.replicas import materialize_run

    project, instance, state, _ = generation_workspace
    run = get_execution(project)["runs"][0]
    final = asyncio.run(materialize_run(project, run["id"]))
    config = get_project(project)
    asyncio.run(
        TargetMonitor(
            project,
            instance,
            os.getpid(),
            config["revision"],
            ExecutionTarget.model_validate(config["config"]["targets"][0]),
        ).poll()
    )
    reserve_submission(project, run["id"], instance, os.getpid(), final["graph_hash"])
    result = tick(generation_workspace)
    assert result["runs"][0]["status"] == "reconciling"
    assert result["plans"][0]["used_submissions"] == 1
    assert not state.get("posts")


def test_known_prompt_is_reconciled_on_original_target_after_config_change(generation_workspace):
    project, _, state, calls = generation_workspace
    tick(generation_workspace)
    config = get_project(project)
    update_project(project, config["revision"], {**config["config"], "targets": []})
    command(project, "stop_plan", plan_id=get_execution(project)["plans"][0]["id"])
    assert tick(generation_workspace)["runs"][0]["status"] == "running"
    assert len(state["posts"]) == 1
    assert ("GET", "/history/fixture-prompt") in calls
    assert get_execution(project)["runs"][1]["status"] == "cancelled"


def test_history_queue_history_rechecks_completion_without_assuming_failure(generation_workspace):
    _, _, state, _ = generation_workspace
    tick(generation_workspace)
    state.update(
        running=[],
        race=True,
        history={
            "fixture-prompt": {
                "prompt": state["prompt"],
                "outputs": {},
                "status": {"status_str": "success", "completed": True},
            }
        },
    )
    assert tick(generation_workspace)["runs"][0]["status"] == "collecting"
    assert state["history_reads"] == 2
    assert len(state["posts"]) == 1
