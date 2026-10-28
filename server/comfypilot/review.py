import json
import math
import sqlite3
from contextlib import closing
from fractions import Fraction

from comfypilot.collection import get_version
from comfypilot.media import get_asset
from comfypilot.project import Project


def get_review_version(project: Project, version_id: str) -> dict:
    version = get_version(project, version_id)
    with closing(sqlite3.connect(project.database)) as db:
        row = db.execute(
            "SELECT document FROM execution_runs WHERE id=?", (version["run_id"],)
        ).fetchone()
    final = json.loads(row[0]).get("materialized", {})
    declaration = final.get("outputs", {}).get(version["slot"], {}).get("time_mapping")
    inputs = [
        (item["slot"], get_asset(project, item["asset_id"])) for item in version["source"]["inputs"]
    ]
    video = next(
        (
            asset
            for slot, asset in inputs
            if asset["kind"] == "video" and (not declaration or slot == declaration["input_slot"])
        ),
        None,
    )
    comparison = {
        "status": "unknown",
        "source_asset_id": video["id"] if video else None,
        "reason": "配方未声明保留输入时间轴，仅供并排参考，不能推断逐帧对应。",
    }
    if declaration and video:
        media = version["media"]
        fps = float(Fraction(media["fps"]))
        if version["kind"] == "video" and (
            math.isclose(fps, float(Fraction(video["media"]["fps"])), abs_tol=0.001)
            and math.isclose(
                media["duration_seconds"],
                video["media"]["duration_seconds"],
                abs_tol=1 / fps,
                rel_tol=0,
            )
        ):
            origin, offset = video, 0.0
            while origin["source"]["type"] == "clip":
                offset += origin["source"]["time_map"]["start_seconds"]
                origin = get_asset(project, origin["source"]["asset_id"])
            comparison = {
                "status": "known",
                "source_asset_id": origin["id"],
                "input_asset_id": video["id"],
                "offset_seconds": offset,
                "duration_seconds": media["duration_seconds"],
                "reason": "按配方保留时间轴声明及片段保存的时间映射定位。",
            }
        else:
            comparison["reason"] = "输出与输入的帧率或时长不符合声明，时间映射不可用。"
    return {"version": version, "comparison": comparison}


def initialize_review_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS review_screenshots (id TEXT PRIMARY KEY, "
        "version_id TEXT NOT NULL REFERENCES output_versions(id), document TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS review_feedback (id TEXT PRIMARY KEY, "
        "task_id TEXT NOT NULL REFERENCES execution_tasks(id), "
        "plan_id TEXT NOT NULL REFERENCES execution_plans(id), "
        "version_id TEXT NOT NULL REFERENCES output_versions(id), "
        "status TEXT NOT NULL CHECK(status IN ('open','addressed','verified')), "
        "document TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS review_holds "
        "(feedback_id TEXT PRIMARY KEY REFERENCES review_feedback(id), "
        "task_id TEXT NOT NULL REFERENCES execution_tasks(id), "
        "plan_id TEXT NOT NULL REFERENCES execution_plans(id))"
    )


def read_review_items(db: sqlite3.Connection, unresolved: bool = True) -> dict:
    rows = db.execute(
        "SELECT document FROM review_feedback "
        + ("WHERE status!='verified' " if unresolved else "")
        + "ORDER BY rowid DESC LIMIT 101"
    ).fetchall()
    return {
        "feedback": [json.loads(row[0]) for row in rows[:100]],
        "has_more_feedback": len(rows) > 100,
    }


def list_review_items(project: Project, unresolved: bool = True) -> dict:
    with closing(sqlite3.connect(project.database)) as db:
        db.execute("BEGIN")
        return read_review_items(db, unresolved)


def apply_review_command(db: sqlite3.Connection, project: Project, command) -> tuple[dict, str]:
    from datetime import UTC, datetime
    from uuid import uuid4

    from comfypilot.execution import SaveFeedback
    from comfypilot.project import ProjectError

    now = datetime.now(UTC).isoformat()
    if isinstance(command, SaveFeedback):
        detail = get_review_version(project, command.version_id)
        version, comparison = detail["version"], detail["comparison"]
        duration = version["media"]["duration_seconds"] or 0.0
        if not 0 <= command.start_seconds <= command.end_seconds <= duration:
            raise ProjectError("feedback_range_invalid", "反馈时间必须位于当前版本内且按顺序填写。")
        screenshot = None
        if command.screenshot_id:
            row = db.execute(
                "SELECT document FROM review_screenshots WHERE id=?", (command.screenshot_id,)
            ).fetchone()
            screenshot = json.loads(row[0]) if row else None
            if (
                not screenshot
                or screenshot["version_id"] != command.version_id
                or not command.start_seconds
                <= screenshot["candidate_seconds"]
                <= command.end_seconds
            ):
                raise ProjectError(
                    "screenshot_mismatch", "截图必须属于当前版本并位于反馈时间范围内。"
                )
        run = db.execute(
            "SELECT plan_id FROM execution_runs WHERE id=?", (version["run_id"],)
        ).fetchone()
        plan = db.execute(
            "SELECT task_id FROM execution_plans WHERE id=?", (run["plan_id"],)
        ).fetchone()
        task = db.execute(
            "SELECT active_plan_id FROM execution_tasks WHERE id=?", (plan["task_id"],)
        ).fetchone()
        item = {
            "id": str(uuid4()),
            "task_id": plan["task_id"],
            "plan_id": run["plan_id"],
            "version_id": version["id"],
            "run_id": version["run_id"],
            "kind": command.kind,
            "text": command.text,
            "start_seconds": command.start_seconds,
            "end_seconds": command.end_seconds,
            "original_range": {
                "start_seconds": command.start_seconds + comparison["offset_seconds"],
                "end_seconds": command.end_seconds + comparison["offset_seconds"],
            }
            if comparison["status"] == "known"
            else None,
            "comparison": comparison,
            "screenshot_id": command.screenshot_id,
            "screenshot": screenshot,
            "regions": [region.model_dump() for region in command.regions],
            "source": command.source,
            "status": "open",
            "created_at": now,
            "updates": [],
        }
        db.execute(
            "INSERT INTO review_feedback VALUES (?,?,?,?,?,?)",
            (
                item["id"],
                item["task_id"],
                item["plan_id"],
                item["version_id"],
                item["status"],
                json.dumps(item),
            ),
        )
        if command.kind == "needs_changes" and task["active_plan_id"] == run["plan_id"]:
            db.execute(
                "INSERT INTO review_holds VALUES (?,?,?)",
                (item["id"], item["task_id"], item["plan_id"]),
            )
        result = {
            "feedback_id": item["id"],
            "version_id": version["id"],
            "screenshot_id": command.screenshot_id,
            "status": "open",
        }
    else:
        row = db.execute(
            "SELECT document FROM review_feedback WHERE id=?", (command.feedback_id,)
        ).fetchone()
        if not row:
            raise ProjectError("feedback_unavailable", "反馈不存在。")
        item = json.loads(row[0])
        if item["status"] == "verified" and command.status != "verified":
            raise ProjectError("feedback_verified", "用户已验证的反馈不能改回Agent处理状态。")
        if (
            command.status == "verified"
            and command.source != "web_user"
            and not command.user_statement
        ):
            raise ProjectError(
                "user_decision_required", "用户验证需要网页用户操作或明确的用户原话。"
            )
        item["status"] = command.status
        item["updates"].append(
            {
                "status": command.status,
                "note": command.note,
                "source": command.source,
                "user_statement": command.user_statement,
                "created_at": now,
            }
        )
        db.execute(
            "UPDATE review_feedback SET status=?,document=? WHERE id=?",
            (item["status"], json.dumps(item), item["id"]),
        )
        if command.status == "verified":
            db.execute("DELETE FROM review_holds WHERE feedback_id=?", (item["id"],))
        result = {"feedback_id": item["id"], "status": item["status"]}
    db.execute("UPDATE execution_control SET review_hold=EXISTS(SELECT 1 FROM review_holds)")
    result["review_hold"] = bool(
        db.execute("SELECT review_hold FROM execution_control").fetchone()[0]
    )
    return result, item["id"]
