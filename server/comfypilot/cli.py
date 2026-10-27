import argparse
import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import yaml

from comfypilot.project import ProjectError, initialize_project, load_project, resolve_project_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="comfypilot", description="ComfyPilot 项目服务")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="在明确指定的项目目录初始化")
    init.add_argument("--project-root", required=True, type=Path)
    for command, help_text in [
        ("serve", "前台运行项目 API 和独立 Worker"),
        ("start", "在后台启动或复用项目实例"),
        ("status", "校验并读取项目实例状态"),
        ("stop", "先暂停派发，再停止本项目实例"),
        ("resume-dispatch", "显式解除项目的手动派发暂停"),
    ]:
        child = commands.add_parser(command, help=help_text)
        child.add_argument("--project-root", required=True, type=Path)
        if command in ("serve", "start"):
            child.add_argument("--port", type=int, help="本次服务端口，默认读取项目配置")
    args = parser.parse_args(argv)
    try:
        root = resolve_project_root(args.project_root)
        if args.command == "init":
            project = initialize_project(root)
            print(json.dumps(project.summary(), ensure_ascii=False))
        else:
            from comfypilot.lifecycle import (
                resume_dispatch,
                start_service,
                status_service,
                stop_service,
            )

            project = load_project(root)
            if args.command in ("serve", "start"):
                port = args.port if args.port is not None else project.config.server.port
                if not 1 <= port <= 65535:
                    raise ProjectError("validation_error", "端口必须在 1 到 65535 之间。")
                if args.command == "serve":
                    from comfypilot.service import run_service

                    run_service(project, port, uuid4())
                    return 0
                result = start_service(project, port)
            elif args.command == "status":
                result = status_service(project)
            elif args.command == "stop":
                result = stop_service(project)
            else:
                result = resume_dispatch(project)
            print(json.dumps(result, ensure_ascii=False))
    except ProjectError as error:
        print(
            json.dumps({"code": error.code, "message": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    except (OSError, sqlite3.Error, ValueError, yaml.YAMLError):
        print(
            json.dumps(
                {
                    "code": "project_unavailable",
                    "message": "项目无法读取或写入，请检查目录权限、配置和数据库完整性。",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    return 0
