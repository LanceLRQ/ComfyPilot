import asyncio
import json
import os
import socket
import sqlite3
import time
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from comfy_fixture import comfy_server
from comfypilot.project import ExecutionTarget, initialize_project
from comfypilot.settings import get_project, update_project


def configured_project(tmp_path, address):
    project = initialize_project(tmp_path)
    config = get_project(project)["config"]
    update_project(
        project,
        1,
        {
            **config,
            "targets": [
                {"id": "render", "name": "测试目标", "location": "remote", "base_url": address}
            ],
            "default_target_id": "render",
        },
    )
    return project


def test_sampling_keeps_zero_values_but_missing_optional_metrics_are_unknown(tmp_path):
    from comfypilot.monitoring import TargetMonitor, get_target_status

    with comfy_server() as (address, calls, _):
        project = configured_project(tmp_path, address)
        instance = uuid4()
        monitor = TargetMonitor(
            project,
            instance,
            os.getpid(),
            2,
            ExecutionTarget.model_validate(get_project(project)["config"]["targets"][0]),
        )
        asyncio.run(monitor.poll())
        snapshot = get_target_status(project, instance, os.getpid(), "render")
        assert snapshot["metrics"]["ram_free"]["value"] == 6000
        assert snapshot["devices"][0]["vram_free"]["value"] == 0
        assert snapshot["metrics"]["gpu_utilization"]["availability"] == "unsupported"
        assert snapshot["metrics"]["queue_running"]["value"] == 1
        assert snapshot["capabilities"]["execute"]["status"] == "available"
        for capability in (
            "media_transfer",
            "managed_listing",
            "managed_delete",
            "environment_management",
        ):
            assert snapshot["capabilities"][capability]["status"] == "unsupported"
        assert "do-not-export" not in json.dumps(snapshot)
        assert snapshot["node_progress"]["availability"] == "unknown"
        asyncio.run(monitor.poll())
        assert calls.count(("GET", "/object_info")) == 1
        assert all(method == "GET" for method, _ in calls)
        assert get_project(project)["revision"] == 2
        with sqlite3.connect(project.database) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(status_samples)")}
            assert columns == {
                "target_id",
                "kind",
                "config_revision",
                "instance_id",
                "worker_pid",
                "document",
            }


def test_partial_responses_do_not_turn_missing_ram_or_devices_into_zero(tmp_path):
    from comfypilot.monitoring import TargetMonitor, get_target_status

    with comfy_server(partial=True) as (address, _, _):
        project = configured_project(tmp_path, address)
        instance = uuid4()
        monitor = TargetMonitor(
            project,
            instance,
            os.getpid(),
            2,
            ExecutionTarget.model_validate(get_project(project)["config"]["targets"][0]),
        )
        asyncio.run(monitor.poll())
        snapshot = get_target_status(project, instance, os.getpid(), "render")
        assert snapshot["metrics"]["ram_free"]["value"] is None
        assert snapshot["metrics"]["ram_free"]["availability"] == "unknown"
        assert snapshot["devices"] is None


def test_samples_expire_and_other_instance_or_worker_never_reuses_old_values(tmp_path):
    from comfypilot.monitoring import TargetMonitor, get_target_status

    with comfy_server() as (address, _, _):
        project = configured_project(tmp_path, address)
        instance = uuid4()
        monitor = TargetMonitor(
            project,
            instance,
            os.getpid(),
            2,
            ExecutionTarget.model_validate(get_project(project)["config"]["targets"][0]),
        )
        asyncio.run(monitor.poll())
        old = get_target_status(project, instance, os.getpid(), "render")
        observed = datetime.fromisoformat(old["metrics"]["ram_free"]["observed_at"])
        future = get_target_status(
            project, instance, os.getpid(), "render", now=observed + timedelta(seconds=15)
        )
        assert future["metrics"]["ram_free"]["availability"] == "stale"
        assert (
            get_target_status(project, uuid4(), os.getpid(), "render")["metrics"]["ram_free"][
                "availability"
            ]
            == "unknown"
        )
        assert (
            get_target_status(project, instance, os.getpid() + 100000, "render")["metrics"][
                "ram_free"
            ]["availability"]
            == "unknown"
        )


def test_slow_target_has_bounded_timeout_and_backoff(tmp_path):
    from comfypilot.monitoring import TargetMonitor, get_target_status

    with comfy_server(delay=3) as (address, _, _):
        project = configured_project(tmp_path, address)
        instance = uuid4()
        monitor = TargetMonitor(
            project,
            instance,
            os.getpid(),
            2,
            ExecutionTarget.model_validate(get_project(project)["config"]["targets"][0]),
        )
        started = time.monotonic()
        asyncio.run(monitor.poll())
        assert time.monotonic() - started < 2.8
        assert monitor.retry_delay >= 5
        snapshot = get_target_status(project, instance, os.getpid(), "render")
        assert snapshot["connection"]["value"] is False
        assert snapshot["metrics"]["ram_free"]["availability"] == "unknown"


@pytest.mark.parametrize("disconnect", [False, True])
def test_missing_steps_and_disconnect_keep_observed_stage_without_inventing_progress(
    tmp_path, disconnect
):
    from comfypilot.monitoring import TargetMonitor, get_target_status

    with comfy_server(progress=False, disconnect=disconnect) as (address, calls, _):
        project = configured_project(tmp_path, address)
        instance = uuid4()
        monitor = TargetMonitor(
            project,
            instance,
            os.getpid(),
            2,
            ExecutionTarget.model_validate(get_project(project)["config"]["targets"][0]),
        )

        async def observe():
            async with asyncio.TaskGroup() as group:
                events = group.create_task(monitor.events())
                async with asyncio.timeout(3):
                    while True:
                        snapshot = get_target_status(project, instance, os.getpid(), "render")
                        if snapshot["stage"]["value"] and (
                            not disconnect or snapshot["websocket"]["value"] is False
                        ):
                            break
                        await asyncio.sleep(0.02)
                events.cancel()
            return snapshot

        snapshot = asyncio.run(observe())
        assert snapshot["stage"]["value"]["phase"] == "executing"
        assert snapshot["stage_elapsed_seconds"] >= 0
        assert snapshot["node_progress"]["availability"] == "unknown"
        assert calls == []


def test_target_secret_is_only_sent_to_configured_endpoint_and_missing_secret_does_not_connect(
    tmp_path,
):
    from comfypilot.monitoring import TargetMonitor, get_target_status
    from comfypilot.settings import update_target

    with comfy_server() as (address, calls, tokens):
        project = configured_project(tmp_path, address)
        target = {**get_project(project)["config"]["targets"][0], "credential_ref": "render-auth"}
        update_target(project, 2, target)
        instance = uuid4()
        monitor = TargetMonitor(
            project, instance, os.getpid(), 3, ExecutionTarget.model_validate(target)
        )
        asyncio.run(monitor.poll())
        assert not calls
        (tmp_path / ".comfypilot/secrets.local.yaml").write_text(
            "render-auth:\n  Authorization: Bearer fixture-secret\n"
        )
        asyncio.run(monitor.poll())
        assert tokens and all(token == "Bearer fixture-secret" for token in tokens)
        assert "fixture-secret" not in json.dumps(
            get_target_status(project, instance, os.getpid(), "render")
        )


@pytest.mark.parametrize("events", [True, False])
def test_real_worker_collects_websocket_steps_without_submitting_any_prompt(tmp_path, events):
    from comfypilot.lifecycle import start_service, status_service, stop_service
    from comfypilot.monitoring import get_target_status

    with comfy_server(events=events) as (address, calls, _):
        project = configured_project(tmp_path, address)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        runtime = start_service(project, port)["runtime"]
        try:
            from comfypilot.mcp_bridge import (
                ConnectionArguments,
                ProjectArguments,
                ProjectBridge,
                TargetStatusArguments,
            )

            bridge = ProjectBridge()
            connected = bridge.execute(
                "connect_service", ProjectArguments(project_root=str(tmp_path))
            )
            arguments = TargetStatusArguments(
                project_root=str(tmp_path),
                connection_ref=connected["connection_ref"],
                target_id="render",
            )
            probe = bridge.execute("probe_target", arguments)
            assert probe["capabilities"]["execute"]["status"] == "available"
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                snapshot = get_target_status(
                    project, runtime["instance_id"], runtime["worker"]["pid"], "render"
                )
                if snapshot["connection"]["value"] is True and (
                    not events or snapshot["node_progress"]["availability"] == "fresh"
                ):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Worker 未采集到替身执行端状态")
            if events:
                assert snapshot["node_progress"]["value"]["value"] == 2
                assert snapshot["node_progress"]["value"]["max"] == 8
                assert snapshot["node_progress"]["source"].endswith("/ws")
            else:
                assert snapshot["node_progress"]["availability"] == "unknown"
            assert status_service(project)["worker_ready"] is True
            shared = bridge.execute("get_target_status", arguments)
            assert shared["config_revision"] == snapshot["config_revision"]
            assert shared["metrics"]["ram_free"]["value"] == 6000
            overview = bridge.execute(
                "get_monitor_status",
                ConnectionArguments(
                    project_root=str(tmp_path), connection_ref=connected["connection_ref"]
                ),
            )
            assert overview["targets"][0]["target_id"] == "render"
            assert all(method == "GET" for method, _ in calls)
        finally:
            stop_service(project)
