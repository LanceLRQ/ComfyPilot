import asyncio
import sqlite3
from pathlib import Path

from fastapi import FastAPI

from comfypilot.backup import ExportRequest, export_project
from comfypilot.project import Project, ProjectError
from comfypilot.runtime import RuntimeRecord


def install_backup_routes(app: FastAPI, project: Project, runtime: RuntimeRecord) -> None:
    @app.post("/api/backup/export")
    async def export(body: ExportRequest):
        try:
            data = await asyncio.to_thread(export_project, project, Path(body.destination))
        except (OSError, sqlite3.Error, ValueError, KeyError) as error:
            raise ProjectError(
                "backup_unavailable", "导出未完成；检查空间/权限/数据库，保留现场并改用新目录重试。"
            ) from error
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }
