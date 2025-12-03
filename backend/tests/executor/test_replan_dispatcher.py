from backend.executor.actions_schema import ActionPlan, ActionStep
from backend.executor.executor import run_steps
from backend.executor.task_context import TaskContext


def test_run_steps_invokes_replan_and_appends_steps():
    original_plan = ActionPlan(
        task="demo",
        steps=[ActionStep(action="wait", params={"seconds": -1})],
    )
    replanned_plan = ActionPlan(
        task="replan",
        steps=[ActionStep(action="wait", params={"seconds": 0})],
    )

    called = {}

    def planner_override(prompt_bundle):
        called["prompt"] = prompt_bundle.prompt_text
        return replanned_plan

    context = TaskContext(user_instruction="demo task", max_replans=1)
    result = run_steps(
        original_plan,
        context=context,
        planner_override=planner_override,
        planner_provider="deepseek",
        capture_replan_screenshot=False,
    )

    assert called, "planner override should be invoked on failure"
    assert context.replan_count == 1
    assert any(log.get("replan", {}).get("success") for log in result["logs"])
    assert len(result["logs"]) >= 2  # original failure + appended success step
    assert result["logs"][-1]["status"] == "success"
    assert result["overall_status"] in {"replanned", "success"}
