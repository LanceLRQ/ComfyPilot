import json

import yaml


def recipe_record(snapshot, recipe_id="swap"):
    return next(item for item in snapshot["recipes"] if item["id"] == recipe_id)


def write_recipe(root, *, recipe_id="swap"):
    folder = root / "modules/demo"
    folder.mkdir(parents=True, exist_ok=True)
    module = {
        "kind": "module",
        "id": "demo",
        "name": "测试模块",
        "version": "test",
        "description": "仅用于标准接口替身测试",
        "instructions": "modules/demo/README.md",
        "inputs": {"video": "video"},
        "outputs": {"main": "video"},
        "checks": ["可读取输出"],
        "feedback": ["逐段反馈"],
    }
    recipe = {
        "kind": "recipe",
        "id": recipe_id,
        "module_id": "demo",
        "name": "测试配方",
        "version": "test",
        "description": "不代表真实 ComfyUI 配方已验收",
        "instructions": "modules/demo/README.md",
        "workflow": "modules/demo/canvas.json",
        "api_graph": "modules/demo/api.json",
        "input_order": ["video"],
        "inputs": {
            "video": {"kind": "video", "bindings": [{"node_id": "1", "input_name": "video"}]}
        },
        "parameters": {
            "prompt": {
                "type": "string",
                "default": "",
                "bindings": [{"node_id": "1", "input_name": "prompt"}],
            },
            "seed": {
                "type": "integer",
                "default": 0,
                "minimum": 0,
                "maximum": 1000,
                "bindings": [{"node_id": "1", "input_name": "seed"}],
            },
        },
        "outputs": {"main": {"node_id": "1", "key": "videos", "kind": "video"}},
    }
    (folder / "module.yaml").write_text(yaml.safe_dump(module, allow_unicode=True))
    (folder / "recipe.yaml").write_text(yaml.safe_dump(recipe, allow_unicode=True))
    (folder / "README.md").write_text("# 测试说明\n安装、验证与回退仅用于替身测试。\n")
    (folder / "canvas.json").write_text(json.dumps({"nodes": [], "links": [], "version": 0.4}))
    (folder / "api.json").write_text(
        json.dumps(
            {
                "1": {
                    "class_type": "FixtureRender",
                    "inputs": {"video": "source.mp4", "seed": 0, "prompt": ""},
                }
            }
        )
    )
    return recipe
