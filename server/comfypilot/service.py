import argparse
import os
import socket
from pathlib import Path
from uuid import UUID

import uvicorn

from comfypilot.app import create_app
from comfypilot.project import Project, ProjectError, load_project, resolve_project_root
from comfypilot.runtime import (
    RuntimeRecord,
    matching_process,
    process_identity,
    project_lock,
    read_runtime,
)


def run_service(
    project: Project, port: int, instance_id: UUID, socket_fd: int | None = None
) -> None:
    os.chdir(project.root)
    with project_lock(project, "service", timeout=0):
        previous = read_runtime(project)
        if previous:
            if matching_process(project, previous.api):
                raise ProjectError("service_busy", "已有项目实例仍存活，请先检查状态。")
            if previous.worker and matching_process(project, previous.worker):
                raise ProjectError(
                    "service_unavailable", "旧 Worker 仍存活，拒绝覆盖运行记录；请检查项目日志。"
                )
        listener = socket.socket(fileno=socket_fd) if socket_fd is not None else socket.socket()
        with listener:
            if socket_fd is None:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    listener.bind(("127.0.0.1", port))
                    listener.listen(128)
                except OSError as error:
                    raise ProjectError(
                        "port_conflict", "端口不可用，请指定其他端口；不会停止占用者。"
                    ) from error
            runtime = RuntimeRecord(
                project_id=project.identity.project_id,
                root=str(project.root),
                instance_id=instance_id,
                port=port,
                api=process_identity(os.getpid()),
            )
            server = uvicorn.Server(
                uvicorn.Config(create_app(project, runtime), host="127.0.0.1", port=port)
            )
            server.run(sockets=[listener])


def main() -> None:
    parser = argparse.ArgumentParser(description="ComfyPilot 项目后台实例")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--instance-id", type=UUID, required=True)
    parser.add_argument("--socket-fd", type=int)
    args = parser.parse_args()
    project = load_project(resolve_project_root(args.project_root))
    run_service(project, args.port, args.instance_id, args.socket_fd)


if __name__ == "__main__":
    main()
