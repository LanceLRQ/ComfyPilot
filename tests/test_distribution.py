import json
import subprocess
import sys
from pathlib import Path


def test_distribution_contains_locked_install_and_runtime_contracts(tmp_path):
    reply = subprocess.run(
        [sys.executable, "-m", "comfypilot", "distribution-path"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert reply.returncode == 0, reply.stderr
    folder = Path(json.loads(reply.stdout)["path"])
    assert all((folder / name).is_file() for name in ("requirements-runtime.txt", "protocols.json"))
    protocols = json.loads((folder / "protocols.json").read_text())
    assert protocols["database_user_version"] == 14
    assert protocols["asset_bridge"]["required_cleanup_feature"] == "delete"
    assert list(tmp_path.iterdir()) == []


def test_runtime_install_lock_matches_uv_lock():
    root = Path(__file__).parents[1]
    exported = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
        ],
        capture_output=True,
        text=True,
        cwd=root,
        check=True,
    )
    assert (root / "packages/distribution/requirements-runtime.txt").read_text() == exported.stdout
