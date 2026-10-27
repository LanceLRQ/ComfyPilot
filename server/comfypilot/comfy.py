import asyncio

import httpx2
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from websockets.asyncio.client import connect

from comfypilot.project import ExecutionTarget


class TargetUnavailable(Exception):
    pass


class SystemStats(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    os: str | None = None
    comfyui_version: str | None = None
    ram_total: int | None = Field(default=None, ge=0)
    ram_free: int | None = Field(default=None, ge=0)


class DeviceStats(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    name: str | None = None
    vram_total: int | None = Field(default=None, ge=0)
    vram_free: int | None = Field(default=None, ge=0)


class Stats(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    system: SystemStats = Field(default_factory=SystemStats)
    devices: list[DeviceStats] | None = None


class Queue(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    queue_running: list | None = None
    queue_pending: list | None = None


class NodeInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    input: dict
    output: list


class EventData(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    node: str | None = None
    prompt_id: str | None = None
    value: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=1)


class ComfyEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    type: str
    data: EventData


class ConfiguredWebSocket(connect):
    def process_redirect(self, error: Exception) -> Exception:
        # 保持用户配置的物理目标，避免重定向时携带自定义认证头转往其他实例。
        return error


async def fetch_documents(target: ExecutionTarget, headers: dict[str, str], catalog: bool) -> dict:
    async with httpx2.AsyncClient(
        headers=headers,
        timeout=2,
        follow_redirects=False,
        trust_env=False,
    ) as client:

        async def read(path, model):
            async with asyncio.timeout(2):
                async with client.stream("GET", target.base_url + path) as response:
                    if response.status_code != 200:
                        raise TargetUnavailable("目标拒绝请求或接口不可用。")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 4 * 1024 * 1024:
                            raise TargetUnavailable("目标响应超过监测上限。")
            return (
                model.validate_json(content)
                if isinstance(model, TypeAdapter)
                else model.model_validate_json(content)
            )

        requests = {"stats": ("/system_stats", Stats), "queue": ("/queue", Queue)}
        if catalog:
            requests["nodes"] = ("/object_info", TypeAdapter(dict[str, NodeInfo]))
        results = await asyncio.gather(
            *(read(path, model) for path, model in requests.values()), return_exceptions=True
        )
        return dict(zip(requests, results, strict=True))
