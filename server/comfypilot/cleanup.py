import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from comfypilot.collection import version_path
from comfypilot.media import asset_path, get_asset
from comfypilot.project import Project, ProjectError
from comfypilot.recipes import content_hash
from comfypilot.replicas import fingerprint, inspect_replica
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
            if not copies:
                reasons.append("original_missing")
            for copy in copies:
                if copy["kind"] == "input":
                    local = get_asset(project, copy["id"])
                    path = asset_path(project, copy["id"])
                    expected = local["content_hash"]
                else:
                    path = version_path(project, copy["id"], "original")
                    expected = copy["hash"]
                if (
                    expected != replica["content_hash"]
                    or (await fingerprint(path))["sha256"] != expected
                ):
                    reasons.append("original_changed")
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
