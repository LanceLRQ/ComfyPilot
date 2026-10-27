import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256

import yaml
from pydantic import ValidationError

from comfypilot.project import ExecutionTarget, Project, ProjectConfig, ProjectError
from comfypilot.runtime import project_lock


def get_project(project: Project) -> dict:
    with closing(sqlite3.connect(project.database.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT revision, config, source_hash FROM config_revisions "
            "ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    config = ProjectConfig.model_validate_json(row[1])
    warnings = []
    endpoints: dict[str, list[str]] = {}
    for target in config.targets:
        endpoints.setdefault(target.base_url, []).append(target.id)
    for ids in endpoints.values():
        if len(ids) > 1:
            warnings.append(
                {
                    "code": "duplicate_target",
                    "target_ids": ids,
                    "message": "这些目标配置了同一地址，请核对是否合并；不自动合并身份。",
                }
            )
    credentials = {}
    for target in config.targets:
        if target.credential_ref is not None:
            try:
                credentials[target.credential_ref] = bool(target_headers(project, target))
            except (ProjectError, OSError):
                credentials[target.credential_ref] = False
                warnings.append(
                    {
                        "code": "credential_unavailable",
                        "target_ids": [target.id],
                        "message": "无法读取执行端凭据，请检查项目秘密文件；有效配置仍保留。",
                    }
                )
    return {
        "revision": row[0],
        "config": config.model_dump(mode="json"),
        "file_changed": sha256((project.root / "comfypilot.yaml").read_bytes()).hexdigest()
        != row[2],
        "warnings": warnings,
        "credentials": credentials,
    }


def target_headers(project: Project, target: ExecutionTarget) -> dict[str, str]:
    if target.credential_ref is None:
        return {}
    path = project.root / ".comfypilot/secrets.local.yaml"
    if not path.is_file():
        return {}
    try:
        values = yaml.safe_load(path.read_text("utf-8"))
        if not isinstance(values, dict):
            raise ValueError
        headers = values.get(target.credential_ref, {})
        if not isinstance(headers, dict) or any(
            key not in {"Authorization", "X-API-Key"}
            or not isinstance(value, str)
            or not value
            or "\r" in value
            or "\n" in value
            for key, value in headers.items()
        ):
            raise ValueError
    except (ValueError, yaml.YAMLError) as error:
        raise ProjectError(
            "validation_error", "秘密文件格式不正确，请检查凭据引用和请求头。"
        ) from error
    return headers


def save_config(
    project: Project,
    expected_revision: int,
    *,
    content: dict | None = None,
    target: dict | None = None,
) -> dict:
    with project_lock(project, "config"):
        with closing(sqlite3.connect(project.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision, config FROM config_revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            if row[0] != expected_revision:
                raise ProjectError(
                    "conflict", "配置修订已变化，请保留草稿、重新读取并合并后再提交。"
                )
            path = project.root / "comfypilot.yaml"
            try:
                if target is not None:
                    selected = ExecutionTarget.model_validate(target)
                    content = json.loads(row[1])
                    content["targets"] = [
                        item for item in content["targets"] if item["id"] != selected.id
                    ] + [selected.model_dump(mode="json")]
                config = ProjectConfig.model_validate(
                    content if content is not None else yaml.safe_load(path.read_text("utf-8"))
                )
            except (ValidationError, yaml.YAMLError) as error:
                raise ProjectError(
                    "validation_error", "项目配置不合法，请检查字段、目标与默认值。"
                ) from error
            if content is not None:
                temporary = project.root / ".comfypilot/config.tmp"
                temporary.write_text(
                    yaml.safe_dump(
                        config.model_dump(mode="json"), allow_unicode=True, sort_keys=False
                    ),
                    "utf-8",
                )
                temporary.replace(path)
            db.execute(
                "INSERT INTO config_revisions VALUES (?, ?, ?, ?)",
                (
                    row[0] + 1,
                    config.model_dump_json(),
                    sha256(path.read_bytes()).hexdigest(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return get_project(project)


def update_project(project: Project, expected_revision: int, config: dict) -> dict:
    return save_config(project, expected_revision, content=config)


def refresh_project(project: Project, expected_revision: int) -> dict:
    return save_config(project, expected_revision)


def update_target(project: Project, expected_revision: int, target: dict) -> dict:
    return save_config(project, expected_revision, target=target)


def resolve_target(project: Project, target_id: str | None = None) -> dict:
    current = get_project(project)
    config = current["config"]
    selected_id = target_id if target_id is not None else config["default_target_id"]
    selected = next(
        (
            target
            for target in config["targets"]
            if target["id"] == selected_id and target["enabled"]
        ),
        None,
    )
    if selected is None:
        if target_id is not None:
            raise ProjectError("target_unavailable", "指定目标未配置或已停用，请明确选择目标。")
        return {
            "status": "needs_input",
            "questions": ["请选择一个已配置且启用的 ComfyUI 执行端。"],
            "target_ids": [target["id"] for target in config["targets"] if target["enabled"]],
        }
    return {
        "status": "selected",
        "config_revision": current["revision"],
        "target": selected,
        "capability_status": "not_probed",
    }
