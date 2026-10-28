import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from uuid import UUID

from comfypilot.monitoring import get_target_status
from comfypilot.project import Project, ProjectError
from comfypilot.recipes import Recipe, content_hash
from comfypilot.resources import freeze_recipe, get_resources
from comfypilot.settings import get_project, resolve_target


def check_frozen_recipe(frozen: dict, target: dict) -> dict:
    recipe = Recipe.model_validate(frozen["manifest"])
    graph = frozen["graph"]
    schemas = target.get("node_schemas", {})
    missing = sorted({node["class_type"] for node in graph.values()} - schemas.keys())
    errors = []
    missing_models = []
    unknown = False
    model_bindings = {(model.node_id, model.input_name): model.name for model in recipe.models}
    input_bindings = {
        (binding.node_id, binding.input_name)
        for slot in recipe.inputs.values()
        for binding in slot.bindings
    }
    for node_id, node in graph.items():
        schema = schemas.get(node["class_type"])
        if schema is None:
            continue
        for key, field in schema["inputs"].items():
            if field["required"] and key not in node["inputs"]:
                errors.append(f"{node_id}.{key} 缺少必填输入")
        for key, value in node["inputs"].items():
            field = schema["inputs"].get(key)
            if field is None:
                errors.append(f"{node_id}.{key} 不在目标节点输入定义中")
                continue
            expected = field["type"]
            if (node_id, key) in model_bindings and not isinstance(expected, list):
                unknown = True
            if isinstance(value, list):
                upstream = schemas.get(graph[value[0]]["class_type"])
                if upstream and (
                    value[1] >= len(upstream["outputs"])
                    or (expected != "*" and upstream["outputs"][value[1]] not in (expected, "*"))
                ):
                    errors.append(f"{node_id}.{key} 连线输出序号或类型不匹配")
            elif (node_id, key) in input_bindings:
                # 媒体路径在后续上传阶段物化，这里只检验绑定槽位。
                if expected != "STRING":
                    unknown = True
            elif isinstance(expected, list):
                if value not in expected:
                    if (node_id, key) in model_bindings:
                        missing_models.append(model_bindings[(node_id, key)])
                    else:
                        errors.append(f"{node_id}.{key} 选项不在目标清单中")
            elif expected in {"INT", "FLOAT", "STRING", "BOOLEAN"}:
                allowed = {
                    "INT": (int,),
                    "FLOAT": (int, float),
                    "STRING": (str,),
                    "BOOLEAN": (bool,),
                }
                if (
                    type(value) not in allowed[expected]
                    or type(value) in (int, float)
                    and (
                        "min" in field
                        and value < field["min"]
                        or "max" in field
                        and value > field["max"]
                    )
                ):
                    errors.append(f"{node_id}.{key} 类型或数值范围不匹配")
            else:
                unknown = True
    for output in recipe.outputs.values():
        schema = schemas.get(graph[output.node_id]["class_type"])
        if schema and schema["output_node"] is not True:
            errors.append(f"{output.node_id} 未声明为可保存结果的输出节点")
    connection = target["connection"]
    catalog_expiry = target.get("node_catalog_expires_at")
    fresh = (
        connection["availability"] == "fresh"
        and connection["value"] is True
        and catalog_expiry is not None
        and datetime.now(UTC) < datetime.fromisoformat(catalog_expiry)
    )
    status = (
        "unknown"
        if not fresh
        else "missing_dependencies"
        if missing or missing_models
        else "invalid"
        if errors
        else "unknown"
        if unknown
        else "passed"
    )
    return {
        "status": status,
        "missing_nodes": missing,
        "missing_models": missing_models,
        "errors": errors,
        "recipe_hash": frozen["recipe_hash"],
        "graph_hash": content_hash(graph),
        "target_id": target["target_id"],
        "config_revision": target["config_revision"],
        "instance_id": target["instance_id"],
        "catalog_hash": target.get("node_catalog_hash"),
        "observed_at": datetime.now(UTC).isoformat(),
        "expires_at": min(connection["expires_at"], catalog_expiry)
        if connection["expires_at"] and catalog_expiry
        else None,
        "scope": "standard_schema_only",
        "materialization": "pending",
        "real_test": "not_run",
        "version_verification": "unknown",
        "note": "仅校验标准节点 schema；自定义验证、节点版本、媒体上传及真实生成需另行验收。",
    }


def preflight_recipe(
    project: Project, recipe_id: str, target_id: str, instance_id: UUID, worker_pid: int
) -> dict:
    resolve_target(project, target_id)
    resources = get_resources(project)
    record = next((item for item in resources["recipes"] if item["id"] == recipe_id), None)
    if record is None or record["manifest"] is None:
        raise ProjectError("resource_unavailable", "配方尚未发现，请先显式刷新资源。")
    recipe = Recipe.model_validate(record["manifest"])
    frozen = freeze_recipe(
        project,
        recipe_id,
        record["hash"],
        {key: "" for key in recipe.inputs},
        {key: value.default for key, value in recipe.parameters.items()},
    )
    report = check_frozen_recipe(
        frozen, get_target_status(project, instance_id, worker_pid, target_id)
    )
    report["worker_pid"] = worker_pid
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute(
            "INSERT INTO resource_preflights VALUES (?,?,?) ON CONFLICT(recipe_hash,target_id) "
            "DO UPDATE SET document=excluded.document",
            (record["hash"], target_id, json.dumps(report)),
        )
    return report


def list_preflights(project: Project, instance_id: UUID, worker_pid: int) -> list:
    current = get_project(project)
    with closing(sqlite3.connect(project.database)) as db:
        reports = [
            json.loads(row[0]) for row in db.execute("SELECT document FROM resource_preflights")
        ]
    for report in reports:
        target = next(
            (item for item in current["config"]["targets"] if item["id"] == report["target_id"]),
            None,
        )
        observed = (
            get_target_status(
                project, instance_id, worker_pid, report["target_id"], current=current
            )
            if target
            else None
        )
        report["stale"] = (
            report["config_revision"] != current["revision"]
            or report["instance_id"] != str(instance_id)
            or report["worker_pid"] != worker_pid
            or report["expires_at"] is None
            or datetime.now(UTC) >= datetime.fromisoformat(report["expires_at"])
            or observed is None
            or observed.get("node_catalog_hash") != report["catalog_hash"]
            or observed["connection"]["availability"] != "fresh"
            or observed["connection"]["value"] is not True
        )
    return reports
