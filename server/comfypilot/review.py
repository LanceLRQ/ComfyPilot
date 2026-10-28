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
