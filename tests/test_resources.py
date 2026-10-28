import hashlib
import json
import sqlite3

import pytest
import yaml
from comfypilot.project import ProjectError, initialize_project
from resource_fixture import recipe_record, write_recipe


def test_refresh_discovers_sources_and_immutable_file_content_without_importing_code(tmp_path):
    from comfypilot.resources import get_resource_file, get_resources, refresh_resources

    project = initialize_project(tmp_path)
    write_recipe(tmp_path)
    code = tmp_path / "custom_nodes/example.py"
    code.write_text("raise RuntimeError('resource files must never execute')\n")
    assert get_resources(project)["revision"] == 0
    snapshot = refresh_resources(project, 0)
    assert snapshot["revision"] == 1
    assert {module["id"] for module in snapshot["modules"]} == {
        "demo",
        "video-character-replacement",
    }
    recipe = recipe_record(snapshot)
    assert recipe["source"] == "project:modules/demo/recipe.yaml"
    assert recipe["states"]["declaration_complete"] is True
    assert recipe["states"]["real_test"] == "not_run"
    assert recipe["api_graph"]["1"]["inputs"]["seed"] == 0
    raw = next(
        file for file in snapshot["files"] if file["source"] == "project:custom_nodes/example.py"
    )
    assert raw["hash"] == hashlib.sha256(code.read_bytes()).hexdigest()
    assert get_resource_file(project, raw["hash"])["text"] == code.read_text()
    assert refresh_resources(project, 1)["revision"] == 1
    code.write_text("new source\n")
    assert get_resource_file(project, raw["hash"])["text"].startswith("raise RuntimeError")
    assert get_resources(project)["revision"] == 1
    assert refresh_resources(project, 1)["revision"] == 2
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert {row[1] for row in db.execute("PRAGMA table_info(resource_blobs)")} == {
            "hash",
            "content",
        }


def test_workflow_limit_uses_effective_project_configuration(tmp_path):
    from comfypilot.resources import refresh_resources
    from comfypilot.settings import get_project, update_project

    project = initialize_project(tmp_path)
    config = get_project(project)["config"]
    config["import_limits"]["workflow_bytes"] = 1024
    update_project(project, 1, config)
    with pytest.raises(ProjectError, match="import_limits.workflow_bytes"):
        refresh_resources(project, 0)


@pytest.mark.parametrize("defect", ["canvas", "bad_link", "cycle", "missing_binding", "output"])
def test_invalid_execution_graph_is_discovered_but_never_declared_executable(tmp_path, defect):
    from comfypilot.resources import refresh_resources

    project = initialize_project(tmp_path)
    recipe = write_recipe(tmp_path)
    path = tmp_path / "modules/demo/api.json"
    graph = json.loads(path.read_text())
    if defect == "canvas":
        graph = {"nodes": [], "links": []}
    elif defect == "bad_link":
        graph["1"]["inputs"]["video"] = ["missing", 0]
    elif defect == "cycle":
        graph["1"]["inputs"]["video"] = ["1", 0]
    elif defect == "missing_binding":
        recipe["parameters"]["seed"]["bindings"][0]["input_name"] = "missing"
    else:
        recipe["outputs"]["main"]["node_id"] = "missing"
    path.write_text(json.dumps(graph))
    (tmp_path / "modules/demo/recipe.yaml").write_text(yaml.safe_dump(recipe))
    record = recipe_record(refresh_resources(project, 0))
    assert record["states"]["discovered"] is True
    assert record["states"]["declaration_complete"] is False
    assert record["errors"]
    assert record["states"]["real_test"] == "not_run"


def test_conflicts_require_explicit_source_mapping_and_preserve_active_revision(tmp_path):
    from comfypilot.resources import get_resources, refresh_resources
    from comfypilot.settings import get_project, update_project

    project = initialize_project(tmp_path)
    write_recipe(tmp_path)
    first = refresh_resources(project, 0)
    duplicate = tmp_path / "workflows/duplicate"
    duplicate.mkdir()
    original = tmp_path / "modules/demo/recipe.yaml"
    (duplicate / "recipe.yaml").write_bytes(original.read_bytes())
    with pytest.raises(ProjectError) as error:
        refresh_resources(project, 1)
    assert error.value.code == "resource_conflict"
    assert get_resources(project)["hash"] == first["hash"]
    current = get_project(project)
    update_project(
        project,
        current["revision"],
        {
            **current["config"],
            "resource_overrides": {
                "recipe:swap": "project:workflows/duplicate/recipe.yaml",
            },
        },
    )
    changed = refresh_resources(project, 1)
    assert recipe_record(changed)["source"] == "project:workflows/duplicate/recipe.yaml"
    assert recipe_record(changed)["overridden_sources"] == ["project:modules/demo/recipe.yaml"]
    with pytest.raises(ProjectError) as error:
        refresh_resources(project, 1)
    assert error.value.code == "conflict"


def test_resource_paths_cannot_escape_and_symlinks_are_not_read(tmp_path):
    from comfypilot.resources import refresh_resources

    project = initialize_project(tmp_path)
    recipe = write_recipe(tmp_path)
    recipe["instructions"] = "../secrets.local.yaml"
    (tmp_path / "modules/demo/recipe.yaml").write_text(yaml.safe_dump(recipe))
    assert recipe_record(refresh_resources(project, 0))["states"]["declaration_complete"] is False
    (tmp_path / "workflows/escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ProjectError) as error:
        refresh_resources(project, 1)
    assert error.value.code == "resource_path_invalid"


def test_custom_node_declaration_and_code_are_part_of_frozen_recipe_version(tmp_path):
    from comfypilot.resources import freeze_recipe, refresh_resources

    project = initialize_project(tmp_path)
    recipe = write_recipe(tmp_path)
    folder = tmp_path / "custom_nodes/demo"
    folder.mkdir()
    node = {
        "kind": "node",
        "id": "demo-node",
        "name": "测试节点",
        "version": "abc123",
        "description": "测试声明",
        "instructions": "custom_nodes/demo/README.md",
        "source": "https://example.com/node",
        "license": "test",
        "python_dependencies": [],
        "system_dependencies": [],
        "installation": "测试安装说明",
        "verification": "测试验证说明",
        "rollback": "测试回退说明",
        "restart_required": True,
    }
    (folder / "node.yaml").write_text(yaml.safe_dump(node))
    (folder / "README.md").write_text("安装、验证与回退")
    (folder / "__init__.py").write_text("raise RuntimeError('must not import')")
    recipe["custom_nodes"] = ["demo-node"]
    (tmp_path / "modules/demo/recipe.yaml").write_text(yaml.safe_dump(recipe))
    first = refresh_resources(project, 0)
    frozen = freeze_recipe(
        project,
        "swap",
        recipe_record(first)["hash"],
        {"video": "b" * 64},
        {"seed": 1, "prompt": ""},
    )
    assert "project:custom_nodes/demo/__init__.py" in frozen["files"]
    assert first["custom_nodes"][0]["manifest"]["version"] == "abc123"
    (folder / "__init__.py").write_text("changed code")
    second = refresh_resources(project, 1)
    assert recipe_record(second)["hash"] != recipe_record(first)["hash"]
    assert recipe_record(second)["states"]["real_test"] == "not_run"


def test_resource_root_replaced_with_symlink_is_rejected_after_service_started(tmp_path):
    from comfypilot.resources import refresh_resources

    project = initialize_project(tmp_path)
    (tmp_path / "workflows").rmdir()
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    (tmp_path / "workflows").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectError) as error:
        refresh_resources(project, 0)
    assert error.value.code == "resource_path_invalid"


def test_parameter_cannot_silently_replace_a_declared_model_dependency(tmp_path):
    from comfypilot.resources import freeze_recipe, refresh_resources

    project = initialize_project(tmp_path)
    recipe = write_recipe(tmp_path)
    recipe["models"] = [
        {
            "name": "fixed.safetensors",
            "source": "https://example.com/model",
            "license": "test",
            "node_id": "1",
            "input_name": "prompt",
        }
    ]
    recipe["parameters"]["prompt"]["default"] = "fixed.safetensors"
    graph_path = tmp_path / "modules/demo/api.json"
    graph = json.loads(graph_path.read_text())
    graph["1"]["inputs"]["prompt"] = "fixed.safetensors"
    graph_path.write_text(json.dumps(graph))
    (tmp_path / "modules/demo/recipe.yaml").write_text(yaml.safe_dump(recipe))
    record = recipe_record(refresh_resources(project, 0))
    assert record["states"]["declaration_complete"]
    with pytest.raises(ProjectError) as error:
        freeze_recipe(
            project,
            "swap",
            record["hash"],
            {"video": "b" * 64},
            {"seed": 1, "prompt": "other.safetensors"},
        )
    assert error.value.code == "recipe_binding_invalid"
