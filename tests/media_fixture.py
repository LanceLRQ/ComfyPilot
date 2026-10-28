import subprocess

import pytest


@pytest.fixture
def media_files(tmp_path):
    video, image = tmp_path / "原片.mp4", tmp_path / "人物.png"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=red:size=64x48:rate=24:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=64x48:rate=24:duration=2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=green:size=32x32",
            "-frames:v",
            "1",
            str(image),
        ],
        check=True,
        capture_output=True,
    )
    return video, image
