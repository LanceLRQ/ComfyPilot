import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from pydantic import ValidationError
from websockets.exceptions import WebSocketException

from comfypilot.comfy import ComfyEvent, ConfiguredWebSocket, Queue, Stats, fetch_documents
from comfypilot.node_schema import normalize_inputs
from comfypilot.project import ExecutionTarget, Project, ProjectConfig, ProjectError
from comfypilot.settings import get_project, target_headers


def metric(
    value,
    source: str,
    unit: str | None = None,
    *,
    now: datetime | None = None,
    unsupported: bool = False,
) -> dict:
    observed = now or datetime.now(UTC)
    return {
        "value": value,
        "source": source,
        "unit": unit,
        "availability": "unsupported" if unsupported else "unknown" if value is None else "fresh",
        "observed_at": observed.isoformat() if value is not None else None,
        "expires_at": (observed + timedelta(seconds=15)).isoformat() if value is not None else None,
        "reason": "标准接口未提供或未配置采集能力。"
        if unsupported
        else "尚无有效采集值。"
        if value is None
        else None,
    }


def store_sample(
    project: Project,
    instance_id: UUID,
    worker_pid: int,
    revision: int,
    target_id: str,
    kind: str,
    data: dict,
) -> None:
    with closing(sqlite3.connect(project.database, timeout=1)) as db, db:
        db.execute(
            "INSERT INTO status_samples VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_id, kind) DO UPDATE SET config_revision=excluded.config_revision, "
            "instance_id=excluded.instance_id, worker_pid=excluded.worker_pid, "
            "document=excluded.document",
            (target_id, kind, revision, str(instance_id), worker_pid, json.dumps(data)),
        )


def get_target_status(
    project: Project,
    instance_id: UUID | str,
    worker_pid: int,
    target_id: str,
    *,
    now: datetime | None = None,
    current: dict | None = None,
) -> dict:
    current = current if current is not None else get_project(project)
    target = next((item for item in current["config"]["targets"] if item["id"] == target_id), None)
    if target is None:
        raise ProjectError("target_unavailable", "执行端未配置，请明确选择项目目标。")
    base = target["base_url"]
    result = {
        "target_id": target_id,
        "config_revision": current["revision"],
        "instance_id": str(instance_id),
        "enabled": target["enabled"],
        "connection": metric(None, base),
        "websocket": metric(None, base + "/ws"),
        "metrics": {
            name: metric(None, base, unit)
            for name, unit in [
                ("os", None),
                ("comfyui_version", None),
                ("ram_total", "bytes"),
                ("ram_free", "bytes"),
                ("queue_running", "count"),
                ("queue_pending", "count"),
            ]
        },
        "devices": None,
        "node_progress": metric(None, base + "/ws", "steps"),
        "stage": metric(None, base + "/ws"),
        "capabilities": {
            "execute": {"status": "unknown", "reason": "尚未验证标准 API 与节点清单。"},
            **{
                name: {"status": "unsupported", "reason": "未配置并验证对应适配器或管理通道。"}
                for name in (
                    "media_transfer",
                    "managed_listing",
                    "managed_delete",
                    "environment_management",
                )
            },
        },
    }
    for name, unit in (
        ("gpu_utilization", "%"),
        ("gpu_temperature", "celsius"),
        ("cpu_utilization", "%"),
        ("disk_free", "bytes"),
        ("full_logs", None),
    ):
        result["metrics"][name] = metric(None, "comfyui.standard_api", unit, unsupported=True)
    with closing(sqlite3.connect(project.database.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT kind, document FROM status_samples WHERE target_id=? AND config_revision=? "
            "AND instance_id=? AND worker_pid=?",
            (target_id, current["revision"], str(instance_id), worker_pid),
        ).fetchall()
    for kind, document in rows:
        data = json.loads(document)
        if kind == "http":
            result["metrics"].update(data["metrics"])
            result["devices"] = data["devices"]
        elif kind == "catalog":
            result["capabilities"]["execute"] = data["execute"]
            result["node_count"] = len(data["node_classes"])
            result["node_classes"] = data["node_classes"]
            result["node_catalog_hash"] = data["hash"]
            result["node_schemas"] = data.get("node_schemas", {})
            result["node_catalog_expires_at"] = data.get("expires_at")
        else:
            result.update(data)
    clock = now or datetime.now(UTC)

    def expire(value):
        if isinstance(value, dict):
            if value.get("availability") == "fresh" and clock >= datetime.fromisoformat(
                value["expires_at"]
            ):
                value["availability"] = "stale"
            for child in value.values():
                expire(child)
        elif isinstance(value, list):
            for child in value:
                expire(child)

    expire(result)
    stage = result["stage"]
    result["stage_elapsed_seconds"] = (
        max(0, (clock - datetime.fromisoformat(stage["observed_at"])).total_seconds())
        if stage["value"] and stage["value"]["phase"] == "executing"
        else None
    )
    return result


class TargetMonitor:
    def __init__(
        self,
        project: Project,
        instance_id: UUID,
        worker_pid: int,
        revision: int,
        target: ExecutionTarget,
    ) -> None:
        self.project, self.instance_id, self.worker_pid = project, instance_id, worker_pid
        self.revision, self.target = revision, target
        self.catalog_fetched = False
        self.failures = 0
        self.client_id = str(uuid4())

    @property
    def retry_delay(self) -> int:
        return min(60, 5 * 2 ** min(max(self.failures - 1, 0), 4))

    def store(self, kind: str, data: dict) -> None:
        store_sample(
            self.project,
            self.instance_id,
            self.worker_pid,
            self.revision,
            self.target.id,
            kind,
            data,
        )

    async def poll(self) -> None:
        try:
            headers = target_headers(self.project, self.target)
            if self.target.credential_ref and not headers:
                raise ProjectError("credential_missing", "请配置执行端所引用的项目凭据。")
            documents = await fetch_documents(self.target, headers, not self.catalog_fetched)
        except (ProjectError, OSError):
            documents = {}
        stats, queue = documents.get("stats"), documents.get("queue")
        healthy = isinstance(stats, Stats) and isinstance(queue, Queue)
        self.store("connection", {"connection": metric(healthy, self.target.base_url)})
        if not healthy:
            self.failures += 1
            self.catalog_fetched = False
            return
        self.failures = 0
        source = self.target.base_url
        values = {
            name: metric(getattr(stats.system, name), source + "/system_stats", unit)
            for name, unit in [
                ("os", None),
                ("comfyui_version", None),
                ("ram_total", "bytes"),
                ("ram_free", "bytes"),
            ]
        }
        for name in ("queue_running", "queue_pending"):
            entries = getattr(queue, name)
            values[name] = metric(
                len(entries) if entries is not None else None, source + "/queue", "count"
            )
        devices = (
            None
            if stats.devices is None
            else [
                {
                    "name": metric(device.name, source + "/system_stats"),
                    "vram_total": metric(device.vram_total, source + "/system_stats", "bytes"),
                    "vram_free": metric(device.vram_free, source + "/system_stats", "bytes"),
                }
                for device in stats.devices
            ]
        )
        self.store("http", {"metrics": values, "devices": devices})
        if "nodes" in documents:
            nodes = documents["nodes"]
            names = sorted(nodes) if isinstance(nodes, dict) else []
            schemas = {}
            if isinstance(nodes, dict):
                for name, node in nodes.items():
                    schemas[name] = {
                        "inputs": normalize_inputs(node.input.required, node.input.optional),
                        "outputs": node.output,
                        "output_node": node.output_node,
                    }
            self.catalog_fetched = True
            self.store(
                "catalog",
                {
                    "node_classes": names,
                    "hash": sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest(),
                    "node_schemas": schemas,
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=15)).isoformat(),
                    "execute": {
                        "status": "available" if names else "unknown",
                        "source": source + "/object_info",
                        "observed_at": datetime.now(UTC).isoformat(),
                        "reason": "标准 API 与节点清单可用；未完成实际生成验收。"
                        if names
                        else "无法取得节点清单，请显式重新探测。",
                    },
                },
            )

    async def events(self) -> None:
        source = self.target.base_url + "/ws"
        uri = source.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        state = {"stage": metric(None, source), "node_progress": metric(None, source, "steps")}
        failures = 0
        while True:
            try:
                headers = target_headers(self.project, self.target)
                if self.target.credential_ref and not headers:
                    raise ProjectError("credential_missing", "请配置项目执行端凭据。")
                async with ConfiguredWebSocket(
                    uri + "?clientId=" + self.client_id,
                    additional_headers=headers,
                    proxy=None,
                    open_timeout=2,
                    close_timeout=1,
                    max_size=4 * 1024 * 1024,
                ) as connection:
                    failures = 0
                    state["websocket"] = metric(True, source)
                    self.store("events", state)
                    while True:
                        try:
                            content = await asyncio.wait_for(connection.recv(), timeout=5)
                        except TimeoutError:
                            state["websocket"] = metric(True, source)
                            self.store("events", state)
                            continue
                        if not isinstance(content, str):
                            continue
                        try:
                            event = ComfyEvent.model_validate_json(content)
                        except ValidationError:
                            continue
                        if event.type == "executing":
                            state["stage"] = metric(
                                {
                                    "phase": "executing"
                                    if event.data.node is not None
                                    else "execution_finished",
                                    "node": event.data.node,
                                    "prompt_id": event.data.prompt_id,
                                },
                                source,
                            )
                            state["node_progress"] = metric(None, source, "steps")
                        elif (
                            event.type == "progress"
                            and event.data.value is not None
                            and event.data.max is not None
                        ):
                            state["node_progress"] = metric(
                                event.data.model_dump(), source, "steps"
                            )
                        elif event.type == "execution_error":
                            state["stage"] = metric(
                                {
                                    "phase": "execution_error",
                                    "node": event.data.node,
                                    "prompt_id": event.data.prompt_id,
                                },
                                source,
                            )
                        else:
                            continue
                        state["websocket"] = metric(True, source)
                        self.store("events", state)
            except (OSError, WebSocketException, TimeoutError, ProjectError):
                state["websocket"] = metric(False, source)
                self.store("events", state)
            failures += 1
            await asyncio.sleep(min(60, 5 * 2 ** min(failures - 1, 4)))

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self.events())
            while True:
                await self.poll()
                await asyncio.sleep(self.retry_delay)


async def monitor_targets(project: Project, instance_id: UUID) -> None:
    tasks: list[asyncio.Task] = []
    revision = 0
    async with asyncio.TaskGroup() as group:
        while True:
            current = get_project(project)
            if current["revision"] != revision:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                revision = current["revision"]
                config = ProjectConfig.model_validate(current["config"])
                tasks = [
                    group.create_task(
                        TargetMonitor(project, instance_id, os.getpid(), revision, target).run()
                    )
                    for target in config.targets
                    if target.enabled
                ]
            await asyncio.sleep(5)
