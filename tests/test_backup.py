import asyncio
import json
import shutil
import sqlite3

import pytest
from comfypilot.collection import list_versions, version_path
from comfypilot.execution import get_execution
from comfypilot.project import ProjectError, initialize_project, load_project, resolve_project_root
from comfypilot.replicas import list_replicas
from test_cleanup import collected
from test_execution import command
from test_generation import generation_workspace, media_files

__all__ = ["generation_workspace", "media_files"]


def test_backup_restores_new_project_and_preserves_review_without_replay(
    generation_workspace, tmp_path
):
    from comfypilot.backup import export_project, restore_project

    project = generation_workspace[0]
    _, _, version = collected(generation_workspace)
    command(project, "save_feedback", version_id=version["id"], kind="note", text="保留的测试反馈")
    command(
        project,
        "record_user_decision",
        version_id=version["id"],
        decision="adopt",
        user_statement="仅采用测试版本",
    )
    secret = project.root / ".comfypilot/secrets.local.yaml"
    secret.write_text("credentials: never-export-this-secret")
    (project.root / ".comfypilot/staging/unfinished.part").write_bytes(b"half")
    bundle = tmp_path / "bundle"
    result = export_project(project, bundle)
    assert result["files"] > 3
    assert not any("secrets.local" in p.name or p.suffix == ".part" for p in bundle.rglob("*"))
    restored = restore_project(bundle, tmp_path / "restored")
    new = load_project(resolve_project_root(tmp_path / "restored"))
    assert restored["project_id"] != str(project.identity.project_id)
    assert new.config.server.host == "127.0.0.1" and not any(t.enabled for t in new.config.targets)
    state = get_execution(new)
    assert state["dispatch"]["manual_pause"]
    assert all(r["status"] in {"succeeded", "failed", "cancelled"} for r in state["runs"])
    assert all(p["status"] == "stopped" for p in state["plans"])
    assert list_replicas(new)["replicas"] == []
    assert list_versions(new)["versions"][0]["content_hash"] == version["content_hash"]
    assert (
        version_path(new, version["id"], "original").read_bytes()
        == version_path(project, version["id"], "original").read_bytes()
    )
    from comfypilot.review import delivery_manifest, list_review_items

    assert delivery_manifest(new)["items"][0]["version_id"] == version["id"]
    assert list_review_items(new)["feedback"][0]["text"] == "保留的测试反馈"
    import os
    from uuid import uuid4

    from comfypilot.generation import advance_generation

    before = len(generation_workspace[2]["posts"])
    asyncio.run(advance_generation(new, uuid4(), os.getpid()))
    assert len(generation_workspace[2]["posts"]) == before
    with sqlite3.connect(new.database) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("defect", ["hash", "escape", "missing", "symlink", "extra"])
def test_restore_rejects_bad_bundle_before_creating_destination(tmp_path, defect):
    from comfypilot.backup import export_project, restore_project

    p = initialize_project(tmp_path / "source")
    bundle = tmp_path / "bundle"
    export_project(p, bundle)
    manifest = bundle / "manifest.json"
    value = json.loads(manifest.read_text())
    file = bundle / value["files"][0]["path"]
    if defect == "hash":
        file.write_bytes(b"corrupt")
    elif defect == "missing":
        file.unlink()
    elif defect == "escape":
        value["files"][0]["path"] = "../outside"
    elif defect == "symlink":
        file.unlink()
        file.symlink_to(p.root / "comfypilot.yaml")
    else:
        (bundle / "unlisted.sh").write_text("unexpected")
    manifest.write_text(json.dumps(value))
    dest = tmp_path / "restored"
    with pytest.raises(ProjectError):
        restore_project(bundle, dest)
    assert not dest.exists()


def test_moving_stopped_project_rebinds_root_and_keeps_uuid(tmp_path):
    old = initialize_project(tmp_path / "old")
    new_path = tmp_path / "new"
    shutil.move(old.root, new_path)
    moved = load_project(resolve_project_root(new_path))
    assert moved.identity == old.identity
    with sqlite3.connect(moved.database) as db:
        assert db.execute("SELECT root FROM project").fetchone()[0] == str(new_path)


def test_corrupt_database_is_rejected_even_with_updated_manifest(tmp_path):
    from comfypilot.backup import export_project, file_hash, restore_project

    p = initialize_project(tmp_path / "source")
    bundle = tmp_path / "bundle"
    export_project(p, bundle)
    database = bundle / ".comfypilot/state.sqlite"
    database.write_bytes(b"not a database")
    manifest = json.loads((bundle / "manifest.json").read_text())
    entry = next(x for x in manifest["files"] if x["path"] == ".comfypilot/state.sqlite")
    entry.update(sha256=file_hash(database), size=database.stat().st_size)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ProjectError, match="损坏"):
        restore_project(bundle, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_stdio_backup_and_offline_restore_use_explicit_roots(tmp_path):
    from test_mcp_bridge import bridge, call
    from test_service import running_service

    root = tmp_path / "source"
    bundle = tmp_path / "bundle"
    with running_service(root):

        async def exercise():
            async with bridge(tmp_path) as client:
                connected = await call(client, "connect_service", root)
                exported = await call(
                    client,
                    "export_project",
                    root,
                    connection_ref=connected["connection_ref"],
                    destination=str(bundle),
                )
                assert exported["files"] == 3
                restored = await call(
                    client, "restore_project", tmp_path / "restored", bundle_path=str(bundle)
                )
                assert (
                    restored["dispatch_paused"]
                    and restored["source_project_id"] != restored["project_id"]
                )

        asyncio.run(exercise())
