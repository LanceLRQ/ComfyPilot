import asyncio
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from comfypilot.project import ProjectError, initialize_project
from media_fixture import media_files

__all__ = ["media_files"]


def imported(project, path, kind):
    from comfypilot.media import (
        ImportAssetRequest,
        get_media_job,
        process_next_media_job,
        queue_import,
    )

    job = queue_import(project, ImportAssetRequest(source_path=str(path), kind=kind))
    assert job["status"] == "queued"
    assert asyncio.run(process_next_media_job(project))
    finished = get_media_job(project, job["id"])
    assert finished["status"] == "succeeded", finished
    return finished["asset_id"]


def test_mjpeg_video_is_not_mistaken_for_a_jpeg_image(tmp_path):
    from comfypilot.media import get_asset

    video = tmp_path / "camera.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=64x48:r=24:d=1",
            "-c:v",
            "mjpeg",
            str(video),
        ],
        check=True,
    )
    project = initialize_project(tmp_path / "project")
    assert get_asset(project, imported(project, video, "video"))["media"]["codec"] == "mjpeg"


@pytest.mark.parametrize("extension", ["jpg", "webp"])
def test_reference_image_formats_are_actually_decoded(tmp_path, media_files, extension):
    from comfypilot.media import get_asset

    _, image = media_files
    target = tmp_path / f"reference.{extension}"
    if extension == "webp":
        target.write_bytes((Path(__file__).parent / "fixtures/reference.webp").read_bytes())
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(image), str(target)], check=True)
    project = initialize_project(tmp_path / "project")
    asset = get_asset(project, imported(project, target, "image"))
    assert asset["media"]["decoded"] is True
    assert asset["media"]["width"] == asset["media"]["height"] == 32


def test_cancelling_media_command_reaps_its_ffmpeg_child():
    import psutil
    from comfypilot.media_tools import media_command

    async def scenario():
        before = {child.pid for child in psutil.Process().children()}
        task = asyncio.create_task(
            media_command(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=red:s=64x48:r=24",
                    "-f",
                    "null",
                    "-",
                ]
            )
        )
        for _ in range(100):
            if any(child.pid not in before for child in psutil.Process().children()):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("FFmpeg 子进程未启动")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert {child.pid for child in psutil.Process().children()} == before

    asyncio.run(scenario())


def test_schema_six_upgrade_preserves_existing_data_and_creates_media_fields(tmp_path):
    from comfypilot.project import load_project

    project = initialize_project(tmp_path / "project")
    with sqlite3.connect(project.database) as db:
        for table in ("media_jobs", "media_assets", "media_blobs"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=6")
        db.execute("CREATE TABLE user_marker (value TEXT)")
        db.execute("INSERT INTO user_marker VALUES ('keep')")
    load_project(project.root)
    with sqlite3.connect(project.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert db.execute("SELECT value FROM user_marker").fetchone()[0] == "keep"
        assert {row[1] for row in db.execute("PRAGMA table_info(media_assets)")} == {
            "id",
            "content_hash",
            "document",
        }
        assert {row[1] for row in db.execute("PRAGMA table_info(media_jobs)")} == {
            "id",
            "status",
            "document",
        }
        assert {row[1] for row in db.execute("PRAGMA table_info(media_blobs)")} == {"hash", "size"}
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_import_copies_decoded_media_with_independent_identities_and_shared_bytes(
    tmp_path, media_files
):
    from comfypilot.media import asset_path, get_asset, list_assets

    project = initialize_project(tmp_path / "project")
    video, image = media_files
    original = video.read_bytes()
    one = get_asset(project, imported(project, video, "video"))
    two = get_asset(project, imported(project, video, "video"))
    photo = get_asset(project, imported(project, image, "image"))
    assert one["id"] != two["id"]
    assert one["content_hash"] == two["content_hash"] == hashlib.sha256(original).hexdigest()
    assert asset_path(project, one["id"]) == asset_path(project, two["id"])
    assert one["media"]["width"] == 64 and one["media"]["height"] == 48
    assert photo["media"]["codec"] == "png"
    assert one["source"]["path"] == str(video)
    video.write_bytes(b"external file changed")
    assert asset_path(project, one["id"]).read_bytes() == original
    assert len(list_assets(project)["assets"]) == 3
    with sqlite3.connect(project.database) as db:
        assert db.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0] == 2
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_explicit_clip_preserves_time_mapping_reference_order_and_actual_frames(
    tmp_path, media_files
):
    from comfypilot.media import (
        ExtractClipRequest,
        asset_path,
        get_asset,
        get_media_job,
        process_next_media_job,
        queue_clip,
    )

    project = initialize_project(tmp_path / "project")
    video, image = media_files
    source_id, reference_id = imported(project, video, "video"), imported(project, image, "image")
    job = queue_clip(
        project,
        ExtractClipRequest(
            source_asset_id=source_id,
            reference_asset_id=reference_id,
            start_seconds=1,
            end_seconds=3,
            fps=24,
            width=64,
            height=48,
        ),
    )
    assert asyncio.run(process_next_media_job(project))
    finished = get_media_job(project, job["id"])
    assert finished["status"] == "succeeded", finished
    clip = get_asset(project, finished["asset_id"])
    assert clip["media"]["frame_count"] == 48
    assert clip["media"]["fps"] == "24/1"
    assert clip["source"]["asset_id"] == source_id
    assert clip["source"]["time_map"] == {
        "origin": "first_video_frame",
        "mode": "normalized_cfr",
        "fps": 24,
        "start_frame": 24,
        "end_frame": 72,
        "start_seconds": 1.0,
        "end_seconds": 3.0,
    }
    assert [item["asset_id"] for item in clip["input_order"]] == [clip["id"], reference_id]
    assert [item["slot"] for item in clip["input_order"]] == ["video", "reference"]
    raw = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(asset_path(project, clip["id"])),
            "-vf",
            "select=eq(n\\,0)+eq(n\\,24)",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    assert len(raw) == 2 * 64 * 48 * 3
    red, blue = raw[:3], raw[64 * 48 * 3 : 64 * 48 * 3 + 3]
    assert red[0] > 200 and red[2] < 30
    assert blue[2] > 200 and blue[0] < 30


def test_import_reads_bounded_chunks_and_checks_configured_limits(
    tmp_path, media_files, monkeypatch
):
    from comfypilot.media import ImportAssetRequest, queue_import
    from comfypilot.settings import get_project, update_project

    project = initialize_project(tmp_path / "project")
    video, _ = media_files
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path != video:
            return stream

        class BoundedReader:
            def read(self, size=-1):
                assert 0 < size <= 1024 * 1024
                return stream.read(size)

            def __getattr__(self, name):
                return getattr(stream, name)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                stream.close()

        return BoundedReader()

    monkeypatch.setattr(Path, "open", guarded_open)
    imported(project, video, "video")
    current = get_project(project)
    update_project(
        project,
        current["revision"],
        {
            **current["config"],
            "import_limits": {"video_bytes": 1, "image_bytes": 1, "workflow_bytes": 1},
        },
    )
    with pytest.raises(ProjectError) as error:
        queue_import(project, ImportAssetRequest(source_path=str(video), kind="video"))
    assert error.value.code == "media_limit"


@pytest.mark.parametrize("defect", ["source_link", "storage_link", "staging_link", "directory"])
def test_media_paths_never_follow_symlinks_or_accept_special_sources(tmp_path, media_files, defect):
    from comfypilot.media import ImportAssetRequest, queue_import

    project = initialize_project(tmp_path / "project")
    video, _ = media_files
    outside = tmp_path / "outside"
    outside.mkdir()
    if defect == "source_link":
        link = tmp_path / "link.mp4"
        link.symlink_to(video)
        video = link
    elif defect in ("storage_link", "staging_link"):
        path = project.root / (
            ".comfypilot/artifacts/inputs" if defect == "storage_link" else ".comfypilot/staging"
        )
        if path.exists():
            path.rmdir()
        path.symlink_to(outside, target_is_directory=True)
    else:
        video = outside
    with pytest.raises(ProjectError):
        queue_import(project, ImportAssetRequest(source_path=str(video), kind="video"))
    assert list(outside.iterdir()) == []


def test_missing_tools_space_and_invalid_image_have_actionable_errors(
    tmp_path, media_files, monkeypatch
):
    from comfypilot.media import (
        ImportAssetRequest,
        get_media_job,
        process_next_media_job,
        queue_import,
    )

    project = initialize_project(tmp_path / "project")
    _, image = media_files
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "which", lambda _: None)
        with pytest.raises(ProjectError) as error:
            queue_import(project, ImportAssetRequest(source_path=str(image), kind="image"))
        assert error.value.code == "media_tools_missing"
    with monkeypatch.context() as patch:
        usage = shutil.disk_usage(project.root)
        patch.setattr(shutil, "disk_usage", lambda _: usage._replace(free=0))
        with pytest.raises(ProjectError) as error:
            queue_import(project, ImportAssetRequest(source_path=str(image), kind="image"))
        assert error.value.code == "storage_insufficient"
    image.write_bytes(image.read_bytes()[:40])
    job = queue_import(project, ImportAssetRequest(source_path=str(image), kind="image"))
    asyncio.run(process_next_media_job(project))
    result = get_media_job(project, job["id"])
    assert result["status"] == "failed" and result["error"]["code"] == "media_invalid"
    assert not list((project.root / ".comfypilot/staging").iterdir())


def test_worker_recovery_blocks_interrupted_jobs_and_removes_only_their_staging(
    tmp_path, media_files
):
    from comfypilot.media import ImportAssetRequest, get_media_job, queue_import, run_media_jobs

    project = initialize_project(tmp_path / "project")
    video, _ = media_files
    job = queue_import(project, ImportAssetRequest(source_path=str(video), kind="video"))
    token = "0123456789abcdef0123456789abcdef.part"
    staged = project.root / ".comfypilot/staging" / token
    staged.write_bytes(b"interrupted copy")
    job.update(status="running", staging_token=token)
    with sqlite3.connect(project.database) as db:
        db.execute(
            "UPDATE media_jobs SET status=?, document=? WHERE id=?",
            ("running", json.dumps(job), job["id"]),
        )

    async def recover():
        runner = asyncio.create_task(run_media_jobs(project))
        await asyncio.sleep(0.1)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    asyncio.run(recover())
    assert get_media_job(project, job["id"])["status"] == "blocked"
    assert not staged.exists()


def test_clip_rejects_non_grid_range_missing_reference_and_outside_duration(tmp_path, media_files):
    from comfypilot.media import ExtractClipRequest, queue_clip
    from pydantic import ValidationError

    project = initialize_project(tmp_path / "project")
    video, _ = media_files
    source_id = imported(project, video, "video")
    for start, end in [(2, 1), (0, 1.01), (float("nan"), 2)]:
        with pytest.raises(ValidationError):
            ExtractClipRequest(source_asset_id=source_id, start_seconds=start, end_seconds=end)
    with pytest.raises(ProjectError, match="时长"):
        queue_clip(
            project, ExtractClipRequest(source_asset_id=source_id, start_seconds=0, end_seconds=5)
        )
    with pytest.raises(ProjectError, match="参考"):
        queue_clip(
            project,
            ExtractClipRequest(
                source_asset_id=source_id,
                reference_asset_id=source_id,
                start_seconds=0,
                end_seconds=1,
            ),
        )
