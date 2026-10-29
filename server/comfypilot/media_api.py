import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse

from comfypilot.collection import get_version, list_versions, version_path
from comfypilot.lan import public_version
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
from comfypilot.posters import poster_path
from comfypilot.project import Project, ProjectError
from comfypilot.replicas import list_replicas
from comfypilot.review import get_review_version
from comfypilot.runtime import RuntimeRecord
from comfypilot.settings import get_project


def install_media_routes(app: FastAPI, project: Project, runtime: RuntimeRecord) -> None:
    poster_gate = asyncio.Semaphore(1)

    async def poster(source, seconds=0):
        async with poster_gate:
            path = await poster_path(project, source, seconds)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def reply(data):
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }

    @app.get("/api/assets")
    def assets():
        return reply(list_assets(project))

    @app.get("/api/versions")
    def versions(request: Request):
        data = list_versions(project)
        if not request.state.local_control:
            data["versions"] = [public_version(x) for x in data["versions"]]
        return reply(data)

    @app.get("/api/versions/{version_id}")
    def review_version(request: Request, version_id: UUID):
        data = get_review_version(project, str(version_id))
        if not request.state.local_control:
            data["version"] = public_version(data["version"])
        return reply(data)

    @app.get("/api/versions/{version_id}/content")
    def version_content(version_id: UUID, variant: Literal["original", "preview"] = "preview"):
        path = version_path(project, str(version_id), variant)
        version = get_version(project, str(version_id))
        media = (
            version["proxy"]["media"]
            if variant == "preview" and version["proxy"]
            else version["media"]
        )
        mime = (
            {"png": "image/png", "mjpeg": "image/jpeg", "webp": "image/webp"}[media["codec"]]
            if version["kind"] == "image"
            else "video/mp4"
            if "mp4" in media["container"]
            else "video/webm"
            if "webm" in media["container"]
            else "video/x-matroska"
        )
        return FileResponse(
            path,
            media_type=mime,
            content_disposition_type="inline",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/versions/{version_id}/poster")
    async def version_poster(version_id: UUID, view: Literal["candidate", "source"] = "candidate"):
        if view == "candidate":
            return await poster(version_path(project, str(version_id), "preview"))
        mapping = get_review_version(project, str(version_id))["comparison"]
        if not mapping["source_asset_id"]:
            raise ProjectError("poster_unavailable", "没有可用的视频参考。")
        return await poster(
            asset_path(project, mapping["source_asset_id"]), mapping.get("offset_seconds", 0)
        )

    @app.get("/api/assets/{asset_id}/poster")
    async def asset_poster(asset_id: UUID):
        return await poster(asset_path(project, str(asset_id)))

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
