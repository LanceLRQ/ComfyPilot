import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import psutil
import pytest


@dataclass
class ProjectCLI:
    root: Path
    port: int
    processes: list[dict] = field(default_factory=list)

    def command(self, action: str) -> list[str]:
        args = [sys.executable, "-m", "comfypilot", action, "--project-root", str(self.root)]
        if action in ("start", "serve"):
            args.extend(["--port", str(self.port)])
        return args

    def call(self, action: str, *, success: bool = True) -> dict:
        result = subprocess.run(self.command(action), capture_output=True, text=True, timeout=25)
        if success:
            assert result.returncode == 0, result.stderr
            data = json.loads(result.stdout)
            if action == "start":
                self.remember(data["runtime"])
            return data
        assert result.returncode != 0, result.stdout
        return json.loads(result.stderr)

    def remember(self, runtime: dict) -> None:
        for role in ("api", "worker"):
            if runtime[role]:
                self.processes.append(runtime[role])


@pytest.fixture
def project_cli(tmp_path: Path):
    root = (tmp_path / "创作 project").resolve()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    cli = ProjectCLI(root, port)
    cli.call("init")
    try:
        yield cli
    finally:
        subprocess.run(cli.command("stop"), capture_output=True, timeout=20)
        for identity in reversed(cli.processes):
            try:
                process = psutil.Process(identity["pid"])
                if process.create_time() == identity["started_at"]:
                    process.terminate()
                    process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass


def test_start_detaches_from_launcher_and_reuses_instance(project_cli: ProjectCLI) -> None:
    first = project_cli.call("start")
    runtime = first["runtime"]
    assert first["reused"] is False
    assert runtime["root"] == str(project_cli.root)
    assert runtime["api"]["pid"] != runtime["worker"]["pid"]
    for role in ("api", "worker"):
        identity = runtime[role]
        process = psutil.Process(identity["pid"])
        assert process.create_time() == identity["started_at"]
        assert process.cwd() == str(project_cli.root)
        assert os.getsid(process.pid) == process.pid
    second = project_cli.call("start")
    assert second["reused"] is True
    assert second["runtime"]["instance_id"] == runtime["instance_id"]
    assert second["runtime"]["api"] == runtime["api"]
    assert project_cli.call("status")["worker_ready"] is True


def test_two_concurrent_starts_create_one_instance(project_cli: ProjectCLI) -> None:
    callers = [
        subprocess.Popen(
            project_cli.command("start"), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(2)
    ]
    results = []
    for caller in callers:
        stdout, stderr = caller.communicate(timeout=25)
        assert caller.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        project_cli.remember(result["runtime"])
        results.append(result)
    assert {result["reused"] for result in results} == {False, True}
    assert results[0]["runtime"] == results[1]["runtime"]


def test_worker_crash_recovers_without_replacing_api(project_cli: ProjectCLI) -> None:
    before = project_cli.call("start")["runtime"]
    psutil.Process(before["worker"]["pid"]).kill()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = project_cli.call("status")
        runtime = status["runtime"]
        if runtime["worker"]["pid"] != before["worker"]["pid"] and status["worker_ready"]:
            break
        time.sleep(0.1)
    else:
        pytest.fail("Worker 没有在 API 仍存活时恢复")
    project_cli.remember(runtime)
    assert runtime["instance_id"] == before["instance_id"]
    assert runtime["api"] == before["api"]


@pytest.mark.parametrize(
    "field", ["project_id", "instance_id", "root", "started_at", "worker_started_at"]
)
def test_identity_mismatch_refuses_reuse_and_stop(project_cli: ProjectCLI, field: str) -> None:
    before = project_cli.call("start")["runtime"]
    path = project_cli.root / ".comfypilot/runtime.json"
    original = path.read_bytes()
    changed = json.loads(original)
    if field in ("project_id", "instance_id"):
        changed[field] = str(uuid4())
    elif field == "root":
        changed["root"] = str(project_cli.root.parent)
    elif field == "started_at":
        changed["api"]["started_at"] += 1
    else:
        changed["worker"]["started_at"] += 1
    path.write_text(json.dumps(changed))
    try:
        for action in ("start", "status", "stop"):
            assert project_cli.call(action, success=False)["code"] == "identity_mismatch"
        assert psutil.pid_exists(before["api"]["pid"])
        assert psutil.pid_exists(before["worker"]["pid"])
    finally:
        path.write_bytes(original)


def test_foreign_port_is_not_interrupted(project_cli: ProjectCLI) -> None:
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", project_cli.port))
        foreign.listen()
        assert project_cli.call("start", success=False)["code"] == "port_conflict"
        assert project_cli.call("serve", success=False)["code"] == "port_conflict"
        assert foreign.getsockname()[1] == project_cli.port
        assert not (project_cli.root / ".comfypilot/runtime.json").exists()


def test_forged_live_pid_cannot_stop_another_process(project_cli: ProjectCLI) -> None:
    project_cli.call("start")
    path = project_cli.root / ".comfypilot/runtime.json"
    original = path.read_bytes()
    foreign = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=project_cli.root
    )
    try:
        process = psutil.Process(foreign.pid)
        changed = json.loads(original)
        changed["api"] = {
            "pid": foreign.pid,
            "started_at": process.create_time(),
            "executable": process.exe(),
        }
        path.write_text(json.dumps(changed))
        assert project_cli.call("stop", success=False)["code"] == "identity_mismatch"
        assert foreign.poll() is None
    finally:
        path.write_bytes(original)
        foreign.terminate()
        foreign.wait(timeout=5)


def test_stop_persists_pause_and_does_not_restart(project_cli: ProjectCLI) -> None:
    before = project_cli.call("start")["runtime"]
    assert project_cli.call("resume-dispatch")["dispatch"]["paused"] is False
    assert project_cli.call("status")["dispatch"]["paused"] is False
    assert project_cli.call("stop")["status"] == "stopped"
    with sqlite3.connect(project_cli.root / ".comfypilot/state.sqlite") as db:
        assert db.execute(
            "SELECT paused, reason FROM service_control WHERE singleton=1"
        ).fetchone() == (
            1,
            "service_stopped",
        )
    time.sleep(1.2)
    for role in ("api", "worker"):
        assert not psutil.pid_exists(before[role]["pid"])
    assert project_cli.call("status")["status"] == "stopped"
    assert project_cli.call("stop")["status"] == "stopped"
    after = project_cli.call("start")
    assert after["runtime"]["instance_id"] != before["instance_id"]
    assert after["dispatch"]["paused"] is True


def test_status_and_stop_do_not_implicitly_start(project_cli: ProjectCLI) -> None:
    assert project_cli.call("status")["status"] == "stopped"
    assert project_cli.call("stop")["status"] == "stopped"
    assert not (project_cli.root / ".comfypilot/runtime.json").exists()
    assert project_cli.call("resume-dispatch", success=False)["code"] == "service_not_running"


def test_stop_saves_pause_before_signalling_api(
    project_cli: ProjectCLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comfypilot.lifecycle import stop_service
    from comfypilot.project import load_project

    runtime = project_cli.call("start")["runtime"]
    project_cli.call("resume-dispatch")
    terminate = psutil.Process.terminate
    observed = []

    def observe(process):
        if process.pid == runtime["api"]["pid"]:
            with sqlite3.connect(project_cli.root / ".comfypilot/state.sqlite") as db:
                observed.append(db.execute("SELECT paused FROM service_control").fetchone()[0])
        return terminate(process)

    monkeypatch.setattr(psutil.Process, "terminate", observe)
    stop_service(load_project(project_cli.root))
    assert observed == [1]


def test_foreign_http_response_cannot_redirect_or_control_processes(
    project_cli: ProjectCLI,
) -> None:
    runtime = project_cli.call("start")["runtime"]
    calls = []

    class ForeignService(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("GET", self.path))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{project_cli.port}/unexpected")
            self.end_headers()

        def do_POST(self):
            calls.append(("POST", self.path))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), ForeignService) as foreign:
        thread = Thread(target=foreign.serve_forever, daemon=True)
        thread.start()
        path = project_cli.root / ".comfypilot/runtime.json"
        original = path.read_bytes()
        changed = json.loads(original)
        changed["port"] = foreign.server_port
        path.write_text(json.dumps(changed))
        try:
            for action in ("start", "status", "stop"):
                assert project_cli.call(action, success=False)["code"] == "identity_mismatch"
            assert calls == [("GET", "/health")] * 3
            assert psutil.pid_exists(runtime["api"]["pid"])
            assert psutil.pid_exists(runtime["worker"]["pid"])
        finally:
            path.write_bytes(original)
            foreign.shutdown()
            thread.join(timeout=3)


def test_worker_recovery_has_a_retry_limit(project_cli: ProjectCLI) -> None:
    runtime = project_cli.call("start")["runtime"]
    api = runtime["api"]
    for attempt in range(4):
        previous_pid = runtime["worker"]["pid"]
        psutil.Process(previous_pid).kill()
        if attempt == 3:
            break
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = project_cli.call("status")
            runtime = status["runtime"]
            if runtime["worker"]["pid"] != previous_pid and status["worker_ready"]:
                project_cli.remember(runtime)
                break
            time.sleep(0.05)
        else:
            pytest.fail("预期的有限恢复没有发生")
    time.sleep(1.5)
    status = project_cli.call("status")
    assert status["runtime"]["api"] == api
    assert status["runtime"]["worker"]["pid"] == previous_pid
    assert status["worker_ready"] is False
    assert project_cli.call("resume-dispatch", success=False)["code"] == "service_not_running"


def test_api_crash_does_not_authorize_killing_surviving_worker(project_cli: ProjectCLI) -> None:
    runtime = project_cli.call("start")["runtime"]
    api = psutil.Process(runtime["api"]["pid"])
    api.kill()
    api.wait(timeout=5)
    for action in ("start", "status", "stop", "serve"):
        assert project_cli.call(action, success=False)["code"] == "service_unavailable"
    assert psutil.pid_exists(runtime["worker"]["pid"])


def test_foreground_serve_cannot_replace_running_instance(project_cli: ProjectCLI) -> None:
    runtime = project_cli.call("start")["runtime"]
    assert project_cli.call("serve", success=False)["code"] == "service_busy"
    assert project_cli.call("status")["runtime"] == runtime
