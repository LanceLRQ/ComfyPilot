import asyncio
import json
import os
from uuid import uuid4

import httpx2
import pytest
from comfypilot.app import create_app
from comfypilot.execution import get_execution
from comfypilot.runtime import RuntimeRecord, process_identity
from test_cleanup import collected
from test_generation import generation_workspace, media_files

__all__ = ["generation_workspace", "media_files"]


def test_lan_peer_cannot_spoof_local_control_or_read_private_state(generation_workspace):
    project = generation_workspace[0]
    _, _, version = collected(generation_workspace)
    runtime = RuntimeRecord(
        project_id=project.identity.project_id,
        root=str(project.root),
        instance_id=uuid4(),
        port=8765,
        api=process_identity(os.getpid()),
    )
    app = create_app(project, runtime)

    async def exercise():
        transport = httpx2.ASGITransport(app=app, client=("10.0.0.2", 12000))
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8765"
        ) as client:
            headers = {
                "X-ComfyPilot-Instance": str(runtime.instance_id),
                "X-Forwarded-For": "127.0.0.1",
                "Origin": "http://127.0.0.1:8765",
            }
            for path in (
                "/health",
                "/api/config",
                "/api/context",
                "/api/cleanup",
                "/api/assets/replicas",
                "/api/execution",
            ):
                assert (await client.get(path, headers=headers)).status_code == 403, path
            for path in ("/api/cleanup/execute", "/api/targets/probe", "/api/execution/command"):
                result = await client.post(
                    path,
                    headers=headers,
                    json={
                        "action": "resume_dispatch",
                        "source": "web_user",
                        "command_id": str(uuid4()),
                        "expected_revision": get_execution(project)["revision"],
                    },
                )
                assert result.status_code == 403, result.text
            data = (await client.get("/api/state")).json()["data"]
            assert data["local_control"] is False
            raw = json.dumps(data)
            assert str(project.root) not in raw
            assert "base_url" not in raw and "credential_ref" not in raw
            assert "parameters" not in raw and "target_snapshot" in raw
            assert data["versions"][0]["id"] == version["id"]
            assert (await client.get("/api/project")).json()["root"] == ""
            assert (
                await client.get("/api/state", headers={"Host": "evil.example"})
            ).status_code == 403
            body = {
                "action": "save_feedback",
                "source": "agent",
                "command_id": str(uuid4()),
                "expected_revision": get_execution(project)["revision"],
                "version_id": version["id"],
                "kind": "note",
                "text": "<script>alert('x')</script>",
            }
            rejected = await client.post(
                "/api/execution/command",
                headers={**headers, "Origin": "https://evil.example"},
                json=body,
            )
            assert rejected.status_code == 403
            saved = await client.post("/api/execution/command", headers=headers, json=body)
            assert saved.status_code == 200, saved.text
            feedback = (await client.get("/api/state")).json()["data"]["feedback"][0]
            assert feedback["source"] == "web_user" and feedback["text"] == body["text"]
            response = await client.get(
                f"/api/versions/{version['id']}/content", headers={"Range": "bytes=0-63"}
            )
            assert response.status_code == 206 and len(response.content) == 64

    asyncio.run(exercise())


def test_lan_binding_requires_explicit_addresses():
    from comfypilot.project import ServerConfig

    assert ServerConfig().host == "127.0.0.1"
    with pytest.raises(ValueError):
        ServerConfig(host="0.0.0.0")
    assert ServerConfig(host="0.0.0.0", lan_hosts=["10.22.33.22"]).host == "0.0.0.0"
