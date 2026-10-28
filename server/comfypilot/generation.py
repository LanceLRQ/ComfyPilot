import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID

import httpx2

from comfypilot.execution import get_execution, record_change, reserve_submission
from comfypilot.monitoring import TargetMonitor
from comfypilot.project import ExecutionTarget, Project, ProjectError
from comfypilot.replicas import materialize_run
from comfypilot.settings import get_project, target_headers


async def request_json(project: Project, target: dict, method: str, path: str, body=None):
    configured = ExecutionTarget.model_validate(target)
    headers = target_headers(project, configured)
    if configured.credential_ref and not headers:
        raise ProjectError("credential_missing", "原目标凭据不可用，请恢复对应项目凭据。")
    async with (
        httpx2.AsyncClient(
            headers=headers, timeout=20, follow_redirects=False, trust_env=False
        ) as client,
        asyncio.timeout(25),
    ):
        async with client.stream(method, configured.base_url + path, json=body) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 16 * 1024 * 1024:
                    raise ProjectError("response_invalid", "执行端响应超过核对上限。")
            document = json.loads(content)
            if not isinstance(document, dict):
                raise ProjectError("response_invalid", "执行端响应必须为JSON对象。")
            return response.status_code, document


def save_run(
    project: Project,
    run_id: str,
    status: str,
    fields: dict,
    *,
    expected_status: str | None = None,
    durable: bool = True,
) -> None:
    with closing(sqlite3.connect(project.database, timeout=5)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT status,document FROM execution_runs WHERE id=?", (run_id,)
        ).fetchone()
        if expected_status is not None and previous[0] != expected_status:
            return
        document = {**json.loads(previous[1]), **fields}
        if previous == (status, json.dumps(document)):
            return
        db.execute(
            "UPDATE execution_runs SET status=?,document=? WHERE id=?",
            (status, json.dumps(document), run_id),
        )
        db.execute(
            "UPDATE execution_control SET reconciliation_required=EXISTS("
            "SELECT 1 FROM execution_runs WHERE status IN ('dispatching','reconciling'))"
        )
        if durable:
            record_change(
                db, "run_state", {"run_id": run_id, "status": status, **fields}, "worker", run_id
            )


def queue_entries(document: dict) -> list[tuple[str, str, dict]]:
    result = []
    for key, status in (("queue_running", "running"), ("queue_pending", "submitted")):
        rows = document.get(key)
        if not isinstance(rows, list):
            raise ProjectError("response_invalid", "队列快照不完整，继续核对。")
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) < 4
                or not isinstance(row[1], str)
                or not isinstance(row[3], dict)
            ):
                raise ProjectError("response_invalid", "队列条目格式不正确。")
            result.append((row[1], status, row[3]))
    return result


async def reconcile(project: Project, run: dict) -> None:
    target = run["intent"]["target_snapshot"]["target"]
    document = run["document"]
    prompt_id = document.get("receipt", {}).get("prompt_id")
    marker = {
        "project_id": str(project.identity.project_id),
        "run_id": run["id"],
        "intent_id": run["intent"]["client_id"],
    }
    path = "/history/" + quote(prompt_id, safe="") if prompt_id else "/history?max_items=100"
    for attempt in range(2):
        code, history = await request_json(project, target, "GET", path)
        if code != 200:
            raise ProjectError("reconciliation_unavailable", "原目标history暂不可用。")
        for identifier, entry in history.items():
            if not isinstance(entry, dict):
                raise ProjectError("response_invalid", "history条目格式不正确。")
            if prompt_id:
                matches = identifier == prompt_id
            else:
                prompt = entry.get("prompt")
                matches = (
                    isinstance(prompt, list)
                    and len(prompt) > 3
                    and isinstance(prompt[3], dict)
                    and prompt[3].get("comfypilot") == marker
                )
            if not matches:
                continue
            status = entry.get("status", {})
            if not isinstance(status, dict):
                raise ProjectError("response_invalid", "history终态格式不正确。")
            receipt = {"prompt_id": identifier, "recovered": not bool(prompt_id)}
            if status.get("completed") is True and status.get("status_str") == "success":
                save_run(
                    project,
                    run["id"],
                    "collecting",
                    {
                        "receipt": receipt,
                        "external_success": {
                            "prompt_id": identifier,
                            "history": entry,
                            "observed_at": datetime.now(UTC).isoformat(),
                        },
                    },
                )
                return
            if status.get("status_str") == "error":
                save_run(
                    project, run["id"], "failed", {"receipt": receipt, "external_failure": entry}
                )
                return
        if attempt == 0:
            code, queue = await request_json(project, target, "GET", "/queue")
            if code != 200:
                raise ProjectError("reconciliation_unavailable", "原目标queue暂不可用。")
            matches = [
                (identifier, status)
                for identifier, status, extra in queue_entries(queue)
                if (identifier == prompt_id if prompt_id else extra.get("comfypilot") == marker)
            ]
            if len(matches) == 1:
                identifier, status = matches[0]
                save_run(
                    project,
                    run["id"],
                    status,
                    {"receipt": {"prompt_id": identifier, "recovered": not bool(prompt_id)}},
                )
                return
            if len(matches) > 1:
                break
    save_run(
        project,
        run["id"],
        "reconciling",
        {
            "reconciliation": {
                "code": "not_confirmed",
                "message": "队列与history均未确认原提交，不重发。",
            }
        },
    )


async def advance_generation(project: Project, instance: UUID, worker_pid: int) -> None:
    state = get_execution(project)
    active = next(
        (
            run
            for run in state["runs"]
            if run["status"] in {"dispatching", "submitted", "running", "reconciling"}
        ),
        None,
    )
    run = active
    reserved = False
    try:
        if active:
            await reconcile(project, active)
            return
        if state["dispatch"]["paused"]:
            return
        run = next((run for run in state["runs"] if run["status"] == "queued"), None)
        if run is None:
            return
        final = await materialize_run(project, run["id"])
        configured = ExecutionTarget.model_validate(final["target_snapshot"]["target"])
        await TargetMonitor(
            project, instance, worker_pid, get_project(project)["revision"], configured
        ).poll()
        result = reserve_submission(project, run["id"], instance, worker_pid, final["graph_hash"])
        reserved = True
        if result["reused"]:
            return
        intent = result["intent"]
        code, receipt = await request_json(
            project,
            intent["target_snapshot"]["target"],
            "POST",
            "/prompt",
            {
                "prompt": final["graph"],
                "client_id": intent["client_id"],
                "extra_data": {
                    "comfypilot": {
                        "project_id": str(project.identity.project_id),
                        "run_id": run["id"],
                        "intent_id": intent["client_id"],
                    }
                },
            },
        )
        if code == 400 and receipt.get("error") and "node_errors" in receipt:
            save_run(project, run["id"], "failed", {"rejection": receipt})
        elif code == 200 and isinstance(receipt.get("prompt_id"), str) and receipt["prompt_id"]:
            save_run(project, run["id"], "submitted", {"receipt": receipt})
        else:
            raise ProjectError("submission_unknown", "未取得可信提交回执，核对原提交。")
    except (ProjectError, OSError, httpx2.HTTPError, TimeoutError, ValueError) as error:
        if run is None:
            return
        status = "reconciling" if active or reserved else "queued"
        field = "reconciliation" if active or reserved else "dispatch_blocked"
        code = error.code if isinstance(error, ProjectError) else "target_unavailable"
        save_run(
            project,
            run["id"],
            status,
            {field: {"code": code}},
            expected_status=None if active or reserved else "queued",
        )


async def run_generation(project: Project, instance: UUID) -> None:
    while True:
        await advance_generation(project, instance, os.getpid())
        await asyncio.sleep(2)
