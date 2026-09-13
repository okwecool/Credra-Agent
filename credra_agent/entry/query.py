"""Latest task queries using public Checkpoint APIs and a disposable projection."""

import base64
import hashlib
import json

from app.cases import HIDDEN_DEMO_CASE_IDS, validate_case_id
from app.runtime.tasks import graph_config, open_checkpointer
from app.tools.artifacts import ArtifactStore
from credra_agent.entry.models import TaskPage, TaskSummary, TaskView
from credra_agent.entry.store import EntryConflict, EntryStore, now
from credra_agent.intent.catalog import load_subject_catalog
from credra_agent.intent.models import TaskSpec
from credra_agent.intent.store import IntentStore
from credra_agent.runtime.ui_store import task_lock

RESULT_FIELDS = (
    "observation_index_ref",
    "evidence_index_ref",
    "coverage_ref",
    "assessment_ref",
    "financial_ref",
    "report_ref",
    "report_draft_ref",
)


class TaskQueryService:
    def __init__(self, settings, *, allowed_subject_ids, scan_batch=200):
        if not 1 <= scan_batch <= 1000:
            raise ValueError("ENTRY_SCAN_BATCH_INVALID")
        self.settings = settings
        self.allowed_subject_ids = frozenset(allowed_subject_ids)
        self.scan_batch = scan_batch
        self.store = EntryStore(settings.checkpoint_db_path)
        self.intents = IntentStore(settings.checkpoint_db_path)
        self.catalog = {
            item.case_id: item for item in load_subject_catalog(settings.data_dir)
        }

    def _visible(self, summary):
        return summary.case_id not in HIDDEN_DEMO_CASE_IDS and (
            summary.subject_id is None or summary.subject_id in self.allowed_subject_ids
        )

    def _view(self, task_id, checkpoint=None, spec=None):
        values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
        case_id = values.get("case_id") or (spec.case_id if spec else None)
        if case_id is not None:
            validate_case_id(case_id)
        run_id = values.get("run_id")
        if values.get("task_spec_ref") and case_id and run_id:
            # A direct Runtime task may have artifacts without an IntentStore row.
            if not isinstance(run_id, str) or not run_id.isalnum():
                raise EntryConflict("ENTRY_RUN_REFERENCE_INVALID")
            spec = TaskSpec.model_validate(
                ArtifactStore(
                    self.settings.data_dir / case_id / "runs" / run_id
                ).read_json(values["task_spec_ref"])
            )
        record = self.catalog.get(case_id)
        subject_id = spec.subject_id if spec else record.subject_id if record else None
        subject_name = (
            spec.subject_name if spec else record.subject_name if record else ""
        )
        version = spec.version if spec else None
        checkpoint_id = (
            checkpoint.config["configurable"]["checkpoint_id"] if checkpoint else None
        )
        digest = hashlib.sha256(
            json.dumps(
                {"task": task_id, "checkpoint": checkpoint_id, "spec": version},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        summary = TaskSummary(
            task_id=task_id,
            case_id=case_id,
            subject_id=subject_id,
            subject_name=subject_name,
            title=f"{subject_name or '待澄清主体'} · {task_id}",
            years=sorted({period.end.year for period in spec.periods}) if spec else [],
            kind="DRAFT"
            if not checkpoint
            else "INVESTIGATION"
            if values.get("graph_version") == "agentic_v2"
            else "LEGACY",
            status=str(values.get("status") or (spec.readiness if spec else "UNKNOWN")),
            graph_version=values.get("graph_version"),
            run_id=run_id,
            spec_version=version,
            checkpoint_at=checkpoint.checkpoint.get("ts") if checkpoint else None,
            state_ref=f"task-state:{digest}",
        )
        errors = (
            [
                value
                for _, channel, value in checkpoint.pending_writes or []
                if channel == "__error__"
            ]
            if checkpoint
            else []
        )
        blocked = (
            "LOGGING_UNAVAILABLE"
            if any("LOGGING_UNAVAILABLE" in str(error) for error in errors)
            else "CHECKPOINT_ERROR"
            if errors
            else None
        )
        return TaskView(
            summary=summary,
            task_spec=spec,
            stop_reason=values.get("stop_reason") or values.get("finish_reason"),
            unresolved_fields=spec.unresolved_fields if spec else [],
            pending_writes=bool(checkpoint.pending_writes) if checkpoint else False,
            execution_blocked=blocked,
            result_refs={
                key: values[key]
                for key in RESULT_FIELDS
                if isinstance(values.get(key), str)
            },
        )

    def refresh(self):
        """Bounded Checkpoint backfill plus a new-head scan; never count versions as tasks."""
        with task_lock(self.settings.checkpoint_db_path, "entry-task-projection"):
            meta = self.store.query_meta()
            with open_checkpointer(self.settings.checkpoint_db_path) as saver:
                head = list(saver.list(None, limit=self.scan_batch + 1))
                if (
                    meta["complete"]
                    and len(head) > self.scan_batch
                    and (
                        meta["watermark"] is None
                        or head[self.scan_batch - 1].config["configurable"][
                            "checkpoint_id"
                        ]
                        > meta["watermark"]
                    )
                ):
                    meta.update(
                        complete=False,
                        cursor=head[self.scan_batch - 1].config["configurable"][
                            "checkpoint_id"
                        ],
                    )
                before = (
                    {"configurable": {"checkpoint_id": meta["cursor"]}}
                    if meta["cursor"]
                    else None
                )
                batch = (
                    list(saver.list(None, before=before, limit=self.scan_batch + 1))
                    if not meta["complete"] and before
                    else head
                )
                seen = set()
                for checkpoint in [*head[: self.scan_batch], *batch[: self.scan_batch]]:
                    config = checkpoint.config["configurable"]
                    if config.get("checkpoint_ns", ""):
                        continue
                    thread_id = config["thread_id"]
                    if thread_id in seen:
                        continue
                    seen.add(thread_id)
                    # Older backfill rows must not overwrite the latest task state.
                    latest = saver.get_tuple(graph_config(thread_id))
                    if latest is None:
                        continue
                    view = self._view(
                        thread_id, latest, self.intents.latest_task_spec(thread_id)
                    )
                    self.store.index(
                        view.summary, latest.config["configurable"]["checkpoint_id"]
                    )
                if not meta["complete"]:
                    last = (
                        batch[min(len(batch), self.scan_batch) - 1].config[
                            "configurable"
                        ]["checkpoint_id"]
                        if batch
                        else None
                    )
                    complete = len(batch) <= self.scan_batch or bool(
                        meta["watermark"] and last and last <= meta["watermark"]
                    )
                    meta.update(complete=complete, cursor=None if complete else last)
                    if complete:
                        meta["watermark"] = (
                            head[0].config["configurable"]["checkpoint_id"]
                            if head
                            else None
                        )
            # Streaming keeps legacy draft backfill memory bounded. Checkpoint state wins.
            for task_id, spec in self.intents.iter_latest_task_specs(
                batch_size=self.scan_batch
            ):
                self.store.index(self._view(task_id, spec=spec).summary)
            self.store.query_meta(meta)
        return meta["complete"]

    def get(self, task_id):
        with open_checkpointer(self.settings.checkpoint_db_path) as saver:
            checkpoint = saver.get_tuple(graph_config(task_id))
        spec = self.intents.latest_task_spec(task_id)
        if checkpoint is None and spec is None:
            raise EntryConflict("ENTRY_TASK_NOT_VISIBLE")
        view = self._view(task_id, checkpoint, spec)
        if not self._visible(view.summary):
            raise EntryConflict("ENTRY_TASK_NOT_VISIBLE")
        return view

    def list(self, *, subject_id=None, status=None, year=None, cursor=None, limit=20):
        if not 1 <= limit <= 50:
            raise EntryConflict("ENTRY_PAGE_LIMIT_INVALID")
        filters = json.dumps(
            [
                str(self.settings.checkpoint_db_path.resolve()),
                sorted(self.allowed_subject_ids),
                subject_id,
                status,
                year,
            ],
            sort_keys=True,
        )
        scope = hashlib.sha256(filters.encode()).hexdigest()
        after = None
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if (
                    set(decoded) != {"after", "scope"}
                    or decoded["scope"] != scope
                    or not isinstance(decoded["after"], list)
                    or len(decoded["after"]) != 2
                    or any(not isinstance(value, str) for value in decoded["after"])
                ):
                    raise ValueError
                after = decoded["after"]
            except (ValueError, TypeError, UnicodeError) as exc:
                raise EntryConflict("ENTRY_CURSOR_INVALID") from exc
        complete = self.refresh()
        candidates = self.store.indexed(
            allowed_subject_ids=self.allowed_subject_ids,
            hidden_cases=HIDDEN_DEMO_CASE_IDS,
            subject_id=subject_id,
            status=status,
            year=year,
            after=after,
            limit=limit + 1,
        )
        # Re-read visible results, without taking the long-running task dispatch lock.
        items = []
        changed = False
        for candidate in candidates[:limit]:
            try:
                item = self.get(candidate.task_id).summary
            except EntryConflict:
                changed = True
                continue
            if (
                (subject_id is not None and item.subject_id != subject_id)
                or (status is not None and item.status != status)
                or (year is not None and year not in item.years)
            ):
                changed = True
                continue
            items.append(item)
        more = len(candidates) > limit
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "after": [
                            candidates[limit - 1].checkpoint_at or "",
                            candidates[limit - 1].task_id,
                        ],
                        "scope": scope,
                    }
                ).encode()
            ).decode()
            if more
            else None
        )
        return TaskPage(
            items=items,
            observed_at=now(),
            next_cursor=next_cursor,
            has_more=more,
            backfill_complete=complete,
            limitations=[
                *(
                    ["历史 Checkpoint 回填尚未完成，当前结果不代表全部历史任务。"]
                    if not complete
                    else []
                ),
                *(
                    [
                        "查询期间任务状态或可见性发生变化，已剔除不再匹配的记录；结果为空不代表不存在匹配任务。"
                    ]
                    if changed
                    else []
                ),
            ],
        )

    def rebuild(self):
        with task_lock(self.settings.checkpoint_db_path, "entry-task-projection"):
            self.store.reset_projection()
        return self.refresh()
