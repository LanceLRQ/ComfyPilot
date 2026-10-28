import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID


def run_cli(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "comfypilot", *args],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_skill_path_locates_delivery_without_creating_project_or_home_files(tmp_path: Path):
    result = run_cli("skill-path", home=tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    skill = Path(data["path"])
    assert skill.is_absolute()
    assert data["name"] == "comfypilot"
    assert (skill / "SKILL.md").is_file()
    assert (skill / "references/operations.md").is_file()
    assert list(tmp_path.iterdir()) == []


def test_initialization_keeps_two_projects_separate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    identities = []
    for name in ["first project", "second-project"]:
        root = tmp_path / name
        result = run_cli("init", "--project-root", str(root), home=home)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        UUID(data["project_id"])
        identities.append(data["project_id"])
        assert data["root"] == str(root.resolve())
        assert (root / "comfypilot.yaml").is_file()
        assert (root / ".comfypilot/project.json").is_file()
        for directory in ["artifacts", "runs", "staging", "logs", "locks"]:
            assert (root / ".comfypilot" / directory).is_dir()
        for directory in ["workflows", "custom_nodes", "modules", "skills"]:
            assert (root / directory).is_dir()
        with sqlite3.connect(root / ".comfypilot/state.sqlite") as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
            assert db.execute("SELECT project_id FROM project").fetchall() == [
                (data["project_id"],)
            ]
    assert identities[0] != identities[1]
    assert list(home.iterdir()) == []


def test_reinitialization_preserves_identity_configuration_and_database(tmp_path: Path) -> None:
    root = tmp_path / "project"
    first = run_cli("init", "--project-root", str(root), home=tmp_path)
    assert first.returncode == 0, first.stderr
    config = root / "comfypilot.yaml"
    config.write_text("schema_version: 1\nname: 用户的项目\nserver:\n  port: 9876\n")
    original_config = config.read_bytes()
    original_metadata = (root / ".comfypilot/project.json").read_bytes()
    with sqlite3.connect(root / ".comfypilot/state.sqlite") as db:
        db.execute("CREATE TABLE user_marker (value TEXT)")
        db.execute("INSERT INTO user_marker VALUES ('preserve')")
    second = run_cli("init", "--project-root", str(root), home=tmp_path)
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["project_id"] == json.loads(second.stdout)["project_id"]
    assert json.loads(second.stdout)["name"] == root.name
    assert config.read_bytes() == original_config
    assert (root / ".comfypilot/project.json").read_bytes() == original_metadata
    with sqlite3.connect(root / ".comfypilot/state.sqlite") as db:
        assert db.execute("SELECT value FROM user_marker").fetchall() == [("preserve",)]


def test_relative_root_is_rejected_without_creating_project(tmp_path: Path) -> None:
    result = run_cli("init", "--project-root", "relative-project", home=tmp_path)
    assert result.returncode != 0
    assert json.loads(result.stderr)["code"] == "validation_error"
    assert not (Path.cwd() / "relative-project").exists()
    assert list(tmp_path.iterdir()) == []


def test_missing_root_is_rejected(tmp_path: Path) -> None:
    result = run_cli("init", home=tmp_path)
    assert result.returncode != 0
    assert "--project-root" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_existing_configuration_is_never_overwritten(tmp_path: Path) -> None:
    config = tmp_path / "comfypilot.yaml"
    config.write_text("用户已有的配置\n")
    result = run_cli("init", "--project-root", str(tmp_path), home=tmp_path)
    assert result.returncode != 0
    assert json.loads(result.stderr)["code"] == "project_conflict"
    assert config.read_text() == "用户已有的配置\n"
    assert not (tmp_path / ".comfypilot").exists()


def test_database_upgrade_preserves_legacy_worker_and_user_data(tmp_path: Path) -> None:
    first = run_cli("init", "--project-root", str(tmp_path), home=tmp_path)
    assert first.returncode == 0, first.stderr
    project_id = json.loads(first.stdout)["project_id"]
    with sqlite3.connect(tmp_path / ".comfypilot/state.sqlite") as db:
        db.execute("DROP TABLE IF EXISTS service_control")
        db.execute("DROP TABLE worker_status")
        db.execute(
            "CREATE TABLE worker_status (project_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, "
            "last_seen_at TEXT NOT NULL, status TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO worker_status VALUES (?, 123, '2026-10-27T00:00:00+00:00', 'stopped')",
            (project_id,),
        )
        db.execute("CREATE TABLE user_marker (value TEXT)")
        db.execute("INSERT INTO user_marker VALUES ('keep')")
        db.execute("PRAGMA user_version=1")
    for _ in range(2):
        result = run_cli("init", "--project-root", str(tmp_path), home=tmp_path)
        assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / ".comfypilot/state.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert db.execute("SELECT pid, instance_id FROM worker_status").fetchall() == [(123, "")]
        assert db.execute("SELECT value FROM user_marker").fetchall() == [("keep",)]
        assert db.execute("SELECT paused FROM service_control").fetchall() == [(1,)]
