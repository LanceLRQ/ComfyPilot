import json
import sqlite3
import time
from urllib.request import Request, urlopen

from comfypilot.execution import get_execution
from comfypilot.generation import save_run
from comfypilot.lifecycle import start_service, stop_service
from comfypilot.project import read_project
from test_collection import completed
from test_execution import command
from test_generation import generation_workspace, media_files
from test_service import running_service

__all__ = ["generation_workspace", "media_files"]


def test_service_stops_with_an_open_event_stream(tmp_path):
    import socket

    from comfypilot.project import initialize_project

    project = initialize_project(tmp_path / "stream-stop")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    start_service(project, port)
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/events/stream", timeout=3) as response:
            assert response.readline().startswith(b"event: latest")
            try:
                result = stop_service(project)
            finally:
                response.close()
            assert result["status"] == "stopped"
    finally:
        stop_service(project)


def test_events_page_and_sse_replay_from_a_consistent_snapshot(tmp_path):
    with running_service(tmp_path / "events") as (address, _, _):
        project = read_project(tmp_path / "events")
        with urlopen(address + "/api/state", timeout=3) as response:
            snapshot = json.load(response)["data"]
        receipts = [
            command(project, "create_task", goal=str(i), acceptance="check") for i in range(3)
        ]
        with urlopen(
            address + f"/api/events?after={snapshot['event_cursor']}&limit=2", timeout=3
        ) as response:
            page = json.load(response)["data"]
        assert [item["event_id"] for item in page["events"]] == [
            item["event_id"] for item in receipts[:2]
        ]
        assert page["has_more"] and page["cursor"] == receipts[1]["event_id"]
        request = Request(
            address + "/api/events/stream?after=0", headers={"Last-Event-ID": str(page["cursor"])}
        )
        started = time.monotonic()
        with urlopen(request, timeout=4) as response:
            block = []
            while line := response.readline().decode().strip():
                block.append(line)
        assert f"id: {receipts[2]['event_id']}" in block
        assert time.monotonic() - started < 3
        with urlopen(address + "/api/state", timeout=3) as response:
            state = json.load(response)["data"]
        assert state["event_cursor"] == receipts[-1]["event_id"]
        assert state["revision"] == receipts[-1]["revision"] and len(state["tasks"]) == 3


def test_progress_overwrites_latest_without_consuming_business_events(generation_workspace):
    project, _, _, _ = generation_workspace
    run_id, _ = completed(generation_workspace)
    save_run(
        project, run_id, "collecting", {"collection": {"phase": "transferring", "bytes_done": 0}}
    )
    before = get_execution(project)
    for i in range(20):
        save_run(
            project,
            run_id,
            "collecting",
            {"collection": {"phase": "transferring", "bytes_done": i}},
            durable=False,
        )
    after = get_execution(project)
    assert (
        after["event_cursor"] == before["event_cursor"] and after["revision"] == before["revision"]
    )
    assert after["runs"][0]["document"]["collection"]["bytes_done"] == 19
    save_run(project, run_id, "collecting", {"collection": {"phase": "checking"}})
    assert get_execution(project)["event_cursor"] == before["event_cursor"] + 1


def test_event_projection_omits_graph_history_and_large_payload(generation_workspace):
    from comfypilot.events import read_events, state_snapshot

    project, _, _, _ = generation_workspace
    run_id, _ = completed(generation_workspace)
    save_run(
        project, run_id, "collecting", {"external_success": {"history": {"base64": "x" * 100000}}}
    )
    page = read_events(project, 0, 100)
    assert len(json.dumps(page)) < 30000 and "base64" not in json.dumps(page)
    snapshot = state_snapshot(project)
    assert "external_success" not in json.dumps(snapshot)
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_state_uses_one_read_transaction(generation_workspace, monkeypatch):
    import comfypilot.events as events

    project, _, _, _ = generation_workspace
    original = events.read_execution
    prior = get_execution(project)

    def interleave(db):
        state = original(db)
        command(project, "create_task", goal="concurrent", acceptance="check")
        return state

    monkeypatch.setattr(events, "read_execution", interleave)
    snapshot = events.state_snapshot(project)
    assert snapshot["revision"] == prior["revision"]
    assert snapshot["event_cursor"] == prior["event_cursor"]
    assert get_execution(project)["revision"] > snapshot["revision"]


def test_shared_monitoring_snapshot_and_latest_have_sources_without_catalog(tmp_path):
    with running_service(tmp_path / "monitor-view") as (address, _, _):
        with urlopen(address + "/api/state", timeout=3) as response:
            snapshot = json.load(response)["data"]
        assert snapshot["monitoring"]["worker"]["status"] == "running"
        disk = snapshot["monitoring"]["platform"]["disk_free"]
        assert disk["value"] > 0 and disk["source"] == "platform.project_filesystem"
        assert disk["observed_at"] and disk["expires_at"]
        assert snapshot["activity"] == []
        with urlopen(address + "/api/events/stream", timeout=3) as response:
            assert response.readline() == b"event: latest\n"
            payload = json.loads(response.readline().decode().removeprefix("data: "))["data"]
        assert payload["monitoring"]["platform"] == snapshot["monitoring"]["platform"]
        assert "node_schemas" not in json.dumps(payload)
        with urlopen(address + "/api/status", timeout=3) as response:
            monitoring = json.load(response)["data"]
        assert monitoring["platform"] == payload["monitoring"]["platform"]
