from datetime import datetime

import pytest

from backend.executor import executor
from backend.executor.actions_schema import ActionPlan, ActionStep
from backend.executor.task_context import TaskContext
from backend.executor.task_registry import TASK_REGISTRY, get_task


@pytest.fixture(autouse=True)
def clear_registry():
    TASK_REGISTRY.clear()
    yield
    TASK_REGISTRY.clear()


def test_task_and_logs_use_timezone_aware_iso(monkeypatch):
    monkeypatch.setitem(
        executor.ACTION_HANDLERS,
        "wait",
        lambda step: {"status": "success", "ok": True},
    )

    plan = ActionPlan(task="ts", steps=[ActionStep(action="wait", params={"seconds": 0})])
    context = TaskContext(user_instruction="ts")
    task_id = "ts-task"

    result = executor.run_steps(plan, context=context, task_id=task_id)

    record = get_task(task_id)
    assert record is not None

    created = datetime.fromisoformat(record.created_at)
    updated = datetime.fromisoformat(record.updated_at)
    log_ts = datetime.fromisoformat(result["logs"][0]["timestamp"])

    assert created.tzinfo is not None
    assert updated.tzinfo is not None
    assert log_ts.tzinfo is not None
    assert created <= updated
