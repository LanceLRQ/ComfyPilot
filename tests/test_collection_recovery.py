import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from comfypilot.collection import collect_run, list_versions
from comfypilot.execution import COMMAND_SCHEMA, apply_command, get_execution
from comfypilot.project import ProjectError
from test_collection import completed
from test_execution import command
from test_generation import generation_workspace, media_files

__all__ = ["generation_workspace", "media_files"]


def test_transient_missing_file_has_exactly_three_retries_and_idempotent_recovery(
    generation_workspace,
):
    project, _, state, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    original = output.read_bytes()
    output.unlink()
    now = datetime.now(UTC)
    for attempt in range(4):
        asyncio.run(collect_run(project, run_id, now=now + timedelta(minutes=attempt)))
        checkpoint = get_execution(project)["runs"][0]["document"]["collection"]
        assert checkpoint["attempts"] == attempt + 1
        assert checkpoint["phase"] == ("retry_wait" if attempt < 3 else "blocked")
    asyncio.run(collect_run(project, run_id, now=now + timedelta(hours=1)))
    assert get_execution(project)["runs"][0]["document"]["collection"]["attempts"] == 4
    output.write_bytes(original)
    body = COMMAND_SCHEMA.validate_python(
        {
            "action": "resume_collection",
            "run_id": run_id,
            "command_id": str(uuid4()),
            "expected_revision": get_execution(project)["revision"],
        }
    )
    first = apply_command(project, body)
    assert apply_command(project, body) == first
    command(project, "pause_dispatch")
    asyncio.run(collect_run(project, run_id))
    assert len(list_versions(project)["versions"]) == 1
    assert len(state["posts"]) == 1
    assert get_execution(project)["plans"][0]["used_submissions"] == 1


def test_retry_wait_is_persistent_and_not_retried_before_deadline(generation_workspace):
    project, _, _, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    output.unlink()
    now = datetime.now(UTC)
    asyncio.run(collect_run(project, run_id, now=now))
    before = get_execution(project)
    asyncio.run(collect_run(project, run_id, now=now))
    assert get_execution(project) == before


def test_technical_failure_cannot_be_overridden_by_resume(generation_workspace):
    project, _, _, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    output.write_bytes(b"not a video")
    asyncio.run(collect_run(project, run_id))
    assert get_execution(project)["runs"][0]["status"] == "failed"
    with pytest.raises(ProjectError) as error:
        command(project, "resume_collection", run_id=run_id)
    assert error.value.code == "collection_not_resumable"


def test_output_fingerprint_is_retained_across_retry_and_changed_bytes_are_blocked(
    generation_workspace,
):
    project, _, _, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    from comfypilot.replicas import read_output

    run = get_execution(project)["runs"][0]
    target = run["document"]["materialized"]["target_snapshot"]["target"]
    declaration = run["document"]["external_success"]["history"]["outputs"]["1"]["videos"][0]
    replica = asyncio.run(read_output(project, target, run_id, "main", declaration))
    output.write_bytes(b"changed")
    asyncio.run(collect_run(project, run_id))
    checkpoint = get_execution(project)["runs"][0]["document"]["collection"]
    assert checkpoint["phase"] == "blocked" and checkpoint["error"]["code"] == "replica_changed"
    with sqlite3.connect(project.database) as db:
        persisted = json.loads(
            db.execute(
                "SELECT document FROM asset_replicas WHERE id=?", (replica["id"],)
            ).fetchone()[0]
        )
    assert persisted["fingerprint"] == replica["fingerprint"]


@pytest.mark.parametrize("defect", ["tools", "space"])
def test_local_prerequisite_failure_blocks_without_retry(generation_workspace, monkeypatch, defect):
    project, _, _, _ = generation_workspace
    run_id, _ = completed(generation_workspace)
    if defect == "tools":
        monkeypatch.setenv("PATH", "")
    else:
        from collections import namedtuple

        usage = namedtuple("Usage", "total used free")
        monkeypatch.setattr("comfypilot.media.shutil.disk_usage", lambda _: usage(10, 10, 0))
    asyncio.run(collect_run(project, run_id))
    checkpoint = get_execution(project)["runs"][0]["document"]["collection"]
    assert checkpoint["phase"] == "blocked" and checkpoint["next_retry_at"] is None
    assert checkpoint["error"]["code"] == (
        "media_tools_missing" if defect == "tools" else "storage_insufficient"
    )


def test_image_collection_really_decodes_image_bytes(generation_workspace, media_files):
    project, _, _, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    output.write_bytes(media_files[1].read_bytes())
    with sqlite3.connect(project.database) as db:
        document = json.loads(
            db.execute("SELECT document FROM execution_runs WHERE id=?", (run_id,)).fetchone()[0]
        )
        document["materialized"]["outputs"]["main"].update(
            kind="image", spec={"width": 32, "height": 32}
        )
        db.execute(
            "UPDATE execution_runs SET document=? WHERE id=?", (json.dumps(document), run_id)
        )
    asyncio.run(collect_run(project, run_id))
    version = list_versions(project)["versions"][0]
    assert version["kind"] == "image" and version["media"]["codec"] == "png"
    assert version["media"]["decoded"] and version["ready"]


def test_recorded_partial_file_is_discarded_and_original_run_resumes(generation_workspace):
    from comfypilot.media import media_directory

    project, _, state, _ = generation_workspace
    run_id, _ = completed(generation_workspace)
    partial = media_directory(project, "staging") / (uuid4().hex + ".part")
    partial.write_bytes(b"incomplete")
    with sqlite3.connect(project.database) as db:
        document = json.loads(
            db.execute("SELECT document FROM execution_runs WHERE id=?", (run_id,)).fetchone()[0]
        )
        document["collection"] = {
            "phase": "transferring",
            "staging_token": partial.name,
            "bytes_done": 10,
            "attempts": 1,
        }
        db.execute(
            "UPDATE execution_runs SET document=? WHERE id=?", (json.dumps(document), run_id)
        )
    asyncio.run(collect_run(project, run_id))
    assert not partial.exists() and len(list_versions(project)["versions"]) == 1
    assert len(state["posts"]) == 1 and get_execution(project)["plans"][0]["used_submissions"] == 1


def test_real_stdio_resumes_blocked_collection_and_replays_receipt(generation_workspace):
    import socket
    import sys

    from comfypilot.lifecycle import start_service, status_service, stop_service
    from mcp import Client, StdioServerParameters

    project, _, state, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    original = output.read_bytes()
    output.unlink()
    for attempt in range(4):
        asyncio.run(
            collect_run(project, run_id, now=datetime.now(UTC) + timedelta(minutes=attempt))
        )
    output.write_bytes(original)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    try:
        start_service(project, port)

        async def scenario():
            async with Client(
                StdioServerParameters(command=sys.executable, args=["-m", "comfypilot.mcp_bridge"])
            ) as client:

                async def tool(name, **fields):
                    response = await client.call_tool(
                        name, {"project_root": str(project.root), **fields}
                    )
                    assert not response.is_error, response
                    return response.structured_content["data"]

                connection = await tool("connect_service")
                ref = {"connection_ref": connection["connection_ref"]}
                current = await tool("get_execution", **ref)
                body = {
                    "run_id": run_id,
                    "command_id": str(uuid4()),
                    "expected_revision": current["revision"],
                    **ref,
                }
                receipt = await tool("resume_collection", **body)
                assert await tool("resume_collection", **body) == receipt
                for _ in range(100):
                    if (await tool("list_versions", **ref))["versions"]:
                        break
                    await asyncio.sleep(0.1)
                assert len(list_versions(project)["versions"]) == 1

        asyncio.run(scenario())
        assert status_service(project)["worker_ready"] and len(state["posts"]) == 1
    finally:
        stop_service(project)
