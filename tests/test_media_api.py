import asyncio
import hashlib
import time

import httpx2
from media_fixture import media_files
from test_mcp_bridge import bridge, call
from test_service import running_service

__all__ = ["media_files"]


def wait_job(client, headers, job):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        response = client.post("/api/assets/jobs/get", headers=headers, json={"job_id": job["id"]})
        response.raise_for_status()
        result = response.json()["data"]
        if result["status"] in {"succeeded", "failed", "blocked"}:
            assert result["status"] == "succeeded", result
            return result
        time.sleep(0.05)
    raise AssertionError("媒体任务未在期限内完成")


def test_stream_upload_clip_and_project_boundary(tmp_path, media_files):
    video, image = media_files
    with running_service(tmp_path / "one") as (address, _, _):
        with httpx2.Client(base_url=address, timeout=15) as client:
            listing = client.get("/api/assets").json()
            headers = {"X-ComfyPilot-Instance": listing["instance_id"]}
            # 流式迭代器无 Content-Length，服务端必须逐块接收。
            response = client.post(
                "/api/assets/upload",
                params={"kind": "image", "filename": image.name},
                headers=headers,
                content=iter([image.read_bytes()[:32], image.read_bytes()[32:]]),
            )
            response.raise_for_status()
            reference = wait_job(client, headers, response.json()["data"])["asset_id"]
            response = client.post(
                "/api/assets/import",
                headers=headers,
                json={"kind": "video", "source_path": str(video)},
            )
            response.raise_for_status()
            original = wait_job(client, headers, response.json()["data"])["asset_id"]
            response = client.post(
                "/api/assets/clips",
                headers=headers,
                json={
                    "source_asset_id": original,
                    "reference_asset_id": reference,
                    "start_seconds": 1,
                    "end_seconds": 3,
                    "width": 64,
                    "height": 48,
                },
            )
            response.raise_for_status()
            clip = wait_job(client, headers, response.json()["data"])["asset_id"]
            asset = client.post("/api/assets/get", headers=headers, json={"asset_id": clip}).json()[
                "data"
            ]
            content = client.get(f"/api/assets/{clip}/content")
            assert hashlib.sha256(content.content).hexdigest() == asset["content_hash"]
            assert asset["media"]["frame_count"] == 48
            assert [slot["slot"] for slot in asset["input_order"]] == ["video", "reference"]
            partial = client.get(f"/api/assets/{clip}/content", headers={"Range": "bytes=0-15"})
            assert partial.status_code == 206 and partial.content == content.content[:16]
            assert client.post("/api/assets/import", json={}).status_code == 409
            assert (
                client.post(
                    "/api/assets/import",
                    headers={**headers, "Origin": "https://evil.test"},
                    json={},
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/api/assets/upload",
                    headers=headers,
                    params={"kind": "image", "filename": "../bad.png"},
                    content=b"bad",
                ).status_code
                == 422
            )
            config = client.get("/api/config").json()["data"]
            config["config"]["import_limits"]["image_bytes"] = len(image.read_bytes())
            assert (
                client.post(
                    "/api/config/update",
                    headers=headers,
                    json={
                        "expected_revision": config["revision"],
                        "config": config["config"],
                    },
                ).status_code
                == 200
            )
            exact = client.post(
                "/api/assets/upload",
                headers=headers,
                params={"kind": "image", "filename": image.name},
                content=iter([image.read_bytes()]),
            )
            exact.raise_for_status()
            wait_job(client, headers, exact.json()["data"])
            rejected = client.post(
                "/api/assets/upload",
                headers=headers,
                params={"kind": "image", "filename": image.name},
                content=iter([image.read_bytes(), b"x"]),
            )
            assert rejected.json()["error"]["code"] == "media_limit"
            with running_service(tmp_path / "two") as (other, _, _):
                assert httpx2.get(f"{other}/api/assets/{clip}/content").status_code == 400
            assert client.get("/api/project").json()["worker"]["status"] == "running"
            assert not list((tmp_path / "one/.comfypilot/staging").iterdir())


def test_mcp_media_jobs_continue_after_client_disconnect(tmp_path, media_files):
    video, _ = media_files
    root = tmp_path / "project"
    with running_service(root) as (address, _, _):

        async def submit():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", root)
                return await call(
                    client,
                    "import_asset",
                    root,
                    source_path=str(video),
                    kind="video",
                    connection_ref=connected["connection_ref"],
                )

        job = asyncio.run(submit())
        with httpx2.Client(base_url=address) as client:
            headers = {"X-ComfyPilot-Instance": client.get("/api/assets").json()["instance_id"]}
            done = wait_job(client, headers, job)

        async def inspect():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", root)
                connection = {"connection_ref": connected["connection_ref"]}
                result = await call(client, "get_media_job", root, job_id=job["id"], **connection)
                assert result["status"] == "succeeded"
                asset = await call(
                    client, "get_asset", root, asset_id=done["asset_id"], **connection
                )
                assert asset["source"]["path"] == str(video)
                listing = await call(client, "list_assets", root, **connection)
                assert listing["assets"][0]["id"] == done["asset_id"]

        asyncio.run(inspect())
