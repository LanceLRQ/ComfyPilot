import asyncio
import errno
import json
import math
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from comfypilot.media_tools import inspect_media, render_clip, require_tools
from comfypilot.project import Project, ProjectError
from comfypilot.settings import get_project

CHUNK_BYTES = 1024 * 1024
RESERVE_BYTES = 16 * 1024 * 1024


class ImportAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_path: str = Field(min_length=1, max_length=4096)
    kind: Literal["video", "image"]


class AssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    asset_id: str = Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")


class MediaJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str = Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")


class UploadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["video", "image"]
    filename: str = Field(min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def filename_only(cls, value):
        if (
            value in {".", ".."}
            or any(char in value for char in ("/", "\\"))
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("文件名不能包含路径或控制字符")
        return value


class ExtractClipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    source_asset_id: str = Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    reference_asset_id: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$"
    )
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    fps: int = Field(default=24, ge=1, le=120)
    width: int = Field(default=768, ge=32, le=4096, multiple_of=2)
    height: int = Field(default=448, ge=32, le=4096, multiple_of=2)

    @model_validator(mode="after")
    def explicit_range(self):
        if self.end_seconds <= self.start_seconds or any(
            not math.isclose(value * self.fps, round(value * self.fps), abs_tol=1e-6, rel_tol=0)
            for value in (self.start_seconds, self.end_seconds)
        ):
            raise ValueError("起止时间必须递增并对齐输出 FPS 的帧网格")
        return self


def initialize_media_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS media_blobs (hash TEXT PRIMARY KEY, size INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS media_assets (id TEXT PRIMARY KEY, content_hash TEXT NOT NULL "
        "REFERENCES media_blobs(hash), document TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS media_jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL "
        "CHECK(status IN ('queued','running','succeeded','failed','blocked')), "
        "document TEXT NOT NULL)"
    )


def media_directory(project: Project, name: str) -> Path:
    directory = project.root / ".comfypilot" / name
    for component in (directory, *directory.parents):
        if component == project.root.parent:
            break
        if component.is_symlink():
            raise ProjectError(
                "media_path_invalid", "受管素材目录不能包含符号链接，请恢复项目目录。"
            )
    if not directory.resolve().is_relative_to(project.root / ".comfypilot"):
        raise ProjectError("media_path_invalid", "素材路径越过项目受管根。")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def check_space(directory: Path, size: int) -> None:
    if shutil.disk_usage(directory).free < size + RESERVE_BYTES:
        raise ProjectError(
            "storage_insufficient", "项目磁盘空间不足，请释放空间或移动项目后重新提交。"
        )


class ImportStream:
    def __init__(self, project: Project, limit: int):
        self.path = media_directory(project, "staging") / (uuid4().hex + ".part")
        self.file = self.path.open("xb")
        self.limit, self.size = limit, 0
        self.hash = sha256()
        self.retained = False

    def __enter__(self):
        return self

    def write(self, chunk: bytes) -> None:
        if self.size + len(chunk) > self.limit:
            raise ProjectError(
                "media_limit", "文件超过项目导入上限，请调整 import_limits 或先裁剪。"
            )
        check_space(self.path.parent, len(chunk))
        self.file.write(chunk)
        self.hash.update(chunk)
        self.size += len(chunk)

    def __exit__(self, *_):
        self.file.close()
        if not self.retained:
            self.path.unlink(missing_ok=True)


def queue_job(project: Project, kind: str, parameters: dict, current: dict) -> dict:
    job = {
        "id": str(uuid4()),
        "asset_id": str(uuid4()),
        "kind": kind,
        "status": "queued",
        "phase": "queued",
        "parameters": parameters,
        "config_revision": current["revision"],
        "limits": current["config"]["import_limits"],
        "bytes_done": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "error": None,
    }
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute(
            "INSERT INTO media_jobs VALUES (?,?,?)", (job["id"], job["status"], json.dumps(job))
        )
    return job


def queue_import(project: Project, request: ImportAssetRequest) -> dict:
    require_tools()
    media_directory(project, "artifacts/inputs")
    staging = media_directory(project, "staging")
    source = Path(request.source_path)
    if (
        not source.is_absolute()
        or any(path.is_symlink() for path in (source, *source.parents))
        or not source.is_file()
    ):
        raise ProjectError("media_path_invalid", "导入需要无符号链接的本机普通文件绝对路径。")
    current = get_project(project)
    size = source.stat().st_size
    if size > current["config"]["import_limits"][request.kind + "_bytes"]:
        raise ProjectError("media_limit", "文件超过项目导入上限，请调整 import_limits 或先裁剪。")
    check_space(staging, size)
    return queue_job(project, "import", request.model_dump(), current)


def queue_upload(project: Project, metadata: UploadMetadata, stream: ImportStream) -> dict:
    stream.file.flush()
    job = queue_job(
        project,
        "upload",
        {
            **metadata.model_dump(),
            "staging_token": stream.path.name,
            "content_hash": stream.hash.hexdigest(),
            "size": stream.size,
        },
        get_project(project),
    )
    stream.retained = True
    return job


def get_asset(project: Project, asset_id: str) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute("SELECT document FROM media_assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        raise ProjectError("asset_unavailable", "素材尚未导入成功或不属于当前项目。")
    return json.loads(row[0])


def asset_path(project: Project, asset_id: str) -> Path:
    asset = get_asset(project, asset_id)
    path = media_directory(project, "artifacts/inputs") / asset["content_hash"]
    if path.is_symlink() or not path.is_file():
        raise ProjectError("media_path_invalid", "受管原件缺失或被符号链接替换，请检查项目存储。")
    return path


def get_media_job(project: Project, job_id: str) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute("SELECT document FROM media_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ProjectError("media_job_unavailable", "媒体任务不存在于当前项目。")
    return json.loads(row[0])


def list_assets(project: Project) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        assets = [
            json.loads(row[0])
            for row in db.execute("SELECT document FROM media_assets ORDER BY rowid DESC LIMIT 100")
        ]
        jobs = [
            json.loads(row[0])
            for row in db.execute("SELECT document FROM media_jobs ORDER BY rowid DESC LIMIT 50")
        ]
    return {
        "assets": assets,
        "jobs": jobs,
        "limits": get_project(project)["config"]["import_limits"],
    }


def queue_clip(project: Project, request: ExtractClipRequest) -> dict:
    require_tools()
    source = get_asset(project, request.source_asset_id)
    if source["kind"] != "video" or source["role"] != "original":
        raise ProjectError(
            "media_range_invalid", "请从已导入原片选择范围，当前不支持对工作片嵌套提取。"
        )
    if request.end_seconds > source["media"]["duration_seconds"] + 1e-6:
        raise ProjectError("media_range_invalid", "范围超过原片时长，请重新指定起止时间。")
    if (
        request.reference_asset_id
        and get_asset(project, request.reference_asset_id)["kind"] != "image"
    ):
        raise ProjectError("media_reference_invalid", "人物参考必须是已导入并解码通过的图片。")
    check_space(media_directory(project, "staging"), 0)
    return queue_job(project, "clip", request.model_dump(), get_project(project))


def save_job(project: Project, job: dict) -> None:
    job["updated_at"] = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute(
            "UPDATE media_jobs SET status=?,document=? WHERE id=?",
            (job["status"], json.dumps(job), job["id"]),
        )


async def process_next_media_job(project: Project) -> bool:
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT document FROM media_jobs WHERE status='queued' ORDER BY rowid LIMIT 1"
        ).fetchone()
        if row is None:
            return False
        job = json.loads(row[0])
        job.update(status="running", phase="preparing")
        db.execute(
            "UPDATE media_jobs SET status='running',document=? WHERE id=?",
            (json.dumps(job), job["id"]),
        )
    temporary = None
    try:
        parameters = job["parameters"]
        if job["kind"] == "upload":
            temporary = media_directory(project, "staging") / parameters["staging_token"]
        require_tools()
        kind = "video" if job["kind"] == "clip" else parameters["kind"]
        limit = job["limits"][kind + "_bytes"]
        if job["kind"] == "import":
            source_path = Path(parameters["source_path"])
            if (
                any(path.is_symlink() for path in (source_path, *source_path.parents))
                or not source_path.is_file()
            ):
                raise ProjectError(
                    "media_path_invalid", "原件已移动或变成链接，请重新选择本机普通文件。"
                )
            job["phase"] = "copying"
            with ImportStream(project, limit) as stream, source_path.open("rb") as source:
                job["staging_token"] = stream.path.name
                save_job(project, job)
                before = source_path.stat()
                while chunk := source.read(CHUNK_BYTES):
                    stream.write(chunk)
                    if stream.size - job["bytes_done"] >= 16 * CHUNK_BYTES:
                        job["bytes_done"] = stream.size
                        save_job(project, job)
                    await asyncio.sleep(0)
                after = source_path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ProjectError(
                        "media_source_changed", "复制期间原件发生变化，请稳定原文件后重新导入。"
                    )
                stream.file.flush()
                temporary, digest, size = stream.path, stream.hash.hexdigest(), stream.size
                stream.retained = True
            filename, origin = source_path.name, {"type": "local_path", "path": str(source_path)}
        elif job["kind"] == "upload":
            temporary = media_directory(project, "staging") / parameters["staging_token"]
            digest, size = parameters["content_hash"], parameters["size"]
            filename, origin = (
                parameters["filename"],
                {"type": "browser_upload", "filename": parameters["filename"]},
            )
        else:
            temporary = media_directory(project, "staging") / (uuid4().hex + ".mp4")
            job["phase"] = "extracting"
            job["staging_token"] = temporary.name
            save_job(project, job)
            await render_clip(
                asset_path(project, parameters["source_asset_id"]), temporary, parameters, limit
            )
            size = temporary.stat().st_size
            digest_builder = sha256()
            with temporary.open("rb") as file:
                while chunk := file.read(CHUNK_BYTES):
                    digest_builder.update(chunk)
                    await asyncio.sleep(0)
            digest = digest_builder.hexdigest()
            filename = f"clip-{job['asset_id']}.mp4"
            fps = parameters["fps"]
            origin = {
                "type": "clip",
                "asset_id": parameters["source_asset_id"],
                "time_map": {
                    "origin": "first_video_frame",
                    "mode": "normalized_cfr",
                    "fps": fps,
                    "start_frame": round(parameters["start_seconds"] * fps),
                    "end_frame": round(parameters["end_seconds"] * fps),
                    "start_seconds": round(parameters["start_seconds"] * fps) / fps,
                    "end_seconds": round(parameters["end_seconds"] * fps) / fps,
                },
                "requested_range": {
                    key: parameters[key] for key in ("start_seconds", "end_seconds")
                },
            }
        if temporary.is_symlink() or not temporary.is_file():
            raise ProjectError("media_path_invalid", "待导入文件已丢失或成为符号链接，请重新导入。")
        if size > limit:
            raise ProjectError("media_limit", "处理后的素材超过项目上限，请缩小范围或调整限额。")
        job.update(phase="decoding", bytes_done=size)
        save_job(project, job)
        media = await inspect_media(temporary, kind, count_frames=job["kind"] == "clip")
        if (
            job["kind"] == "clip"
            and media["frame_count"]
            != origin["time_map"]["end_frame"] - origin["time_map"]["start_frame"]
        ):
            raise ProjectError(
                "media_range_invalid", "解码后帧数与显式范围不符，请检查原片时长或缩小范围。"
            )
        asset = {
            "id": job["asset_id"],
            "content_hash": digest,
            "size": size,
            "kind": kind,
            "role": "clip"
            if job["kind"] == "clip"
            else "original"
            if kind == "video"
            else "reference",
            "filename": filename,
            "source": origin,
            "media": media,
            "created_at": datetime.now(UTC).isoformat(),
            "input_order": [],
        }
        if job["kind"] == "clip":
            asset["input_order"] = [
                {"slot": "video", "asset_id": asset["id"], "content_hash": digest}
            ]
            if parameters["reference_asset_id"]:
                reference = get_asset(project, parameters["reference_asset_id"])
                asset["input_order"].append(
                    {
                        "slot": "reference",
                        "asset_id": reference["id"],
                        "content_hash": reference["content_hash"],
                    }
                )
        destination = media_directory(project, "artifacts/inputs") / digest
        if destination.is_symlink():
            raise ProjectError("media_path_invalid", "受管文件被符号链接替换，请检查项目存储。")
        if not destination.exists():
            temporary.replace(destination)
        with closing(sqlite3.connect(project.database)) as db, db:
            db.execute("INSERT OR IGNORE INTO media_blobs VALUES (?,?)", (digest, size))
            db.execute(
                "INSERT INTO media_assets VALUES (?,?,?)", (asset["id"], digest, json.dumps(asset))
            )
            job.update(status="succeeded", phase="ready")
            db.execute(
                "UPDATE media_jobs SET status=?,document=? WHERE id=?",
                (job["status"], json.dumps(job), job["id"]),
            )
    except asyncio.CancelledError:
        job.update(
            status="blocked",
            phase="blocked",
            error={
                "code": "media_interrupted",
                "message": "平台停止，中断的媒体任务未自动重放，请重新提交。",
            },
        )
        save_job(project, job)
        raise
    except (ProjectError, OSError, TimeoutError) as error:
        code = (
            error.code
            if isinstance(error, ProjectError)
            else "storage_insufficient"
            if isinstance(error, OSError) and error.errno == errno.ENOSPC
            else "media_timeout"
            if isinstance(error, TimeoutError)
            else "media_io_error"
        )
        message = (
            str(error)
            if isinstance(error, ProjectError)
            else "媒体处理未完成，请检查本机文件权限、磁盘空间与工具后重新提交。"
        )
        status = (
            "blocked"
            if code in {"media_tools_missing", "storage_insufficient", "media_timeout"}
            else "failed"
        )
        job.update(status=status, phase=status, error={"code": code, "message": message})
        save_job(project, job)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


async def run_media_jobs(project: Project) -> None:
    with closing(sqlite3.connect(project.database)) as db:
        interrupted = [
            json.loads(row[0])
            for row in db.execute("SELECT document FROM media_jobs WHERE status='running'")
        ]
    for job in interrupted:
        token = job.get("staging_token") or job["parameters"].get("staging_token")
        if token:
            (media_directory(project, "staging") / token).unlink(missing_ok=True)
        job.update(
            status="blocked",
            phase="blocked",
            error={
                "code": "media_interrupted",
                "message": "上次处理被中断，请重新提交；未自动重放。",
            },
        )
        save_job(project, job)
    while True:
        if not await process_next_media_job(project):
            await asyncio.sleep(0.5)
