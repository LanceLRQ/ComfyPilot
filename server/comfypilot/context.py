from comfypilot.project import Project
from comfypilot.resources import get_resources
from comfypilot.settings import get_project


def build_context(project: Project, state: dict, monitoring: dict) -> dict:
    configuration = get_project(project)
    resources = get_resources(project)
    return {
        "project": project.summary(),
        "revision": state["revision"],
        "event_cursor": state["event_cursor"],
        "tasks": state["tasks"],
        "active_plans": [plan for plan in state["plans"] if plan["status"] == "active"],
        "authorizations": state["authorizations"],
        "active_runs": [
            run
            for run in state["runs"]
            if run["status"] not in {"succeeded", "failed", "cancelled"}
        ],
        "recent_runs": state["runs"][-20:],
        "collections": state["collections"],
        "adopted_versions": state["adoptions"],
        "candidates": state["versions"],
        "unresolved_feedback": [item for item in state["feedback"] if item["status"] != "verified"],
        "control": {key: state[key] for key in ("dispatch", "paused_plan_ids", "review_holds")},
        "monitoring": monitoring,
        "configuration": {
            "revision": configuration["revision"],
            "default_target_id": configuration["config"]["default_target_id"],
            "targets": configuration["config"]["targets"],
            "warnings": configuration["warnings"],
        },
        "resources": {
            "revision": resources["revision"],
            **{
                kind: [
                    {key: record[key] for key in ("id", "hash", "states", "errors", "files")}
                    for record in resources[kind]
                ]
                for kind in ("modules", "recipes", "custom_nodes")
            },
        },
        "references": {
            "delivery": "/api/review/delivery",
            "log_path": str(project.root / ".comfypilot/logs/service.log"),
            "resource_tool": "get_resource_file(resource_hash)",
            "media": "/api/versions/{version_id}/content?variant=preview|original",
        },
        "limits": {
            "candidate_window": 100,
            "recent_run_window": 20,
            "events_page_max": 100,
            "wait_seconds_max": 25,
            "host_continuation": "事件返回不代表宿主自动继续推理；离线不主动唤醒Agent。",
        },
    }
