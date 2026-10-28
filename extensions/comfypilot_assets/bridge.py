import asyncio
import json
import re
import shutil
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from aiohttp import web

CHUNK = 1024 * 1024
LIMIT = 64 * 1024**3
PREFIX = "/comfypilot/v1"


def create_routes(input_dir, output_dir) -> web.RouteTableDef:
    roots = {"input": Path(input_dir).resolve(), "output": Path(output_dir).resolve()}
    routes = web.RouteTableDef()
    locks = {}

    def managed_path(project, role, relative):
        parts = PurePosixPath(relative).parts
        if (
            role not in roots
            or not relative
            or len(relative) > 4096
            or PurePosixPath(relative).is_absolute()
            or ".." in parts
            or "\\" in relative
            or any(ord(c) < 32 for c in relative)
            or len(parts) < 3
            or parts[:2] != ("ComfyPilot", project)
        ):
            raise ValueError("invalid_path")
        path = roots[role].joinpath(*parts)
        if not roots[role].is_dir() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("invalid_path")
        if not path.resolve().is_relative_to(roots[role]):
            raise ValueError("invalid_path")
        return path

    async def proof(path):
        before = path.stat()
        if not path.is_file():
            raise ValueError("file_required")
        digest = sha256()
        with path.open("rb") as file:
            while chunk := file.read(CHUNK):
                digest.update(chunk)
                await asyncio.sleep(0)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise web.HTTPConflict(text="asset_changed")
        value = {
            "sha256": digest.hexdigest(),
            "size": after.st_size,
            "mtime_ns": after.st_mtime_ns,
            "inode": after.st_ino,
        }
        return {
            "content_hash": digest.hexdigest(),
            "size": after.st_size,
            "fingerprint": value,
            "fingerprint_id": sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(),
        }

    def ledger(project):
        return managed_path(project, "input", f"ComfyPilot/{project}/.bridge.sqlite")

    def remember(project, role, relative, record):
        path = ledger(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            **record,
            "role": role,
            "relative_path": relative,
            "managed": True,
            "cleanup_eligible": False,
        }
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS replicas(role TEXT, path TEXT, document TEXT, "
                "PRIMARY KEY(role,path))"
            )
            previous = db.execute(
                "SELECT document FROM replicas WHERE role=? AND path=?", (role, relative)
            ).fetchone()
            if (
                previous
                and not json.loads(previous[0]).get("deleted")
                and json.loads(previous[0])["fingerprint"] != record["fingerprint"]
            ):
                raise web.HTTPConflict(text="asset_changed")
            db.execute(
                "INSERT INTO replicas VALUES (?,?,?) ON CONFLICT(role,path) "
                "DO UPDATE SET document=excluded.document",
                (role, relative, json.dumps(record)),
            )
        return record

    async def handler(request, deletion=None):
        temporary = None
        try:
            if request.path == PREFIX + "/capabilities":
                return web.json_response(
                    {
                        "protocol": 1,
                        "namespace": "ComfyPilot",
                        "features": ["upload", "download", "stat", "known", "delete"],
                        "max_bytes": LIMIT,
                    }
                )
            project = request.match_info["project"]
            if str(UUID(project)) != project:
                raise ValueError("project_uuid_required")
            if request.method == "DELETE":
                body = deletion
                operation = body["operation_id"]
                if str(UUID(operation)) != operation:
                    raise ValueError("operation_uuid_required")
                role, relative = body["role"], body["path"]
                path = managed_path(project, role, relative)
                database = ledger(project)
                if not database.is_file():
                    raise web.HTTPConflict(text="unknown_file")
                request_hash = sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS deletions (id TEXT PRIMARY KEY, "
                        "request_hash TEXT NOT NULL, document TEXT NOT NULL)"
                    )
                    prior = db.execute(
                        "SELECT request_hash,document FROM deletions WHERE id=?", (operation,)
                    ).fetchone()
                    if prior:
                        if prior[0] != request_hash:
                            raise web.HTTPConflict(text="operation_mismatch")
                        return web.json_response(json.loads(prior[1]))
                    row = db.execute(
                        "SELECT document FROM replicas WHERE role=? AND path=?", (role, relative)
                    ).fetchone()
                    if not row:
                        raise web.HTTPConflict(text="unknown_file")
                    known = json.loads(row[0])
                    if known["fingerprint"] != body["fingerprint"]:
                        raise web.HTTPPreconditionFailed(text="asset_changed")
                    if path.exists():
                        current = await proof(path)
                        if current["fingerprint"] != body["fingerprint"]:
                            raise web.HTTPPreconditionFailed(text="asset_changed")
                        before = shutil.disk_usage(path.parent).free
                        path.unlink()
                        result = {
                            "status": "deleted",
                            "free_change_bytes": shutil.disk_usage(path.parent).free - before,
                        }
                    else:
                        result = {"status": "already_missing", "free_change_bytes": None}
                    known["deleted"] = True
                    db.execute(
                        "UPDATE replicas SET document=? WHERE role=? AND path=?",
                        (json.dumps(known), role, relative),
                    )
                    db.execute(
                        "INSERT INTO deletions VALUES (?,?,?)",
                        (operation, request_hash, json.dumps(result)),
                    )
                return web.json_response(result)
            if request.method == "PUT":
                filename = request.match_info["filename"]
                if not re.fullmatch(
                    r"[a-f0-9-]{36}\.(mp4|mov|mkv|webm|png|jpg|jpeg|webp)", filename
                ):
                    raise ValueError("asset_filename_required")
                UUID(filename.rsplit(".", 1)[0])
                digest = request.headers.get("X-Content-SHA256", "")
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("sha256_required")
                relative = f"ComfyPilot/{project}/{filename}"
                destination = managed_path(project, "input", relative)
                if destination.exists():
                    db_path = ledger(project)
                    if not db_path.is_file():
                        raise web.HTTPConflict(text="unknown_file")
                    with closing(sqlite3.connect(db_path)) as db:
                        row = db.execute(
                            "SELECT document FROM replicas WHERE role='input' AND path=?",
                            (relative,),
                        ).fetchone()
                    current = await proof(destination)
                    if not row or current["content_hash"] != digest:
                        raise web.HTTPConflict(text="unknown_or_changed_file")
                    return web.json_response(remember(project, "input", relative, current))
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.parent / (uuid4().hex + ".part")
                hashed, size = sha256(), 0
                with temporary.open("xb") as file:
                    async for chunk in request.content.iter_chunked(CHUNK):
                        size += len(chunk)
                        if size > LIMIT:
                            raise web.HTTPRequestEntityTooLarge(max_size=LIMIT, actual_size=size)
                        if shutil.disk_usage(destination.parent).free < len(chunk) + 16 * CHUNK:
                            raise web.HTTPInsufficientStorage()
                        hashed.update(chunk)
                        file.write(chunk)
                if hashed.hexdigest() != digest:
                    raise ValueError("content_hash_mismatch")
                destination.hardlink_to(temporary)
                return web.json_response(
                    remember(project, "input", relative, await proof(destination))
                )
            endpoint = request.path.rsplit("/", 1)[1]
            if endpoint == "known":
                path = ledger(project)
                if not path.is_file():
                    return web.json_response({"replicas": []})
                with closing(sqlite3.connect(path)) as db:
                    records = [
                        json.loads(row[0])
                        for row in db.execute(
                            "SELECT document FROM replicas ORDER BY rowid DESC LIMIT 1000"
                        )
                    ]
                return web.json_response({"replicas": records})
            relative = request.query.get("path", "")
            role = request.query.get("role", "output") if endpoint == "stat" else "output"
            path = managed_path(project, role, relative)
            current = await proof(path)
            if endpoint == "stat":
                return web.json_response(remember(project, role, relative, current))
            if request.headers.get("X-ComfyPilot-Fingerprint") != current["fingerprint_id"]:
                raise web.HTTPPreconditionFailed(text="asset_changed")
            response = web.StreamResponse(
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(current["size"]),
                }
            )
            await response.prepare(request)
            with path.open("rb") as file:
                while chunk := file.read(CHUNK):
                    await response.write(chunk)
            await response.write_eof()
            return response
        except FileNotFoundError:
            raise web.HTTPNotFound(text="asset_missing") from None
        except (ValueError, KeyError):
            raise web.HTTPBadRequest(text="invalid_request") from None
        except FileExistsError:
            raise web.HTTPConflict(text="asset_conflict") from None
        except OSError:
            raise web.HTTPServiceUnavailable(text="asset_io_unavailable") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def serialized(request):
        project = request.match_info.get("project", "")
        body = None
        if request.method == "DELETE":
            raw = await request.content.read(16385)
            try:
                body = json.loads(raw)
                if (
                    len(raw) > 16384
                    or not isinstance(body, dict)
                    or set(body) != {"operation_id", "role", "path", "fingerprint"}
                ):
                    raise ValueError
                if not all(
                    isinstance(body[key], str) for key in ("operation_id", "role", "path")
                ) or not isinstance(body["fingerprint"], dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text="invalid_delete_request") from None
            role, relative = body["role"], body["path"]
        elif request.method == "PUT":
            role, relative = "input", f"ComfyPilot/{project}/{request.match_info['filename']}"
        elif request.path.rsplit("/", 1)[1] in {"stat", "download"}:
            role = (
                request.query.get("role", "output") if request.path.endswith("/stat") else "output"
            )
            relative = request.query.get("path", "")
        else:
            return await handler(request, body)
        async with locks.setdefault((project, role, relative), asyncio.Lock()):
            return await handler(request, body)

    routes.get(PREFIX + "/capabilities")(serialized)
    base = PREFIX + "/projects/{project}"
    routes.put(base + "/input/{filename}")(serialized)
    routes.delete(base + "/delete")(serialized)
    for suffix in ("stat", "download", "known"):
        routes.get(base + "/" + suffix)(serialized)
    return routes
