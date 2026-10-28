import asyncio
import json
import math
import shutil
from fractions import Fraction
from pathlib import Path

from comfypilot.project import ProjectError


def require_tools() -> dict[str, str]:
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    if not all(tools.values()):
        raise ProjectError(
            "media_tools_missing",
            "缺少 ffmpeg/ffprobe，请在平台运行环境安装并加入 PATH 后重新提交。",
        )
    return tools


async def media_command(arguments: list[str], timeout: int = 300) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.CancelledError, TimeoutError):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
        raise
    if process.returncode:
        if b"No space left on device" in error:
            raise ProjectError(
                "storage_insufficient", "媒体处理空间不足，请释放项目磁盘空间后重试。"
            )
        raise ProjectError("media_invalid", "媒体无法完整解码或格式不支持，请检查原件及编码。")
    return output


async def inspect_media(path: Path, kind: str, *, count_frames: bool = False) -> dict:
    tools = require_tools()
    raw = await media_command(
        [
            tools["ffprobe"],
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,png_pipe,jpeg_pipe,webp_pipe,image2",
            *(["-count_frames"] if count_frames else []),
            "-show_entries",
            "format=format_name,duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,nb_read_frames,duration,start_time",
            "-of",
            "json",
            str(path),
        ],
        30,
    )
    try:
        document = json.loads(raw)
        stream = next(item for item in document["streams"] if item["codec_type"] == "video")
        codec = stream["codec_name"]
        width, height = int(stream["width"]), int(stream["height"])
        image = document["format"]["format_name"] in {
            "png_pipe",
            "jpeg_pipe",
            "webp_pipe",
            "image2",
        } and codec in {"png", "mjpeg", "webp"}
        if (kind == "image") != image or width <= 0 or height <= 0:
            raise ValueError
        fps = Fraction(stream.get("avg_frame_rate", "0/1"))
        if fps <= 0:
            fps = Fraction(stream["r_frame_rate"])
        duration = (
            None
            if image
            else float(stream.get("duration", document["format"].get("duration", "nan")))
        )
        if not image and (fps <= 0 or not math.isfinite(duration) or duration <= 0):
            raise ValueError
        frames = stream.get("nb_read_frames", stream.get("nb_frames"))
        frames = int(frames) if frames and frames != "N/A" else None
    except (ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError) as error:
        raise ProjectError(
            "media_invalid", "媒体类型、时长或画面信息无效，请提供视频或 PNG/JPEG/WebP 图片。"
        ) from error
    await media_command(
        [
            tools["ffmpeg"],
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,png_pipe,jpeg_pipe,webp_pipe,image2",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            *(["-frames:v", "1"] if image else []),
            "-f",
            "null",
            "-",
        ]
    )
    return {
        "codec": codec,
        "container": document["format"]["format_name"],
        "width": width,
        "height": height,
        "fps": str(fps) if fps.denominator != 1 else f"{fps}/1",
        "duration_seconds": duration,
        "frame_count": frames,
        "video_start_time": stream.get("start_time"),
        "audio_streams": sum(item["codec_type"] == "audio" for item in document["streams"]),
        "decoded": True,
    }


async def render_clip(source: Path, output: Path, settings: dict, limit: int) -> None:
    tools = require_tools()
    fps = settings["fps"]
    start, end = round(settings["start_seconds"] * fps), round(settings["end_seconds"] * fps)
    filters = (
        f"setpts=PTS-STARTPTS,fps={fps}:start_time=0,trim=start_frame={start}:end_frame={end},"
        f"setpts=PTS-STARTPTS,scale={settings['width']}:{settings['height']}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={settings['width']}:{settings['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    await media_command(
        [
            tools["ffmpeg"],
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-n",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-vf",
            filters,
            "-an",
            "-frames:v",
            str(end - start),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-fs",
            str(limit + 1),
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(output),
        ]
    )
