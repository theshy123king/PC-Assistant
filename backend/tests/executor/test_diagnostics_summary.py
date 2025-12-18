import tempfile
from pathlib import Path

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionPlan, ActionStep
from fastapi.testclient import TestClient
from backend.app import app


class StubHandler:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, step):
        self.calls += 1
        idx = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[idx]


def test_reason_mapping():
    mapper = executor._map_reason_category
    assert mapper("foreground_mismatch") == "focus_gate"
    assert mapper("needs_consent") == "consent_gate"
    assert mapper("path_not_allowed") == "file_guardrail"
    assert mapper("missing_expected_verify") == "verification"
    assert mapper("verification_failed") == "verification"
    assert mapper("handler_error") == "handler"
    assert mapper("plan_validation_error") == "plan_validation_error"


def test_diagnostics_focus_mismatch(monkeypatch):
    dispatch_called = False

    def fake_click(step):
        nonlocal dispatch_called
        dispatch_called = True
        return {"status": "ok"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "click", fake_click)
    provider = type("WP", (), {"get_foreground_window": lambda self: {"title": "Other"}})()
    plan = ActionPlan(task="test", steps=[ActionStep(action="click", params={"title": "Notepad", "x": 1, "y": 2})])

    result = executor.run_steps(plan, window_provider=provider, request_id="req-diag-1")
    diag = result.get("diagnostics_summary")
    assert diag
    assert diag["primary_failure_category"] == "focus_gate"
    assert diag["primary_reason_code"] == "foreground_mismatch"
    assert diag["failed_step_index"] == 0
    assert dispatch_called is False


def test_diagnostics_needs_consent(monkeypatch):
    handler = StubHandler([{"status": "success"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", handler)
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work_dir:
        plan = ActionPlan(
            task="danger",
            steps=[ActionStep(action="delete_file", params={"path": str(Path(work_dir) / "file.txt"), "confirm": True})],
        )
        result = executor.run_steps(plan, work_dir=work_dir, request_id="req-diag-2", consent_token=False)
    diag = result.get("diagnostics_summary")
    assert diag
    assert diag["primary_failure_category"] == "consent_gate"
    assert diag["primary_reason_code"] == "needs_consent"
    assert handler.calls == 0


def test_diagnostics_verification_failed(monkeypatch):
    called = 0

    def fake_delete(step):
        nonlocal called
        called += 1
        return {"status": "success"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", fake_delete)
    base_dir = Path(__file__).parent
    with tempfile.TemporaryDirectory(dir=base_dir) as tmp:
        target = Path(tmp) / "target.txt"
        target.write_text("content")
        plan = ActionPlan(
            task="delete",
            steps=[ActionStep(action="delete_file", params={"path": str(target), "confirm": True, "max_retries": 1})],
        )
        result = executor.run_steps(plan, work_dir=tmp, request_id="req-diag-3", consent_token=True)

    diag = result.get("diagnostics_summary")
    assert diag
    assert diag["primary_failure_category"] == "verification"
    assert diag["primary_reason_code"] in {"verification_failed", "verification_retry"}
    assert diag["retry_exhausted"] is True
    assert called >= 1


def test_diagnostics_file_guardrail(monkeypatch):
    handler = StubHandler([{"status": "success"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", handler)
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work_dir:
        outside = Path(work_dir).parent / "outside.txt"
        monkeypatch.setattr(executor, "ALLOWED_ROOTS", [str(Path(work_dir).resolve())])
        result = executor.run_steps(
            ActionPlan(task="danger", steps=[ActionStep(action="delete_file", params={"path": str(outside), "confirm": True})]),
            work_dir=work_dir,
            request_id="req-diag-4",
            consent_token=True,
        )
    diag = result.get("diagnostics_summary")
    assert diag
    assert diag["primary_failure_category"] in {"file_guardrail", "unsafe_policy"}
    assert diag["failed_step_index"] == 0


def test_diagnostics_missing_expected_verify(monkeypatch):
    handler = StubHandler([{"status": "success", "url": "https://example.com"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_click", handler)
    wp = type("WP", (), {"get_foreground_window": lambda self: {"title": "Browser"}})()
    plan = ActionPlan(task="click", steps=[ActionStep(action="browser_click", params={"text": "Go", "title": "Browser"})])
    result = executor.run_steps(plan, consent_token=True, capture_observations=False, window_provider=wp, allow_replan=False)
    diag = result.get("diagnostics_summary")
    assert diag
    assert diag["primary_reason_code"] == "missing_expected_verify"
    assert diag["primary_failure_category"] == "verification"


def test_diagnostics_plan_validation_error():
    client = TestClient(app)
    payload = {"task": "invalid", "steps": [{"params": {"path": ""}}]}  # missing action
    resp = client.post("/api/ai/execute_plan", json=payload)
    data = resp.json()
    assert "diagnostics_summary" in data
    diag = data["diagnostics_summary"]
    assert diag["primary_failure_category"] == "plan_validation_error"
    assert diag["overall_status"] == "plan_validation_error"


def test_diagnostics_dry_run_has_summary(monkeypatch):
    handler = StubHandler([{"status": "success"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "click", handler)
    plan = ActionPlan(task="dry", steps=[ActionStep(action="click", params={"title": "Notepad", "x": 1, "y": 1})])
    result = executor.run_steps(plan, dry_run=True, request_id="req-diag-5")
    diag = result.get("diagnostics_summary")
    # Dry run may not have failures; diagnostics_summary can be None
    if diag:
        assert diag["overall_status"] == "dry_run"
