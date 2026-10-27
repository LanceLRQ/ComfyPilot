import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from comfypilot.project import Project, ProjectError


class ProcessIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pid: int = Field(ge=1)
    started_at: float = Field(gt=0)
    executable: str


class RuntimeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    root: str
    instance_id: UUID
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    api: ProcessIdentity
    worker: ProcessIdentity | None = None
    phase: Literal["starting", "running", "stopping", "stopped"] = "starting"


class ServiceHealth(BaseModel):
    status: Literal["ok"]
    project_id: UUID
    api_pid: int
    runtime: RuntimeRecord
    worker_ready: bool


def process_identity(pid: int) -> ProcessIdentity:
    process = psutil.Process(pid)
    return ProcessIdentity(pid=pid, started_at=process.create_time(), executable=process.exe())


def matching_process(project: Project, identity: ProcessIdentity) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        if (
            process.create_time() != identity.started_at
            or process.exe() != identity.executable
            or Path(process.cwd()).resolve() != project.root
        ):
            raise ProjectError("identity_mismatch", "PID 的启动信息或目录不匹配，拒绝接管进程。")
        return process
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as error:
        raise ProjectError("identity_unavailable", "无权读取进程身份，拒绝接管进程。") from error


def read_runtime(project: Project) -> RuntimeRecord | None:
    path = project.root / ".comfypilot/runtime.json"
    if not path.exists():
        return None
    try:
        runtime = RuntimeRecord.model_validate_json(path.read_text("utf-8"))
    except ValidationError as error:
        raise ProjectError(
            "runtime_invalid", "运行记录格式不完整，请检查 runtime.json 和日志。"
        ) from error
    if runtime.project_id != project.identity.project_id or runtime.root != str(project.root):
        raise ProjectError("identity_mismatch", "运行记录所属项目或根目录不匹配，拒绝接管。")
    return runtime


def write_runtime(project: Project, runtime: RuntimeRecord) -> None:
    temporary = project.root / ".comfypilot/runtime.tmp"
    temporary.write_text(runtime.model_dump_json(indent=2) + "\n", "utf-8")
    temporary.replace(project.root / ".comfypilot/runtime.json")


@contextmanager
def project_lock(project: Project, name: str, *, timeout: float = 15):
    if os.name != "posix":
        raise ProjectError("unsupported_platform", "项目进程管理当前仅支持 macOS 和 Linux。")
    import fcntl

    with (project.root / f".comfypilot/locks/{name}.lock").open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise ProjectError(
                        "service_busy", "本项目正在进行其他进程操作，请稍后重试。"
                    ) from error
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
