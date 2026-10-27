import socket
import subprocess
import sys
import time
from http.client import HTTPConnection, HTTPException
from uuid import uuid4

import psutil
from pydantic import ValidationError

from comfypilot.project import Project, ProjectError, read_dispatch, set_dispatch
from comfypilot.runtime import (
    RuntimeRecord,
    ServiceHealth,
    matching_process,
    project_lock,
    read_runtime,
)


def inspect_service(project: Project, expected: RuntimeRecord) -> ServiceHealth:
    if matching_process(project, expected.api) is None:
        raise ProjectError("service_not_running", "项目 API 已退出，请检查状态后显式启动。")
    connection = HTTPConnection("127.0.0.1", expected.port, timeout=1)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            raise ProjectError("identity_mismatch", "端口没有返回有效健康回执，拒绝接管。")
        health = ServiceHealth.model_validate_json(response.read(65536))
    except (OSError, HTTPException) as error:
        raise ProjectError(
            "service_unavailable", "无法取得本机服务回执；请检查端口和项目日志。"
        ) from error
    except ValidationError as error:
        raise ProjectError("identity_mismatch", "端口响应不是预期的项目服务，拒绝接管。") from error
    finally:
        connection.close()
    actual = health.runtime
    if (
        health.project_id != expected.project_id
        or health.api_pid != expected.api.pid
        or actual.project_id != expected.project_id
        or actual.root != expected.root
        or actual.instance_id != expected.instance_id
        or actual.api != expected.api
        or actual.port != expected.port
    ):
        raise ProjectError("identity_mismatch", "服务回执与项目、实例或进程记录不一致，拒绝接管。")
    if (
        expected.worker
        and matching_process(project, expected.worker)
        and expected.worker != actual.worker
    ):
        raise ProjectError("identity_mismatch", "Worker 记录与服务回执不一致，拒绝接管。")
    if actual.worker and matching_process(project, actual.worker) is None:
        health.worker_ready = False
    return health


def status_service(project: Project) -> dict:
    runtime = read_runtime(project)
    if runtime is None:
        return {
            "status": "stopped",
            "runtime": None,
            "worker_ready": False,
            "dispatch": read_dispatch(project),
        }
    if matching_process(project, runtime.api) is None:
        if runtime.worker and matching_process(project, runtime.worker):
            raise ProjectError(
                "service_unavailable", "API 已退出但 Worker 仍存活，保留现场并检查项目日志。"
            )
        return {
            "status": "stopped",
            "runtime": runtime.model_dump(mode="json"),
            "worker_ready": False,
            "dispatch": read_dispatch(project),
        }
    health = inspect_service(project, runtime)
    return {
        "status": "running",
        "runtime": health.runtime.model_dump(mode="json"),
        "worker_ready": health.worker_ready,
        "dispatch": read_dispatch(project),
    }


def start_service(project: Project, port: int) -> dict:
    with project_lock(project, "control"):
        status = status_service(project)
        if status["status"] == "running":
            if status["runtime"]["port"] != port:
                raise ProjectError(
                    "port_conflict", "本项目已在另一端口运行，请先停止或使用已有端口。"
                )
            if status["runtime"]["phase"] != "running":
                raise ProjectError("service_busy", "项目实例正在启动或停止，请稍后重试。")
            if not status["worker_ready"]:
                raise ProjectError(
                    "worker_unavailable", "API 已运行但 Worker 未就绪，请检查项目日志。"
                )
            return {**status, "reused": True}
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
                listener.listen(128)
            except OSError as error:
                raise ProjectError(
                    "port_conflict", "端口不可用，请指定其他端口；不会停止占用者。"
                ) from error
            instance_id = str(uuid4())
            with (project.root / ".comfypilot/logs/service.log").open("ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "comfypilot.service",
                        "--project-root",
                        str(project.root),
                        "--port",
                        str(port),
                        "--instance-id",
                        instance_id,
                        "--socket-fd",
                        str(listener.fileno()),
                    ],
                    cwd=project.root,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    pass_fds=(listener.fileno(),),
                    start_new_session=True,
                )
            ready = False
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise ProjectError(
                            "start_failed", "服务启动失败，请检查 .comfypilot/logs/service.log。"
                        )
                    runtime = read_runtime(project)
                    if runtime and str(runtime.instance_id) == instance_id:
                        try:
                            health = inspect_service(project, runtime)
                        except ProjectError as error:
                            if error.code != "service_unavailable":
                                raise
                        else:
                            if health.worker_ready and health.runtime.phase == "running":
                                ready = True
                                return {
                                    "status": "running",
                                    "reused": False,
                                    "runtime": health.runtime.model_dump(mode="json"),
                                    "worker_ready": True,
                                    "dispatch": read_dispatch(project),
                                }
                    time.sleep(0.05)
                raise ProjectError("start_timeout", "服务未按时就绪，请检查项目日志与磁盘状态。")
            finally:
                if not ready:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=10)


def stop_service(project: Project) -> dict:
    with project_lock(project, "control"):
        runtime = read_runtime(project)
        if runtime and matching_process(project, runtime.api):
            health = inspect_service(project, runtime)
            set_dispatch(project, paused=True, reason="service_stopped")
            process = matching_process(project, health.runtime.api)
            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except psutil.TimeoutExpired as error:
                    raise ProjectError(
                        "stop_timeout", "暂停已保存，API 尚未退出，请检查项目日志。"
                    ) from error
            latest = read_runtime(project)
            if latest and latest.worker and matching_process(project, latest.worker):
                raise ProjectError(
                    "stop_incomplete", "暂停已保存，但 Worker 尚未退出，请检查项目日志。"
                )
        elif runtime and runtime.worker and matching_process(project, runtime.worker):
            raise ProjectError(
                "service_unavailable", "API 已退出但 Worker 仍存活，无法取得身份回执；保留现场。"
            )
        else:
            set_dispatch(project, paused=True, reason="service_stopped")
        return {
            "status": "stopped",
            "project_id": str(project.identity.project_id),
            "dispatch": read_dispatch(project),
        }


def resume_dispatch(project: Project) -> dict:
    with project_lock(project, "control"):
        status = status_service(project)
        if status["status"] != "running" or not status["worker_ready"]:
            raise ProjectError("service_not_running", "请先显式启动并确认 Worker 就绪。")
        set_dispatch(project, paused=False, reason="explicit_resume")
        return {**status, "dispatch": read_dispatch(project)}
