"""Trace event schema reserved for the runtime tracing phase."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TraceStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRY = "RETRY"


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    node: str
    event_type: str
    input_summary: str | None = None
    output_summary: str | None = None
    start_time: datetime
    end_time: datetime
    latency_ms: int = Field(ge=0)
    status: TraceStatus
    error: str | None = None
