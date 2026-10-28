import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse

from comfypilot.media import (
    CHUNK_BYTES,
    AssetRequest,
    ExtractClipRequest,
    ImportAssetRequest,
    ImportStream,
    MediaJobRequest,
    UploadMetadata,
    asset_path,
    check_space,
    get_asset,
    get_media_job,
    list_assets,
    media_directory,
    queue_clip,
    queue_import,
    queue_upload,
)
from comfypilot.media_tools import require_tools
from comfypilot.project import Project, ProjectError
from comfypilot.replicas import list_replicas
from comfypilot.runtime import RuntimeRecord
from comfypilot.settings import get_project


def install_media_routes(app: FastAPI, project: Project, runtime: RuntimeRecord) -> None:
    def reply(data):
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }

    @app.get("/api/assets")
    def assets():
        return reply(list_assets(project))

    @app.post("/api/assets/get")
    def asset(body: AssetRequest):
        return reply(get_asset(project, body.asset_id))

    @app.get("/api/assets/replicas")
    def replicas():
        return reply(list_replicas(project))

    @app.post("/api/assets/jobs/get")
    def job(body: MediaJobRequest):
        return reply(get_media_job(project, body.job_id))

    @app.post("/api/assets/import")
    def import_asset(body: ImportAssetRequest):
        return reply(queue_import(project, body))

    @app.post("/api/assets/clips")
    def clip(body: ExtractClipRequest):
        return reply(queue_clip(project, body))

    @app.post("/api/assets/upload")
    async def upload(request: Request, metadata: Annotated[UploadMetadata, Query()]):
        require_tools()
        media_directory(project, "artifacts/inputs")
        limit = get_project(project)["config"]["import_limits"][metadata.kind + "_bytes"]
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isdecimal():
                raise ProjectError("validation_error", "上传 Content-Length 无效。")
            if int(length) > limit:
                raise ProjectError(
                    "media_limit", "文件超过项目导入上限，请先裁剪或调整 import_limits。"
                )
            check_space(media_directory(project, "staging"), int(length))
        with ImportStream(project, limit) as stream:
            async for chunk in request.stream():
                for offset in range(0, len(chunk), CHUNK_BYTES):
                    stream.write(memoryview(chunk)[offset : offset + CHUNK_BYTES])
                    await asyncio.sleep(0)
            return reply(queue_upload(project, metadata, stream))

    @app.get("/api/assets/{asset_id}/content")
    def content(asset_id: UUID):
        asset = get_asset(project, str(asset_id))
        if asset["kind"] == "image":
            mime = {"png": "image/png", "mjpeg": "image/jpeg", "webp": "image/webp"}[
                asset["media"]["codec"]
            ]
        else:
            mime = (
                "video/webm"
                if asset["filename"].lower().endswith(".webm")
                else "video/x-matroska"
                if asset["filename"].lower().endswith(".mkv")
                else "video/quicktime"
                if asset["filename"].lower().endswith(".mov")
                else "video/mp4"
            )
        return FileResponse(
            asset_path(project, asset["id"]),
            media_type=mime,
            filename=asset["filename"],
            content_disposition_type="inline",
            headers={"X-Content-Type-Options": "nosniff"},
        )
