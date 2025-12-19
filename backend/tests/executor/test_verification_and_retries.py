import tempfile
from pathlib import Path

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionPlan, ActionStep


def test_file_verification_retries_then_fails(monkeypatch):
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
            steps=[
                ActionStep(action="delete_file", params={"path": str(target), "confirm": True, "max_retries": 2}),
            ],
        )
        result = executor.run_steps(plan, work_dir=tmp, request_id="req-ver-1", consent_token=True)

    assert called >= 1
    assert result["overall_status"] == "error"
    entry = result["logs"][0]
    assert entry["reason"] == "verification_failed"
    assert len(entry["attempts"]) == 3


def test_browser_extract_text_single_attempt(monkeypatch):
    called = 0

    def fake_extract(step):
        nonlocal called
        called += 1
        return {"status": "success", "text": "status ok"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_extract_text", fake_extract)

    plan = ActionPlan(
        task="read",
        steps=[ActionStep(action="browser_extract_text", params={"text": "status", "max_retries": 3})],
    )
    result = executor.run_steps(plan, work_dir=str(Path.cwd()), request_id="req-ver-2", consent_token=True)

    assert called == 1
    assert result["overall_status"] == "success"


def test_wait_until_not_retried(monkeypatch):
    called = 0

    def fake_wait(step):
        nonlocal called
        called += 1
        return {"status": "error"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "wait_until", fake_wait)

    plan = ActionPlan(task="wait", steps=[ActionStep(action="wait_until", params={"condition": "ui_stable"})])
    result = executor.run_steps(plan, work_dir=str(Path.cwd()), request_id="req-ver-3", consent_token=True)

    assert called == 1
    entry = result["logs"][0]
    assert result["overall_status"] == "error"
    assert len(entry["attempts"]) == 1


def test_wait_until_timeout_fails_by_default(monkeypatch):
    def fake_wait(step):
        return {"status": "timeout", "ok": False, "condition": "ui_stable", "elapsed": 0.1}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "wait_until", fake_wait)

    plan = ActionPlan(task="wait", steps=[ActionStep(action="wait_until", params={"condition": "ui_stable"})])
    result = executor.run_steps(plan, work_dir=str(Path.cwd()), request_id="req-ver-4", consent_token=True)

    entry = result["logs"][0]
    assert result["overall_status"] == "error"
    assert entry["reason"] == "timeout"
    assert entry["status"] == "error"


def test_wait_until_timeout_allowed_succeeds(monkeypatch):
    def fake_wait(step):
        return {"status": "timeout", "ok": False, "condition": "ui_stable", "elapsed": 0.1, "timeout_allowed": True}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "wait_until", fake_wait)

    plan = ActionPlan(
        task="wait",
        steps=[ActionStep(action="wait_until", params={"condition": "ui_stable", "allow_timeout": True})],
    )
    result = executor.run_steps(plan, work_dir=str(Path.cwd()), request_id="req-ver-5", consent_token=True)

    entry = result["logs"][0]
    assert result["overall_status"] == "success"
    assert entry["reason"] == "timeout_allowed"
    assert entry["status"] == "success"
