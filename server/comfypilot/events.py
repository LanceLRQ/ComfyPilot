import asyncio
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Annotated

from fastapi import FastAPI, Header, Query
from fastapi.responses import StreamingResponse

from comfypilot.collection import read_versions
from comfypilot.execution import read_execution
from comfypilot.project import Project, ProjectError
from comfypilot.runtime import RuntimeRecord


def state_snapshot(project: Project) -> dict:
    with closing(sqlite3.connect(project.database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        state = read_execution(db)
        state.update(read_versions(db))
    for plan in state["plans"]:
        plan.pop("resources", None)
        plan.pop("targets", None)
    for run in state["runs"]:
        run["has_intent"] = run.pop("intent") is not None
        run["document"] = {
            key: value
            for key, value in run["document"].items()
            if key in {"step", "collection", "receipt", "failure", "dispatch_blocked"}
        }
        collection = run["document"].get("collection")
        if collection:
            collection.pop("outputs", None)
            collection.pop("checks", None)
            collection.pop("staging_token", None)
    for item in state["collections"]:
        for key in ("outputs", "checks", "staging_token"):
            item.pop(key, None)
    return state


def read_events(project: Project, after: int, limit: int) -> dict:
    if not 0 <= after <= 2**63 - 1 or not 1 <= limit <= 100:
        raise ProjectError("validation_error", "事件游标需非负，每页 1 至 100 项。")
    with closing(sqlite3.connect(project.database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT event_id,revision,action,object_id,source,created_at FROM business_events "
            "WHERE event_id>? ORDER BY event_id LIMIT ?",
            (after, limit + 1),
        ).fetchall()
    events = [dict(row) for row in rows[:limit]]
    return {
        "events": events,
        "cursor": events[-1]["event_id"] if events else after,
        "has_more": len(rows) > limit,
    }


def install_event_routes(
    app: FastAPI, project: Project, runtime: RuntimeRecord, stopping: Callable[[], bool] | None
) -> None:
    def reply(data):
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }

    @app.get("/api/state")
    def snapshot():
        return reply(state_snapshot(project))

    @app.get("/api/events")
    def events(
        after: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return reply(read_events(project, after, limit))

    @app.get("/api/events/stream")
    async def stream(
        after: int = Query(default=0, ge=0, le=2**63 - 1),
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        if last_event_id is not None:
            if (
                not last_event_id.isdecimal()
                or len(last_event_id) > 19
                or int(last_event_id) > 2**63 - 1
            ):
                raise ProjectError("validation_error", "Last-Event-ID 无效。")
            after = max(after, int(last_event_id))

        async def messages():
            cursor = after
            while not (stopping and stopping()):
                page = await asyncio.to_thread(read_events, project, cursor, 100)
                for event in page["events"]:
                    yield (
                        f"id: {event['event_id']}\nevent: business\n"
                        f"data: {json.dumps(reply(event))}\n\n"
                    )
                cursor = page["cursor"]
                if page["has_more"]:
                    continue
                state = await asyncio.to_thread(state_snapshot, project)
                yield (
                    "event: latest\ndata: "
                    + json.dumps(reply({"collections": state["collections"]}))
                    + "\n\n"
                )
                await asyncio.sleep(1)

        return StreamingResponse(
            messages(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
