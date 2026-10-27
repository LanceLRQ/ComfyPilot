import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import yaml

from comfypilot.project import ProjectError, initialize_project, load_project, resolve_project_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="comfypilot", description="ComfyPilot 项目服务")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="在明确指定的项目目录初始化")
    init.add_argument("--project-root", required=True, type=Path)
    serve = commands.add_parser("serve", help="在回环地址运行项目 API 和独立 Worker")
    serve.add_argument("--project-root", required=True, type=Path)
    serve.add_argument("--port", type=int, help="本次服务端口，默认读取项目配置")
    args = parser.parse_args(argv)
    try:
        root = resolve_project_root(args.project_root)
        if args.command == "init":
            project = initialize_project(root)
            print(json.dumps(project.summary(), ensure_ascii=False))
        else:
            import uvicorn

            from comfypilot.app import create_app

            project = load_project(root)
            port = args.port if args.port is not None else project.config.server.port
            if not 1 <= port <= 65535:
                raise ProjectError("validation_error", "端口必须在 1 到 65535 之间。")
            os.chdir(root)
            uvicorn.run(create_app(project), host="127.0.0.1", port=port)
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
