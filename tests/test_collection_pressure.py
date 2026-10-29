"""Opt-in real media/HTTP/Worker pressure test; generation history is a protocol fixture."""

import asyncio
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from comfypilot.collection import list_versions, version_path
from comfypilot.execution import get_execution
from comfypilot.lifecycle import start_service, stop_service
from comfypilot.media import get_asset
from comfypilot.project import read_project
from comfypilot.settings import get_project, update_project
from test_bridge import routes
from test_execution import command, plan_body
from test_generation import generation_workspace, media_files, tick

__all__ = ["generation_workspace", "media_files"]
pytestmark = pytest.mark.skipif(
    os.environ.get("COMFYPILOT_PRESSURE") != "1",
    reason="opt-in: hundreds of MiB and real media tools",
)


@pytest.fixture(scope="module")
def pressure_media(tmp_path_factory):
    path = tmp_path_factory.mktemp("pressure-media") / "noise.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:size=1024x576:rate=24,noise=alls=100:allf=t+u",
            "-frames:v",
            "400",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    assert path.stat().st_size >= 256 * 1024**2
    return path


@contextmanager
def bridge_and_proxy(tmp_path, upstream):
    inputs, outputs = tmp_path / "bridge-input", tmp_path / "bridge-output"
    inputs.mkdir()
    outputs.mkdir()
    stopped, ready = Event(), Event()
    state = {"downloads": 0, "sent": 0}

    async def serve():
        app = web.Application()
        app.add_routes(routes(inputs, outputs))
        async with TestServer(app) as server:
            state["bridge"] = str(server.make_url("/")).rstrip("/")
            ready.set()
            while not stopped.is_set():
                await asyncio.sleep(0.05)

    thread = Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    assert ready.wait(5)

    class Proxy(BaseHTTPRequestHandler):
        def forward(self):
            asset = self.path.startswith("/comfypilot/v1/")
            body = None
            if self.headers.get("Transfer-Encoding") == "chunked":
                chunks = []
                while size := int(self.rfile.readline().strip(), 16):
                    chunks.append(self.rfile.read(size))
                    assert self.rfile.read(2) == b"\r\n"
                assert self.rfile.readline() == b"\r\n"
                body = b"".join(chunks)
            elif self.headers.get("Content-Length"):
                body = self.rfile.read(int(self.headers["Content-Length"]))
            request = Request(
                (
                    state["bridge"]
                    if asset or (remote and self.path == "/system_stats")
                    else upstream
                )
                + self.path,
                data=body,
                method=self.command,
                headers={
                    k: v
                    for k, v in self.headers.items()
                    if k.lower()
                    not in {"host", "connection", "content-length", "transfer-encoding"}
                },
            )
            try:
                response = urlopen(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                self.send_response(response.status)
                for key in ("Content-Type", "Content-Length"):
                    if response.headers.get(key):
                        self.send_header(key, response.headers[key])
                self.end_headers()
                download = asset and "/download?" in self.path
                if download:
                    state["downloads"] += 1
                try:
                    while chunk := response.read(1024 * 1024):
                        self.wfile.write(chunk)
                        if download:
                            state["sent"] += len(chunk)
                            time.sleep(0.08)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        do_GET = forward
        do_POST = forward
        do_PUT = forward

        def log_message(self, *_args):
            pass

    if remote := os.environ.get("COMFYPILOT_PRESSURE_BRIDGE"):
        assert remote.startswith(("http://", "https://"))
        state["bridge"] = remote.rstrip("/")
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    proxy_thread = Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        yield f"http://127.0.0.1:{proxy.server_port}", outputs, state
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(5)
        stopped.set()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("restart", [False, True], ids=["continuous", "restart-during-transfer"])
def test_large_bridge_collection_keeps_monitor_alive_and_registers_once(
    generation_workspace, pressure_media, tmp_path, restart
):
    project, _, external, _ = generation_workspace
    original = get_execution(project)
    command(project, "stop_plan", plan_id=original["plans"][0]["id"])
    asset = get_asset(project, original["runs"][0]["document"]["step"]["asset_ids"]["video"])
    with bridge_and_proxy(tmp_path, get_project(project)["config"]["targets"][0]["base_url"]) as (
        address,
        outputs,
        transport,
    ):
        config = get_project(project)
        update_project(
            project,
            config["revision"],
            {
                **config["config"],
                "targets": [
                    {
                        "id": "render",
                        "name": "受控压力通道",
                        "base_url": address,
                        "assets": {"mode": "bridge"},
                    }
                ],
            },
        )
        # The synthetic pressure output is deliberately independent of the small input timeline.
        recipe_path = project.root / "modules/demo/recipe.yaml"
        import yaml
        from comfypilot.resources import refresh_resources

        recipe = yaml.safe_load(recipe_path.read_text())
        recipe["outputs"]["main"]["spec"] = {}
        recipe_path.write_text(yaml.safe_dump(recipe))
        refresh_resources(project, 1)
        body = plan_body(project, original["tasks"][0]["id"], count=1, limit=1)
        body["steps"][0].update(
            asset_ids={"video": asset["id"]}, inputs={"video": asset["content_hash"]}
        )
        body["mandate"]["allowed_inputs"] = {"video": asset["content_hash"]}
        body["mandate"]["instruction"] = "仅验证合成大文件自动收片，不调用真实GPU"
        plan = command(project, "create_plan", new_instruction=True, **body)["result"]["plan_id"]
        command(project, "activate_plan", plan_id=plan)
        run_id = command(project, "execute_plan", plan_id=plan, action_indices=[0])["result"][
            "run_ids"
        ][0]
        command(project, "resume_dispatch")
        tick(generation_workspace)
        run = next(r for r in get_execution(project)["runs"] if r["id"] == run_id)
        assert run["status"] == "submitted", run
        command(project, "pause_dispatch")
        relative = run["document"]["materialized"]["outputs"]["main"]["prefix"] + ".mp4"
        output = outputs / relative
        output.parent.mkdir(parents=True)
        shutil.copyfile(pressure_media, output)
        if os.environ.get("COMFYPILOT_PRESSURE_BRIDGE"):
            peer = os.environ["COMFYPILOT_PRESSURE_SSH"]
            remote_root = os.environ["COMFYPILOT_PRESSURE_OUTPUT"]
            assert Path(remote_root).is_absolute()
            remote_path = str(Path(remote_root) / relative)
            subprocess.run(
                ["ssh", peer, "mkdir -p -- " + shlex.quote(str(Path(remote_path).parent))],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["scp", str(output), f"{peer}:{remote_path}"], check=True, capture_output=True
            )
        prompt_id = run["document"]["receipt"]["prompt_id"]
        external.update(
            running=[],
            history={
                prompt_id: {
                    "prompt": external["prompt"],
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
        tick(generation_workspace)
        assert (
            next(r for r in get_execution(project)["runs"] if r["id"] == run_id)["status"]
            == "collecting"
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        project = read_project(project.root)
        start_service(project, port)
        samples = []
        phases = set()
        did_restart = False
        started = time.monotonic()
        try:
            while time.monotonic() - started < 240:
                before = time.monotonic()
                with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as response:
                    snapshot = json.load(response)["data"]
                elapsed = time.monotonic() - before
                active = next(r for r in snapshot["runs"] if r["id"] == run_id)
                phase = active["document"].get("collection", {}).get("phase")
                phases.add(phase)
                worker = snapshot["monitoring"]["worker"]
                metric = snapshot["monitoring"]["targets"][0]["metrics"]["ram_free"]
                samples.append(
                    {
                        "elapsed": time.monotonic() - started,
                        "latency": elapsed,
                        "heartbeat": worker["last_seen_at"],
                        "worker": worker["status"],
                        "target_sample": metric["observed_at"],
                        "target_availability": metric["availability"],
                        "phase": phase,
                    }
                )
                assert worker["status"] == "running", samples[-1]
                if (
                    restart
                    and not did_restart
                    and phase == "transferring"
                    and transport["sent"] >= 8 * 1024**2
                ):
                    stop_service(project)
                    start_service(project, port)
                    did_restart = True
                if active["status"] in {"succeeded", "failed"} or phase == "blocked":
                    break
                time.sleep(0.2)
            versions = list_versions(project)["versions"]
            assert len(versions) == 1 and versions[0]["ready"], (active, phases)
            version = versions[0]
            assert version["media"]["frame_count"] == 400 and version["media"]["codec"] == "ffv1"
            assert version["proxy"]["media"]["codec"] == "h264"
            assert (
                version_path(project, version["id"], "original").stat().st_size
                == pressure_media.stat().st_size
            )
            assert len(external["posts"]) == 1
            assert (
                next(p for p in get_execution(project)["plans"] if p["id"] == plan)[
                    "used_submissions"
                ]
                == 1
            )
            assert did_restart == restart
            assert transport["downloads"] == (2 if restart else 1)
            heartbeats = sorted({s["heartbeat"] for s in samples})
            gaps = [
                (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
                for a, b in zip(heartbeats, heartbeats[1:], strict=False)
            ]
            assert len(heartbeats) >= 4 and max(gaps) < 10
            observed = sorted({s["target_sample"] for s in samples if s["target_sample"]})
            monitor_gaps = [
                (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
                for a, b in zip(observed, observed[1:], strict=False)
            ]
            assert len(observed) >= 4 and max(monitor_gaps) < 15
            assert all(s["target_availability"] != "stale" for s in samples)
            assert max(s["latency"] for s in samples) < 3
            assert {"transferring", "checking", "proxy"}.issubset(phases)
            result = {
                "bridge_deployment": "remote"
                if os.environ.get("COMFYPILOT_PRESSURE_BRIDGE")
                else "local",
                "bytes": pressure_media.stat().st_size,
                "restart": restart,
                "phases": sorted(phases),
                "duration": time.monotonic() - started,
                "max_api_latency": max(s["latency"] for s in samples),
                "max_heartbeat_gap": max(gaps),
                "max_monitor_gap": max(monitor_gaps),
                "target_samples": len(observed),
                "downloads": transport["downloads"],
                "additional_posts": 0,
                "versions": len(versions),
                "project": str(project.root),
            }
            (tmp_path / "pressure-result.json").write_text(
                json.dumps({"result": result, "samples": samples}, indent=2)
            )
            print(json.dumps(result))
        finally:
            stop_service(project)
