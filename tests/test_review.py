import asyncio
import json
import sqlite3

import pytest
import yaml
from comfypilot.collection import collect_run, list_versions
from comfypilot.execution import get_execution
from comfypilot.media import (
    ExtractClipRequest,
    get_asset,
    get_media_job,
    process_next_media_job,
    queue_clip,
)
from comfypilot.resources import refresh_resources
from test_collection import completed
from test_execution import command, plan_body
from test_generation import generation_workspace, media_files

__all__ = ["generation_workspace", "media_files"]


@pytest.fixture
def review_workspace(generation_workspace):
    project, _, _, _ = generation_workspace
    original = get_execution(project)["runs"][0]["document"]["step"]["asset_ids"]["video"]
    job = queue_clip(
        project,
        ExtractClipRequest(
            source_asset_id=original, start_seconds=1.0, end_seconds=3.0, width=64, height=48
        ),
    )
    asyncio.run(process_next_media_job(project))
    asset = get_asset(project, get_media_job(project, job["id"])["asset_id"])
    path = project.root / "modules/demo/recipe.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["outputs"]["main"]["spec"].update(frame_count=48, duration_seconds=2)
    manifest["outputs"]["main"]["time_mapping"] = {"input_slot": "video", "mode": "identity"}
    path.write_text(yaml.safe_dump(manifest))
    refresh_resources(project, 1)
    task = get_execution(project)["tasks"][0]["id"]
    body = plan_body(project, task)
    for step in body["steps"]:
        step.update(asset_ids={"video": asset["id"]}, inputs={"video": asset["content_hash"]})
    body["mandate"]["allowed_inputs"] = {"video": asset["content_hash"]}
    plan = command(project, "create_plan", **body)["result"]["plan_id"]
    command(project, "activate_plan", plan_id=plan)
    queued = command(project, "execute_plan", plan_id=plan, action_indices=[0, 1])
    run_id, _ = completed(generation_workspace, queued["result"]["run_ids"][0])
    asyncio.run(collect_run(project, run_id))
    version = list_versions(project)["versions"][0]
    return generation_workspace, version, original


def test_review_exposes_offset_only_for_a_declared_and_verified_time_map(review_workspace):
    from comfypilot.review import get_review_version

    workspace, version, original = review_workspace
    detail = get_review_version(workspace[0], version["id"])
    assert detail["comparison"]["status"] == "known"
    assert detail["comparison"]["source_asset_id"] == original
    assert detail["comparison"]["offset_seconds"] == 1.0
    assert detail["comparison"]["duration_seconds"] == 2.0
    assert detail["version"]["source"]["prompt_id"] == "fixture-prompt"


def test_review_does_not_infer_temporal_alignment_from_matching_duration(generation_workspace):
    from comfypilot.review import get_review_version

    project, _, _, _ = generation_workspace
    run_id, _ = completed(generation_workspace)
    asyncio.run(collect_run(project, run_id))
    version = list_versions(project)["versions"][0]
    detail = get_review_version(project, version["id"])
    assert detail["comparison"]["status"] == "unknown"
    assert detail["comparison"]["source_asset_id"]
    assert "offset_seconds" not in detail["comparison"]
    assert "声明" in detail["comparison"]["reason"]


def test_declared_map_is_unavailable_when_output_timing_does_not_match(review_workspace):
    from comfypilot.review import get_review_version

    workspace, version, _ = review_workspace
    version["media"]["duration_seconds"] = 3
    with sqlite3.connect(workspace[0].database) as db:
        db.execute(
            "UPDATE output_versions SET document=? WHERE id=?", (json.dumps(version), version["id"])
        )
    assert get_review_version(workspace[0], version["id"])["comparison"]["status"] == "unknown"
