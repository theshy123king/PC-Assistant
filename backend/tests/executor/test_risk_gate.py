import tempfile
from pathlib import Path

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionPlan, ActionStep
from fastapi.testclient import TestClient


class StubHandler:
    def __init__(self):
        self.calls = 0

    def __call__(self, step):
        self.calls += 1
        return {"status": "ok"}


def test_high_risk_blocks_without_consent(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", handler)

    base_dir = Path(__file__).parent
    with tempfile.TemporaryDirectory(dir=base_dir) as tmp:
        plan = ActionPlan(
            task="danger",
            steps=[ActionStep(action="delete_file", params={"path": str(Path(tmp) / "target.txt"), "confirm": True})],
        )
        result = executor.run_steps(plan, work_dir=tmp, request_id="req-risk-1", consent_token=False)

    assert handler.calls == 0
    assert result["overall_status"] == "error"
    entry = result["logs"][0]
    assert entry["reason"] == "needs_consent"
    assert entry["request_id"] == "req-risk-1"
    assert entry["risk"]["level"] == executor.RISK_HIGH


def test_high_risk_allows_with_consent(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", handler)

    base_dir = Path(__file__).parent
    with tempfile.TemporaryDirectory(dir=base_dir) as tmp:
        plan = ActionPlan(
            task="danger",
            steps=[ActionStep(action="delete_file", params={"path": str(Path(tmp) / "target.txt"), "confirm": True})],
        )
        result = executor.run_steps(plan, work_dir=tmp, request_id="req-risk-2", consent_token=True)

    assert handler.calls == 1
    assert result["overall_status"] != "error"


def test_dry_run_surfaces_risk(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", handler)

    base_dir = Path(__file__).parent
    with tempfile.TemporaryDirectory(dir=base_dir) as tmp:
        plan = ActionPlan(
            task="danger",
            steps=[ActionStep(action="delete_file", params={"path": str(Path(tmp) / "target.txt"), "confirm": True})],
        )
        result = executor.run_steps(plan, work_dir=tmp, request_id="req-risk-3", dry_run=True)

    assert handler.calls == 0
    assert result["overall_status"] == "dry_run"
    assert result["logs"][0]["risk"]["level"] == executor.RISK_HIGH


def test_execute_plan_blocks_without_consent(monkeypatch):
    handler = StubHandler()
    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", handler)

    from backend.app import app

    client = TestClient(app)
    path = str(Path(__file__).parent / "tmpfile.txt")
    payload = {
        "task": "danger",
        "steps": [{"action": "delete_file", "params": {"path": path, "confirm": True}}],
    }
    resp = client.post("/api/ai/execute_plan", json=payload)
    data = resp.json()

    assert handler.calls == 0
    assert data.get("overall_status") == "error"
    logs = data.get("logs") or []
    assert logs, data
    assert logs[0].get("reason") in {"needs_consent", "foreground_mismatch", "no_target_hint"}
    assert "request_id" in data
