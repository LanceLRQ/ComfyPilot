import asyncio
import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from comfy_fixture import comfy_server
from comfypilot.collection import collect_run, list_versions
from comfypilot.execution import get_execution
from comfypilot.media import get_asset
from comfypilot.project import ProjectError, initialize_project
from comfypilot.resources import refresh_resources
from comfypilot.settings import get_project, update_project
from media_fixture import media_files
from resource_fixture import write_recipe
from test_execution import command, plan_body
from test_generation import tick
from test_media import imported

__all__ = ["media_files"]


def finish(workspace, run, target):
    project, _, state, _ = workspace
    relative = run["document"]["materialized"]["outputs"]["main"]["prefix"] + ".mp4"
    output = Path(target["assets"]["output_dir"]) / relative
    output.parent.mkdir(parents=True)
    source = (
        Path(target["assets"]["input_dir"]) / state["posts"][0]["prompt"]["1"]["inputs"]["video"]
    )
    output.write_bytes(source.read_bytes())
    state.update(
        running=[],
        history={
            "fixture-prompt": {
                "prompt": state["prompt"],
                "outputs": {
                    "1": {
                        "videos": [
                            {
                                "filename": output.name,
                                "subfolder": str(Path(relative).parent),
                                "type": "output",
                            }
                        ]
                    }
                },
                "status": {"completed": True, "status_str": "success"},
            }
        },
    )
    tick(workspace)
    asyncio.run(collect_run(project, run["id"]))


def test_inflight_target_switch_preserves_original_collection_and_shared_quota(
    tmp_path, media_files
):
    state_a, state_b = {}, {}
    with (
        comfy_server(busy=False, generation=state_a) as (address_a, calls_a, _),
        comfy_server(busy=False, generation=state_b) as (address_b, calls_b, _),
    ):
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
        targets = []
        for name, address in (("render", address_a), ("other", address_b)):
            for role in ("input", "output"):
                (tmp_path / name / role).mkdir(parents=True)
            targets.append(
                {
                    "id": name,
                    "name": name,
                    "base_url": address,
                    "assets": {
                        "mode": "local",
                        "input_dir": str(tmp_path / name / "input"),
                        "output_dir": str(tmp_path / name / "output"),
                    },
                }
            )
        config = get_project(project)
        update_project(
            project,
            config["revision"],
            {**config["config"], "targets": targets, "default_target_id": "render"},
        )
        asset = imported(project, media_files[0], "video")
        digest = get_asset(project, asset)["content_hash"]
        task = command(project, "create_task", goal="切换执行端", acceptance="原端收片且额度累计")
        body = plan_body(project, task["result"]["task_id"])
        for step in body["steps"]:
            step.update(asset_ids={"video": asset}, inputs={"video": digest})
        body["mandate"].update(
            allowed_targets=["render", "other"], allowed_inputs={"video": digest}
        )
        plan_a = command(project, "create_plan", **{**body, "steps": body["steps"][:1]})["result"][
            "plan_id"
        ]
        command(project, "activate_plan", plan_id=plan_a)
        command(project, "execute_plan", plan_id=plan_a, action_indices=[0])
        command(project, "resume_dispatch")
        instance = uuid4()
        workspace_a = project, instance, state_a, calls_a
        workspace_b = project, instance, state_b, calls_b
        tick(workspace_a)
        original = copy.deepcopy(tick(workspace_a)["runs"][0])
        assert original["status"] == "running"
        command(project, "pause_dispatch")
        command(project, "pause_plan", plan_id=plan_a)
        config = get_project(project)
        update_project(
            project, config["revision"], {**config["config"], "default_target_id": "other"}
        )
        for step in body["steps"]:
            step["target_id"] = "other"
        plan_b = command(project, "create_plan", **body)["result"]["plan_id"]
        command(project, "activate_plan", plan_id=plan_b)
        queued = command(project, "execute_plan", plan_id=plan_b, action_indices=[0])
        assert queued["control"]["authorization"]["used_submissions"] == 1
        assert queued["control"]["authorization"]["queued_submissions"] == 1
        state = tick(workspace_b)
        assert plan_b in state["paused_plan_ids"] and state["runs"][1]["intent"] is None
        assert state["runs"][0] == original
        assert not state_b.get("posts")
        finish(workspace_a, original, targets[0])
        collected = copy.deepcopy(get_execution(project)["runs"][0])
        old_version = copy.deepcopy(list_versions(project)["versions"][0])
        assert collected["status"] == "succeeded" and old_version["ready"]
        command(project, "resume_plan", plan_id=plan_b)
        command(project, "resume_dispatch")
        tick(workspace_b)
        second = tick(workspace_b)["runs"][1]
        assert second["status"] == "running"
        # 两端故意返回同一个 prompt_id，仍须按冻结的目标分开对账。
        assert (
            second["document"]["receipt"]["prompt_id"]
            == original["document"]["receipt"]["prompt_id"]
        )
        assert second["intent"]["target_snapshot"]["target"]["id"] == "other"
        finish(workspace_b, second, targets[1])
        state = get_execution(project)
        assert state["runs"][0] == collected
        for key in ("step", "target_snapshot", "resource_snapshot", "materialized", "receipt"):
            assert collected["document"][key] == original["document"][key]
        assert collected["intent"] == original["intent"]
        versions = list_versions(project)["versions"]
        assert len(versions) == 2 and all(version["ready"] for version in versions)
        assert (
            next(version for version in versions if version["id"] == old_version["id"])
            == old_version
        )
        assert len({plan["authorization_id"] for plan in state["plans"]}) == 1
        assert sum(plan["used_submissions"] for plan in state["plans"]) == 2
        with pytest.raises(ProjectError) as error:
            command(project, "execute_plan", plan_id=plan_b, action_indices=[1])
        assert error.value.code == "mandate_exceeded"
        assert get_execution(project) == state
        assert len(state_a["posts"]) == len(state_b["posts"]) == 1
