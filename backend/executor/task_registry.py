"""
In-memory task registry for takeover/resume.

Note: This is single-process only. Do not run with multiple workers.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from enum import Enum

from backend.utils.time_utils import now_iso_utc


class TaskStatus(str, Enum):
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskRecord:
    task_id: str
    status: TaskStatus
    created_at: str
    updated_at: str
    user_text: Optional[str] = None
    plan: Dict[str, Any] = field(default_factory=dict)
    step_index: int = 0
    step_results: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    context_snapshot: Dict[str, Any] = field(default_factory=dict)
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


TASK_REGISTRY: Dict[str, TaskRecord] = {}
_LOCK = threading.Lock()


def create_task(
    user_text: Optional[str],
    plan: Dict[str, Any],
    status: TaskStatus = TaskStatus.RUNNING,
    task_id: Optional[str] = None,
) -> TaskRecord:
    now = now_iso_utc()
    record = TaskRecord(
        task_id=task_id or uuid.uuid4().hex,
        status=status,
        created_at=now,
        updated_at=now,
        user_text=user_text,
        plan=plan,
    )
    with _LOCK:
        TASK_REGISTRY[record.task_id] = record
    return record


def get_task(task_id: str) -> Optional[TaskRecord]:
    with _LOCK:
        return TASK_REGISTRY.get(task_id)


def update_task(task_id: str, **fields: Any) -> Optional[TaskRecord]:
    with _LOCK:
        record = TASK_REGISTRY.get(task_id)
        if not record:
            return None
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        record.updated_at = now_iso_utc()
        return record


def list_tasks(limit: int = 50) -> List[TaskRecord]:
    with _LOCK:
        values = list(TASK_REGISTRY.values())
    return values[:limit]


__all__ = ["TaskStatus", "TaskRecord", "TASK_REGISTRY", "create_task", "get_task", "update_task", "list_tasks"]
