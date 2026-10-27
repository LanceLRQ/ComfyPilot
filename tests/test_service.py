import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psutil
import pytest


@contextmanager
def running_service(root: Path):
    init = subprocess.run(
        [sys.executable, "-m", "comfypilot", "init", "--project-root", str(root)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    identity = json.loads(init.stdout)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "comfypilot",
            "serve",
            "--project-root",
            str(root),
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    address = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            assert process.poll() is None, process.stdout.read()
            try:
                with urlopen(address + "/api/project", timeout=1) as response:
                    data = json.load(response)
                if data["worker"]["status"] == "running":
                    break
            except URLError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("API 与独立 Worker 未在期限内就绪")
        yield address, data, identity
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        process.stdout.close()


def test_api_reports_project_and_a_separate_live_worker(tmp_path: Path) -> None:
    with running_service(tmp_path / "project") as (address, data, identity):
        assert data["project_id"] == identity["project_id"]
        assert data["root"] == str((tmp_path / "project").resolve())
        assert data["worker"]["pid"] != os.getpid()
        assert data["worker"]["last_seen_at"]
        worker_pid = data["worker"]["pid"]
        with urlopen(address + "/health", timeout=2) as response:
            health = json.load(response)
            assert health["project_id"] == identity["project_id"]
            assert health["api_pid"] != worker_pid
            assert Path(psutil.Process(health["api_pid"]).cwd()) == (tmp_path / "project").resolve()
            assert Path(psutil.Process(worker_pid).cwd()) == (tmp_path / "project").resolve()
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)


def test_two_running_projects_never_share_state(tmp_path: Path) -> None:
    with running_service(tmp_path / "one") as (address_one, one, _):
        with running_service(tmp_path / "two") as (address_two, two, _):
            assert one["project_id"] != two["project_id"]
            assert one["worker"]["pid"] != two["worker"]["pid"]
            assert address_one != address_two
            with urlopen(address_one + "/api/project", timeout=2) as response:
                assert json.load(response)["name"] == "one"


def test_service_serves_built_web_and_keeps_project_files_private(tmp_path: Path) -> None:
    from urllib.error import HTTPError

    with running_service(tmp_path / "project") as (address, _, _):
        with urlopen(address + "/", timeout=2) as response:
            assert response.headers.get_content_type() == "text/html"
            assert "ComfyPilot" in response.read().decode()
        (tmp_path / "project/private-marker.txt").write_text("not-public")
        for path in ["/private-marker.txt", "/%2e%2e/comfypilot.yaml"]:
            with pytest.raises(HTTPError) as error:
                urlopen(address + path, timeout=2)
            assert error.value.code == 404


def test_serve_does_not_initialize_an_unknown_directory(tmp_path: Path) -> None:
    root = tmp_path / "unknown"
    result = subprocess.run(
        [sys.executable, "-m", "comfypilot", "serve", "--project-root", str(root)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "project_incomplete" in result.stderr
    assert not root.exists()
