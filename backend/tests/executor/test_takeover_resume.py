import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.executor import executor
from backend.executor.actions_schema import ActionPlan, ActionStep
from backend.executor.task_registry import get_task, TASK_REGISTRY, TaskStatus
from backend.executor.task_context import TaskContext


@pytest.fixture(autouse=True)
def clear_registry():
    TASK_REGISTRY.clear()
    yield
    TASK_REGISTRY.clear()


def test_takeover_creates_registry_and_resume(monkeypatch):
    # Make wait_until instant success and wait a no-op.
    monkeypatch.setitem(
        executor.ACTION_HANDLERS,
        "wait_until",
        lambda step: {"status": "success", "ok": True, "condition": "ui_stable"},
    )
    monkeypatch.setitem(
        executor.ACTION_HANDLERS,
        "wait",
        lambda step: {"status": "success", "ok": True},
    )

    plan = ActionPlan(
        task="demo",
        steps=[
            ActionStep(action="wait_until", params={"condition": "ui_stable"}),
            ActionStep(action="take_over", params={}),
            ActionStep(action="wait", params={"seconds": 0.1}),
        ],
    )
    context = TaskContext(user_instruction="demo")
    task_id = "test-task"

    result = executor.run_steps(plan, context=context, task_id=task_id)

    assert result["overall_status"] == "awaiting_user"
    record = get_task(task_id)
    assert record is not None
    assert record.status == TaskStatus.AWAITING_USER
    assert record.step_index == 2  # next step after take_over

    client = TestClient(app)
    status_resp = client.get(f"/api/tasks/{task_id}/status")
    assert status_resp.status_code == 200
    data = status_resp.json()
    assert data["status"] == TaskStatus.AWAITING_USER.value
    assert data["step_index"] == 2

    resume_resp = client.post(f"/api/tasks/{task_id}/resume", json={})
    assert resume_resp.status_code == 200
    resume_data = resume_resp.json()
    assert resume_data["overall_status"] == "success"

    final_record = get_task(task_id)
    assert final_record is not None
    assert final_record.status == TaskStatus.COMPLETED
    assert final_record.step_index == 3
