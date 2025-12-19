from pathlib import Path

import backend.executor.executor as ex
from backend.executor.actions_schema import ActionPlan, ActionStep


def test_replan_success_sets_final_status(monkeypatch):
    # First wait_until fails, replan adds a successful wait_until.
    def fake_wait(step):
        if step.params.get("tag") == "fail":
            return {"status": "error", "reason": "timeout"}
        return {"status": "success", "ok": True, "condition": "ui_stable"}

    monkeypatch.setitem(ex.ACTION_HANDLERS, "wait_until", fake_wait)

    def fake_replan(**kwargs):
        return {
            "success": True,
            "plan": ActionPlan(
                task="replan",
                steps=[ActionStep(action="wait_until", params={"condition": "ui_stable", "tag": "success"})],
            ),
        }

    monkeypatch.setattr(ex, "_invoke_replan", fake_replan)

    plan = ActionPlan(
        task="replan-flow",
        steps=[ActionStep(action="wait_until", params={"condition": "ui_stable", "tag": "fail"})],
    )
    result = ex.run_steps(plan, work_dir=str(Path.cwd()), request_id="req-replan-1", consent_token=True)

    assert result["final_status"] == "success_with_replan"
    assert result["summary"]["final_status"] == "success_with_replan"
    assert result["summary"]["replans"]["count"] >= 1
    assert result["summary"]["recovered_failures"] >= 1
