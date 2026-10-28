import re
from ipaddress import ip_address

from fastapi import Request


def local_control(request: Request) -> bool:
    return bool(request.client and ip_address(request.client.host).is_loopback)


def review_route(path: str, method: str) -> bool:
    if method == "POST":
        return path in {"/api/execution/command", "/api/review/screenshots"}
    return method in {"GET", "HEAD"} and (
        path
        in {
            "/api/project",
            "/api/state",
            "/api/status",
            "/api/versions",
            "/api/events",
            "/api/events/stream",
            "/api/review/items",
            "/api/review/delivery",
        }
        or bool(
            re.fullmatch(
                r"/api/(versions/[a-f0-9-]{36}(/content)?|assets/[a-f0-9-]{36}/content|review/screenshots/[a-f0-9-]{36})",
                path,
            )
        )
    )


def public_source(source: dict) -> dict:
    return {
        **{k: source[k] for k in ("segment_id", "graph_hash", "prompt_id") if k in source},
        "inputs": [{"slot": x["slot"], "asset_id": x["asset_id"]} for x in source["inputs"]],
        "target_snapshot": {
            "target": {k: source["target_snapshot"]["target"][k] for k in ("id", "name")}
        },
    }


def public_version(version: dict) -> dict:
    return {
        **{
            k: version[k]
            for k in (
                "id",
                "task_id",
                "run_id",
                "slot",
                "kind",
                "ready",
                "content_hash",
                "media",
                "created_at",
            )
            if k in version
        },
        "checks": {"status": version["checks"]["status"]},
        "source": public_source(version["source"]),
    }


def public_monitor(value):
    if isinstance(value, list):
        return [public_monitor(x) for x in value]
    if not isinstance(value, dict):
        return value
    if "availability" in value:
        return {
            **value,
            "source": "执行端采集",
            "reason": "请在本机查看详情" if value["reason"] else None,
        }
    return {
        key: None
        if key == "pid"
        else "请在本机查看详情"
        if key == "reason"
        else public_monitor(child)
        for key, child in value.items()
        if key not in {"base_url", "target_snapshot", "credential_ref"}
    }


def public_state(state: dict) -> dict:
    result = {
        k: state[k]
        for k in (
            "revision",
            "event_cursor",
            "dispatch",
            "paused_plan_ids",
            "feedback",
            "adoptions",
            "decisions",
            "evaluations",
            "activity",
        )
    }
    result["local_control"] = False
    result["tasks"] = [
        {k: task[k] for k in ("id", "active_plan_id", "goal")} for task in state["tasks"]
    ]
    result["plans"] = [{k: plan[k] for k in ("id", "task_id", "status")} for plan in state["plans"]]
    result["runs"] = [
        {
            **{
                k: run[k]
                for k in ("id", "plan_id", "action_index", "status", "has_intent", "started_at")
            },
            "document": {
                "step": {
                    k: run["document"].get("step", {})[k]
                    for k in ("target_id", "segment_id")
                    if k in run["document"].get("step", {})
                }
            },
        }
        for run in state["runs"]
    ]
    result["versions"] = [public_version(x) for x in state["versions"]]
    result.update(public_latest(state))
    return result


def public_latest(state: dict) -> dict:
    return {
        "collections": [
            {k: x[k] for k in ("run_id", "phase", "bytes_done", "bytes_total") if k in x}
            for x in state["collections"]
        ],
        "monitoring": public_monitor(state["monitoring"]),
    }
