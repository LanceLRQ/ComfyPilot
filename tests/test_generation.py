import asyncio
import json
import os
from uuid import uuid4

import pytest
import yaml
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


def test_monitor_follows_persisted_submission_client_after_dispatch_and_restart(
    generation_workspace,
):
    from comfypilot.generation import advance_generation
    from comfypilot.monitoring import TargetMonitor, get_target_status
    from comfypilot.project import ExecutionTarget

    project, instance, state, _ = generation_workspace
    target = ExecutionTarget.model_validate(get_project(project)["config"]["targets"][0])

    async def observe():
        monitor = TargetMonitor(project, instance, os.getpid(), 2, target)
        events = asyncio.create_task(monitor.events())
        try:
            async with asyncio.timeout(2):
                while not state.get("websocket_clients"):
                    await asyncio.sleep(0.02)
            assert state["websocket_clients"] == [monitor.client_id]
            await advance_generation(project, instance, os.getpid())
            intent = get_execution(project)["runs"][0]["intent"]["client_id"]
            async with asyncio.timeout(3):
                while True:
                    snapshot = get_target_status(project, instance, os.getpid(), "render")
                    if snapshot["node_progress"]["availability"] == "fresh":
                        break
                    await asyncio.sleep(0.02)
            assert snapshot["stage"]["value"]["prompt_id"] == "fixture-prompt"
            assert snapshot["node_progress"]["value"]["value"] == 2
            assert state["websocket_clients"][-1] == intent
        finally:
            events.cancel()
            await asyncio.gather(events, return_exceptions=True)

        recovered = TargetMonitor(project, uuid4(), os.getpid(), 2, target)
        events = asyncio.create_task(recovered.events())
        try:
            async with asyncio.timeout(2):
                while len(state["websocket_clients"]) < 3:
                    await asyncio.sleep(0.02)
            assert state["websocket_clients"][-1] == intent
            changed = target.model_copy(update={"base_url": "http://127.0.0.1:1"})
            other = TargetMonitor(project, instance, os.getpid(), 2, changed)
            assert other.event_client_id() == other.client_id
            from comfypilot.generation import save_run

            run = get_execution(project)["runs"][0]
            save_run(project, run["id"], "collecting", {})
            async with asyncio.timeout(2):
                while state["websocket_clients"][-1] != recovered.client_id:
                    await asyncio.sleep(0.02)
            snapshot = get_target_status(project, recovered.instance_id, os.getpid(), "render")
            assert snapshot["node_progress"]["value"] is None
            assert snapshot["stage"]["value"] is None
            assert len(state["posts"]) == 1
        finally:
            events.cancel()
            await asyncio.gather(events, return_exceptions=True)

    asyncio.run(observe())


@pytest.fixture
def generation_workspace(tmp_path, media_files):
    state = {}
    with comfy_server(busy=False, generation=state) as (address, calls, _):
        project = initialize_project(tmp_path / "project")
        recipe = write_recipe(project.root)
        recipe["outputs"]["main"].update(
            filename_prefix={"node_id": "1", "input_name": "filename_prefix"},
            spec={"width": 64, "height": 48, "fps": 24, "frame_count": 96, "duration_seconds": 4},
        )
        (project.root / "modules/demo/recipe.yaml").write_text(yaml.safe_dump(recipe))
        graph_path = project.root / "modules/demo/api.json"
        graph = json.loads(graph_path.read_text())
        graph["1"]["inputs"]["filename_prefix"] = "output"
        graph_path.write_text(json.dumps(graph))
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
