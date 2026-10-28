import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from comfypilot.project import ProjectError, initialize_project, load_project


@pytest.fixture
def project(tmp_path: Path):
    return initialize_project(tmp_path)


def test_disk_changes_need_refresh_and_invalid_refresh_preserves_active_config(project):
    from comfypilot.settings import get_project, refresh_project

    assert get_project(project)["revision"] == 1
    path = project.root / "comfypilot.yaml"
    content = yaml.safe_load(path.read_text())
    content["name"] = "显式刷新后才生效"
    path.write_text(yaml.safe_dump(content, allow_unicode=True))
    assert load_project(project.root).config.name != "显式刷新后才生效"
    assert get_project(project)["file_changed"] is True
    refreshed = refresh_project(project, 1)
    assert refreshed["revision"] == 2
    assert load_project(project.root).config.name == "显式刷新后才生效"
    path.write_text("name: [invalid\n")
    with pytest.raises(ProjectError, match="配置") as error:
        refresh_project(project, 2)
    assert error.value.code == "validation_error"
    assert get_project(project)["revision"] == 2
    assert load_project(project.root).config.name == "显式刷新后才生效"


def test_stale_update_rejects_without_overwriting_file_or_revision(project):
    from comfypilot.settings import get_project, update_project

    config = get_project(project)["config"]
    first = update_project(project, 1, {**config, "name": "网页修改"})
    before = (project.root / "comfypilot.yaml").read_bytes()
    with pytest.raises(ProjectError) as error:
        update_project(project, 1, {**config, "name": "Agent 的旧草稿"})
    assert error.value.code == "conflict"
    assert get_project(project)["config"]["name"] == "网页修改"
    assert first["revision"] == 2
    assert (project.root / "comfypilot.yaml").read_bytes() == before


def test_explicit_routes_and_default_changes_keep_old_target_snapshots(project):
    from comfypilot.settings import get_project, resolve_target, update_project, update_target

    assert resolve_target(project)["status"] == "needs_input"
    config = get_project(project)["config"]
    update_project(
        project,
        1,
        {
            **config,
            "targets": [
                {
                    "id": "local",
                    "name": "本机",
                    "location": "local",
                    "base_url": "http://127.0.0.1:8188",
                },
                {
                    "id": "remote",
                    "name": "远程",
                    "location": "remote",
                    "base_url": "https://render.example/comfy",
                },
            ],
            "default_target_id": "local",
        },
    )
    original = resolve_target(project)
    assert original["target"]["id"] == "local"
    assert resolve_target(project, "remote")["target"]["id"] == "remote"
    changed = update_target(
        project,
        2,
        {
            **original["target"],
            "base_url": "http://127.0.0.1:8288",
        },
    )
    assert changed["revision"] == 3
    assert resolve_target(project)["target"]["base_url"] == "http://127.0.0.1:8288"
    assert original["target"]["base_url"] == "http://127.0.0.1:8188"
    with sqlite3.connect(project.database) as db:
        old = json.loads(
            db.execute("SELECT config FROM config_revisions WHERE revision=2").fetchone()[0]
        )
        assert old["targets"][0]["base_url"] == "http://127.0.0.1:8188"
    with pytest.raises(ProjectError) as error:
        resolve_target(project, "not-configured")
    assert error.value.code == "target_unavailable"


def test_duplicate_endpoint_is_reported_and_disabled_default_is_not_routed(project):
    from comfypilot.settings import get_project, resolve_target, update_project

    config = get_project(project)["config"]
    result = update_project(
        project,
        1,
        {
            **config,
            "targets": [
                {
                    "id": "one",
                    "name": "主机",
                    "base_url": "http://EXAMPLE.COM:80/",
                    "enabled": False,
                },
                {"id": "alias", "name": "别名", "base_url": "http://example.com"},
            ],
            "default_target_id": "one",
        },
    )
    assert result["warnings"][0]["code"] == "duplicate_target"
    assert result["warnings"][0]["target_ids"] == ["one", "alias"]
    assert resolve_target(project)["status"] == "needs_input"
    assert resolve_target(project, "alias")["target"]["id"] == "alias"


def test_credentials_stay_outside_public_config_and_errors(project):
    from comfypilot.settings import get_project, update_target

    secret = "test-only-secret-marker"
    (project.root / ".comfypilot/secrets.local.yaml").write_text(
        yaml.safe_dump({"render-auth": {"Authorization": f"Bearer {secret}"}})
    )
    result = update_target(
        project,
        1,
        {
            "id": "remote",
            "name": "远端",
            "base_url": "https://render.example",
            "credential_ref": "render-auth",
        },
    )
    assert secret not in json.dumps(result)
    assert result["credentials"]["render-auth"] is True
    assert secret not in (project.root / "comfypilot.yaml").read_text()
    with pytest.raises(ProjectError) as error:
        update_target(
            project,
            2,
            {
                "id": "bad",
                "name": "bad",
                "base_url": f"https://user:{secret}@render.example",
            },
        )
    assert error.value.code == "validation_error"
    assert secret not in str(error.value)
    assert get_project(project)["revision"] == 2


def test_new_config_tables_are_migrated_without_losing_project_data(project):
    with sqlite3.connect(project.database) as db:
        db.execute("DROP TABLE config_revisions")
        db.execute("CREATE TABLE user_marker (value TEXT)")
        db.execute("INSERT INTO user_marker VALUES ('keep-existing-data')")
        db.execute("PRAGMA user_version=2")
    migrated = load_project(project.root)
    assert migrated.identity == project.identity
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 14
        assert db.execute("SELECT value FROM user_marker").fetchone()[0] == "keep-existing-data"
        columns = {row[1]: row[2] for row in db.execute("PRAGMA table_info(config_revisions)")}
        assert columns == {
            "revision": "INTEGER",
            "config": "TEXT",
            "source_hash": "TEXT",
            "accepted_at": "TEXT",
        }


def test_invalid_secret_file_does_not_hide_valid_configuration(project):
    from comfypilot.settings import get_project, update_target

    update_target(
        project,
        1,
        {
            "id": "remote",
            "name": "远端",
            "base_url": "https://render.example",
            "credential_ref": "render-auth",
        },
    )
    (project.root / ".comfypilot/secrets.local.yaml").write_text("bad: [private-secret")
    result = get_project(project)
    assert result["revision"] == 2
    assert result["credentials"]["render-auth"] is False
    assert result["warnings"][0]["code"] == "credential_unavailable"
    assert "private-secret" not in json.dumps(result)


def test_connected_mcp_and_http_share_revisions_and_reject_wrong_instance_and_origin(project):
    import asyncio
    import socket
    import sys
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from comfypilot.lifecycle import start_service, stop_service
    from mcp import Client, StdioServerParameters

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    runtime = start_service(project, port)["runtime"]
    address = f"http://127.0.0.1:{port}"

    def request(path, body=None, **headers):
        value = Request(
            address + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "X-ComfyPilot-Instance": runtime["instance_id"],
                **headers,
            },
        )
        try:
            with urlopen(value, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    async def scenario():
        async with Client(
            StdioServerParameters(
                command=sys.executable, args=["-m", "comfypilot.mcp_bridge"], cwd=project.root
            )
        ) as client:
            args = {"project_root": str(project.root)}
            connected = await client.call_tool("connect_service", args)
            args["connection_ref"] = connected.structured_content["data"]["connection_ref"]
            result = await client.call_tool("get_project", args)
            assert result.is_error is False
            assert result.structured_content["data"]["revision"] == 1
            config = result.structured_content["data"]["config"]
            status, updated = request(
                "/api/config/update",
                {"expected_revision": 1, "config": {**config, "name": "网页已修改"}},
            )
            assert status == 200 and updated["data"]["revision"] == 2
            conflict = await client.call_tool(
                "update_project",
                {**args, "expected_revision": 1, "config": {**config, "name": "过期草稿"}},
            )
            assert conflict.is_error and conflict.structured_content["error"]["code"] == "conflict"
            current = await client.call_tool("get_project", args)
            assert current.structured_content["data"]["config"]["name"] == "网页已修改"
            status, _ = request(
                "/api/config/refresh",
                {"expected_revision": 2},
                **{"X-ComfyPilot-Instance": "wrong-instance"},
            )
            assert status == 409
            status, _ = request(
                "/api/config/refresh", {"expected_revision": 2}, Origin="https://untrusted.example"
            )
            assert status == 403
            secret = "must-not-echo-private-input"
            status, invalid = request(
                "/api/config/update", {"expected_revision": 2, "config": {"password": secret}}
            )
            assert status == 422 and secret not in json.dumps(invalid)
            status, state = request("/api/project")
            assert status == 200 and state["name"] == "网页已修改"

    try:
        asyncio.run(scenario())
    finally:
        stop_service(project)
