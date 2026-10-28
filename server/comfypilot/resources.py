import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath

import yaml
from pydantic import ValidationError

from comfypilot.project import Project, ProjectError
from comfypilot.recipes import (
    CustomNode,
    Module,
    Recipe,
    content_hash,
    validate_graph,
    validate_parameter,
)
from comfypilot.settings import get_project


def initialize_resource_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS resource_blobs (hash TEXT PRIMARY KEY, content BLOB NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS resource_revisions (revision INTEGER PRIMARY KEY, "
        "config_revision INTEGER NOT NULL, hash TEXT NOT NULL, document TEXT NOT NULL, "
        "accepted_at TEXT NOT NULL)"
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(execution_plans)")}
    if "resources" not in columns:
        db.execute("ALTER TABLE execution_plans ADD COLUMN resources TEXT NOT NULL DEFAULT '{}'")
    db.execute(
        "CREATE TABLE IF NOT EXISTS resource_preflights (recipe_hash TEXT NOT NULL, "
        "target_id TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(recipe_hash,target_id))"
    )


def get_resources(project: Project) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute(
            "SELECT document FROM resource_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    return (
        json.loads(row[0])
        if row
        else {
            "revision": 0,
            "hash": None,
            "modules": [],
            "recipes": [],
            "custom_nodes": [],
            "files": [],
        }
    )


def get_resource_file(project: Project, resource_hash: str) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute(
            "SELECT content FROM resource_blobs WHERE hash=?", (resource_hash,)
        ).fetchone()
    if row is None:
        raise ProjectError("resource_unavailable", "该内容不在已保存的资源快照中。")
    try:
        text = row[0].decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return {"hash": resource_hash, "size": len(row[0]), "text": text, "binary": text is None}


def scan_files(project: Project) -> tuple[dict, dict]:
    file_limit = get_project(project)["config"]["import_limits"]["workflow_bytes"]
    preset_root = Path(__file__).parent / "presets"
    if not preset_root.exists():
        preset_root = Path(__file__).parents[2] / "presets"
    files, blobs = {}, {}
    size = 0
    for namespace, root, folders in [
        ("builtin", preset_root, [preset_root]),
        (
            "project",
            project.root,
            [project.root / name for name in ("workflows", "custom_nodes", "modules")],
        ),
    ]:
        for folder in folders:
            if folder.is_symlink():
                raise ProjectError("resource_path_invalid", "资源根目录不能是符号链接。")
            for parent, directories, names in os.walk(folder, followlinks=False):
                for name in [*directories, *names]:
                    if (Path(parent) / name).is_symlink():
                        raise ProjectError(
                            "resource_path_invalid", "资源目录含符号链接，请移除链接后刷新。"
                        )
                directories[:] = sorted(
                    name
                    for name in directories
                    if not name.startswith(".")
                    and name
                    not in {
                        "node_modules",
                        "__pycache__",
                        "venv",
                        "env",
                    }
                )
                for name in sorted(names):
                    if name.startswith(".") or name.endswith((".pyc", ".tmp", ".bak")):
                        continue
                    path = Path(parent) / name
                    if not path.is_file() or path.stat().st_size > file_limit:
                        raise ProjectError(
                            "resource_limit",
                            "资源仅接受普通文件，单文件受 import_limits.workflow_bytes 限制。",
                        )
                    content = path.read_bytes()
                    size += len(content)
                    if len(content) > file_limit or size > 32 * 1024 * 1024 or len(files) >= 1000:
                        raise ProjectError(
                            "resource_limit", "资源刷新最多 1000 个文件、合计 32 MiB。"
                        )
                    source = f"{namespace}:{path.relative_to(root).as_posix()}"
                    digest = sha256(content).hexdigest()
                    files[source] = {"source": source, "hash": digest, "size": len(content)}
                    blobs[digest] = content
    return files, blobs


def build_catalog(files: dict, blobs: dict, overrides: dict) -> dict:
    groups = {}
    schemas = {"module.yaml": Module, "recipe.yaml": Recipe, "node.yaml": CustomNode}
    for source, file in files.items():
        filename = source.rsplit("/", 1)[-1]
        if filename not in schemas:
            continue
        schema = schemas[filename]
        errors = []
        manifest = None
        try:
            manifest = schema.model_validate(yaml.safe_load(blobs[file["hash"]].decode("utf-8")))
        except (ValidationError, ValueError, yaml.YAMLError):
            errors.append("声明格式不符合 schema，请检查必填项和类型")
        record = {
            "id": manifest.id if manifest else source,
            "source": source,
            "manifest": manifest.model_dump(mode="json") if manifest else None,
            "errors": errors,
            "overridden_sources": [],
            "states": {
                "discovered": True,
                "declaration_complete": False,
                "dependencies": "unknown",
                "target_preflight": "not_checked",
                "real_test": "not_run",
            },
        }
        # 同目录的说明、工作流与代码共同确定资源版本。
        prefix = source.rsplit("/", 1)[0] + "/"
        related = {name: item["hash"] for name, item in files.items() if name.startswith(prefix)}
        if manifest:
            namespace = source.split(":", 1)[0]
            references = [manifest.instructions]
            if isinstance(manifest, Recipe):
                references.extend(item for item in (manifest.workflow, manifest.api_graph) if item)
            for reference in references:
                path = PurePosixPath(reference)
                key = f"{namespace}:{reference}"
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in reference
                    or key not in files
                ):
                    errors.append("文件引用必须指向同一来源根目录下已发现的相对路径")
                    continue
                related[key] = files[key]["hash"]
            if isinstance(manifest, Recipe):
                if manifest.api_graph is None:
                    errors.append("缺少独立 API 图，画布只能作为原件保存")
                elif not errors:
                    try:
                        raw = json.loads(blobs[files[f"{namespace}:{manifest.api_graph}"]["hash"]])
                        record["api_graph"] = validate_graph(manifest, raw)
                    except (ValueError, ValidationError):
                        errors.append("API 图、输入参数绑定或输出规则不合法")
        record["files"] = related
        record["hash"] = content_hash({"source": source, "files": related})
        kind = {Module: "module", Recipe: "recipe", CustomNode: "node"}[schema]
        groups.setdefault(f"{kind}:{record['id']}", []).append(record)
    selected = {"modules": [], "recipes": [], "custom_nodes": []}
    if set(overrides) - groups.keys():
        raise ProjectError("resource_conflict", "覆盖映射引用了未发现的资源 ID，请核对配置。")
    for key, records in sorted(groups.items()):
        chosen = overrides.get(key)
        if chosen is not None and chosen not in {record["source"] for record in records}:
            raise ProjectError("resource_conflict", f"覆盖映射来源不存在：{key}")
        if len(records) > 1 and chosen is None:
            raise ProjectError(
                "resource_conflict", f"资源同名：{key}；请在 resource_overrides 显式选择来源。"
            )
        record = next((item for item in records if item["source"] == chosen), records[0])
        record["overridden_sources"] = [item["source"] for item in records if item is not record]
        collection = {"module": "modules", "recipe": "recipes", "node": "custom_nodes"}[
            key.split(":", 1)[0]
        ]
        selected[collection].append(record)
    for collection in ("modules", "custom_nodes"):
        for record in selected[collection]:
            record["states"]["declaration_complete"] = not record["errors"]
    modules = {item["id"]: item for item in selected["modules"]}
    nodes = {item["id"]: item for item in selected["custom_nodes"]}
    for record in selected["recipes"]:
        if record["manifest"] is None:
            continue
        manifest = Recipe.model_validate(record["manifest"])
        required = [
            modules.get(manifest.module_id),
            *(nodes.get(key) for key in manifest.custom_nodes),
        ]
        if any(item is None or not item["states"]["declaration_complete"] for item in required):
            record["errors"].append("模块或自定义节点声明缺失、不完整")
        else:
            module = required[0]["manifest"]
            if {key: value.kind for key, value in manifest.inputs.items()} != module["inputs"] or {
                key: value.kind for key, value in manifest.outputs.items()
            } != module["outputs"]:
                record["errors"].append("配方输入输出与模块声明不一致")
        record["hash"] = content_hash(
            {
                "recipe": record["hash"],
                "dependencies": [item["hash"] if item else None for item in required],
            }
        )
        record["dependency_files"] = {
            name: digest for item in required if item for name, digest in item["files"].items()
        }
        record["states"]["declaration_complete"] = not record["errors"]
    return {**selected, "files": list(files.values())}


def refresh_resources(project: Project, expected_revision: int) -> dict:
    current = get_project(project)
    try:
        files, blobs = scan_files(project)
    except OSError as error:
        raise ProjectError("resource_unavailable", "资源读取失败；上次有效快照仍保留。") from error
    document = build_catalog(files, blobs, current["config"]["resource_overrides"])
    if len(json.dumps(document).encode()) > 30 * 1024 * 1024:
        raise ProjectError("resource_limit", "解析后的资源目录超过 30 MiB，请拆分项目资源。")
    digest = content_hash(document)
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT revision,hash,document FROM resource_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        revision = row[0] if row else 0
        config_revision = db.execute("SELECT MAX(revision) FROM config_revisions").fetchone()[0]
        if revision != expected_revision or config_revision != current["revision"]:
            raise ProjectError("conflict", "资源或配置修订已变化，请重新读取后刷新。")
        if row and row[1] == digest:
            return json.loads(row[2])
        document.update(
            revision=revision + 1,
            hash=digest,
            config_revision=config_revision,
            accepted_at=datetime.now(UTC).isoformat(),
        )
        db.executemany("INSERT OR IGNORE INTO resource_blobs VALUES (?,?)", blobs.items())
        db.execute(
            "INSERT INTO resource_revisions VALUES (?,?,?,?,?)",
            (
                revision + 1,
                config_revision,
                digest,
                json.dumps(document),
                document["accepted_at"],
            ),
        )
    return document


def freeze_recipe(
    project: Project, recipe_id: str, recipe_hash: str, inputs: dict, parameters: dict
) -> dict:
    snapshot = get_resources(project)
    record = next((item for item in snapshot["recipes"] if item["id"] == recipe_id), None)
    if record is None or record["hash"] != recipe_hash:
        raise ProjectError(
            "resource_changed", "配方不在当前资源快照中或哈希已变化，请刷新并重新创建方案。"
        )
    if not record["states"]["declaration_complete"]:
        raise ProjectError("recipe_incomplete", "配方声明、API 图或依赖说明不完整，不能冻结运行。")
    recipe = Recipe.model_validate(record["manifest"])
    if set(inputs) != set(recipe.inputs) or set(parameters) != set(recipe.parameters):
        raise ProjectError("recipe_binding_invalid", "步骤必须完整提供配方声明的输入与参数。")
    graph = json.loads(json.dumps(record["api_graph"]))
    try:
        for key, value in parameters.items():
            validate_parameter(recipe.parameters[key], value)
            for binding in recipe.parameters[key].bindings:
                graph[binding.node_id]["inputs"][binding.input_name] = value
        if any(
            graph[model.node_id]["inputs"][model.input_name] != model.name
            for model in recipe.models
        ):
            raise ValueError("参数不能替换配方声明的固定模型依赖，请创建包含新依赖的配方修订")
    except ValueError as error:
        raise ProjectError("recipe_binding_invalid", str(error)) from error
    return {
        "resource_revision": snapshot["revision"],
        "recipe_id": recipe_id,
        "recipe_hash": recipe_hash,
        "manifest": record["manifest"],
        "graph": graph,
        "graph_hash": content_hash(graph),
        "files": {**record["files"], **record["dependency_files"]},
        "inputs": inputs,
        "parameters": parameters,
        "materialization": "pending",
        "real_test": "not_run",
    }
