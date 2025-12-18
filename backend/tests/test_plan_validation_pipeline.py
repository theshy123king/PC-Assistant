from fastapi.testclient import TestClient

import backend.executor.executor as executor
from backend.app import app


client = TestClient(app)


def test_execute_plan_invalid_plan_returns_structured_errors():
    payload = {"task": "bad plan", "steps": [{"action": "click", "params": {}}]}

    resp = client.post("/api/ai/execute_plan", json=payload)
    data = resp.json()

    assert data["error"] == "invalid action plan"
    assert data.get("request_id")
    assert data.get("validation_errors")
    err = data["validation_errors"][0]
    assert err["step_index"] == 0
    assert err["action"] == "click"
    assert err.get("reason")


def test_execute_plan_dry_run_blocks_dispatch(monkeypatch):
    called = False

    def fake_run_steps(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(executor, "run_steps", fake_run_steps)
    payload = {
        "task": "click safely",
        "dry_run": True,
        "steps": [
            {
                "action": "click",
                "params": {"x": 1, "y": 2},
            }
        ],
    }

    resp = client.post("/api/ai/execute_plan", json=payload)
    data = resp.json()

    assert called is False
    assert data["dry_run"] is True
    assert data["mode"] == "dry_run"
    assert "no side effects" in data["note"]
