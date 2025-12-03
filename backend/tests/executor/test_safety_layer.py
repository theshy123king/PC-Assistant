import tempfile
from contextlib import contextmanager
from pathlib import Path

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionPlan, ActionStep
from backend.executor.task_context import TaskContext


@contextmanager
def _sandbox_dir():
    # Keep test files inside the repository so workspace checks pass.
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
        yield Path(tmp)


def test_run_steps_blocks_delete_without_confirm():
    with _sandbox_dir() as base:
        victim = base / "victim.txt"
        victim.write_text("danger")
        plan = ActionPlan(task="danger", steps=[ActionStep(action="delete_file", params={"path": str(victim)})])
        ctx = TaskContext(user_instruction="delete the file", max_replans=0)

        result = executor.run_steps(plan, context=ctx, allow_replan=False, max_retries=0)

        assert result["overall_status"] == "unsafe"
        assert victim.exists()
        log = result["logs"][0]
        assert log["status"] == "unsafe"
        assert log.get("safety", {}).get("code") == "confirm_required"


def test_run_steps_allows_delete_with_confirm():
    with _sandbox_dir() as base:
        victim = base / "ok.txt"
        victim.write_text("safe delete")
        plan = ActionPlan(
            task="delete",
            steps=[ActionStep(action="delete_file", params={"path": str(victim), "confirm": True})],
        )
        ctx = TaskContext(user_instruction="delete ok", max_replans=0)

        result = executor.run_steps(plan, context=ctx, allow_replan=False, max_retries=0)

        assert result["overall_status"] == "success"
        assert not victim.exists()
        assert result["logs"][0]["status"] == "success"


def test_dangerous_request_is_blocked_before_execution():
    plan = ActionPlan(task="noop", steps=[ActionStep(action="wait", params={"seconds": 0})])
    ctx = TaskContext(user_instruction="Please rm -rf the system drive", max_replans=0)

    result = executor.run_steps(plan, context=ctx, allow_replan=False, max_retries=0)

    assert result["overall_status"] == "unsafe"
    first_log = result["logs"][0]
    assert first_log["status"] == "unsafe"
    assert first_log.get("safety", {}).get("code") == "dangerous_request"
