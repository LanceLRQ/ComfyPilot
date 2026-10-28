import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from comfypilot.media import CHUNK_BYTES, check_space
from comfypilot.project import ExecutionTarget, Project, ProjectError
from comfypilot.settings import target_headers


class BridgeFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    mtime_ns: int
    inode: int


class BridgeRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    fingerprint: BridgeFingerprint
    fingerprint_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    relative_path: str
    role: str


def client_for(project: Project, target: dict) -> httpx2.AsyncClient:
    configured = ExecutionTarget.model_validate(target)
    headers = target_headers(project, configured)
    if configured.credential_ref and not headers:
        raise ProjectError("credential_missing", "素材桥接凭据未配置。")
    return httpx2.AsyncClient(
        base_url=configured.base_url + "/comfypilot/v1/",
        headers=headers,
        timeout=30,
        follow_redirects=False,
        trust_env=False,
    )


def check_status(code: int) -> None:
    if code == 200:
        return
    if code == 404:
        raise ProjectError("output_unavailable", "素材或桥接接口不存在，请核对安装与输出。")
    if code in {409, 412}:
        raise ProjectError("replica_changed", "远端文件未知或指纹变化，不覆盖原证据。")
    if code == 507:
        raise ProjectError("storage_insufficient", "执行端素材空间不足。")
    raise ProjectError("bridge_unavailable", "素材桥接请求未成功，请核对能力与连接。")


async def bridge_json(project: Project, target: dict, method: str, path: str, **arguments) -> dict:
    try:
        async with client_for(project, target) as client, asyncio.timeout(300):
            async with client.stream(method, path, **arguments) as response:
                check_status(response.status_code)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 4 * CHUNK_BYTES:
                        raise ProjectError("bridge_unavailable", "桥接元数据超过上限。")
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError
                return value
    except (httpx2.HTTPError, TimeoutError, ValueError) as error:
        raise ProjectError("bridge_unavailable", "桥接连接或元数据不可用。") from error


async def probe_bridge(project: Project, target: dict) -> dict:
    try:
        async with asyncio.timeout(2):
            value = await bridge_json(project, target, "GET", "capabilities")
    except TimeoutError as error:
        raise ProjectError("bridge_unavailable", "素材桥接能力探测超时。") from error
    except ProjectError as error:
        if error.code == "output_unavailable":
            raise ProjectError(
                "media_transfer_unavailable", "未安装兼容素材桥接，通用视频传输不可用。"
            ) from error
        raise
    if (
        value.get("protocol") != 1
        or value.get("namespace") != "ComfyPilot"
        or not isinstance(value.get("features"), list)
        or not all(isinstance(item, str) for item in value["features"])
        or not {"upload", "download", "stat", "known"}.issubset(value["features"])
    ):
        raise ProjectError("media_transfer_unavailable", "素材桥接协议或能力不兼容。")
    return value


async def remote_stat(project: Project, target: dict, role: str, relative: str) -> dict:
    path = f"projects/{project.identity.project_id}/stat?" + urlencode(
        {"role": role, "path": relative}
    )
    try:
        record = BridgeRecord.model_validate(
            await bridge_json(project, target, "GET", path)
        ).model_dump()
    except ValueError as error:
        raise ProjectError("bridge_unavailable", "桥接素材指纹格式不正确。") from error
    if record["role"] != role or record["relative_path"] != relative:
        raise ProjectError("replica_changed", "桥接返回的素材身份不一致。")
    return record


async def upload_input(
    project: Project, target: dict, source: Path, relative: str, digest: str
) -> dict:
    await probe_bridge(project, target)

    async def chunks():
        with source.open("rb") as file:
            while chunk := file.read(CHUNK_BYTES):
                yield chunk
                await asyncio.sleep(0)

    path = f"projects/{project.identity.project_id}/input/" + Path(relative).name
    await bridge_json(
        project, target, "PUT", path, content=chunks(), headers={"X-Content-SHA256": digest}
    )
    return await remote_stat(project, target, "input", relative)


async def download_output(
    project: Project, target: dict, replica: dict, destination: Path, progress
):
    path = f"projects/{project.identity.project_id}/download?" + urlencode(
        {"path": replica["relative_path"]}
    )
    size = 0
    try:
        async with client_for(project, target) as client, asyncio.timeout(300):
            async with client.stream(
                "GET", path, headers={"X-ComfyPilot-Fingerprint": replica["fingerprint_id"]}
            ) as response:
                check_status(response.status_code)
                with destination.open("xb") as file:
                    async for chunk in response.aiter_bytes(chunk_size=CHUNK_BYTES):
                        size += len(chunk)
                        if size > replica["size"]:
                            raise ProjectError("replica_changed", "远端输出超过保存的大小。")
                        check_space(destination.parent, len(chunk))
                        file.write(chunk)
                        progress(size)
        return (await remote_stat(project, target, "output", replica["relative_path"]))[
            "fingerprint"
        ]
    except (httpx2.HTTPError, TimeoutError) as error:
        raise ProjectError(
            "bridge_unavailable", "远端输出传输中断，保留检查点等待重试。"
        ) from error
