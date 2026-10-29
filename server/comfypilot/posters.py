from pathlib import Path
from tempfile import TemporaryDirectory

from comfypilot.media import media_directory
from comfypilot.media_tools import media_command, require_tools
from comfypilot.project import Project, ProjectError


async def poster_path(project: Project, source: Path, seconds: float = 0) -> Path:
    cache = media_directory(project, "cache/posters")
    target = cache / f"{source.name}-{round(seconds * 1000)}.jpg"
    if target.is_symlink():
        raise ProjectError("media_path_invalid", "预览缓存不能是符号链接。")
    if target.is_file():
        return target
    tools = require_tools()
    with TemporaryDirectory(prefix="poster-", dir=cache) as directory:
        temporary = Path(directory) / "frame.jpg"
        await media_command(
            [
                tools["ffmpeg"],
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mov,matroska,png_pipe,jpeg_pipe,webp_pipe,image2",
                "-ss",
                str(seconds),
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=480:270:force_original_aspect_ratio=decrease",
                "-q:v",
                "4",
                str(temporary),
            ],
            timeout=15,
        )
        if not temporary.is_file():
            raise ProjectError("poster_unavailable", "当前媒体没有可读取的预览帧。")
        temporary.replace(target)
    return target
