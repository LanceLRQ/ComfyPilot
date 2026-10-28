import asyncio
import socket
import time
from contextlib import contextmanager
from threading import Thread

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect


@contextmanager
def comfy_server(
    *, events=True, delay=0, partial=False, progress=True, disconnect=False, busy=True
):
    app = FastAPI()
    calls = []
    tokens = []

    @app.middleware("http")
    async def record(request, call_next):
        calls.append((request.method, request.url.path))
        tokens.append(request.headers.get("authorization"))
        return await call_next(request)

    @app.get("/system_stats")
    async def stats():
        await asyncio.sleep(delay)
        if partial:
            return {"system": {"comfyui_version": "fixture"}}
        return {
            "system": {
                "os": "linux",
                "ram_total": 10000,
                "ram_free": 6000,
                "comfyui_version": "fixture",
                "python_version": "fixture",
                "argv": ["--private-token=do-not-export"],
            },
            "devices": [
                {
                    "name": "fixture GPU",
                    "type": "cuda",
                    "index": 0,
                    "vram_total": 8000,
                    "vram_free": 0,
                    "torch_vram_total": 8000,
                    "torch_vram_free": 4000,
                }
            ],
        }

    @app.get("/queue")
    async def queue():
        return {
            "queue_running": [[0, "external-prompt", {"private": "do-not-export"}, {}, []]]
            if busy
            else [],
            "queue_pending": [],
        }

    @app.get("/object_info")
    async def nodes():
        return {
            "FixtureRender": {
                "input": {
                    "required": {
                        "video": ["STRING", {}],
                        "seed": ["INT", {"min": 0, "max": 1000}],
                        "prompt": ["STRING", {}],
                    }
                },
                "output": ["IMAGE"],
                "output_node": True,
            },
            "KSampler": {"input": {"required": {}}, "output": ["LATENT"], "category": "sampling"},
        }

    @app.websocket("/ws")
    async def websocket(connection: WebSocket):
        await connection.accept()
        if events:
            await connection.send_json(
                {"type": "executing", "data": {"node": "3", "prompt_id": "external-prompt"}}
            )
            if progress:
                await connection.send_json(
                    {
                        "type": "progress",
                        "data": {"value": 2, "max": 8, "node": "3", "prompt_id": "external-prompt"},
                    }
                )
        if disconnect:
            await connection.close()
            return
        try:
            while True:
                await connection.receive_text()
        except WebSocketDisconnect:
            pass

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", ws="websockets-sansio"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        try:
            yield address, calls, tokens
        finally:
            server.should_exit = True
            thread.join(timeout=6)
            assert not thread.is_alive()
