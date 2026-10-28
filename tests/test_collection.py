import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from comfypilot.execution import get_execution
from comfypilot.project import ProjectError
from comfypilot.settings import get_project
from test_execution import command
from test_generation import generation_workspace, media_files, tick

__all__ = ["generation_workspace", "media_files"]


def completed(workspace):
    project, _, state, _ = workspace
    tick(workspace)
    run = get_execution(project)["runs"][0]
    target = get_project(project)["config"]["targets"][0]
    relative = run["document"]["materialized"]["outputs"]["main"]["prefix"] + ".mp4"
    output = Path(target["assets"]["output_dir"]) / relative
    output.parent.mkdir(parents=True)
    source = (
        Path(target["assets"]["input_dir"]) / state["posts"][0]["prompt"]["1"]["inputs"]["video"]
    )
    output.write_bytes(source.read_bytes())
    state.update(
        running=[],
        history={
            "fixture-prompt": {
                "prompt": state["prompt"],
                "outputs": {
                    "1": {
                        "videos": [
                            {
                                "filename": output.name,
                                "subfolder": str(Path(relative).parent),
                                "type": "output",
                            }
                        ]
                    }
                },
                "status": {"completed": True, "status_str": "success"},
            }
        },
    )
    tick(workspace)
    return run["id"], output


def test_collection_decodes_checks_and_registers_once_while_paused(generation_workspace):
    from comfypilot.collection import collect_run, list_versions, version_path

    project, _, state, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    command(project, "pause_dispatch")
    asyncio.run(collect_run(project, run_id))
    result = list_versions(project)
    assert len(result["versions"]) == 1
    version = result["versions"][0]
    assert version["ready"] and version["checks"]["status"] == "passed"
    assert version["media"]["frame_count"] == 96
    assert version_path(project, version["id"], "original").read_bytes() == output.read_bytes()
    asyncio.run(collect_run(project, run_id))
    assert len(list_versions(project)["versions"]) == 1 and len(state["posts"]) == 1
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT COUNT(*) FROM output_checks").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM business_events WHERE action='clip_ready'").fetchone()[
                0
            ]
            == 1
        )


@pytest.mark.parametrize("defect", ["corrupt", "spec", "missing", "no_success"])
def test_unverified_media_never_becomes_reviewable(generation_workspace, defect):
    from comfypilot.collection import collect_run, list_versions

    project, _, _, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    if defect == "corrupt":
        output.write_bytes(b"broken video")
    elif defect == "missing":
        output.unlink()
    else:
        with sqlite3.connect(project.database) as db:
            document = json.loads(
                db.execute("SELECT document FROM execution_runs WHERE id=?", (run_id,)).fetchone()[
                    0
                ]
            )
            if defect == "no_success":
                document.pop("external_success")
            else:
                document["materialized"]["outputs"]["main"]["spec"]["width"] = 100
            db.execute(
                "UPDATE execution_runs SET document=? WHERE id=?", (json.dumps(document), run_id)
            )
    asyncio.run(collect_run(project, run_id))
    assert list_versions(project)["versions"] == []
    phase = get_execution(project)["runs"][0]["document"]["collection"]["phase"]
    assert phase in {"blocked", "failed", "retry_wait"}


def test_collection_requires_a_known_version_id(generation_workspace):
    from comfypilot.collection import version_path

    with pytest.raises(ProjectError):
        version_path(generation_workspace[0], "../../foreign", "original")


def test_a_collects_while_b_generates_and_identical_bytes_keep_two_origins(generation_workspace):
    from comfypilot.collection import collect_run, list_versions

    project, _, state, _ = generation_workspace
    first, output = completed(generation_workspace)
    result = tick(generation_workspace)
    assert result["runs"][0]["status"] == "collecting"
    assert result["runs"][1]["status"] == "submitted"
    asyncio.run(collect_run(project, first))
    second = result["runs"][1]
    relative = second["document"]["materialized"]["outputs"]["main"]["prefix"] + ".mp4"
    target = get_project(project)["config"]["targets"][0]
    other = Path(target["assets"]["output_dir"]) / relative
    other.parent.mkdir(parents=True)
    other.write_bytes(output.read_bytes())
    state.update(
        running=[],
        history={
            "fixture-prompt-2": {
                "prompt": state["prompt"],
                "outputs": {
                    "1": {
                        "videos": [
                            {
                                "filename": other.name,
                                "subfolder": str(Path(relative).parent),
                                "type": "output",
                            }
                        ]
                    }
                },
                "status": {"completed": True, "status_str": "success"},
            }
        },
    )
    tick(generation_workspace)
    asyncio.run(collect_run(project, second["id"]))
    versions = list_versions(project)["versions"]
    assert len(versions) == 2 and len({v["id"] for v in versions}) == 2
    assert len({v["content_hash"] for v in versions}) == 1
    assert {v["run_id"] for v in versions} == {first, second["id"]}


def test_incompatible_video_gets_separately_verified_h264_proxy(generation_workspace):
    import subprocess

    from comfypilot.collection import collect_run, list_versions, version_path

    project, _, _, _ = generation_workspace
    run_id, output = completed(generation_workspace)
    converted = output.with_name("converted.mkv")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-n", "-i", str(output), "-c:v", "ffv1", str(converted)],
        check=True,
        capture_output=True,
    )
    output.write_bytes(converted.read_bytes())
    asyncio.run(collect_run(project, run_id))
    version = list_versions(project)["versions"][0]
    assert version["media"]["codec"] == "ffv1"
    assert version["proxy"]["media"]["codec"] == "h264"
    assert version["content_hash"] != version["proxy"]["content_hash"]
    assert version_path(project, version["id"], "original") != version_path(
        project, version["id"], "preview"
    )


def test_real_worker_resumes_saved_success_and_serves_range_preview(generation_workspace):
    import socket
    import time
    from urllib.request import Request, urlopen

    from comfypilot.collection import list_versions
    from comfypilot.lifecycle import start_service, stop_service

    project, _, state, _ = generation_workspace
    completed(generation_workspace)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    try:
        start_service(project, port)
        for _ in range(100):
            versions = list_versions(project)["versions"]
            if versions:
                break
            time.sleep(0.1)
        assert len(versions) == 1
        request = Request(
            f"http://127.0.0.1:{port}/api/versions/{versions[0]['id']}/content",
            headers={"Range": "bytes=0-99"},
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 206 and len(response.read()) == 100
        assert len(state["posts"]) == 1
    finally:
        stop_service(project)
