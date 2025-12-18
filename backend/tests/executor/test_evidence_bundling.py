import tempfile
from pathlib import Path

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionPlan, ActionStep


class MockWindowProvider:
    def __init__(self, windows):
        self.windows = list(windows)
        self.calls = 0

    def get_foreground_window(self):
        self.calls += 1
        if self.windows:
            return self.windows[min(self.calls - 1, len(self.windows) - 1)]
        return {}


def test_evidence_attached_on_success(monkeypatch):
    called = 0

    def fake_extract(step):
        nonlocal called
        called += 1
        return {"status": "success", "text": "ok"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_extract_text", fake_extract)

    plan = ActionPlan(task="read", steps=[ActionStep(action="browser_extract_text", params={"text": "status"})])
    result = executor.run_steps(
        plan,
        request_id="req-ev-1",
        consent_token=True,
        capture_observations=False,
        capture_ocr=False,
    )

    assert called == 1
    assert result["overall_status"] == "success"
    entry = result["logs"][-1]
    assert entry["evidence"]
    assert entry["evidence"]["action"] == "browser_extract_text"
    assert entry["attempts"][0]["evidence"]
    assert entry["attempts"][0]["evidence"]["capture_phase"] == "verify"


def test_focus_gate_evidence(monkeypatch):
    dispatch_called = False

    def fake_click(step):
        nonlocal dispatch_called
        dispatch_called = True
        return {"status": "ok"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "click", fake_click)
    provider = MockWindowProvider([{"title": "Other", "class": "other", "pid": 2, "hwnd": 22}])
    plan = ActionPlan(task="focus", steps=[ActionStep(action="click", params={"title": "Notepad", "x": 1, "y": 2})])

    result = executor.run_steps(
        plan,
        window_provider=provider,
        request_id="req-ev-2",
        consent_token=True,
        capture_observations=False,
    )

    assert dispatch_called is False
    entry = result["logs"][0]
    evidence = entry.get("evidence")
    assert evidence
    assert evidence["reason"] == "foreground_mismatch"
    assert evidence["focus_expected"]["title"] == "Notepad"
    assert evidence["focus_actual"]["title"] == "Other"
    assert evidence["capture_phase"] == "gate"
    assert evidence["before_obs_ref"] is None
    assert evidence["after_obs_ref"] is None


def test_consent_gate_evidence(monkeypatch):
    handler_calls = 0

    def fake_delete(step):
        nonlocal handler_calls
        handler_calls += 1
        return {"status": "success"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", fake_delete)

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmpdir:
        target = Path(tmpdir) / "target.txt"
        target.write_text("content")
        plan = ActionPlan(
            task="danger",
            steps=[ActionStep(action="delete_file", params={"path": str(target), "confirm": True})],
        )
        result = executor.run_steps(
            plan,
            work_dir=tmpdir,
            request_id="req-ev-3",
            consent_token=False,
            capture_observations=False,
        )

    assert handler_calls == 0
    assert result["overall_status"] == "error"
    entry = result["logs"][0]
    evidence = entry.get("evidence")
    assert evidence
    assert evidence["reason"] == "needs_consent"
    assert evidence["risk"]["level"] == executor.RISK_HIGH
    assert evidence["capture_phase"] == "gate"


def test_verification_failure_evidence(monkeypatch):
    handler_calls = 0

    def fake_delete(step):
        nonlocal handler_calls
        handler_calls += 1
        return {"status": "success"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "delete_file", fake_delete)

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmpdir:
        target = Path(tmpdir) / "target.txt"
        target.write_text("content")
        plan = ActionPlan(
            task="delete",
            steps=[
                ActionStep(
                    action="delete_file",
                    params={"path": str(target), "confirm": True, "max_retries": 1},
                )
            ],
        )
        result = executor.run_steps(
            plan,
            work_dir=tmpdir,
            request_id="req-ev-4",
            consent_token=True,
            capture_observations=False,
        )

    assert handler_calls >= 1
    entry = result["logs"][0]
    evidence = entry["attempts"][-1]["evidence"]
    assert entry["reason"] == "verification_failed"
    assert evidence["reason"] == "verification_failed"
    assert evidence["expected"]["path"] == str(target)


def test_dry_run_evidence(monkeypatch):
    provider = MockWindowProvider([{"title": "Other"}])
    plan = ActionPlan(task="dry", steps=[ActionStep(action="click", params={"title": "Notepad", "x": 1, "y": 2})])

    result = executor.run_steps(
        plan,
        dry_run=True,
        window_provider=provider,
        request_id="req-ev-5",
        capture_observations=False,
    )

    assert provider.calls == 0
    assert result["overall_status"] == "dry_run"
    entry = result["logs"][0]
    evidence = entry["evidence"]
    assert evidence["dry_run"] is True
    assert evidence["capture_phase"] == "preflight"
    assert evidence["foreground"] is None
    assert evidence["before_obs_ref"] is None
    assert evidence["after_obs_ref"] is None
