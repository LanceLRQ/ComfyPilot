import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from comfypilot.media import CHUNK_BYTES, check_space
from comfypilot.project import (
    Project,
    ProjectConfig,
    ProjectError,
    ProjectIdentity,
    migrate_database,
    resolve_project_root,
)


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    destination: str = Field(min_length=1, max_length=4096)


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bundle_path: str = Field(min_length=1, max_length=4096)


class BackupFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format: Literal[1]
    project_id: str
    created_at: str
    database_version: Literal[14]
    files: list[BackupFile] = Field(min_length=3, max_length=100000)


def allowed_file(relative: str) -> bool:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in relative
        or any(ord(x) < 32 for x in relative)
    ):
        return False
    if relative in {"comfypilot.yaml", ".comfypilot/project.json", ".comfypilot/state.sqlite"}:
        return True
    if re.fullmatch(r"\.comfypilot/artifacts/(inputs|outputs|review)/[a-f0-9]{64}", relative):
        return True
    return bool(
        path.parts and path.parts[0] in {"workflows", "custom_nodes", "modules", "skills"}
    ) and not any(
        p.startswith(".")
        or p in {"node_modules", "__pycache__", "venv", "env"}
        or p.startswith("secrets.")
        or p.endswith((".part", ".tmp", ".bak", ".pyc", ".pem", ".key"))
        for p in path.parts[1:]
    )


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def media_references(db: sqlite3.Connection) -> dict[str, tuple[str, int]]:
    files = {}
    assets = {
        row[0]: json.loads(row[1]) for row in db.execute("SELECT id,document FROM media_assets")
    }
    for asset in assets.values():
        if asset["source"]["type"] == "clip" and asset["source"]["asset_id"] not in assets:
            raise ProjectError("backup_invalid", "片段的来源素材引用缺失，请保留项目并核对备份。")
        files[f".comfypilot/artifacts/inputs/{asset['content_hash']}"] = (
            asset["content_hash"],
            asset["size"],
        )
    for (raw,) in db.execute("SELECT document FROM output_versions"):
        version = json.loads(raw)
        if any(x["asset_id"] not in assets for x in version["source"]["inputs"]):
            raise ProjectError("backup_invalid", "版本的来源素材引用缺失，不能恢复。")
        for item in [version, *([version["proxy"]] if version.get("proxy") else [])]:
            files[f".comfypilot/artifacts/outputs/{item['content_hash']}"] = (
                item["content_hash"],
                item["size"],
            )
    for (raw,) in db.execute("SELECT document FROM review_screenshots"):
        item = json.loads(raw)
        files[f".comfypilot/artifacts/review/{item['content_hash']}"] = (
            item["content_hash"],
            item["size"],
        )
    return files


def validate_database(path: Path, project_id: str) -> dict:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 14:
            raise ProjectError(
                "unsupported_schema", "备份数据库格式不受当前程序支持，请使用对应版本。"
            )
        if (
            db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ProjectError(
                "backup_invalid", "数据库完整性或外键检查失败；保留原件并使用完整备份。"
            )
        if db.execute("SELECT project_id FROM project").fetchall() != [(project_id,)]:
            raise ProjectError("project_mismatch", "备份中的项目身份与数据库不一致。")
        for digest, content in db.execute("SELECT hash,content FROM resource_blobs"):
            if sha256(content).hexdigest() != digest:
                raise ProjectError("backup_invalid", "资源快照哈希不符，请重新导出。")
        return media_references(db)


def export_project(project: Project, destination: Path) -> dict:
    destination = resolve_project_root(destination)
    if destination.exists() or destination.is_relative_to(project.root):
        raise ProjectError("backup_conflict", "导出目标必须是项目外尚不存在的新目录。")
    destination.mkdir(parents=True)
    state = destination / ".comfypilot"
    state.mkdir()
    marker = state / "transfer.incomplete"
    marker.touch()
    database = state / "state.sqlite"
    with (
        closing(sqlite3.connect(project.database.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(database)) as copy,
    ):
        source.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")
        raw = copy.execute(
            "SELECT config FROM config_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()[0]
    (state / "project.json").write_text(project.identity.model_dump_json(indent=2) + "\n")
    (destination / "comfypilot.yaml").write_text(
        yaml.safe_dump(json.loads(raw), allow_unicode=True, sort_keys=False)
    )
    migrate_database(Project(destination, project.identity, ProjectConfig.model_validate_json(raw)))
    with closing(sqlite3.connect(database)) as copy:
        copy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        copy.execute("PRAGMA journal_mode=DELETE")
    required = validate_database(database, str(project.identity.project_id))
    paths = set(required)
    for folder in ("workflows", "custom_nodes", "modules", "skills"):
        for path in (project.root / folder).rglob("*"):
            if path.is_symlink():
                raise ProjectError("backup_invalid", "项目资源含符号链接，请移除链接后重新导出。")
            relative = path.relative_to(project.root).as_posix()
            if path.is_file() and allowed_file(relative):
                paths.add(relative)
    for relative in sorted(paths):
        if not allowed_file(relative):
            raise ProjectError("backup_invalid", "数据库中的素材路径不符合受管格式。")
        source, target = project.root / relative, destination / relative
        if not source.is_file() or any(p.is_symlink() for p in (source, *source.parents)):
            raise ProjectError(
                "backup_invalid", "受管原件缺失或为符号链接；导出未完成，请保留现场。"
            )
        check_space(destination, source.stat().st_size)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = file_hash(source)
        shutil.copyfile(source, target)
        if file_hash(target) != before or file_hash(source) != before:
            raise ProjectError("backup_invalid", "文件在导出时发生变化，请在操作完成后重新导出。")
        if relative in required and (before, target.stat().st_size) != required[relative]:
            raise ProjectError(
                "backup_invalid", "原件与数据库保存的哈希或大小不符，不能导出为完整备份。"
            )
    files = [
        BackupFile(
            path=p.relative_to(destination).as_posix(), sha256=file_hash(p), size=p.stat().st_size
        )
        for p in sorted(destination.rglob("*"))
        if p.is_file() and p != marker
    ]
    manifest = BackupManifest(
        format=1,
        project_id=str(project.identity.project_id),
        created_at=datetime.now(UTC).isoformat(),
        database_version=14,
        files=files,
    )
    (destination / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    marker.unlink()
    return {
        "bundle_path": str(destination),
        "project_id": manifest.project_id,
        "files": len(files),
        "bytes": sum(x.size for x in files),
    }


def restore_project(bundle: Path, destination: Path) -> dict:
    if not bundle.is_absolute() or not destination.is_absolute():
        raise ProjectError("validation_error", "备份和恢复目录必须是明确的绝对路径。")
    destination = resolve_project_root(destination)
    if destination.exists():
        raise ProjectError("backup_conflict", "恢复仅接受尚不存在的新目录，不覆盖现有项目。")
    if destination.is_relative_to(bundle.resolve()) or not destination.parent.is_dir():
        raise ProjectError("backup_conflict", "恢复目录必须在备份外，且父目录已存在。")
    try:
        if any(p.is_symlink() for p in (bundle, *bundle.parents)):
            raise ProjectError("backup_invalid", "备份路径不能包含符号链接。")
        bundle = bundle.resolve()
        manifest = BackupManifest.model_validate_json((bundle / "manifest.json").read_text())
        paths = {entry.path for entry in manifest.files}
        actual = set()
        for path in bundle.rglob("*"):
            if path.is_symlink():
                raise ProjectError("backup_invalid", "备份包含符号链接，未创建恢复目录。")
            if path.is_file() and path != bundle / "manifest.json":
                actual.add(path.relative_to(bundle).as_posix())
        if len(paths) != len(manifest.files) or paths != actual:
            raise ProjectError("backup_invalid", "备份文件清单不完整或含额外文件，请重新核对导出。")
        for item in manifest.files:
            if not allowed_file(item.path) or item.path != PurePosixPath(item.path).as_posix():
                raise ProjectError("backup_invalid", "清单包含越界或不允许恢复的文件。")
            path = bundle / item.path
            if path.stat().st_size != item.size or file_hash(path) != item.sha256:
                raise ProjectError("backup_invalid", "备份文件大小或哈希不匹配，未创建恢复目录。")
        identity = ProjectIdentity.model_validate_json(
            (bundle / ".comfypilot/project.json").read_text()
        )
        if str(identity.project_id) != manifest.project_id:
            raise ProjectError("project_mismatch", "备份身份与清单不一致。")
        required = validate_database(bundle / ".comfypilot/state.sqlite", manifest.project_id)
        indexed = {x.path: (x.sha256, x.size) for x in manifest.files}
        if any(indexed.get(path) != proof for path, proof in required.items()):
            raise ProjectError("backup_invalid", "数据库引用的原件、代理或截图缺失/不匹配。")
        config = ProjectConfig.model_validate(
            yaml.safe_load((bundle / "comfypilot.yaml").read_text())
        )
    except (OSError, ValueError, KeyError, sqlite3.Error, ValidationError, yaml.YAMLError) as error:
        raise ProjectError(
            "backup_invalid", "备份缺失或损坏，请保留原件并重新导出完整项目。"
        ) from error
    check_space(destination.parent, sum(item.size for item in manifest.files))
    destination.mkdir()
    (destination / ".comfypilot").mkdir()
    marker = destination / ".comfypilot/transfer.incomplete"
    marker.touch()
    for item in manifest.files:
        path = destination / item.path
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / item.path, path)
        if file_hash(path) != item.sha256:
            raise ProjectError(
                "backup_invalid", "复制后哈希不符，恢复未完成；保留证据并重新恢复到新目录。"
            )
    identity = ProjectIdentity(project_id=uuid4(), created_at=datetime.now(UTC))
    (destination / ".comfypilot/project.json").write_text(identity.model_dump_json(indent=2) + "\n")
    config.server.host, config.server.lan_hosts = "127.0.0.1", []
    for target in config.targets:
        target.enabled = False
    for name in ("logs", "locks", "staging", "runs"):
        (destination / ".comfypilot" / name).mkdir(exist_ok=True)
    text = yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
    (destination / "comfypilot.yaml").write_text(text)
    with closing(sqlite3.connect(destination / ".comfypilot/state.sqlite")) as db, db:
        for table in (
            "worker_status",
            "status_samples",
            "resource_preflights",
            "cleanup_claims",
            "asset_references",
            "asset_replicas",
        ):
            db.execute(f"DELETE FROM {table}")
        db.execute(
            "UPDATE project SET project_id=?,root=?,created_at=?",
            (str(identity.project_id), str(destination), identity.created_at.isoformat()),
        )
        db.execute("UPDATE execution_tasks SET active_plan_id=NULL")
        db.execute("UPDATE execution_plans SET status='stopped'")
        db.execute(
            "UPDATE execution_runs SET status='cancelled' "
            "WHERE status NOT IN ('succeeded','failed','cancelled')"
        )
        for job_id, raw in db.execute(
            "SELECT id,document FROM media_jobs WHERE status IN ('queued','running')"
        ).fetchall():
            job = json.loads(raw)
            job.update(
                status="blocked",
                phase="blocked",
                error={
                    "code": "restored_history",
                    "message": "恢复的旧媒体任务仅留作证据；如需继续请重新明确提出。",
                },
            )
            db.execute(
                "UPDATE media_jobs SET status='blocked',document=? WHERE id=?",
                (json.dumps(job), job_id),
            )
        db.execute("UPDATE execution_control SET reconciliation_required=0")
        db.execute(
            "UPDATE service_control SET paused=1,reason='project_restored',updated_at=?",
            (datetime.now(UTC).isoformat(),),
        )
        revision = db.execute("SELECT MAX(revision)+1 FROM config_revisions").fetchone()[0]
        db.execute(
            "INSERT INTO config_revisions VALUES (?,?,?,?)",
            (
                revision,
                config.model_dump_json(),
                sha256(text.encode()).hexdigest(),
                datetime.now(UTC).isoformat(),
            ),
        )
    marker.unlink()
    return {
        "root": str(destination),
        "project_id": str(identity.project_id),
        "source_project_id": manifest.project_id,
        "dispatch_paused": True,
        "targets_disabled": True,
        "warnings": []
        if all(shutil.which(x) for x in ("ffmpeg", "ffprobe"))
        else ["缺少ffmpeg/ffprobe；历史可读，处理素材前请安装并加入PATH。"],
    }
