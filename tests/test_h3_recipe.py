import asyncio
import copy
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from comfy_fixture import comfy_server
from comfypilot.monitoring import TargetMonitor, get_target_status
from comfypilot.project import ExecutionTarget, ProjectError, initialize_project
from comfypilot.resource_preflight import check_frozen_recipe, list_preflights, preflight_recipe
from comfypilot.resources import freeze_recipe, get_resource_file, refresh_resources
from comfypilot.settings import get_project, update_project


@pytest.fixture
def h3(tmp_path):
    catalog = json.loads((Path(__file__).parent / "fixtures/h3-object-info.json").read_text())
    with comfy_server(catalog=catalog, busy=False) as (address, calls, _):
        project = initialize_project(tmp_path)
        config = get_project(project)["config"]
        update_project(
            project,
            1,
            {
                **config,
                "targets": [
                    {"id": name, "name": name, "base_url": address} for name in ("ready", "missing")
                ],
            },
        )
        instance = uuid4()
        snapshot = refresh_resources(project, 0)
        records = [item for item in snapshot["recipes"] if item["id"] == "minimax-h3-replacement"]
        assert len(records) == 1, "安装包需要提供首发 H3 配方"
        record = records[0]
        assert record["states"]["declaration_complete"], record["errors"]
        frozen = freeze_recipe(
            project,
            record["id"],
            record["hash"],
            {"video": "a" * 64, "reference": "b" * 64},
            {name: item["default"] for name, item in record["manifest"]["parameters"].items()},
        )
        for name in ("ready", "missing"):
            if name == "missing":
                catalog["UNETLoader"]["input"]["required"]["unet_name"][0] = ["other.safetensors"]
            asyncio.run(
                TargetMonitor(
                    project,
                    instance,
                    os.getpid(),
                    2,
                    ExecutionTarget(id=name, name=name, base_url=address),
                ).poll()
            )
        yield project, instance, frozen, calls


def test_builtin_h3_graph_binds_media_parameters_and_output_without_uploading(h3):
    project, instance, frozen, calls = h3
    target = get_target_status(project, instance, os.getpid(), "ready")
    report = check_frozen_recipe(frozen, target)
    assert report["status"] == "passed", report
    assert report["materialization"] == "pending"
    assert report["real_test"] == "not_run"
    assert report["version_verification"] == "unknown"
    assert frozen["graph"]["136"]["inputs"]["ref_images.ref_image_0"] == ["137", 0]
    assert frozen["graph"]["136"]["inputs"]["ref_videos.ref_video_0"] == ["201", 0]
    assert frozen["manifest"]["outputs"]["main"] == {
        "node_id": "92",
        "key": "images",
        "kind": "video",
        "filename_prefix": {"node_id": "92", "input_name": "filename_prefix"},
    }
    assert frozen["graph"]["92"]["inputs"]["format"] == "mp4"
    assert frozen["graph"]["92"]["inputs"]["format.codec"] == "h264"
    assert len(frozen["manifest"]["models"]) == 4
    assert frozen["manifest"]["custom_nodes"] == []
    assert all(model["sha256"] for model in frozen["manifest"]["models"])
    canvas = next(value for key, value in frozen["files"].items() if key.endswith("/canvas.json"))
    drawing = json.loads(get_resource_file(project, canvas)["text"])
    assert {str(node["id"]): node["type"] for node in drawing["nodes"]} == {
        key: node["class_type"] for key, node in frozen["graph"].items()
    }
    for node in drawing["nodes"]:
        inputs = frozen["graph"][str(node["id"])]["inputs"]
        for key, value in node.get("widgets_values_named", {}).items():
            if key in inputs and not isinstance(inputs[key], list):
                assert value == inputs[key], (node["id"], key)
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("defect", ["autogrow", "branch", "required", "enum", "range", "media"])
def test_h3_v3_schema_rejects_invalid_flat_inputs_and_selected_branches(h3, defect):
    project, instance, frozen, _ = h3
    graph = frozen["graph"]
    if defect == "autogrow":
        graph["136"]["inputs"]["ref_images.ref_image_9"] = ["137", 0]
    elif defect == "branch":
        graph["92"]["inputs"].update({"format.codec": "auto", "format.codec.encoding": "auto"})
    elif defect == "required":
        graph["92"]["inputs"].pop("format.codec")
    elif defect == "enum":
        graph["136"]["inputs"]["ref_image_size"] = "unsupported"
    elif defect == "range":
        graph["136"]["inputs"]["width"] = 16
    else:
        # 未声明为媒体上传槽位的枚举不能忽略选项校验。
        target = get_target_status(project, instance, os.getpid(), "ready")
        target["node_schemas"]["LoadVideo"]["inputs"]["file"].pop("upload", None)
        assert check_frozen_recipe(frozen, target)["status"] == "unknown"
        return
    report = check_frozen_recipe(frozen, get_target_status(project, instance, os.getpid(), "ready"))
    assert report["status"] == "invalid", report
    assert report["errors"]


def test_h3_nested_branch_requires_only_active_fields(h3):
    project, instance, frozen, _ = h3
    target = get_target_status(project, instance, os.getpid(), "ready")
    frozen["graph"]["92"]["inputs"]["format.codec.encoding"] = "re-encode"
    assert check_frozen_recipe(frozen, target)["status"] == "invalid"
    frozen["graph"]["92"]["inputs"]["format.codec.encoding.crf"] = 23.0
    assert check_frozen_recipe(frozen, target)["status"] == "passed"
    frozen["graph"]["92"]["inputs"]["format.codec.encoding.crf"] = 80
    assert check_frozen_recipe(frozen, target)["status"] == "invalid"


def test_h3_preflight_evidence_is_scoped_to_target_not_an_installation_receipt(h3):
    project, instance, _, _ = h3
    missing = preflight_recipe(project, "minimax-h3-replacement", "missing", instance, os.getpid())
    assert missing["status"] == "missing_dependencies"
    assert missing["missing_models"] == ["minimax_h3_ref2va_pruned_int8_convrot.safetensors"]
    ready = preflight_recipe(project, "minimax-h3-replacement", "ready", instance, os.getpid())
    assert ready["status"] == "passed"
    reports = {item["target_id"]: item for item in list_preflights(project, instance, os.getpid())}
    assert reports["missing"]["status"] == "missing_dependencies"
    assert reports["ready"]["status"] == "passed"
    assert all(item["real_test"] == "not_run" for item in reports.values())


def test_h3_parameters_cannot_silently_change_frame_grid_or_model(h3):
    project, _, frozen, _ = h3
    parameters = copy.deepcopy(frozen["parameters"])
    parameters["length"] = 125
    with pytest.raises(ProjectError, match="参数"):
        freeze_recipe(
            project, frozen["recipe_id"], frozen["recipe_hash"], frozen["inputs"], parameters
        )
