import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from comfypilot.monitoring import get_target_status
from comfypilot.project import Project, ProjectError, dispatch_state
from comfypilot.recipes import content_hash
from comfypilot.resource_preflight import check_frozen_recipe
from comfypilot.resources import freeze_recipe
from comfypilot.settings import get_project, resolve_target

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
ContentHash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Parameter = bool | int | float | Annotated[str, Field(max_length=4096)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Step(StrictModel):
    segment_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    recipe_id: Identifier
    recipe_hash: ContentHash
    inputs: dict[Identifier, ContentHash] = Field(min_length=1, max_length=32)
    asset_ids: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    parameters: dict[Identifier, Parameter] = Field(max_length=64)
    target_id: Identifier
    required_nodes: list[Identifier] = Field(min_length=1, max_length=128)


class ExecutionMandate(StrictModel):
    instruction: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    source: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    allowed_recipes: dict[Identifier, ContentHash] = Field(min_length=1, max_length=32)
    allowed_targets: list[Identifier] = Field(min_length=1, max_length=32)
    allowed_inputs: dict[Identifier, ContentHash] = Field(min_length=1, max_length=32)
    allowed_parameters: dict[Identifier, list[Parameter]] = Field(max_length=64)
    max_submissions: int = Field(ge=1, le=100)
    stop_conditions: list[Literal["on_failure", "after_limit"]] = Field(min_length=1, max_length=2)


class Plan(StrictModel):
    task_id: Identifier
    steps: list[Step] = Field(min_length=1, max_length=100)
    mandate: ExecutionMandate
    feedback_ids: list[Identifier] = Field(
        default_factory=list, max_length=100, exclude_if=lambda value: not value
    )


class Command(StrictModel):
    command_id: str = Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    expected_revision: int = Field(ge=0)
    source: Literal["agent", "web_user"] = "agent"


class CreateTask(Command):
    action: Literal["create_task"] = "create_task"
    goal: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    acceptance: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class CreatePlan(Command, Plan):
    action: Literal["create_plan"] = "create_plan"
    new_instruction: bool = Field(default=False, exclude_if=lambda value: not value)


class ActivatePlan(Command):
    action: Literal["activate_plan"] = "activate_plan"
    plan_id: Identifier


class ExecutePlan(Command):
    action: Literal["execute_plan"] = "execute_plan"
    plan_id: Identifier
    action_indices: list[Annotated[int, Field(ge=0, le=99)]] = Field(min_length=1, max_length=100)


class StopPlan(Command):
    action: Literal["stop_plan"] = "stop_plan"
    plan_id: Identifier


class PausePlan(Command):
    action: Literal["pause_plan"] = "pause_plan"
    plan_id: Identifier


class ResumePlan(Command):
    action: Literal["resume_plan"] = "resume_plan"
    plan_id: Identifier


class CancelQueuedRuns(Command):
    action: Literal["cancel_queued_runs"] = "cancel_queued_runs"
    plan_id: Identifier
    run_ids: list[Identifier] = Field(min_length=1, max_length=100)


class PauseDispatch(Command):
    action: Literal["pause_dispatch"] = "pause_dispatch"


class ResumeDispatch(Command):
    action: Literal["resume_dispatch"] = "resume_dispatch"


class ResumeCollection(Command):
    action: Literal["resume_collection"] = "resume_collection"
    run_id: Identifier


class Region(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def contained(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("圈选区域必须在画面范围内")
        return self


class SaveFeedback(Command):
    action: Literal["save_feedback"] = "save_feedback"
    version_id: Identifier
    kind: Literal["note", "needs_changes"]
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    start_seconds: float = Field(default=0.0, ge=0)
    end_seconds: float = Field(default=0.0, ge=0)
    screenshot_id: Identifier | None = None
    regions: list[Region] = Field(default_factory=list, max_length=10)


class UpdateFeedback(Command):
    action: Literal["update_feedback"] = "update_feedback"
    feedback_id: Identifier
    status: Literal["addressed", "verified"]
    note: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    user_statement: str | None = Field(default=None, min_length=1, max_length=4000, pattern=r"\S")


class RecordUserDecision(Command):
    action: Literal["record_user_decision"] = "record_user_decision"
    version_id: Identifier
    decision: Literal["adopt", "reject"]
    user_statement: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class RecordEvaluation(Command):
    action: Literal["record_evaluation"] = "record_evaluation"
    version_id: Identifier
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")


AnyCommand = (
    CreateTask
    | CreatePlan
    | ActivatePlan
    | ExecutePlan
    | StopPlan
    | PauseDispatch
    | ResumeDispatch
    | ResumeCollection
    | SaveFeedback
    | UpdateFeedback
    | RecordUserDecision
    | RecordEvaluation
    | PausePlan
    | ResumePlan
    | CancelQueuedRuns
)
COMMAND_SCHEMA = TypeAdapter(Annotated[AnyCommand, Field(discriminator="action")])


def initialize_execution_schema(db: sqlite3.Connection) -> None:
    for statement in (
        "CREATE TABLE IF NOT EXISTS execution_control (singleton INTEGER "
        "PRIMARY KEY CHECK(singleton=1), "
        "revision INTEGER NOT NULL, review_hold INTEGER NOT NULL CHECK(review_hold IN (0,1)), "
        "reconciliation_required INTEGER NOT NULL CHECK(reconciliation_required IN (0,1)))",
        "INSERT OR IGNORE INTO execution_control VALUES (1, 0, 0, 0)",
        "CREATE TABLE IF NOT EXISTS execution_tasks (id TEXT PRIMARY KEY, goal TEXT NOT NULL, "
        "acceptance TEXT NOT NULL, active_plan_id TEXT, revision INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS execution_plans (id TEXT PRIMARY KEY, task_id TEXT NOT NULL "
        "REFERENCES execution_tasks(id), status TEXT NOT NULL CHECK(status IN "
        "('draft','active','superseded','stopped')), document TEXT NOT "
        "NULL, targets TEXT NOT NULL, "
        "used_submissions INTEGER NOT NULL DEFAULT 0 CHECK(used_submissions>=0))",
        "CREATE UNIQUE INDEX IF NOT EXISTS one_active_plan ON execution_plans(task_id) "
        "WHERE status='active'",
        "CREATE TABLE IF NOT EXISTS execution_runs (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL "
        "REFERENCES execution_plans(id), action_index INTEGER NOT NULL, status TEXT NOT NULL "
        "CHECK(status IN ('queued','cancelled','dispatching','submitted','running','reconciling',"
        "'collecting','succeeded','failed')), document TEXT NOT NULL, intent TEXT, "
        "UNIQUE(plan_id,action_index))",
        "CREATE UNIQUE INDEX IF NOT EXISTS one_active_generation ON execution_runs((1)) "
        "WHERE status IN ('dispatching','submitted','running','reconciling','collecting')",
        "CREATE TABLE IF NOT EXISTS business_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "revision INTEGER NOT NULL, action TEXT NOT NULL, object_id TEXT, source TEXT NOT NULL, "
        "document TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS command_receipts (command_id TEXT PRIMARY KEY, "
        "fingerprint TEXT NOT NULL, document TEXT NOT NULL)",
    ):
        db.execute(statement)


def get_execution(project: Project) -> dict:
    with closing(sqlite3.connect(project.database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        return read_execution(db)


def read_execution(db: sqlite3.Connection) -> dict:
    control = db.execute("SELECT * FROM execution_control").fetchone()
    state = {"revision": control["revision"], "dispatch": dispatch_state(db)}
    for name in ("tasks", "plans", "runs"):
        rows = [dict(row) for row in db.execute(f"SELECT * FROM execution_{name} ORDER BY rowid")]
        for row in rows:
            for key in ("document", "targets", "intent", "resources"):
                if row.get(key) is not None:
                    row[key] = json.loads(row[key])
        state[name] = rows
    state["event_cursor"] = db.execute(
        "SELECT COALESCE(MAX(event_id),0) FROM business_events"
    ).fetchone()[0]
    state["review_holds"] = [dict(row) for row in db.execute("SELECT * FROM review_holds")]
    state["paused_plan_ids"] = [row[0] for row in db.execute("SELECT plan_id FROM plan_pauses")]
    return state


def check_scope(plan: Plan) -> None:
    mandate = plan.mandate
    for step in plan.steps:
        if (
            mandate.allowed_recipes.get(step.recipe_id) != step.recipe_hash
            or step.target_id not in mandate.allowed_targets
            or any(mandate.allowed_inputs.get(key) != value for key, value in step.inputs.items())
            or any(
                not any(
                    type(value) is type(allowed) and value == allowed
                    for allowed in mandate.allowed_parameters.get(key, [])
                )
                for key, value in step.parameters.items()
            )
        ):
            raise ProjectError("mandate_exceeded", "步骤超出用户指示的配方、目标、输入或参数范围。")


def load_plan(db: sqlite3.Connection, plan_id: str):
    row = db.execute("SELECT * FROM execution_plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise ProjectError("plan_unavailable", "方案不存在，请重新读取当前项目。")
    return row, Plan.model_validate_json(row["document"])


def check_targets(project: Project, targets: dict) -> None:
    current = get_project(project)
    for target_id, snapshot in targets.items():
        target = next(
            (item for item in current["config"]["targets"] if item["id"] == target_id), None
        )
        if target != snapshot["target"] or not target["enabled"]:
            raise ProjectError("target_changed", "方案目标已改变或停用，请创建并生效新方案。")


def record_change(db, action: str, result: dict, source: str, object_id: str | None = None) -> dict:
    revision = db.execute(
        "UPDATE execution_control SET revision=revision+1 RETURNING revision"
    ).fetchone()[0]
    event_id = db.execute(
        "INSERT INTO business_events(revision,action,object_id,source,document,created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING event_id",
        (revision, action, object_id, source, json.dumps(result), datetime.now(UTC).isoformat()),
    ).fetchone()[0]
    return {"revision": revision, "event_id": event_id, "result": result}


def authorization_usage(db: sqlite3.Connection, plan_id: str) -> dict:
    root_id = db.execute(
        "SELECT authorization_id FROM execution_plans WHERE id=?", (plan_id,)
    ).fetchone()[0]
    _, root = load_plan(db, root_id)
    used = db.execute(
        "SELECT COALESCE(SUM(used_submissions),0) FROM execution_plans WHERE authorization_id=?",
        (root_id,),
    ).fetchone()[0]
    queued = db.execute(
        "SELECT COUNT(*) FROM execution_runs r JOIN execution_plans p ON p.id=r.plan_id "
        "WHERE p.authorization_id=? AND r.status='queued' AND r.intent IS NULL",
        (root_id,),
    ).fetchone()[0]
    return {
        "authorization_id": root_id,
        "max_submissions": root.mandate.max_submissions,
        "used_submissions": used,
        "queued_submissions": queued,
        "remaining_submissions": max(0, root.mandate.max_submissions - used - queued),
    }


def check_mandate_reuse(original: ExecutionMandate, proposed: ExecutionMandate) -> None:
    if (
        original.instruction != proposed.instruction
        or original.source != proposed.source
        or proposed.max_submissions > original.max_submissions
        or not set(proposed.allowed_targets).issubset(original.allowed_targets)
        or not set(original.stop_conditions).issubset(proposed.stop_conditions)
        or any(
            original.allowed_recipes.get(key) != value
            for key, value in proposed.allowed_recipes.items()
        )
        or any(
            original.allowed_inputs.get(key) != value
            for key, value in proposed.allowed_inputs.items()
        )
        or any(
            not any(
                type(value) is type(allowed) and value == allowed
                for allowed in original.allowed_parameters.get(key, [])
            )
            for key, values in proposed.allowed_parameters.items()
            for value in values
        )
    ):
        raise ProjectError(
            "new_instruction_required",
            "扩大或改变用户指示需要明确的新指示；沿用时可在原范围内收窄。",
        )


def apply_command(project: Project, command: AnyCommand) -> dict:
    fingerprint = sha256(
        json.dumps(command.model_dump(exclude={"expected_revision"}), sort_keys=True).encode()
    ).hexdigest()
    with closing(sqlite3.connect(project.database, timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT * FROM command_receipts WHERE command_id=?", (command.command_id,)
        ).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ProjectError(
                    "command_mismatch", "command_id 已用于其他请求，请使用新的命令 ID。"
                )
            return json.loads(previous["document"])
        revision = db.execute("SELECT revision FROM execution_control").fetchone()[0]
        if revision != command.expected_revision:
            raise ProjectError("conflict", "业务修订已变化，请保留草稿并重新读取。")
        object_id = None
        affected_run_ids = []
        if isinstance(command, CreateTask):
            object_id = str(uuid4())
            db.execute(
                "INSERT INTO execution_tasks VALUES (?, ?, ?, NULL, ?)",
                (object_id, command.goal, command.acceptance, revision + 1),
            )
            result = {"task_id": object_id}
        elif isinstance(command, CreatePlan):
            steps = [
                step
                if step.segment_id
                else step.model_copy(
                    update={"segment_id": step.asset_ids.get("video") or content_hash(step.inputs)}
                )
                for step in command.steps
            ]
            plan = Plan(
                task_id=command.task_id,
                steps=steps,
                mandate=command.mandate,
                feedback_ids=command.feedback_ids,
            )
            check_scope(plan)
            from comfypilot.cleanup import check_cleanup_claims

            check_cleanup_claims(
                db, {value for step in plan.steps for value in step.inputs.values()}
            )
            if not db.execute(
                "SELECT id FROM execution_tasks WHERE id=?", (command.task_id,)
            ).fetchone():
                raise ProjectError("task_unavailable", "任务不存在，请重新读取当前项目。")
            parent = db.execute(
                "SELECT p.id,p.document,p.authorization_id FROM execution_plans p "
                "JOIN execution_tasks t ON t.id=p.task_id WHERE p.task_id=? "
                "ORDER BY (p.id=t.active_plan_id) DESC,p.rowid DESC LIMIT 1",
                (command.task_id,),
            ).fetchone()
            if parent and not command.new_instruction:
                _, original = load_plan(db, parent["authorization_id"])
                check_mandate_reuse(original.mandate, plan.mandate)
            for feedback_id in plan.feedback_ids:
                feedback = db.execute(
                    "SELECT task_id FROM review_feedback WHERE id=?", (feedback_id,)
                ).fetchone()
                if not feedback or feedback[0] != plan.task_id:
                    raise ProjectError("feedback_mismatch", "关联反馈必须属于当前任务。")
            targets = {
                step.target_id: resolve_target(project, step.target_id) for step in plan.steps
            }
            resources = {}
            for index, step in enumerate(plan.steps):
                frozen = freeze_recipe(
                    project, step.recipe_id, step.recipe_hash, step.inputs, step.parameters
                )
                if step.asset_ids:
                    from comfypilot.media import get_asset

                    if set(step.asset_ids) != set(step.inputs) or any(
                        get_asset(project, asset_id)["content_hash"] != step.inputs[slot]
                        or get_asset(project, asset_id)["kind"]
                        != frozen["manifest"]["inputs"][slot]["kind"]
                        for slot, asset_id in step.asset_ids.items()
                    ):
                        raise ProjectError("asset_mismatch", "素材身份、类型与授权内容哈希不匹配。")
                frozen["asset_ids"] = step.asset_ids
                if set(step.required_nodes) != {
                    node["class_type"] for node in frozen["graph"].values()
                }:
                    raise ProjectError(
                        "recipe_binding_invalid", "所需节点必须与冻结 API 图完全一致。"
                    )
                resources[str(index)] = frozen
            object_id = str(uuid4())
            authorization_id = (
                parent["authorization_id"] if parent and not command.new_instruction else object_id
            )
            db.execute(
                "INSERT INTO execution_plans(id,task_id,status,document,targets,resources,"
                "parent_plan_id,authorization_id) "
                "VALUES (?,?,'draft',?,?,?,?,?)",
                (
                    object_id,
                    command.task_id,
                    plan.model_dump_json(),
                    json.dumps(targets),
                    json.dumps(resources),
                    parent["id"] if parent else None,
                    authorization_id,
                ),
            )
            result = {
                "plan_id": object_id,
                "status": "draft",
                "authorization_id": authorization_id,
                "new_instruction": command.new_instruction,
            }
        elif isinstance(command, (ActivatePlan, StopPlan, ExecutePlan)):
            row, plan = load_plan(db, command.plan_id)
            object_id = command.plan_id
            if isinstance(command, ActivatePlan):
                if row["status"] != "draft":
                    raise ProjectError("plan_unavailable", "只有草稿方案可以显式生效。")
                check_targets(project, json.loads(row["targets"]))
                from comfypilot.review import link_plan_feedback

                link_plan_feedback(db, command.plan_id, plan, command.source)
                cancelled = db.execute(
                    "UPDATE execution_runs SET status='cancelled' WHERE "
                    "status='queued' AND intent IS NULL "
                    "AND plan_id IN (SELECT id FROM execution_plans WHERE task_id=? "
                    "AND status='active') RETURNING id",
                    (plan.task_id,),
                ).fetchall()
                affected_run_ids = [item[0] for item in cancelled]
                db.execute(
                    "INSERT OR IGNORE INTO plan_pauses SELECT ?,q.source,q.created_at "
                    "FROM plan_pauses q JOIN execution_plans p ON p.id=q.plan_id "
                    "WHERE p.task_id=? AND p.status='active'",
                    (command.plan_id, plan.task_id),
                )
                db.execute(
                    "DELETE FROM plan_pauses WHERE plan_id IN "
                    "(SELECT id FROM execution_plans WHERE task_id=? AND status='active')",
                    (plan.task_id,),
                )
                db.execute(
                    "UPDATE execution_plans SET status='superseded' WHERE task_id=? "
                    "AND status='active'",
                    (plan.task_id,),
                )
                db.execute(
                    "UPDATE execution_plans SET status='active' WHERE id=?", (command.plan_id,)
                )
                db.execute(
                    "UPDATE execution_tasks SET active_plan_id=?, revision=? WHERE id=?",
                    (command.plan_id, revision + 1, plan.task_id),
                )
                result = {"plan_id": command.plan_id, "status": "active"}
            elif isinstance(command, StopPlan):
                db.execute("DELETE FROM plan_pauses WHERE plan_id=?", (command.plan_id,))
                db.execute(
                    "UPDATE execution_plans SET status='stopped' WHERE id=?", (command.plan_id,)
                )
                db.execute(
                    "UPDATE execution_tasks SET active_plan_id=NULL, revision=? WHERE "
                    "active_plan_id=?",
                    (revision + 1, command.plan_id),
                )
                affected = db.execute(
                    "UPDATE execution_runs SET status='cancelled' WHERE plan_id=? "
                    "AND status='queued' AND intent IS NULL RETURNING id",
                    (command.plan_id,),
                ).fetchall()
                result = {
                    "plan_id": command.plan_id,
                    "cancelled_run_ids": [item[0] for item in affected],
                }
                affected_run_ids = result["cancelled_run_ids"]
            else:
                if row["status"] != "active":
                    raise ProjectError("plan_unavailable", "只能执行当前生效的方案。")
                check_scope(plan)
                targets = json.loads(row["targets"])
                check_targets(project, targets)
                indices = list(dict.fromkeys(command.action_indices))
                resources = json.loads(row["resources"])
                if any(str(index) not in resources for index in indices):
                    raise ProjectError("resource_unavailable", "旧方案缺少冻结资源，请创建新方案。")
                if any(index >= len(plan.steps) for index in indices):
                    raise ProjectError("mandate_exceeded", "动作索引超出已确定的方案。")
                existing = {
                    item["action_index"]: item["id"]
                    for item in db.execute(
                        "SELECT action_index,id FROM execution_runs WHERE plan_id=?",
                        (command.plan_id,),
                    )
                }
                usage = authorization_usage(db, command.plan_id)
                added = sum(index not in existing for index in indices)
                if (
                    added > usage["remaining_submissions"]
                    or len(existing) + added > plan.mandate.max_submissions
                ):
                    raise ProjectError(
                        "mandate_exceeded", "同一用户指示跨修订的生成次数/额度不足。"
                    )
                for index in indices:
                    if index not in existing:
                        existing[index] = str(uuid4())
                        affected_run_ids.append(existing[index])
                        db.execute(
                            "INSERT INTO execution_runs VALUES (?,?,?,'queued',?,NULL)",
                            (
                                existing[index],
                                command.plan_id,
                                index,
                                json.dumps(
                                    {
                                        "step": plan.steps[index].model_dump(),
                                        "target_snapshot": targets[plan.steps[index].target_id],
                                        "resource_snapshot": resources[str(index)],
                                    }
                                ),
                            ),
                        )
                result = {
                    "plan_id": command.plan_id,
                    "run_ids": [existing[index] for index in indices],
                }
        elif isinstance(command, (PausePlan, ResumePlan, CancelQueuedRuns)):
            row, _ = load_plan(db, command.plan_id)
            object_id = command.plan_id
            if isinstance(command, CancelQueuedRuns):
                affected_run_ids = list(dict.fromkeys(command.run_ids))
                for run_id in affected_run_ids:
                    run = db.execute(
                        "SELECT * FROM execution_runs WHERE id=?", (run_id,)
                    ).fetchone()
                    if not run or run["plan_id"] != command.plan_id:
                        raise ProjectError("run_unavailable", "运行不属于指定方案。")
                    if run["status"] != "queued" or run["intent"] is not None:
                        raise ProjectError(
                            "run_not_cancellable", "只能取消等待中且无提交意图的运行。"
                        )
                db.executemany(
                    "UPDATE execution_runs SET status='cancelled' WHERE id=?",
                    [(run_id,) for run_id in affected_run_ids],
                )
                result = {"plan_id": command.plan_id, "cancelled_run_ids": affected_run_ids}
            else:
                if row["status"] != "active":
                    raise ProjectError("plan_unavailable", "只能暂停或恢复当前有效方案。")
                paused = isinstance(command, PausePlan)
                if paused:
                    db.execute(
                        "INSERT OR IGNORE INTO plan_pauses VALUES (?,?,?)",
                        (command.plan_id, command.source, datetime.now(UTC).isoformat()),
                    )
                else:
                    db.execute("DELETE FROM plan_pauses WHERE plan_id=?", (command.plan_id,))
                affected_run_ids = [
                    row[0]
                    for row in db.execute(
                        "SELECT id FROM execution_runs WHERE plan_id=? "
                        "AND status='queued' AND intent IS NULL",
                        (command.plan_id,),
                    )
                ]
                result = {"plan_id": command.plan_id, "paused": paused}
        elif isinstance(command, (RecordUserDecision, RecordEvaluation)):
            from comfypilot.review import apply_decision_command

            result, object_id = apply_decision_command(db, project, command)
        elif isinstance(command, (SaveFeedback, UpdateFeedback)):
            from comfypilot.review import apply_review_command

            result, object_id = apply_review_command(db, project, command)
        elif isinstance(command, ResumeCollection):
            run = db.execute(
                "SELECT status,document FROM execution_runs WHERE id=?", (command.run_id,)
            ).fetchone()
            if not run:
                raise ProjectError("run_unavailable", "运行不存在。")
            document = json.loads(run["document"])
            checkpoint = document.get("collection", {})
            if (
                run["status"] != "collecting"
                or not document.get("external_success")
                or checkpoint.get("phase") != "blocked"
            ):
                raise ProjectError(
                    "collection_not_resumable",
                    "仅可恢复已有外部成功且被阻塞的收片；技术失败不可绕过。",
                )
            checkpoint.update(
                phase="waiting_output",
                attempts=0,
                next_retry_at=None,
                error=None,
                recoveries=checkpoint.get("recoveries", 0) + 1,
            )
            document["collection"] = checkpoint
            db.execute(
                "UPDATE execution_runs SET document=? WHERE id=?",
                (json.dumps(document), command.run_id),
            )
            object_id = command.run_id
            affected_run_ids = [command.run_id]
            result = {
                "run_id": command.run_id,
                "phase": "waiting_output",
                "recoveries": checkpoint["recoveries"],
            }
        else:
            paused = isinstance(command, PauseDispatch)
            db.execute(
                "UPDATE service_control SET paused=?, reason=?, updated_at=? WHERE singleton=1",
                (
                    int(paused),
                    "manual_pause" if paused else "explicit_resume",
                    datetime.now(UTC).isoformat(),
                ),
            )
            result = {"manual_pause": paused, "dispatch": dispatch_state(db)}
            affected_run_ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM execution_runs WHERE status='queued' AND intent IS NULL"
                )
            ]
        receipt = {
            "command_id": command.command_id,
            "action": command.action,
            **record_change(db, command.action, result, command.source, object_id),
        }
        receipt["affected_steps"] = [
            {"run_id": row["id"], "plan_id": row["plan_id"], "action_index": row["action_index"]}
            for run_id in affected_run_ids
            for row in db.execute(
                "SELECT id,plan_id,action_index FROM execution_runs WHERE id=?", (run_id,)
            )
        ]
        receipt["control"] = {
            "dispatch": dispatch_state(db),
            "paused_plan_ids": [row[0] for row in db.execute("SELECT plan_id FROM plan_pauses")],
            "review_holds": [dict(row) for row in db.execute("SELECT * FROM review_holds")],
        }
        if plan_id := result.get("plan_id"):
            receipt["control"]["authorization"] = authorization_usage(db, plan_id)
        db.execute(
            "INSERT INTO command_receipts VALUES (?,?,?)",
            (command.command_id, fingerprint, json.dumps(receipt)),
        )
        return receipt


def reserve_submission(
    project: Project, run_id: str, instance_id: UUID, worker_pid: int, graph_hash: str
) -> dict:
    graph_hash = TypeAdapter(ContentHash).validate_python(graph_hash)
    with closing(sqlite3.connect(project.database, timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        run = db.execute("SELECT * FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise ProjectError("run_unavailable", "运行不存在。")
        if run["intent"] is not None:
            return {"run_id": run_id, "intent": json.loads(run["intent"]), "reused": True}
        row, plan = load_plan(db, run["plan_id"])
        if run["status"] != "queued" or row["status"] != "active":
            raise ProjectError("plan_unavailable", "运行或方案已停止，不能建立提交意图。")
        if db.execute("SELECT 1 FROM plan_pauses WHERE plan_id=?", (run["plan_id"],)).fetchone():
            raise ProjectError("dispatch_paused", "方案后续派发已暂停。")
        control = db.execute(
            "SELECT review_hold,reconciliation_required FROM execution_control"
        ).fetchone()
        held = db.execute("SELECT 1 FROM review_holds WHERE task_id=?", (plan.task_id,)).fetchone()
        legacy_hold = control[0] and not db.execute("SELECT 1 FROM review_holds").fetchone()
        if (
            held
            or legacy_hold
            or control[1]
            or db.execute("SELECT paused FROM service_control").fetchone()[0]
        ):
            raise ProjectError("dispatch_paused", "派发仍被手动暂停、审片挂起或待核对状态阻止。")
        if db.execute(
            "SELECT id FROM execution_runs WHERE status IN "
            "('dispatching','submitted','running','reconciling')"
        ).fetchone():
            raise ProjectError("dispatch_busy", "项目已有在途生成，串行等待。")
        usage = authorization_usage(db, run["plan_id"])
        if (
            usage["used_submissions"] >= usage["max_submissions"]
            or row["used_submissions"] >= plan.mandate.max_submissions
        ):
            raise ProjectError("mandate_exceeded", "提交额度已耗尽；不明提交仍占用额度。")
        if (
            "on_failure" in plan.mandate.stop_conditions
            and db.execute(
                "SELECT r.id FROM execution_runs r JOIN execution_plans p ON p.id=r.plan_id "
                "WHERE p.authorization_id=? AND r.status='failed'",
                (usage["authorization_id"],),
            ).fetchone()
        ):
            raise ProjectError("dispatch_paused", "用户指示在失败后停止，请重新取得执行指示。")
        check_scope(plan)
        targets = json.loads(row["targets"])
        check_targets(project, targets)
        step = plan.steps[run["action_index"]]
        from comfypilot.cleanup import check_cleanup_claims

        check_cleanup_claims(db, set(step.inputs.values()))
        snapshot = get_target_status(project, instance_id, worker_pid, step.target_id)
        if (
            snapshot["connection"]["availability"] != "fresh"
            or snapshot["connection"]["value"] is not True
        ):
            raise ProjectError(
                "capability_unavailable", "执行端状态未知、过期或不可连接，请重新探测。"
            )
        if snapshot["capabilities"]["execute"]["status"] != "available" or not set(
            step.required_nodes
        ).issubset(snapshot.get("node_classes", [])):
            raise ProjectError("capability_unavailable", "执行端缺少生成能力或方案所需节点。")
        for name in ("queue_running", "queue_pending"):
            metric = snapshot["metrics"][name]
            if metric["availability"] != "fresh" or metric["value"] != 0:
                raise ProjectError(
                    "external_queue_busy", "外部队列非空或未知，等待且不接管其他任务。"
                )
        document = json.loads(run["document"])
        frozen = document.get("resource_snapshot")
        if materialized := document.get("materialized"):
            if content_hash(materialized["graph"]) != materialized["graph_hash"]:
                raise ProjectError("resource_changed", "最终提交图与保存哈希不一致。")
            frozen = {
                **frozen,
                "graph": materialized["graph"],
                "graph_hash": materialized["graph_hash"],
            }
        if frozen is None or graph_hash != frozen["graph_hash"]:
            raise ProjectError("resource_changed", "提交图哈希与运行冻结资源不一致。")
        preflight = check_frozen_recipe(frozen, snapshot)
        if preflight["status"] != "passed":
            raise ProjectError(
                "capability_unavailable", "冻结配方未通过当前目标的标准 schema 预检。"
            )
        intent = {
            "client_id": str(uuid4()),
            "graph_hash": graph_hash,
            "created_at": datetime.now(UTC).isoformat(),
            "target_snapshot": targets[step.target_id],
            "preflight": preflight,
        }
        db.execute(
            "UPDATE execution_runs SET status='dispatching',intent=? WHERE id=?",
            (json.dumps(intent), run_id),
        )
        db.execute(
            "UPDATE execution_plans SET used_submissions=used_submissions+1 WHERE id=?",
            (run["plan_id"],),
        )
        result = {"run_id": run_id, "intent": intent, "reused": False}
        return {**result, **record_change(db, "submission_reserved", result, "worker", run_id)}
