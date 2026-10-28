import json
import os
import sqlite3

import pytest
from comfypilot.project import ProjectError
from comfypilot.resources import get_resource_file, get_resources, refresh_resources
from test_execution import command, plan_body, prepared
from test_execution import workspace as execution_workspace

workspace = execution_workspace


def test_plan_and_run_freeze_resources_and_parameters_before_disk_refresh(workspace):
    from comfypilot.execution import get_execution

    project, _, _ = workspace
    _, plan_id = prepared(project)
    before = get_execution(project)["plans"][0]["resources"]
    path = project.root / "modules/demo/api.json"
    graph = json.loads(path.read_text())
    graph["1"]["inputs"]["prompt"] = "changed on disk"
    path.write_text(json.dumps(graph))
    refresh_resources(project, 1)
    command(project, "execute_plan", plan_id=plan_id, action_indices=[0])
    run = get_execution(project)["runs"][0]["document"]["resource_snapshot"]
    assert run == before["0"]
    assert run["graph"]["1"]["inputs"]["prompt"] == ""
    assert run["materialization"] == "pending"
    old = run["files"]["project:modules/demo/api.json"]
    assert "changed on disk" not in get_resource_file(project, old)["text"]
    task = command(project, "create_task", goal="新方案", acceptance="拒绝旧hash")
    body = plan_body(project, task["result"]["task_id"])
    body["steps"][0]["recipe_hash"] = before["0"]["recipe_hash"]
    body["mandate"]["allowed_recipes"]["swap"] = before["0"]["recipe_hash"]
    with pytest.raises(ProjectError, match="配方"):
        command(project, "create_plan", **body)


def test_preflight_records_missing_dependencies_and_never_claims_real_test(workspace):
    from comfypilot.resource_preflight import preflight_recipe

    project, instance, calls = workspace
    report = preflight_recipe(project, "swap", "render", instance, os.getpid())
    assert report["status"] == "passed"
    assert report["real_test"] == "not_run"
    assert report["version_verification"] == "unknown"
    with sqlite3.connect(project.database) as db:
        data = json.loads(
            db.execute("SELECT document FROM status_samples WHERE kind='catalog'").fetchone()[0]
        )
        data["node_schemas"].pop("FixtureRender")
        db.execute("UPDATE status_samples SET document=? WHERE kind='catalog'", (json.dumps(data),))
    missing = preflight_recipe(project, "swap", "render", instance, os.getpid())
    assert missing["status"] == "missing_dependencies"
    assert "FixtureRender" in missing["missing_nodes"]
    assert all(method == "GET" for method, _ in calls)
    assert get_resources(project)["recipes"][0]["states"]["real_test"] == "not_run"


def test_claim_rejects_graph_hash_that_does_not_match_frozen_run(workspace):
    from comfypilot.execution import reserve_submission

    project, instance, _ = workspace
    _, plan_id = prepared(project)
    run_id = command(project, "execute_plan", plan_id=plan_id, action_indices=[0])["result"][
        "run_ids"
    ][0]
    command(project, "resume_dispatch")
    with pytest.raises(ProjectError) as error:
        reserve_submission(project, run_id, instance, os.getpid(), "c" * 64)
    assert error.value.code == "resource_changed"


@pytest.mark.parametrize(
    "defect,expected",
    [
        ("required", "invalid"),
        ("model", "invalid"),
        ("range", "invalid"),
        ("catalog_expired", "unknown"),
        ("binding", "unknown"),
    ],
)
def test_preflight_uses_current_node_schema_and_catalog_freshness(workspace, defect, expected):
    from comfypilot.resource_preflight import preflight_recipe

    project, instance, _ = workspace
    with sqlite3.connect(project.database) as db:
        data = json.loads(
            db.execute("SELECT document FROM status_samples WHERE kind='catalog'").fetchone()[0]
        )
        schema = data["node_schemas"]["FixtureRender"]
        if defect == "required":
            schema["inputs"]["extra"] = {"type": "STRING", "required": True}
        elif defect == "model":
            schema["inputs"]["prompt"]["type"] = ["model.safetensors"]
        elif defect == "range":
            schema["inputs"]["seed"]["min"] = 100
        elif defect == "binding":
            schema["inputs"]["video"]["type"] = "CUSTOM_MEDIA"
        else:
            data["expires_at"] = "2000-01-01T00:00:00+00:00"
        db.execute("UPDATE status_samples SET document=? WHERE kind='catalog'", (json.dumps(data),))
    assert preflight_recipe(project, "swap", "render", instance, os.getpid())["status"] == expected


def test_declared_missing_model_is_separate_dependency_evidence(workspace):
    from comfypilot.monitoring import get_target_status
    from comfypilot.resource_preflight import check_frozen_recipe
    from comfypilot.resources import freeze_recipe

    project, instance, _ = workspace
    record = get_resources(project)["recipes"][0]
    frozen = freeze_recipe(
        project,
        "swap",
        record["hash"],
        {"video": "b" * 64},
        {"seed": 0, "prompt": "missing.safetensors"},
    )
    frozen["manifest"]["models"] = [
        {
            "name": "missing.safetensors",
            "source": "https://example.com/model",
            "license": "test",
            "node_id": "1",
            "input_name": "prompt",
            "sha256": None,
        }
    ]
    target = get_target_status(project, instance, os.getpid(), "render")
    target["node_schemas"]["FixtureRender"]["inputs"]["prompt"]["type"] = ["available.safetensors"]
    report = check_frozen_recipe(frozen, target)
    assert report["status"] == "missing_dependencies"
    assert report["missing_models"] == ["missing.safetensors"]


def test_new_catalog_invalidates_preflight_without_changing_configuration(workspace):
    from comfypilot.resource_preflight import list_preflights, preflight_recipe

    project, instance, _ = workspace
    preflight_recipe(project, "swap", "render", instance, os.getpid())
    assert not list_preflights(project, instance, os.getpid())[0]["stale"]
    with sqlite3.connect(project.database) as db:
        row = db.execute("SELECT document FROM status_samples WHERE kind='catalog'").fetchone()
        data = json.loads(row[0])
        data["hash"] = "changed catalog"
        db.execute("UPDATE status_samples SET document=? WHERE kind='catalog'", (json.dumps(data),))
    assert list_preflights(project, instance, os.getpid())[0]["stale"]
