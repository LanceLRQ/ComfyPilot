import asyncio
import errno
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from comfypilot.execution import get_execution, record_change
from comfypilot.generation import save_run
from comfypilot.media import get_asset, media_directory
from comfypilot.media_tools import inspect_media, media_command, require_tools
from comfypilot.project import Project, ProjectError
from comfypilot.replicas import fingerprint, read_output, transfer_output


def initialize_collection_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS output_versions (id TEXT PRIMARY KEY, "
        "run_id TEXT NOT NULL REFERENCES execution_runs(id), slot TEXT NOT NULL, "
        "document TEXT NOT NULL, UNIQUE(run_id,slot))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS output_checks (version_id TEXT PRIMARY KEY "
        "REFERENCES output_versions(id), document TEXT NOT NULL)"
    )


def list_versions(project: Project) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        db.execute("BEGIN")
        return read_versions(db)


def read_versions(db: sqlite3.Connection) -> dict:
    versions = []
    for row in db.execute(
        "SELECT v.document,p.task_id FROM output_versions v "
        "JOIN execution_runs r ON r.id=v.run_id JOIN execution_plans p ON p.id=r.plan_id "
        "ORDER BY v.rowid DESC LIMIT 100"
    ):
        versions.append({**json.loads(row[0]), "task_id": row[1]})
    return {
        "versions": versions,
        "collections": [
            {
                "run_id": row[0],
                **json.loads(row[1]).get(
                    "collection",
                    {"phase": "failed" if row[2] == "failed" else "waiting_output"},
                ),
            }
            for row in db.execute(
                "SELECT id,document,status FROM execution_runs "
                "WHERE status IN ('collecting','failed') ORDER BY rowid LIMIT 50"
            )
        ],
    }


def get_version(project: Project, version_id: str) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute(
            "SELECT document FROM output_versions WHERE id=?", (version_id,)
        ).fetchone()
    if not row:
        raise ProjectError("version_unavailable", "版本或媒体不存在。")
    return json.loads(row[0])


def version_path(project: Project, version_id: str, variant: str) -> Path:
    version = get_version(project, version_id)
    if variant not in {"original", "preview"}:
        raise ProjectError("version_unavailable", "版本媒体类型不存在。")
    document = version.get("proxy") if variant == "preview" and version.get("proxy") else version
    path = media_directory(project, "artifacts/outputs") / document["content_hash"]
    if not version["ready"] or path.is_symlink() or not path.is_file():
        raise ProjectError("version_unavailable", "版本尚未就绪或受管文件已丢失。")
    return path


async def collect_run(project: Project, run_id: str, *, now: datetime | None = None) -> None:
    run = next(item for item in get_execution(project)["runs"] if item["id"] == run_id)
    if run["status"] != "collecting" or run["document"].get("collection", {}).get("phase") in {
        "failed",
        "blocked",
    }:
        return
    document = run["document"]
    now = now or datetime.now(UTC)
    prior = document.get("collection", {})
    if prior.get("next_retry_at") and datetime.fromisoformat(prior["next_retry_at"]) > now:
        return
    checkpoint = {**document.get("collection", {}), "phase": "waiting_output", "error": None}
    checkpoint["attempts"] = checkpoint.get("attempts", 0) + 1
    checkpoint["next_retry_at"] = None

    def phase(name, **fields):
        checkpoint.update(phase=name, **fields)
        save_run(
            project,
            run_id,
            "failed" if name == "failed" else "collecting",
            {"collection": checkpoint},
        )

    temporary = None
    try:
        if token := checkpoint.get("staging_token"):
            if not re.fullmatch(r"[a-f0-9]{32}\.(part|mp4)", token):
                raise ProjectError("media_path_invalid", "暂存检查点路径无效。")
            abandoned = media_directory(project, "staging") / token
            if abandoned.is_symlink():
                raise ProjectError("media_path_invalid", "暂存检查点被链接替换。")
            abandoned.unlink(missing_ok=True)
        phase("waiting_output")
        success = document.get("external_success")
        if not success:
            raise ProjectError("success_required", "缺少已保存的外部成功证据，禁止收片。")
        require_tools()
        final = document["materialized"]
        for slot, expected in final["outputs"].items():
            with closing(sqlite3.connect(project.database)) as db:
                if db.execute(
                    "SELECT 1 FROM output_versions WHERE run_id=? AND slot=?", (run_id, slot)
                ).fetchone():
                    continue
            outputs = success["history"].get("outputs", {})
            if not isinstance(outputs, dict):
                raise ProjectError("output_invalid", "history输出格式不正确。")
            node = outputs.get(expected["node_id"], {})
            declarations = node.get(expected["key"], []) if isinstance(node, dict) else []
            if not isinstance(declarations, list) or len(declarations) != 1:
                raise ProjectError("output_missing", "配方输出应声明一个文件，等待完整输出声明。")
            replica = await read_output(
                project, final["target_snapshot"]["target"], run_id, slot, declarations[0]
            )
            if not replica["managed"]:
                raise ProjectError("output_unmanaged", "输出不属于本Run受管空间，保留声明证据。")
            checkpoint.setdefault("outputs", {})[slot] = replica
            phase("transferring", slot=slot, bytes_done=0, bytes_total=replica["size"])
            temporary = media_directory(project, "staging") / (uuid4().hex + ".part")
            phase("transferring", staging_token=temporary.name)

            def progress(size):
                checkpoint.update(bytes_done=size)
                save_run(project, run_id, "collecting", {"collection": checkpoint}, durable=False)

            source_proof = await transfer_output(
                project, final["target_snapshot"]["target"], replica, temporary, progress
            )
            proof = await fingerprint(temporary)
            if (
                proof["sha256"] != replica["content_hash"]
                or proof["size"] != replica["size"]
                or source_proof != replica["fingerprint"]
            ):
                raise ProjectError("replica_changed", "输出在传输期间变化，不覆盖旧证据。")
            phase("checking")
            media = await inspect_media(temporary, expected["kind"], count_frames=True)
            mismatches = []
            for key, value in (expected.get("spec") or {}).items():
                if value is None:
                    continue
                actual = float(Fraction(media["fps"])) if key == "fps" else media.get(key)
                tolerance = (
                    1 / float(Fraction(media["fps"])) if key == "duration_seconds" else 0.001
                )
                if actual is None or not math.isclose(actual, value, abs_tol=tolerance, rel_tol=0):
                    mismatches.append({"field": key, "expected": value, "actual": actual})
            checks = {
                "status": "passed" if not mismatches else "failed",
                "decoded": True,
                "media": media,
                "mismatches": mismatches,
            }
            checkpoint["checks"] = checks
            if mismatches:
                raise ProjectError("media_spec_mismatch", "输出规格与配方不符，不生成可审版本。")
            destination = media_directory(project, "artifacts/outputs") / proof["sha256"]
            if destination.is_symlink():
                raise ProjectError("media_path_invalid", "受管原件被链接替换。")
            if not destination.exists():
                temporary.replace(destination)
            temporary.unlink(missing_ok=True)
            temporary = None
            proxy = None
            if expected["kind"] == "video" and (
                media["codec"] != "h264" or "mp4" not in media["container"]
            ):
                phase("proxy")
                temporary = media_directory(project, "staging") / (uuid4().hex + ".mp4")
                phase("proxy", staging_token=temporary.name)
                tools = require_tools()
                await media_command(
                    [
                        tools["ffmpeg"],
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-n",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        str(destination),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a?",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-movflags",
                        "+faststart",
                        str(temporary),
                    ]
                )
                proxy_media = await inspect_media(temporary, "video", count_frames=True)
                if (
                    proxy_media["frame_count"] != media["frame_count"]
                    or proxy_media["width"] != media["width"]
                    or proxy_media["height"] != media["height"]
                ):
                    raise ProjectError("media_spec_mismatch", "播放代理与原件画面规格不一致。")
                proxy_proof = await fingerprint(temporary)
                proxy = {
                    "content_hash": proxy_proof["sha256"],
                    "size": proxy_proof["size"],
                    "media": proxy_media,
                    "filename": "preview.mp4",
                }
                proxy_path = destination.parent / proxy["content_hash"]
                if proxy_path.is_symlink():
                    raise ProjectError("media_path_invalid", "受管代理被链接替换。")
                if not proxy_path.exists():
                    temporary.replace(proxy_path)
                temporary.unlink(missing_ok=True)
                temporary = None
            phase("registering")
            version = {
                "id": str(uuid4()),
                "run_id": run_id,
                "slot": slot,
                "kind": expected["kind"],
                "content_hash": proof["sha256"],
                "size": proof["size"],
                "filename": replica["declaration"]["filename"],
                "media": media,
                "checks": checks,
                "proxy": proxy,
                "ready": True,
                "source": {
                    "segment_id": document["step"].get("segment_id"),
                    "inputs": [
                        {**item, "source": get_asset(project, item["asset_id"])["source"]}
                        for item in final["inputs"]
                    ],
                    "target_snapshot": final["target_snapshot"],
                    "graph_hash": final["graph_hash"],
                    "prompt_id": success["prompt_id"],
                },
                "created_at": datetime.now(UTC).isoformat(),
            }
            with closing(sqlite3.connect(project.database)) as db, db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                inserted = db.execute(
                    "INSERT OR IGNORE INTO output_versions VALUES (?,?,?,?)",
                    (version["id"], run_id, slot, json.dumps(version)),
                ).rowcount
                if inserted:
                    db.execute(
                        "INSERT INTO output_checks VALUES (?,?)",
                        (version["id"], json.dumps(checks)),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO asset_references VALUES (?,?)",
                        (version["id"], replica["id"]),
                    )
                    record_change(
                        db,
                        "clip_ready",
                        {"version_id": version["id"], "run_id": run_id, "slot": slot},
                        "worker",
                        run_id,
                    )
        checkpoint.update(phase="ready", error=None)
        save_run(project, run_id, "succeeded", {"collection": checkpoint})
    except (ProjectError, OSError, TimeoutError) as error:
        code = (
            error.code
            if isinstance(error, ProjectError)
            else (
                "storage_insufficient"
                if isinstance(error, OSError) and error.errno == errno.ENOSPC
                else "output_unavailable"
            )
        )
        transient = code in {"output_unavailable", "output_missing", "bridge_unavailable"}
        retrying = transient and checkpoint["attempts"] < 4
        phase(
            "failed"
            if code in {"media_invalid", "media_spec_mismatch"}
            else "retry_wait"
            if retrying
            else "blocked",
            next_retry_at=(now + timedelta(seconds=2 ** checkpoint["attempts"])).isoformat()
            if retrying
            else None,
            error={
                "code": code,
                "message": str(error)
                if isinstance(error, ProjectError)
                else "输出暂不可读，保留检查点等待处理。",
            },
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def run_collection(project: Project) -> None:
    while True:
        for run in get_execution(project)["runs"]:
            if run["status"] == "collecting":
                await collect_run(project, run["id"])
        await asyncio.sleep(2)
