import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from comfypilot.media import CHUNK_BYTES, asset_path, check_space, get_asset
from comfypilot.project import Project, ProjectError
from comfypilot.recipes import content_hash


class OutputDeclaration(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    filename: str = Field(min_length=1, max_length=255)
    subfolder: str = Field(default="", max_length=4096)
    type: str = Field(default="output", max_length=32)


def initialize_replica_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS asset_replicas (id TEXT PRIMARY KEY, "
        "target_key TEXT NOT NULL, relative_path TEXT NOT NULL, role TEXT NOT NULL, "
        "document TEXT NOT NULL, UNIQUE(target_key,role,relative_path))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS asset_references (owner_id TEXT NOT NULL, "
        "replica_id TEXT NOT NULL REFERENCES asset_replicas(id), "
        "PRIMARY KEY(owner_id,replica_id))"
    )


def local_path(target: dict, project_id: str, role: str, relative: str) -> Path:
    config = target.get("assets")
    if not config or config["mode"] != "local":
        raise ProjectError(
            "media_transfer_unavailable", "请显式配置目标素材适配器和可访问的input/output目录。"
        )
    parts = PurePosixPath(relative).parts
    if (
        role not in {"input", "output"}
        or PurePosixPath(relative).is_absolute()
        or ".." in parts
        or "\\" in relative
        or any(ord(c) < 32 for c in relative)
        or parts[:2] != ("ComfyPilot", project_id)
    ):
        raise ProjectError("replica_path_invalid", "素材路径不属于本项目受管空间。")
    root = Path(config[role + "_dir"])
    path = root.joinpath(*parts)
    if not root.is_dir() or any(item.is_symlink() for item in (path, *path.parents)):
        raise ProjectError(
            "replica_path_invalid", "素材目录不存在或含符号链接，请核对配置和真实目录。"
        )
    if not path.resolve().is_relative_to(root.resolve()):
        raise ProjectError("replica_path_invalid", "素材真实路径越过目标根目录。")
    return path


async def fingerprint(path: Path) -> dict:
    before = path.stat()
    if not path.is_file() or path.is_symlink():
        raise ProjectError("replica_path_invalid", "副本必须是无符号链接的普通文件。")
    digest = sha256()
    with path.open("rb") as file:
        while chunk := file.read(CHUNK_BYTES):
            digest.update(chunk)
            await asyncio.sleep(0)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ProjectError("replica_changed", "素材在读取期间变化，保留现场并重新核对。")
    return {
        "sha256": digest.hexdigest(),
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "inode": after.st_ino,
    }


async def inspect_replica(project: Project, target: dict, role: str, relative: str) -> dict:
    if (target.get("assets") or {}).get("mode") == "bridge":
        from comfypilot.asset_bridge import remote_stat

        return await remote_stat(project, target, role, relative)
    proof = await fingerprint(local_path(target, str(project.identity.project_id), role, relative))
    return {"fingerprint": proof, "content_hash": proof["sha256"], "size": proof["size"]}


async def probe_asset_capabilities(project: Project, target: dict) -> dict:
    assets = target.get("assets")
    status, reason = "unsupported", "未配置素材适配器。"
    deletion = {"status": status, "reason": reason}
    if assets:
        try:
            if assets["mode"] == "bridge":
                from comfypilot.asset_bridge import probe_bridge

                bridge = await probe_bridge(project, target)
                deletion = {
                    "status": "available" if "delete" in bridge["features"] else "unsupported",
                    "reason": "已验证受限删除能力。"
                    if "delete" in bridge["features"]
                    else "旧桥接未声明 delete，请按说明升级扩展。",
                }
            else:
                for role, permission in (("input", os.W_OK), ("output", os.R_OK)):
                    local_path(
                        target,
                        str(project.identity.project_id),
                        role,
                        f"ComfyPilot/{project.identity.project_id}/probe",
                    )
                    if not os.access(assets[role + "_dir"], permission):
                        raise ProjectError("media_transfer_unavailable", "本机素材目录权限不足。")
                writable = os.access(assets["output_dir"], os.W_OK)
                deletion = {
                    "status": "available" if writable else "unsupported",
                    "reason": "已验证受管目录写权限。" if writable else "输出目录不可写。",
                }
            status, reason = "available", "已验证配置的素材适配器。"
        except (ProjectError, OSError) as error:
            status = (
                "unsupported"
                if isinstance(error, ProjectError) and error.code == "media_transfer_unavailable"
                else "unknown"
            )
            reason = error.code if isinstance(error, ProjectError) else "directory_unavailable"
            deletion = {"status": status, "reason": reason}
    return {
        **{
            name: {"status": status, "reason": reason}
            for name in ("media_transfer", "managed_listing")
        },
        "managed_delete": deletion,
    }


def list_replicas(project: Project) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        return {
            "replicas": [
                json.loads(row[0]) for row in db.execute("SELECT document FROM asset_replicas")
            ],
            "references": [
                {"owner_id": row[0], "replica_id": row[1]}
                for row in db.execute("SELECT owner_id,replica_id FROM asset_references")
            ],
        }


def save_replica(project: Project, replica: dict, owner: str) -> None:
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(
            "SELECT 1 FROM cleanup_claims WHERE replica_id=?", (replica["id"],)
        ).fetchone():
            raise ProjectError("cleanup_in_progress", "副本正在清理，不能增加引用。")
        db.execute(
            "INSERT INTO asset_replicas VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET document=excluded.document",
            (
                replica["id"],
                replica["target_key"],
                replica["relative_path"],
                replica["role"],
                json.dumps(replica),
            ),
        )
        db.execute("INSERT OR IGNORE INTO asset_references VALUES (?,?)", (owner, replica["id"]))


async def put_input(project: Project, target: dict, asset_id: str, owner: str) -> dict:
    asset = get_asset(project, asset_id)
    suffix = Path(asset["filename"]).suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".webp"}:
        raise ProjectError("media_invalid", "输入文件扩展名不受支持，请显式转换后导入。")
    relative = f"ComfyPilot/{project.identity.project_id}/{asset_id}{suffix}"
    key = content_hash(target)
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute(
            "SELECT document FROM asset_replicas "
            "WHERE target_key=? AND role='input' AND relative_path=?",
            (key, relative),
        ).fetchone()
    reusable_id = json.loads(row[0])["id"] if row else str(uuid4())
    if row and json.loads(row[0]).get("cleanup_state") != "deleted":
        replica = json.loads(row[0])
        if (await inspect_replica(project, target, "input", relative))["fingerprint"] != replica[
            "fingerprint"
        ]:
            raise ProjectError("replica_changed", "既有副本指纹变化，不覆盖或接管同名文件。")
        save_replica(project, replica, owner)
        return replica
    remote = (target.get("assets") or {}).get("mode") == "bridge"
    if remote:
        from comfypilot.asset_bridge import upload_input

        proof = (
            await upload_input(
                project, target, asset_path(project, asset_id), relative, asset["content_hash"]
            )
        )["fingerprint"]
        if proof["sha256"] != asset["content_hash"] or proof["size"] != asset["size"]:
            raise ProjectError("asset_mismatch", "上传副本与授权素材哈希/大小不一致。")
        replica = {
            "id": reusable_id,
            "project_id": str(project.identity.project_id),
            "target_key": key,
            "target_snapshot": target,
            "asset_id": asset_id,
            "role": "input",
            "relative_path": relative,
            "size": proof["size"],
            "content_hash": proof["sha256"],
            "fingerprint": proof,
            "managed": True,
            "cleanup_eligible": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
        save_replica(project, replica, owner)
        return replica
    destination = local_path(target, str(project.identity.project_id), "input", relative)
    if destination.exists():
        raise ProjectError("replica_conflict", "目标存在未登记同名文件，不接管或覆盖。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    check_space(destination.parent, asset["size"])
    temporary = destination.parent / (uuid4().hex + ".part")
    digest, size = sha256(), 0
    try:
        with asset_path(project, asset_id).open("rb") as source, temporary.open("xb") as output:
            while chunk := source.read(CHUNK_BYTES):
                check_space(destination.parent, len(chunk))
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                await asyncio.sleep(0)
        if size != asset["size"] or digest.hexdigest() != asset["content_hash"]:
            raise ProjectError("asset_mismatch", "项目原件与登记哈希不符，未上传，请重新核对素材。")
        # 使用硬链接以不覆盖方式就位，两个文件处于同一目标目录。
        destination.hardlink_to(temporary)
        replica = {
            "id": reusable_id,
            "project_id": str(project.identity.project_id),
            "target_key": key,
            "target_snapshot": target,
            "asset_id": asset_id,
            "role": "input",
            "relative_path": relative,
            "size": size,
            "content_hash": digest.hexdigest(),
            "fingerprint": await fingerprint(destination),
            "managed": True,
            "cleanup_eligible": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
        save_replica(project, replica, owner)
        return replica
    finally:
        temporary.unlink(missing_ok=True)


async def read_output(
    project: Project, target: dict, run_id: str, slot: str, declaration: dict
) -> dict:
    try:
        declaration = OutputDeclaration.model_validate(declaration).model_dump()
    except ValidationError as error:
        raise ProjectError(
            "output_invalid", "执行端输出声明格式不正确，保留history供核对。"
        ) from error
    relative = str(
        PurePosixPath(declaration.get("subfolder", "")) / declaration.get("filename", "")
    )
    prefix = f"ComfyPilot/{project.identity.project_id}/{run_id}/"
    if declaration.get("type") != "output" or not relative.startswith(prefix):
        return {
            "managed": False,
            "cleanup_eligible": False,
            "declaration": declaration,
            "reason": "输出不在本Run受管空间，仅保留声明证据。",
        }
    inspected = await inspect_replica(project, target, "output", relative)
    proof = inspected["fingerprint"]
    replica = {
        "id": str(uuid4()),
        "project_id": str(project.identity.project_id),
        "target_key": content_hash(target),
        "target_snapshot": target,
        "asset_id": f"{run_id}:{slot}",
        "role": "output",
        "relative_path": relative,
        "size": proof["size"],
        "content_hash": proof["sha256"],
        "fingerprint": proof,
        "fingerprint_id": inspected.get("fingerprint_id"),
        "managed": True,
        "cleanup_eligible": False,
        "declaration": declaration,
    }
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute(
            "SELECT document FROM asset_replicas "
            "WHERE target_key=? AND role='output' AND relative_path=?",
            (replica["target_key"], relative),
        ).fetchone()
    if row:
        old = json.loads(row[0])
        if old["fingerprint"] != proof:
            raise ProjectError("replica_changed", "目标输出指纹已变化，保留旧证据。")
        replica = old
    save_replica(project, replica, run_id)
    return replica


async def transfer_output(
    project: Project, target: dict, replica: dict, destination: Path, progress
):
    if (target.get("assets") or {}).get("mode") == "bridge":
        from comfypilot.asset_bridge import download_output

        return await download_output(project, target, replica, destination, progress)
    source = local_path(
        target, str(project.identity.project_id), "output", replica["relative_path"]
    )
    size = 0
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        while chunk := incoming.read(CHUNK_BYTES):
            check_space(destination.parent, len(chunk))
            outgoing.write(chunk)
            size += len(chunk)
            progress(size)
            await asyncio.sleep(0)
    return await fingerprint(source)


async def materialize_run(project: Project, run_id: str) -> dict:
    from comfypilot.execution import Plan, check_targets

    with closing(sqlite3.connect(project.database)) as db:
        db.row_factory = sqlite3.Row
        run = db.execute("SELECT * FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise ProjectError("run_unavailable", "运行不存在。")
        plan = db.execute("SELECT * FROM execution_plans WHERE id=?", (run["plan_id"],)).fetchone()
    document = json.loads(run["document"])
    if "materialized" in document:
        final = document["materialized"]
        for replica in final["inputs"]:
            await put_input(
                project, final["target_snapshot"]["target"], replica["asset_id"], run_id
            )
        return final
    if run["status"] != "queued" or run["intent"] or plan["status"] != "active":
        raise ProjectError("plan_unavailable", "仅准备当前有效方案的未提交运行。")
    targets = json.loads(plan["targets"])
    check_targets(project, targets)
    step = Plan.model_validate_json(plan["document"]).steps[run["action_index"]]
    frozen = document["resource_snapshot"]
    if set(step.asset_ids) != set(step.inputs):
        raise ProjectError(
            "asset_identity_required", "执行需要为每个输入提供独立asset_ids；请建立新方案。"
        )
    target = targets[step.target_id]["target"]
    graph = json.loads(json.dumps(frozen["graph"]))
    replicas = []
    for slot in frozen["manifest"]["input_order"]:
        asset = get_asset(project, step.asset_ids[slot])
        if (
            asset["content_hash"] != step.inputs[slot]
            or asset["kind"] != frozen["manifest"]["inputs"][slot]["kind"]
        ):
            raise ProjectError("asset_mismatch", "素材身份或类型与方案授权哈希不符。")
        replica = await put_input(project, target, asset["id"], run_id)
        replicas.append({"slot": slot, **replica})
        for binding in frozen["manifest"]["inputs"][slot]["bindings"]:
            graph[binding["node_id"]]["inputs"][binding["input_name"]] = replica["relative_path"]
    outputs = {}
    for slot, output in frozen["manifest"]["outputs"].items():
        prefix = f"ComfyPilot/{project.identity.project_id}/{run_id}/{slot}"
        if binding := output.get("filename_prefix"):
            graph[binding["node_id"]]["inputs"][binding["input_name"]] = prefix
        outputs[slot] = {**output, "prefix": prefix, "managed_prefix_bound": bool(binding)}
    result = {
        "graph": graph,
        "graph_hash": content_hash(graph),
        "inputs": replicas,
        "outputs": outputs,
        "target_snapshot": targets[step.target_id],
        "resource_snapshot": frozen,
    }
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        check_targets(project, targets)
        document["materialized"] = result
        changed = db.execute(
            "UPDATE execution_runs SET document=? WHERE id=? AND status='queued' "
            "AND intent IS NULL AND plan_id IN "
            "(SELECT id FROM execution_plans WHERE status='active')",
            (json.dumps(document), run_id),
        ).rowcount
        if not changed:
            raise ProjectError("plan_unavailable", "准备期间方案已停止，不提交生成。")
    return result
