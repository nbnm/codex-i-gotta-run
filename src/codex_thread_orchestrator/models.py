from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ClientInfo(Model):
    name: str = "codex_thread_orchestrator"
    title: str = "Codex Thread Orchestrator"
    version: str = "0.1.0"


class ThreadStatus(Model):
    type: str = "unknown"
    active_flags: list[str] = Field(default_factory=list, alias="activeFlags")


class PlanEntry(Model):
    step: str
    status: Literal["pending", "inProgress", "completed"]


class AppServerTurn(Model):
    id: str
    status: str = "unknown"
    items: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None
    error: dict[str, Any] | None = None
    thread_id: str | None = Field(default=None, alias="threadId")


class AppServerThread(Model):
    id: str
    name: str | None = None
    preview: str | None = None
    cwd: str | None = None
    source_kind: str | None = Field(default=None, alias="sourceKind")
    model_provider: str | None = Field(default=None, alias="modelProvider")
    created_at: int | float | None = Field(default=None, alias="createdAt")
    updated_at: int | float | None = Field(default=None, alias="updatedAt")
    ephemeral: bool | None = None
    archived: bool = False
    status: ThreadStatus = Field(default_factory=ThreadStatus)
    turns: list[AppServerTurn] = Field(default_factory=list)


class ThreadRecord(Model):
    thread_id: str
    name: str | None = None
    preview: str | None = None
    cwd: str | None = None
    source_kind: str | None = None
    model_provider: str | None = None
    created_at: int | float | None = None
    updated_at: int | float | None = None
    status_type: str = "unknown"
    status_payload: dict[str, Any] = Field(default_factory=dict)
    active_turn_id: str | None = None
    last_seen_at: str = Field(default_factory=utc_now_iso)
    archived: bool = False
    raw_thread: dict[str, Any] = Field(default_factory=dict)


class TurnRecord(Model):
    turn_id: str
    thread_id: str | None = None
    status: str = "unknown"
    started_at: str | None = None
    completed_at: str | None = None
    summary: str | None = None
    error_json: dict[str, Any] | None = None
    diff: str | None = None
    plan: list[PlanEntry] = Field(default_factory=list)
    raw_turn: dict[str, Any] = Field(default_factory=dict)


class EventRecord(Model):
    id: str
    thread_id: str | None = None
    turn_id: str | None = None
    event_type: str
    payload_json: dict[str, Any]
    received_at: str = Field(default_factory=utc_now_iso)


class QueuedInputRecord(Model):
    id: str
    thread_id: str
    mode: Literal["steer", "continue", "auto"] = "auto"
    text: str
    status: Literal["queued", "submitted", "done", "failed", "cancelled"] = "queued"
    created_at: str = Field(default_factory=utc_now_iso)
    submitted_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    action_taken: str | None = None
    turn_id: str | None = None


class ConnectionState(Model):
    app_server_instance: str | None = None
    initialized_at: str | None = None
    last_event_at: str | None = None
    last_error: str | None = None
    platform_family: str | None = None
    platform_os: str | None = None
    user_agent: str | None = None


class InitializeResponse(Model):
    user_agent: str | None = Field(default=None, alias="userAgent")
    platform_family: str | None = Field(default=None, alias="platformFamily")
    platform_os: str | None = Field(default=None, alias="platformOs")


class ListThreadsResponse(Model):
    data: list[AppServerThread] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class ThreadEnvelope(Model):
    thread: AppServerThread


class TurnEnvelope(Model):
    turn: AppServerTurn


class SteerResult(Model):
    turn_id: str = Field(alias="turnId")


class UnsubscribeResult(Model):
    status: str


class JsonRpcErrorPayload(Model):
    code: int
    message: str
    data: dict[str, Any] | None = None

