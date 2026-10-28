import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from comfypilot.collection import read_versions
from comfypilot.execution import authorization_usage, read_execution
from comfypilot.lan import public_latest, public_state
from comfypilot.project import Project, ProjectError
from comfypilot.review import read_decisions, read_review_items
from comfypilot.runtime import RuntimeRecord


class EventsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    after: int = Field(default=0, ge=0, le=2**63 - 1)
    limit: int = Field(default=50, ge=1, le=100)


class WaitEventsRequest(EventsRequest):
    timeout_seconds: int = Field(default=20, ge=0, le=25)


def state_snapshot(project: Project) -> dict:
    with closing(sqlite3.connect(project.database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        state = read_execution(db)
        state.update(read_versions(db))
        state.update(read_review_items(db, unresolved=False))
        state.update(read_decisions(db))
        state["authorizations"] = [
            {"plan_id": plan["id"], **authorization_usage(db, plan["id"])}
            for plan in state["plans"]
            if plan["status"] == "active"
        ]
        state["activity"] = [
            dict(row)
            for row in db.execute(
                "SELECT event_id,revision,action,object_id,source,created_at "
                "FROM business_events ORDER BY event_id DESC LIMIT 20"
            )
        ]
    for plan in state["plans"]:
        plan.pop("resources", None)
        plan.pop("targets", None)
    for run in state["runs"]:
        intent = run.pop("intent")
        run["has_intent"] = intent is not None
        run["started_at"] = intent["created_at"] if intent else None
        run["references"] = {
            kind: f"/api/execution/runs/{run['id']}/evidence?kind={kind}"
            for kind in ("graph", "history", "checks")
        }
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
    app: FastAPI,
    project: Project,
    runtime: RuntimeRecord,
    stopping: Callable[[], bool] | None,
    monitoring: Callable[[], dict],
) -> None:
    def reply(data):
        return {
            "project_id": str(project.identity.project_id),
            "instance_id": str(runtime.instance_id),
            "data": data,
        }

    @app.get("/api/state")
    def snapshot(request: Request):
        state = {
            **state_snapshot(project),
            "monitoring": monitoring(),
            "local_control": request.state.local_control,
        }
        return reply(state if request.state.local_control else public_state(state))

    @app.get("/api/context")
    def context():
        from comfypilot.context import build_context

        return reply(build_context(project, state_snapshot(project), monitoring()))

    @app.post("/api/events/read")
    def read(body: EventsRequest):
        return reply(read_events(project, body.after, body.limit))

    @app.post("/api/events/wait")
    async def wait(body: WaitEventsRequest, request: Request):
        deadline = time.monotonic() + body.timeout_seconds
        while True:
            page = await asyncio.to_thread(read_events, project, body.after, body.limit)
            interrupted = bool(stopping and stopping()) or await request.is_disconnected()
            if page["events"] or time.monotonic() >= deadline or interrupted:
                return reply(
                    {
                        **page,
                        "timed_out": not page["events"] and not interrupted,
                        "interrupted": interrupted,
                    }
                )
            await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))

    @app.get("/api/events")
    def events(
        after: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return reply(read_events(project, after, limit))

    @app.get("/api/events/stream")
    async def stream(
        request: Request,
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
                latest = {"collections": state["collections"], "monitoring": monitoring()}
                if not request.state.local_control:
                    latest = public_latest(latest)
                yield ("event: latest\ndata: " + json.dumps(reply(latest)) + "\n\n")
                await asyncio.sleep(1)

        return StreamingResponse(
            messages(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
