import asyncio
import importlib.util
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from media_fixture import media_files

__all__ = ["media_files"]


def routes(input_dir, output_dir):
    file = Path(__file__).parents[1] / "extensions/comfypilot_assets/bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_extension", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_routes(input_dir, output_dir)


def test_extension_registers_routes_through_comfy_entrypoint(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    registry = web.RouteTableDef()
    monkeypatch.setitem(
        sys.modules,
        "folder_paths",
        SimpleNamespace(
            get_input_directory=lambda: str(tmp_path / "input"),
            get_output_directory=lambda: str(tmp_path / "output"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "server",
        SimpleNamespace(PromptServer=SimpleNamespace(instance=SimpleNamespace(routes=registry))),
    )
    file = Path(__file__).parents[1] / "extensions/comfypilot_assets/__init__.py"
    spec = importlib.util.spec_from_file_location(
        "bridge_entrypoint", file, submodule_search_locations=[str(file.parent)]
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "bridge_entrypoint", module)
    spec.loader.exec_module(module)
    assert len(registry) == 5
    app = web.Application()
    app.add_routes(registry)


def test_bridge_streaming_identity_listing_and_no_overwrite(tmp_path):
    async def scenario():
        for role in ("input", "output"):
            (tmp_path / role).mkdir()
        app = web.Application()
        app.add_routes(routes(tmp_path / "input", tmp_path / "output"))
        project, asset = str(uuid4()), str(uuid4())
        body = b"stream chunk" * 800000
        digest = sha256(body).hexdigest()
        async with TestServer(app) as server, httpx2.AsyncClient() as client:
            base = str(server.make_url("/comfypilot/v1"))
            assert (await client.get(base + "/capabilities")).json()["protocol"] == 1
            url = base + f"/projects/{project}/input/{asset}.mp4"

            async def chunks():
                for i in range(0, len(body), 65536):
                    yield body[i : i + 65536]

            response = await client.put(url, content=chunks(), headers={"X-Content-SHA256": digest})
            assert response.status_code == 200, response.text
            record = response.json()
            assert record["content_hash"] == digest and record["size"] == len(body)
            assert (tmp_path / "input" / record["relative_path"]).read_bytes() == body
            again = await client.put(url, content=body, headers={"X-Content-SHA256": digest})
            assert again.status_code == 200
            (tmp_path / "input/ComfyPilot" / project / "unknown.mp4").write_bytes(b"unknown")
            known = (await client.get(base + f"/projects/{project}/known")).json()
            assert len(known["replicas"]) == 1
            conflict = await client.put(
                url,
                content=b"different",
                headers={"X-Content-SHA256": sha256(b"different").hexdigest()},
            )
            assert conflict.status_code == 409
            assert not list(tmp_path.rglob("*.part"))

    asyncio.run(scenario())


def test_bridge_rejects_path_escape_symlink_and_changed_output(tmp_path):
    async def scenario():
        for role in ("input", "output"):
            (tmp_path / role).mkdir()
        project, run = str(uuid4()), str(uuid4())
        relative = f"ComfyPilot/{project}/{run}/main.mp4"
        output = tmp_path / "output" / relative
        output.parent.mkdir(parents=True)
        output.write_bytes(b"original")
        app = web.Application()
        app.add_routes(routes(tmp_path / "input", tmp_path / "output"))
        async with TestServer(app) as server, httpx2.AsyncClient() as client:
            base = str(server.make_url(f"/comfypilot/v1/projects/{project}"))
            proof = (
                await client.get(base + "/stat", params={"role": "output", "path": relative})
            ).json()
            response = await client.get(
                base + "/download",
                params={"path": relative},
                headers={"X-ComfyPilot-Fingerprint": proof["fingerprint_id"]},
            )
            assert response.content == b"original"
            output.write_bytes(b"changed")
            assert (
                await client.get(
                    base + "/download",
                    params={"path": relative},
                    headers={"X-ComfyPilot-Fingerprint": proof["fingerprint_id"]},
                )
            ).status_code == 412
            for path in (
                "/etc/passwd",
                f"ComfyPilot/{project}/../foreign.mp4",
                f"ComfyPilot/{uuid4()}/{run}/main.mp4",
            ):
                assert (
                    await client.get(base + "/stat", params={"role": "output", "path": path})
                ).status_code == 400
            link = output.with_name("link.mp4")
            link.symlink_to(output)
            assert (
                await client.get(
                    base + "/stat",
                    params={"role": "output", "path": str(Path(relative).with_name("link.mp4"))},
                )
            ).status_code == 400
            assert (await client.delete(base + "/download")).status_code == 405

    asyncio.run(scenario())


def test_platform_bridge_adapter_transfers_real_media_and_probes_capability(tmp_path, media_files):
    import os

    from comfypilot.monitoring import TargetMonitor, get_target_status
    from comfypilot.project import ExecutionTarget, ProjectError, initialize_project
    from comfypilot.replicas import list_replicas, put_input, read_output, transfer_output
    from comfypilot.settings import get_project, resolve_target, update_project
    from test_media import imported

    project = initialize_project(tmp_path / "project")
    asset = imported(project, media_files[0], "video")
    for role in ("input", "output"):
        (tmp_path / role).mkdir()

    async def scenario():
        app = web.Application()
        app.add_routes(routes(tmp_path / "input", tmp_path / "output"))
        async with TestServer(app) as server:
            config = get_project(project)
            update_project(
                project,
                config["revision"],
                {
                    **config["config"],
                    "targets": [
                        {
                            "id": "remote",
                            "name": "桥接测试",
                            "base_url": str(server.make_url("/")),
                            "assets": {"mode": "bridge"},
                        }
                    ],
                },
            )
            target = resolve_target(project, "remote")["target"]
            run = str(uuid4())
            replica = await put_input(project, target, asset, run)
            assert (await put_input(project, target, asset, "second"))["id"] == replica["id"]
            assert len(list_replicas(project)["references"]) == 2
            relative = f"ComfyPilot/{project.identity.project_id}/{run}/main.mp4"
            output = tmp_path / "output" / relative
            output.parent.mkdir(parents=True)
            output.write_bytes(media_files[0].read_bytes())
            declared = {
                "filename": output.name,
                "subfolder": str(Path(relative).parent),
                "type": "output",
            }
            downloaded = await read_output(project, target, run, "main", declared)
            destination = tmp_path / "received.mp4"
            progress = []
            proof = await transfer_output(project, target, downloaded, destination, progress.append)
            assert proof == downloaded["fingerprint"]
            assert destination.read_bytes() == output.read_bytes()
            assert progress[-1] == output.stat().st_size
            instance = uuid4()
            await TargetMonitor(
                project,
                instance,
                os.getpid(),
                get_project(project)["revision"],
                ExecutionTarget.model_validate(target),
            ).poll()
            status = get_target_status(project, instance, os.getpid(), "remote")
            assert status["capabilities"]["media_transfer"]["status"] == "available"
            assert status["capabilities"]["managed_delete"]["status"] == "unsupported"
            output.write_bytes(b"changed")
            with pytest.raises(ProjectError) as error:
                await transfer_output(
                    project, target, downloaded, tmp_path / "changed.part", progress.append
                )
            assert error.value.code == "replica_changed"

    asyncio.run(scenario())


def test_missing_bridge_is_explicitly_unsupported_without_upload_fallback(tmp_path):
    from comfy_fixture import comfy_server
    from comfypilot.project import initialize_project
    from comfypilot.replicas import probe_asset_capabilities

    project = initialize_project(tmp_path / "project")
    with comfy_server(busy=False) as (address, calls, _):
        target = {
            "id": "remote",
            "name": "无桥接",
            "base_url": address,
            "assets": {"mode": "bridge"},
        }
        capabilities = asyncio.run(probe_asset_capabilities(project, target))
        assert capabilities["media_transfer"]["status"] == "unsupported"
        assert all(method == "GET" for method, _ in calls)
