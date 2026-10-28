import asyncio
import json
import sqlite3

import pytest
from comfypilot.project import ProjectError, initialize_project
from comfypilot.settings import get_project, resolve_target, update_project
from media_fixture import media_files
from test_execution import workspace
from test_media import imported

__all__ = ["media_files", "workspace"]


def test_malformed_output_is_reported_as_a_structured_error(tmp_path):
    from comfypilot.replicas import read_output

    project = initialize_project(tmp_path / "project")
    target = local_target(project, tmp_path / "comfy")
    with pytest.raises(ProjectError) as error:
        asyncio.run(read_output(project, target, "run-a", "main", {"filename": None}))
    assert error.value.code == "output_invalid"


def test_known_output_fingerprint_is_preserved_and_path_escape_is_rejected(tmp_path, media_files):
    from comfypilot.replicas import local_path, read_output

    project = initialize_project(tmp_path / "project")
    target = local_target(project, tmp_path / "comfy")
    relative = f"ComfyPilot/{project.identity.project_id}/run-a/main.mp4"
    path = local_path(target, str(project.identity.project_id), "output", relative)
    path.parent.mkdir(parents=True)
    path.write_bytes(media_files[0].read_bytes())
    declaration = {
        "type": "output",
        "subfolder": str(path.parent.relative_to(tmp_path / "comfy/output")),
        "filename": path.name,
    }
    first = asyncio.run(read_output(project, target, "run-a", "main", declaration))
    assert first["managed"] and first["size"] == path.stat().st_size
    assert (
        asyncio.run(read_output(project, target, "run-a", "main", declaration))["id"] == first["id"]
    )
    path.write_bytes(b"replaced")
    with pytest.raises(ProjectError, match="指纹"):
        asyncio.run(read_output(project, target, "run-a", "main", declaration))
    for relative in [f"ComfyPilot/{project.identity.project_id}/../foreign.mp4", "/etc/passwd"]:
        with pytest.raises(ProjectError):
            local_path(target, str(project.identity.project_id), "output", relative)


def local_target(project, root):
    for name in ("input", "output"):
        (root / name).mkdir(parents=True, exist_ok=True)
    config = get_project(project)
    update_project(
        project,
        config["revision"],
        {
            **config["config"],
            "targets": [
                {
                    "id": "render",
                    "name": "本机目录",
                    "base_url": "http://127.0.0.1:8188",
                    "assets": {
                        "mode": "local",
                        "input_dir": str(root / "input"),
                        "output_dir": str(root / "output"),
                    },
                }
            ],
        },
    )
    return resolve_target(project, "render")["target"]


def test_local_replicas_are_scoped_fingerprinted_and_do_not_adopt_unknown_files(
    tmp_path, media_files
):
    from comfypilot.replicas import list_replicas, put_input, read_output

    project = initialize_project(tmp_path / "one")
    target = local_target(project, tmp_path / "comfy")
    asset_id = imported(project, media_files[0], "video")
    replica = asyncio.run(put_input(project, target, asset_id, "run-a"))
    expected = f"ComfyPilot/{project.identity.project_id}/{asset_id}.mp4"
    assert replica["relative_path"] == expected
    assert replica["size"] == media_files[0].stat().st_size
    assert replica["fingerprint"]["sha256"] == replica["content_hash"]
    assert asyncio.run(put_input(project, target, asset_id, "run-b"))["id"] == replica["id"]
    assert len(list_replicas(project)["references"]) == 2
    other = initialize_project(tmp_path / "two")
    other_target = local_target(other, tmp_path / "comfy")
    other_id = imported(other, media_files[0], "video")
    other_replica = asyncio.run(put_input(other, other_target, other_id, "run-a"))
    assert other_replica["relative_path"] != expected
    assert len(list_replicas(project)["replicas"]) == 1
    assert (tmp_path / "comfy/input" / expected).read_bytes() == media_files[0].read_bytes()
    unknown = tmp_path / "comfy/input/unknown.mp4"
    unknown.write_bytes(b"unknown")
    assert not any(
        row["relative_path"] == "unknown.mp4" for row in list_replicas(project)["replicas"]
    )
    outside = asyncio.run(
        read_output(
            project,
            target,
            "run-a",
            "main",
            {"type": "output", "subfolder": "outside", "filename": "movie.mp4"},
        )
    )
    assert outside["managed"] is False and outside["cleanup_eligible"] is False
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert {row[1] for row in db.execute("PRAGMA table_info(asset_references)")} == {
            "owner_id",
            "replica_id",
        }
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_materialized_run_uses_independent_asset_identity_and_output_prefix(
    workspace, media_files, tmp_path
):
    from comfypilot.execution import get_execution
    from comfypilot.replicas import materialize_run
    from comfypilot.resources import refresh_resources
    from resource_fixture import recipe_record
    from test_execution import command, plan_body

    project, _, _ = workspace
    local_target(project, tmp_path / "comfy")
    recipe_path = project.root / "modules/demo/recipe.yaml"
    import yaml

    recipe = yaml.safe_load(recipe_path.read_text())
    recipe["outputs"]["main"]["filename_prefix"] = {"node_id": "1", "input_name": "filename_prefix"}
    recipe_path.write_text(yaml.safe_dump(recipe))
    graph_path = project.root / "modules/demo/api.json"
    graph = json.loads(graph_path.read_text())
    graph["1"]["inputs"]["filename_prefix"] = "old"
    graph_path.write_text(json.dumps(graph))
    snapshot = refresh_resources(project, 1)
    asset_id = imported(project, media_files[0], "video")
    from comfypilot.media import get_asset

    digest = get_asset(project, asset_id)["content_hash"]
    task = command(project, "create_task", goal="素材准备", acceptance="保存最终图")
    body = plan_body(project, task["result"]["task_id"], count=1)
    body["steps"][0].update(asset_ids={"video": asset_id}, inputs={"video": digest})
    body["steps"][0]["recipe_hash"] = recipe_record(snapshot)["hash"]
    body["mandate"]["allowed_recipes"]["swap"] = recipe_record(snapshot)["hash"]
    body["mandate"]["allowed_inputs"] = {"video": digest}
    plan = command(project, "create_plan", **body)["result"]["plan_id"]
    command(project, "activate_plan", plan_id=plan)
    run = command(project, "execute_plan", plan_id=plan, action_indices=[0])["result"]["run_ids"][0]
    final = asyncio.run(materialize_run(project, run))
    assert (
        final["graph"]["1"]["inputs"]["video"]
        == f"ComfyPilot/{project.identity.project_id}/{asset_id}.mp4"
    )
    assert (
        final["graph"]["1"]["inputs"]["filename_prefix"]
        == f"ComfyPilot/{project.identity.project_id}/{run}/main"
    )
    assert final["inputs"][0]["asset_id"] == asset_id
    assert get_execution(project)["runs"][0]["document"]["materialized"] == final
    assert asyncio.run(materialize_run(project, run)) == final


@pytest.mark.parametrize("defect", ["symlink", "tamper", "missing_config"])
def test_adapter_rejects_unsafe_or_changed_input(tmp_path, media_files, defect):
    from comfypilot.media import asset_path
    from comfypilot.replicas import put_input

    project = initialize_project(tmp_path / "project")
    target = local_target(project, tmp_path / "comfy")
    asset_id = imported(project, media_files[0], "video")
    if defect == "symlink":
        (tmp_path / "comfy/input/ComfyPilot").symlink_to(tmp_path, target_is_directory=True)
    elif defect == "tamper":
        asset_path(project, asset_id).write_bytes(b"changed")
    else:
        target["assets"] = None
    with pytest.raises(ProjectError):
        asyncio.run(put_input(project, target, asset_id, "run-a"))
