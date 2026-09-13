"""Versioned conversation entry contracts, separate from investigation actions."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from credra_agent.intent.models import IntentDraft, TaskSpec


class EntryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSummary(EntryModel):
    task_id: str
    case_id: str | None = None
    subject_id: str | None = None
    subject_name: str = ""
    title: str
    years: list[int] = Field(default_factory=list)
    kind: Literal["DRAFT", "INVESTIGATION", "LEGACY"]
    status: str = "UNKNOWN"
    graph_version: str | None = None
    run_id: str | None = None
    spec_version: int | None = None
    checkpoint_at: str | None = None
    state_ref: str


class TaskView(EntryModel):
    summary: TaskSummary
    task_spec: TaskSpec | None = None
    stop_reason: str | None = None
    unresolved_fields: list[str] = Field(default_factory=list)
    pending_writes: bool = False
    execution_blocked: Literal["LOGGING_UNAVAILABLE", "CHECKPOINT_ERROR"] | None = None
    result_refs: dict[str, str] = Field(default_factory=dict)


class TaskPage(EntryModel):
    items: list[TaskSummary]
    observed_at: datetime
    scope: Literal["LOCAL_WORKSPACE"] = "LOCAL_WORKSPACE"
    next_cursor: str | None = None
    has_more: bool = False
    backfill_complete: bool
    limitations: list[str] = Field(default_factory=list)


class EntryReply(EntryModel):
    decision: Literal["reply"]
    text: str = Field(min_length=1, max_length=4000)
    fact_refs: list[str] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class EntryAskUser(EntryModel):
    decision: Literal["ask_user"]
    question: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=10)
    missing_fields: list[str] = Field(default_factory=list, max_length=20)


class EntryCallTool(EntryModel):
    decision: Literal["call_tool"]
    tool: str = Field(min_length=1, max_length=80)
    arguments: dict
    reason: str = Field(min_length=1, max_length=1000)


class EntryDecision(
    RootModel[
        Annotated[
            EntryReply | EntryAskUser | EntryCallTool, Field(discriminator="decision")
        ]
    ]
):
    pass


class EntryToolResult(EntryModel):
    schema_version: Literal["entry_tool_result_v1"] = "entry_tool_result_v1"
    call_id: str
    tool: str
    status: Literal[
        "SUCCESS",
        "NO_RESULT",
        "WAITING_CLARIFICATION",
        "ACCEPTED",
        "UNAVAILABLE",
        "REJECTED",
        "LIMITED",
        "UNKNOWN",
    ]
    data: dict = Field(default_factory=dict)
    fact_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    observed_at: datetime
    state_ref: str | None = None
    next_cursor: str | None = None
    external_requests: Literal[0] = 0


class EntryPermissions(EntryModel):
    allowed_subject_ids: list[str]
    entry_authorized: bool = False
    entry_policy_ref: str | None = None
    conversation_remaining_requests: int | None = Field(default=None, ge=0)
    conversation_remaining_tokens: int | None = Field(default=None, ge=0)
    task_policy_ref: str | None = None
    task_remaining_requests: int | None = Field(default=None, ge=0)
    task_remaining_tokens: int | None = Field(default=None, ge=0)
    investigation_control_enabled: bool = False
    logging_healthy: bool = False
    request_profile_compatible: bool = False


class EntryContext(EntryModel):
    context_version: Literal["entry_context_v1"] = "entry_context_v1"
    conversation_id: str
    message_id: str
    query: str = Field(min_length=1)
    anchor_date: date
    timezone: str
    snapshot_at: datetime
    history: list[dict]
    selected_task_id: str | None
    current_task: TaskView | None
    pending_question: EntryAskUser | None
    task_page: TaskPage
    imported_cases: list[dict]
    case_catalog_has_more: bool = False
    case_catalog_next_cursor: str | None = None
    tools: list[dict]
    tool_contracts: dict[str, dict]
    investigation_capabilities: list[dict]
    permissions: EntryPermissions
    limitations: list[str] = Field(default_factory=list)


class ListTasksArgs(EntryModel):
    subject_id: str | None = None
    status: str | None = Field(default=None, max_length=80)
    year: int | None = Field(default=None, ge=1900, le=9999)
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=20, ge=1, le=50)


class TaskArgs(EntryModel):
    task_id: str = Field(min_length=1, max_length=200)


class ListCasesArgs(EntryModel):
    subject_hint: str | None = Field(default=None, max_length=200)
    year: int | None = Field(default=None, ge=1900, le=9999)
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=20, ge=1, le=50)


class TaskResultArgs(TaskArgs):
    section: Literal["summary", "references"] = "summary"
    reference_id: str | None = Field(default=None, max_length=200)


class PrepareInvestigationArgs(EntryModel):
    draft: IntentDraft
    case_id: str | None = Field(default=None, max_length=64)


class ClarifyArgs(TaskArgs):
    expected_spec_version: int = Field(ge=1)
    draft: IntentDraft


class ResumeArgs(TaskArgs):
    expected_state_ref: str = Field(min_length=1, max_length=200)
