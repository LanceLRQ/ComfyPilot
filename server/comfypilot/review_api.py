import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from comfypilot.collection import get_version
from comfypilot.lan import public_source
from comfypilot.media import ImportStream, media_directory
from comfypilot.media_tools import inspect_media, require_tools
from comfypilot.project import Project, ProjectError
from comfypilot.review import delivery_manifest, list_review_items
from comfypilot.runtime import RuntimeRecord


class ScreenshotMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version_id: UUID
    candidate_seconds: float = Field(ge=0)


def install_review_routes(app: FastAPI, project: Project, runtime: RuntimeRecord) -> None:
    def reply(data):
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }

    @app.get("/api/review/items")
    def items(unresolved: bool = True):
        return reply(list_review_items(project, unresolved))

    @app.get("/api/review/delivery")
    def delivery(request: Request, download: bool = False):
        data = delivery_manifest(project)
        if not request.state.local_control:
            for item in data["items"]:
                item["provenance"] = public_source(item["provenance"])
        return JSONResponse(
            reply(data),
            headers={"Content-Disposition": 'attachment; filename="comfypilot-delivery.json"'}
            if download
            else None,
        )

    @app.post("/api/review/screenshots")
    async def upload(request: Request, metadata: Annotated[ScreenshotMetadata, Query()]):
        version = get_version(project, str(metadata.version_id))
        if metadata.candidate_seconds > (version["media"]["duration_seconds"] or 0):
            raise ProjectError("feedback_range_invalid", "截图时间超过当前版本。")
        require_tools()
        with ImportStream(project, 8 * 1024 * 1024) as stream:
            async for chunk in request.stream():
                stream.write(chunk)
                await asyncio.sleep(0)
            stream.file.flush()
            media = await inspect_media(stream.path, "image")
            if (
                media["codec"] not in {"png", "mjpeg"}
                or max(media["width"], media["height"]) > 4096
            ):
                raise ProjectError("screenshot_invalid", "截图仅接受最长边4096像素内的PNG/JPEG。")
            digest = stream.hash.hexdigest()
            destination = media_directory(project, "artifacts/review") / digest
            if destination.is_symlink():
                raise ProjectError("media_path_invalid", "截图文件被链接替换。")
            if not destination.exists():
                stream.path.replace(destination)
            screenshot_id = str(uuid4())
            document = {
                "id": screenshot_id,
                "version_id": str(metadata.version_id),
                "candidate_seconds": metadata.candidate_seconds,
                "content_hash": digest,
                "size": stream.size,
                "mime": "image/png" if media["codec"] == "png" else "image/jpeg",
                "url": f"/api/review/screenshots/{screenshot_id}",
                "created_at": datetime.now(UTC).isoformat(),
            }
            with closing(sqlite3.connect(project.database)) as db, db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute(
                    "INSERT INTO review_screenshots VALUES (?,?,?)",
                    (screenshot_id, str(metadata.version_id), json.dumps(document)),
                )
            return reply(document)

    @app.get("/api/review/screenshots/{screenshot_id}")
    def screenshot(screenshot_id: UUID):
        with closing(sqlite3.connect(project.database)) as db:
            row = db.execute(
                "SELECT document FROM review_screenshots WHERE id=?", (str(screenshot_id),)
            ).fetchone()
        if not row:
            raise ProjectError("screenshot_unavailable", "截图不存在。")
        document = json.loads(row[0])
        path = media_directory(project, "artifacts/review") / document["content_hash"]
        if path.is_symlink() or not path.is_file():
            raise ProjectError("screenshot_unavailable", "截图受管文件丢失。")
        return FileResponse(
            path, media_type=document["mime"], headers={"X-Content-Type-Options": "nosniff"}
        )
