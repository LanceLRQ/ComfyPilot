import asyncio
import os
import sqlite3
import subprocess
from uuid import uuid4

import httpx2
from comfypilot.app import create_app
from comfypilot.execution import get_execution
from comfypilot.runtime import RuntimeRecord, process_identity
from test_cleanup import collected
from test_generation import generation_workspace, media_files
from test_review import review_workspace

__all__ = ["generation_workspace", "media_files", "review_workspace"]


def poster_client(project, peer="127.0.0.1"):
    runtime = RuntimeRecord(
        project_id=project.identity.project_id,
        root=str(project.root),
        instance_id=uuid4(),
        port=8765,
        api=process_identity(os.getpid()),
    )
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(project, runtime), client=(peer, 12000)),
        base_url="http://127.0.0.1:8765",
    )


def test_posters_are_real_cached_images_without_business_changes(generation_workspace):
    project = generation_workspace[0]
    _, _, version = collected(generation_workspace)
    before = get_execution(project)

    async def exercise():
        async with poster_client(project) as client:
            url = f"/api/versions/{version['id']}/poster"
            response = await client.get(url)
            assert response.status_code == 200, response.text
            assert response.headers["content-type"] == "image/jpeg"
            assert response.headers["cache-control"].startswith("private")
            assert response.content.startswith(b"\xff\xd8")
            cache = next((project.root / ".comfypilot/cache/posters").glob("*.jpg"))
            stamp = cache.stat().st_mtime_ns
            assert (await client.get(url)).content == response.content
            assert cache.stat().st_mtime_ns == stamp
            width, height = (
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=width,height",
                        "-of",
                        "csv=p=0",
                        str(cache),
                    ],
                    text=True,
                )
                .strip()
                .split(",")
            )
            assert int(width) <= 480 and int(height) <= 270
            source = await client.get(url + "?view=source")
            assert source.status_code == 200 and source.content.startswith(b"\xff\xd8")
            asset = version["source"]["inputs"][0]["asset_id"]
            assert (await client.get(f"/api/assets/{asset}/poster")).content == source.content
            assert (await client.get(url + "?view=invalid")).status_code == 422
            assert (await client.get(f"/api/versions/{uuid4()}/poster")).status_code == 400
            assert (await client.get("/api/assets/not-a-uuid/poster")).status_code == 422

    asyncio.run(exercise())
    assert get_execution(project) == before
    assert not list((project.root / ".comfypilot/cache/posters").glob("poster-*"))


def test_source_poster_uses_verified_mapping_offset(review_workspace):
    workspace, version, original = review_workspace
    project = workspace[0]

    async def exercise():
        async with poster_client(project) as client:
            response = await client.get(f"/api/versions/{version['id']}/poster?view=source")
            assert response.status_code == 200, response.text
            assert response.content.startswith(b"\xff\xd8")
            from comfypilot.media import get_asset

            asset = get_asset(project, original)
            assert (
                project.root / f".comfypilot/cache/posters/{asset['content_hash']}-1000.jpg"
            ).is_file()

    asyncio.run(exercise())


def test_lan_posters_are_available_but_managed_paths_stay_protected(generation_workspace):
    project = generation_workspace[0]
    _, _, version = collected(generation_workspace)

    async def exercise():
        async with poster_client(project, "10.0.0.2") as client:
            url = f"/api/versions/{version['id']}/poster"
            assert (await client.get(url)).status_code == 200
            assert (await client.get(url + "?view=source")).status_code == 200
            assert (await client.get("/api/config")).status_code == 403
            asset = version["source"]["inputs"][0]["asset_id"]
            assert (await client.get(f"/api/assets/{asset}/poster")).status_code == 200
            from comfypilot.media import asset_path

            path = asset_path(project, asset)
            path.unlink()
            path.symlink_to(project.root / "comfypilot.yaml")
            assert (await client.get(f"/api/assets/{asset}/poster")).status_code == 400
            path.unlink()

    asyncio.run(exercise())


def test_failed_poster_does_not_publish_partial_cache(generation_workspace, monkeypatch):
    project = generation_workspace[0]
    _, _, version = collected(generation_workspace)
    import comfypilot.posters as posters
    from comfypilot.project import ProjectError

    async def fail(*args, **kwargs):
        raise ProjectError("media_invalid", "测试解码失败")

    monkeypatch.setattr(posters, "media_command", fail)

    async def exercise():
        async with poster_client(project) as client:
            response = await client.get(f"/api/versions/{version['id']}/poster")
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "media_invalid"

    asyncio.run(exercise())
    assert list((project.root / ".comfypilot/cache/posters").iterdir()) == []
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_image_asset_poster_is_decoded_without_video_timing(tmp_path, media_files):
    from comfypilot.project import initialize_project
    from test_media import imported

    project = initialize_project(tmp_path / "project")
    asset_id = imported(project, media_files[1], "image")

    async def exercise():
        async with poster_client(project) as client:
            result = await client.get(f"/api/assets/{asset_id}/poster")
            assert result.status_code == 200, result.text
            assert result.content.startswith(b"\xff\xd8")

    asyncio.run(exercise())
