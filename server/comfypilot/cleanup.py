import fcntl
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from comfypilot.collection import version_path
from comfypilot.media import asset_path, get_asset
from comfypilot.project import Project, ProjectError
from comfypilot.recipes import content_hash
from comfypilot.replicas import fingerprint, inspect_replica, local_path
from comfypilot.settings import resolve_target


class CleanupScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_id: str = Field(min_length=1, max_length=64)
    replica_ids: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    roles: list[Literal["input", "output"]] = Field(default_factory=lambda: ["input", "output"])


class PrepareCleanup(CleanupScope):
    user_instruction: str = Field(min_length=1, max_length=4000, pattern=r"\S")


def initialize_cleanup_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS cleanup_plans (id TEXT PRIMARY KEY, document TEXT NOT NULL)"
    )


def replica_references(db: sqlite3.Connection, replica: dict) -> tuple[list, list, list]:
    reasons, references, local_copies = [], [], []
    owners = [
        r[0]
        for r in db.execute(
            "SELECT owner_id FROM asset_references WHERE replica_id=?", (replica["id"],)
        )
    ]
    if not owners:
        reasons.append("unknown_reference")
    for owner in owners:
        run = db.execute("SELECT status FROM execution_runs WHERE id=?", (owner,)).fetchone()
        if not run:
            run = db.execute(
                "SELECT r.status FROM output_versions v JOIN execution_runs r ON r.id=v.run_id "
                "WHERE v.id=?",
                (owner,),
            ).fetchone()
        references.append({"kind": "run", "id": owner, "status": run[0] if run else "unknown"})
        if not run:
            reasons.append("unknown_reference")
        elif run[0] not in {"succeeded", "failed", "cancelled"}:
            reasons.append("active_run")
    if db.execute("SELECT reconciliation_required FROM execution_control").fetchone()[0]:
        reasons.append("reconciliation_required")
    if replica["role"] == "input":
        for row in db.execute(
            "SELECT id,status,document FROM execution_plans WHERE status IN ('draft','active')"
        ):
            plan = json.loads(row[2])
            if any(replica["content_hash"] in step["inputs"].values() for step in plan["steps"]):
                references.append({"kind": "plan", "id": row[0], "status": row[1]})
                reasons.append("plan_reference")
        local_copies.append({"kind": "input", "id": replica["asset_id"]})
    else:
        run_id, _, slot = replica["asset_id"].partition(":")
        versions = db.execute(
            "SELECT id,document FROM output_versions WHERE run_id=? AND slot=?", (run_id, slot)
        ).fetchall()
        if not versions:
            reasons.append("uncollected_output")
        for version_id, raw in versions:
            version = json.loads(raw)
            references.append({"kind": "version", "id": version_id})
            if not db.execute(
                "SELECT 1 FROM user_decisions WHERE version_id=?", (version_id,)
            ).fetchone():
                reasons.append("pending_review")
            if db.execute(
                "SELECT 1 FROM review_feedback WHERE version_id=? AND status!='verified'",
                (version_id,),
            ).fetchone():
                reasons.append("unresolved_feedback")
            if not version["ready"] or version["checks"]["status"] != "passed":
                reasons.append("unverified_original")
            local_copies.append(
                {"kind": "output", "id": version_id, "hash": version["content_hash"]}
            )
    return list(dict.fromkeys(reasons)), references, local_copies


async def inspect_storage(project: Project, scope: CleanupScope) -> dict:
    selected = resolve_target(project, scope.target_id)
    target = selected["target"]
    with closing(sqlite3.connect(project.database)) as db:
        db.execute("BEGIN")
        revision = db.execute("SELECT revision FROM execution_control").fetchone()[0]
        replicas = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT document FROM asset_replicas WHERE target_key=?", (content_hash(target),)
            )
        ]
        by_id = {r["id"]: r for r in replicas}
        if scope.replica_ids is not None and not set(scope.replica_ids).issubset(by_id):
            raise ProjectError("replica_unavailable", "清单包含未登记或不属于指定目标的副本。")
        entries = []
        for replica in replicas:
            if replica["role"] not in scope.roles or (
                scope.replica_ids is not None and replica["id"] not in scope.replica_ids
            ):
                continue
            reasons, references, copies = replica_references(db, replica)
            if not replica["managed"] or replica["project_id"] != str(project.identity.project_id):
                reasons.append("ownership_unknown")
            entries.append((replica, reasons, references, copies))
    items = []
    for replica, reasons, references, copies in entries:
        try:
            observed = await inspect_replica(
                project, target, replica["role"], replica["relative_path"]
            )
            if observed["fingerprint"] != replica["fingerprint"]:
                reasons.append("fingerprint_changed")
            await verify_originals(project, replica, copies)
        except (ProjectError, OSError) as error:
            reasons.append(
                error.code if isinstance(error, ProjectError) else "original_or_replica_unavailable"
            )
        items.append(
            {
                "replica_id": replica["id"],
                "project_id": replica["project_id"],
                "target_key": replica["target_key"],
                "role": replica["role"],
                "relative_path": replica["relative_path"],
                "size": replica["size"],
                "fingerprint": replica["fingerprint"],
                "references": references,
                "protection_reasons": list(dict.fromkeys(reasons)),
                "eligible": not reasons,
            }
        )
    return {
        "project_id": str(project.identity.project_id),
        "target_snapshot": selected,
        "reference_revision": revision,
        "items": items,
        "total_bytes": sum(x["size"] for x in items),
        "estimated_bytes": sum(x["size"] for x in items if x["eligible"]),
        "scope": scope.model_dump(),
        "boundary": "仅登记副本；未知文件、半文件、模型、节点及本地正式版本不在清单内。",
    }


async def prepare_cleanup(project: Project, request: PrepareCleanup) -> dict:
    result = await inspect_storage(
        project, CleanupScope(**request.model_dump(exclude={"user_instruction"}))
    )
    result.update(
        id=str(uuid4()),
        created_at=datetime.now(UTC).isoformat(),
        user_instruction=request.user_instruction,
    )
    with closing(sqlite3.connect(project.database)) as db, db:
        db.execute("INSERT INTO cleanup_plans VALUES (?,?)", (result["id"], json.dumps(result)))
    return result


class ExecuteCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_id: str = Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    command_id: str = Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")


def initialize_cleanup_execution_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS cleanup_claims (replica_id TEXT PRIMARY KEY "
        "REFERENCES asset_replicas(id), plan_id TEXT NOT NULL REFERENCES cleanup_plans(id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS cleanup_receipts (command_id TEXT PRIMARY KEY, "
        "plan_id TEXT NOT NULL REFERENCES cleanup_plans(id), document TEXT)"
    )


def check_cleanup_claims(db: sqlite3.Connection, hashes) -> None:
    for (raw,) in db.execute(
        "SELECT r.document FROM cleanup_claims c JOIN asset_replicas r ON r.id=c.replica_id"
    ):
        if json.loads(raw)["content_hash"] in hashes:
            raise ProjectError(
                "cleanup_in_progress", "相关素材正在清理，请稍后读取并重新提出方案。"
            )


async def verify_originals(project: Project, replica: dict, copies: list) -> None:
    if not copies:
        raise ProjectError("original_missing", "没有可核对的项目原件。")
    for copy in copies:
        if copy["kind"] == "input":
            local = get_asset(project, copy["id"])
            path = asset_path(project, copy["id"])
            expected = local["content_hash"]
        else:
            path = version_path(project, copy["id"], "original")
            expected = copy["hash"]
        if expected != replica["content_hash"] or (await fingerprint(path))["sha256"] != expected:
            raise ProjectError("original_changed", "项目原件哈希变化，不能删除执行端副本。")


async def unlink_replica(project: Project, replica: dict, operation_id: str) -> dict:
    target = replica["target_snapshot"]
    if (target.get("assets") or {}).get("mode") == "bridge":
        from comfypilot.asset_bridge import bridge_json, probe_bridge

        capabilities = await probe_bridge(project, target)
        if "delete" not in capabilities["features"]:
            raise ProjectError("cleanup_unavailable", "目标桥接不支持受限删除，请按说明升级扩展。")
        return await bridge_json(
            project,
            target,
            "DELETE",
            f"projects/{project.identity.project_id}/delete",
            json={
                "operation_id": operation_id,
                "role": replica["role"],
                "path": replica["relative_path"],
                "fingerprint": replica["fingerprint"],
            },
        )
    path = local_path(
        target, str(project.identity.project_id), replica["role"], replica["relative_path"]
    )
    if not path.exists():
        return {"status": "already_missing", "free_change_bytes": None}
    observed = await fingerprint(path)
    if observed != replica["fingerprint"]:
        return {"status": "changed", "free_change_bytes": None}
    before = shutil.disk_usage(path.parent).free
    path.unlink()
    return {"status": "deleted", "free_change_bytes": shutil.disk_usage(path.parent).free - before}


def list_cleanup(project: Project) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        return {
            "plans": [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT document FROM cleanup_plans ORDER BY rowid DESC LIMIT 50"
                )
            ],
            "receipts": [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT document FROM cleanup_receipts WHERE document IS NOT NULL "
                    "ORDER BY rowid DESC LIMIT 50"
                )
            ],
        }


async def execute_cleanup(project: Project, request: ExecuteCleanup) -> dict:
    from comfypilot.execution import record_change

    lock = (project.root / ".comfypilot/locks/cleanup.lock").open("a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProjectError(
                "cleanup_busy", "本项目已有清理执行，请保留命令ID并稍后读取。"
            ) from error
        with closing(sqlite3.connect(project.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT plan_id,document FROM cleanup_receipts WHERE command_id=?",
                (request.command_id,),
            ).fetchone()
            if prior:
                if prior[0] != request.plan_id:
                    raise ProjectError("command_mismatch", "命令ID已用于其他清理计划。")
                if prior[1]:
                    return json.loads(prior[1])
            row = db.execute(
                "SELECT document FROM cleanup_plans WHERE id=?", (request.plan_id,)
            ).fetchone()
            if not row:
                raise ProjectError("cleanup_unavailable", "清理计划不存在，请先生成固定清单。")
            plan = json.loads(row[0])
            db.execute(
                "INSERT OR IGNORE INTO cleanup_receipts VALUES (?,?,NULL)",
                (request.command_id, request.plan_id),
            )
        results = []
        for item in plan["items"]:
            result = {
                "replica_id": item["replica_id"],
                "status": "protected",
                "reason": "protected_at_preview",
            }
            if item["eligible"]:
                try:
                    selected = resolve_target(project, plan["scope"]["target_id"])
                    if content_hash(selected["target"]) != item["target_key"]:
                        raise ProjectError(
                            "target_changed", "目标身份已变化，旧清单不会转向新地址。"
                        )
                    with closing(sqlite3.connect(project.database)) as db, db:
                        db.execute("BEGIN IMMEDIATE")
                        row = db.execute(
                            "SELECT document FROM asset_replicas WHERE id=?", (item["replica_id"],)
                        ).fetchone()
                        if not row:
                            raise ProjectError("replica_unavailable", "副本记录不存在。")
                        replica = json.loads(row[0])
                        if (
                            replica["fingerprint"] != item["fingerprint"]
                            or replica["target_key"] != item["target_key"]
                            or replica["relative_path"] != item["relative_path"]
                            or replica["project_id"] != plan["project_id"]
                            or not replica["managed"]
                        ):
                            raise ProjectError("replica_changed", "副本身份发生变化。")
                        reasons, _, copies = replica_references(db, replica)
                        if reasons:
                            raise ProjectError("reference_protected", "、".join(reasons))
                        claim = db.execute(
                            "SELECT plan_id FROM cleanup_claims WHERE replica_id=?",
                            (replica["id"],),
                        ).fetchone()
                        if claim and claim[0] != request.plan_id:
                            raise ProjectError("cleanup_in_progress", "其他清单已领取副本。")
                        db.execute(
                            "INSERT OR IGNORE INTO cleanup_claims VALUES (?,?)",
                            (replica["id"], request.plan_id),
                        )
                    await verify_originals(project, replica, copies)
                    operation = str(uuid5(NAMESPACE_URL, request.command_id + ":" + replica["id"]))
                    result.update(await unlink_replica(project, replica, operation), reason=None)
                    if result["status"] not in {"deleted", "already_missing", "changed"}:
                        raise ProjectError("bridge_unavailable", "桥接删除回执格式不正确。")
                    if result["status"] in {"deleted", "already_missing"}:
                        replica["cleanup_state"] = "deleted"
                        with closing(sqlite3.connect(project.database)) as db, db:
                            db.execute(
                                "UPDATE asset_replicas SET document=? WHERE id=?",
                                (json.dumps(replica), replica["id"]),
                            )
                except (ProjectError, OSError) as error:
                    code = error.code if isinstance(error, ProjectError) else "asset_io_unavailable"
                    status = (
                        "changed"
                        if code in {"replica_changed", "target_changed"}
                        else "protected"
                        if code
                        in {
                            "reference_protected",
                            "original_changed",
                            "original_missing",
                            "cleanup_in_progress",
                        }
                        else "failed"
                    )
                    result.update(status=status, reason=code)
                finally:
                    with closing(sqlite3.connect(project.database)) as db, db:
                        db.execute(
                            "DELETE FROM cleanup_claims WHERE replica_id=? AND plan_id=?",
                            (item["replica_id"], request.plan_id),
                        )
            results.append(result)
        receipt = {
            "command_id": request.command_id,
            "plan_id": request.plan_id,
            "items": results,
            "deleted_bytes": sum(
                item["size"]
                for item, result in zip(plan["items"], results, strict=True)
                if result["status"] == "deleted"
            ),
        }
        with closing(sqlite3.connect(project.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            event = record_change(db, "execute_cleanup", receipt, "local_control", request.plan_id)
            receipt.update(revision=event["revision"], event_id=event["event_id"])
            db.execute(
                "UPDATE cleanup_receipts SET document=? WHERE command_id=?",
                (json.dumps(receipt), request.command_id),
            )
        return receipt
    finally:
        lock.close()
