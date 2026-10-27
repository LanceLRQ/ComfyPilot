import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from comfypilot.cli import main


def test_network_filesystem_is_rejected_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import psutil

    monkeypatch.setattr(
        psutil,
        "disk_partitions",
        lambda **_: [
            SimpleNamespace(
                device="server:/share", mountpoint=str(tmp_path), fstype="nfs", opts="rw"
            )
        ],
    )
    root = tmp_path / "project"
    assert main(["init", "--project-root", str(root)]) != 0
    assert "unsupported_storage" in capsys.readouterr().err
    assert not root.exists()


def test_insufficient_space_is_rejected_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage._replace(free=0))
    root = tmp_path / "project"
    assert main(["init", "--project-root", str(root)]) != 0
    assert "storage_insufficient" in capsys.readouterr().err
    assert not root.exists()


def test_symlinked_database_cannot_write_outside_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    assert main(["init", "--project-root", str(root)]) == 0
    capsys.readouterr()
    database = root / ".comfypilot/state.sqlite"
    outside = tmp_path / "outside.sqlite"
    database.rename(outside)
    original = outside.read_bytes()
    database.symlink_to(outside)
    assert main(["init", "--project-root", str(root)]) != 0
    assert "validation_error" in capsys.readouterr().err
    assert outside.read_bytes() == original


def test_future_database_schema_is_not_downgraded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "--project-root", str(tmp_path)]) == 0
    with sqlite3.connect(tmp_path / ".comfypilot/state.sqlite") as db:
        db.execute("PRAGMA user_version=999")
    capsys.readouterr()
    assert main(["init", "--project-root", str(tmp_path)]) != 0
    assert "unsupported_schema" in capsys.readouterr().err
    with sqlite3.connect(tmp_path / ".comfypilot/state.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999
