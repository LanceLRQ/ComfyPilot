import asyncio
import socket
import time
from contextlib import contextmanager
from threading import Thread

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response


@contextmanager
def comfy_server(
    *,
    events=True,
    delay=0,
    partial=False,
    progress=True,
    disconnect=False,
    busy=True,
    catalog=None,
    generation=None,
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
        if generation is not None:
            return {"queue_running": generation.get("running", []), "queue_pending": []}
        return {
            "queue_running": [[0, "external-prompt", {"private": "do-not-export"}, {}, []]]
            if busy
            else [],
            "queue_pending": [],
        }

    @app.post("/prompt")
    async def prompt(request: Request):
        body = await request.json()
        generation.setdefault("posts", []).append(body)
        if generation.get("reject"):
            return JSONResponse({"error": {"type": "prompt_invalid"}, "node_errors": {}}, 400)
        identifier = "fixture-prompt" if len(generation["posts"]) == 1 else "fixture-prompt-2"
        row = [0, identifier, body["prompt"], body["extra_data"], ["1"]]
        generation["running"] = [row]
        generation["prompt"] = row
        if generation.get("lost_receipt"):
            return Response("{", media_type="application/json")
        return {"prompt_id": identifier, "number": 0, "node_errors": {}}

    @app.get("/history")
    @app.get("/history/{prompt_id}")
    async def history(prompt_id: str | None = None):
        generation["history_reads"] = generation.get("history_reads", 0) + 1
        if generation.get("race") and generation["history_reads"] == 1:
            return {}
        return generation.get("history", {})

    @app.get("/object_info")
    async def nodes():
        if catalog is not None:
            return catalog
        return {
            "FixtureRender": {
                "input": {
                    "optional": {"filename_prefix": ["STRING", {}]}
                    if generation is not None
                    else {},
                    "required": {
                        "video": ["STRING", {}],
                        "seed": ["INT", {"min": 0, "max": 1000}],
                        "prompt": ["STRING", {}],
                    },
                },
                "output": ["IMAGE"],
                "output_node": True,
            },
            "KSampler": {"input": {"required": {}}, "output": ["LATENT"], "category": "sampling"},
        }

    @app.websocket("/ws")
    async def websocket(connection: WebSocket):
        await connection.accept()
        if events and generation is None:
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
            if generation is not None:
                client_id = connection.query_params.get("clientId")
                generation.setdefault("websocket_clients", []).append(client_id)
                sent = False
                while True:
                    try:
                        await asyncio.wait_for(connection.receive_text(), timeout=0.05)
                    except TimeoutError:
                        posts = generation.get("posts", [])
                        if posts and client_id == posts[-1]["client_id"] and not sent:
                            await connection.send_json(
                                {
                                    "type": "executing",
                                    "data": {"node": "1", "prompt_id": "fixture-prompt"},
                                }
                            )
                            await connection.send_json(
                                {
                                    "type": "progress",
                                    "data": {
                                        "value": 2,
                                        "max": 8,
                                        "node": "1",
                                        "prompt_id": "fixture-prompt",
                                    },
                                }
                            )
                            sent = True
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
