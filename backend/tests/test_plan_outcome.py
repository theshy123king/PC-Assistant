from backend.contracts.plan_outcome import ensure_plan_outcome, maybe_short_circuit_to_clarification
from backend.executor.actions_schema import ActionPlan, ActionStep
from fastapi.testclient import TestClient
from backend.app import app


def test_short_circuit_browser_clarification():
    client = TestClient(app)
    payload = {"user_text": "打开浏览器并访问 Google 首页", "provider": "doubao"}
    resp = client.post("/api/ai/run", json=payload)
    data = resp.json()
    assert data.get("plan_status") == "awaiting_user"
    clarification = data.get("clarification")
    assert clarification and len(clarification.get("options", [])) >= 3


def test_ensure_plan_outcome_empty_steps_to_clarification():
    plan = ActionPlan(task="empty", steps=[])
    outcome = ensure_plan_outcome("empty plan", plan.model_dump())
    assert outcome.get("plan_status") == "awaiting_user"
    assert "clarification" in outcome


def test_ensure_plan_outcome_ok_when_steps_present():
    plan = ActionPlan(task="one", steps=[ActionStep(action="wait", params={"seconds": 1})])
    outcome = ensure_plan_outcome("one step", plan.model_dump())
    assert outcome.get("plan_status") == "ok"
