import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psutil
import yaml
from pydantic import BaseModel, ConfigDict, Field


class ProjectError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    name: str
    server: ServerConfig = Field(default_factory=ServerConfig)


class ProjectIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    project_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class Project:
    root: Path
    identity: ProjectIdentity
    config: ProjectConfig

    @property
    def database(self) -> Path:
        return self.root / ".comfypilot/state.sqlite"

    def summary(self) -> dict[str, str | int]:
        return {
            "project_id": str(self.identity.project_id),
            "root": str(self.root),
            "name": self.config.name,
            "created_at": self.identity.created_at.isoformat(),
            "schema_version": self.identity.schema_version,
        }


def resolve_project_root(value: Path) -> Path:
    if not value.is_absolute():
        raise ProjectError("validation_error", "project_root 必须是用户明确指定的绝对路径。")
    root = value.resolve()
    for name in [
        "comfypilot.yaml",
        ".comfypilot",
        ".comfypilot/project.json",
        ".comfypilot/state.sqlite",
        ".comfypilot/state.sqlite-wal",
        ".comfypilot/state.sqlite-shm",
        ".comfypilot/runtime.json",
        ".comfypilot/runtime.tmp",
        ".comfypilot/artifacts",
        ".comfypilot/runs",
        ".comfypilot/staging",
        ".comfypilot/logs",
        ".comfypilot/locks",
        ".comfypilot/locks/control.lock",
        ".comfypilot/locks/service.lock",
        ".comfypilot/locks/worker.lock",
        ".comfypilot/logs/service.log",
        "workflows",
        "custom_nodes",
        "modules",
        "skills",
    ]:
        if not (root / name).resolve().is_relative_to(root):
            raise ProjectError("validation_error", f"项目资源不能通过符号链接指向目录之外：{name}")
    probe = root / ".comfypilot"
    while not probe.exists():
        probe = probe.parent
    partitions = [
        partition
        for partition in psutil.disk_partitions(all=True)
        if probe.is_relative_to(Path(partition.mountpoint))
    ]
    if not partitions:
        raise ProjectError(
            "unsupported_storage", "无法确认数据库所在磁盘，请使用可识别的本机磁盘。"
        )
    partition = max(partitions, key=lambda item: len(Path(item.mountpoint).parts))
    if partition.fstype.lower() not in {
        "apfs",
        "hfs",
        "hfsplus",
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "zfs",
        "ufs",
        "f2fs",
        "overlay",
        "tmpfs",
        "ramfs",
    }:
        raise ProjectError(
            "unsupported_storage",
            f"SQLite 需要本机磁盘，不支持当前文件系统 {partition.fstype}；请移动项目目录。",
        )
    if shutil.disk_usage(probe).free < 16 * 1024 * 1024:
        raise ProjectError(
            "storage_insufficient", "启动至少需要 16 MiB 可用空间，请释放空间后重试。"
        )
    return root


def migrate_database(project: Project) -> None:
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise ProjectError("unsupported_schema", "数据库格式比当前程序更新，请使用对应版本。")
        db.execute(
            "CREATE TABLE IF NOT EXISTS project "
            "(project_id TEXT PRIMARY KEY, root TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS worker_status "
            "(project_id TEXT PRIMARY KEY REFERENCES project(project_id), pid INTEGER NOT NULL, "
            "last_seen_at TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK (status IN ('running', 'stopped')))"
        )
        existing = db.execute("SELECT project_id FROM project").fetchall()
        project_id = str(project.identity.project_id)
        if existing and existing != [(project_id,)]:
            raise ProjectError("project_mismatch", "项目标识和数据库不一致，请检查项目目录或备份。")
        db.execute(
            "INSERT OR IGNORE INTO project (project_id, root, created_at) VALUES (?, ?, ?)",
            (project_id, str(project.root), project.identity.created_at.isoformat()),
        )
        if version < 2:
            columns = {row[1] for row in db.execute("PRAGMA table_info(worker_status)")}
            if "instance_id" not in columns:
                db.execute(
                    "ALTER TABLE worker_status ADD COLUMN instance_id TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS service_control "
                "(singleton INTEGER PRIMARY KEY CHECK (singleton=1), "
                "paused INTEGER NOT NULL CHECK (paused IN (0,1)), "
                "reason TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO service_control VALUES (1, 1, 'initialization', ?)",
                (datetime.now(UTC).isoformat(),),
            )
            db.execute("PRAGMA user_version=2")


def read_dispatch(project: Project) -> dict[str, bool | str]:
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute(
            "SELECT paused, reason, updated_at FROM service_control WHERE singleton=1"
        ).fetchone()
    return {"paused": bool(row[0]), "reason": row[1], "updated_at": row[2]}


def set_dispatch(project: Project, *, paused: bool, reason: str) -> None:
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute(
            "UPDATE service_control SET paused=?, reason=?, updated_at=? WHERE singleton=1",
            (int(paused), reason, datetime.now(UTC).isoformat()),
        )


def load_project(root: Path) -> Project:
    state = root / ".comfypilot"
    if not all(
        (root / name).is_file()
        for name in ["comfypilot.yaml", ".comfypilot/project.json", ".comfypilot/state.sqlite"]
    ):
        raise ProjectError("project_incomplete", "项目未初始化或文件不完整，请先初始化或检查备份。")
    project = Project(
        root=root,
        identity=ProjectIdentity.model_validate_json((state / "project.json").read_text("utf-8")),
        config=ProjectConfig.model_validate(
            yaml.safe_load((root / "comfypilot.yaml").read_text("utf-8"))
        ),
    )
    migrate_database(project)
    return project


def initialize_project(root: Path) -> Project:
    state = root / ".comfypilot"
    if (state / "project.json").exists():
        return load_project(root)
    if (root / "comfypilot.yaml").exists() or state.exists():
        raise ProjectError(
            "project_conflict", "目录已有配置或不完整状态；不会覆盖，请先检查这些文件。"
        )
    root.mkdir(parents=True, exist_ok=True)
    for name in ["artifacts", "runs", "staging", "logs", "locks"]:
        (state / name).mkdir(parents=True)
    for name in ["workflows", "custom_nodes", "modules", "skills"]:
        (root / name).mkdir(exist_ok=True)
    project = Project(
        root=root,
        identity=ProjectIdentity(project_id=uuid4(), created_at=datetime.now(UTC)),
        config=ProjectConfig(name=root.name),
    )
    (state / "project.json").write_text(project.identity.model_dump_json(indent=2) + "\n", "utf-8")
    (root / "comfypilot.yaml").write_text(
        yaml.safe_dump(project.config.model_dump(), allow_unicode=True, sort_keys=False), "utf-8"
    )
    migrate_database(project)
    return project
