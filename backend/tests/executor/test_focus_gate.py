import types

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


def test_focus_mismatch_blocks_dispatch(monkeypatch):
    dispatch_called = False

    def fake_click(step):
        nonlocal dispatch_called
        dispatch_called = True
        return {"status": "ok"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "click", fake_click)
    provider = MockWindowProvider([{"title": "Other"}])
    plan = ActionPlan(task="test", steps=[ActionStep(action="click", params={"title": "Notepad", "x": 1, "y": 2})])

    result = executor.run_steps(plan, window_provider=provider, request_id="req-1")

    assert dispatch_called is False
    assert result["overall_status"] == "error"
    entry = result["logs"][0]
    assert entry["reason"] == "foreground_mismatch"
    assert entry["request_id"] == "req-1"
    assert entry["expected_window"]["title"] == "Notepad"
    assert entry["actual_window"]["title"] == "Other"


def test_no_target_hint_blocks(monkeypatch):
    dispatch_called = False

    def fake_click(step):
        nonlocal dispatch_called
        dispatch_called = True
        return {"status": "ok"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "click", fake_click)
    provider = MockWindowProvider([{"title": "Anything"}])
    plan = ActionPlan(task="test", steps=[ActionStep(action="click", params={"x": 1, "y": 2})])

    result = executor.run_steps(plan, window_provider=provider, request_id="req-2")

    assert dispatch_called is False
    assert result["overall_status"] == "error"
    entry = result["logs"][0]
    assert entry["reason"] == "no_target_hint"
    assert entry["request_id"] == "req-2"


def test_focus_fixer_sets_last_focus_for_next_input(monkeypatch):
    click_called = False

    def fake_activate(step):
        return {"status": "success"}

    def fake_click(step):
        nonlocal click_called
        click_called = True
        return {"status": "ok"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "activate_window", fake_activate)
    monkeypatch.setitem(executor.ACTION_HANDLERS, "click", fake_click)
    provider = MockWindowProvider(
        [
            {"title": "Notes", "class": "notepad", "pid": 1, "hwnd": 11},  # after activate_window
            {"title": "Notes", "class": "notepad", "pid": 1, "hwnd": 11},  # before click gate
        ]
    )
    plan = ActionPlan(
        task="test",
        steps=[
            ActionStep(action="activate_window", params={"title_keywords": ["Notes"]}),
            ActionStep(action="click", params={"x": 1, "y": 2}),
        ],
    )

    result = executor.run_steps(plan, window_provider=provider, request_id="req-3")

    assert result["overall_status"] == "success"
    assert click_called is True
    assert provider.calls >= 2  # activate fetch + gate


def test_dry_run_skips_focus_checks(monkeypatch):
    provider = MockWindowProvider([{"title": "Other"}])
    plan = ActionPlan(task="test", steps=[ActionStep(action="click", params={"title": "Notepad", "x": 1, "y": 2})])

    result = executor.run_steps(plan, dry_run=True, window_provider=provider, request_id="req-4")

    assert provider.calls == 0
    assert result["overall_status"] == "dry_run"
    assert result["logs"][0]["status"] == "skipped"


def test_execute_plan_blocks_on_focus_mismatch(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import app

    dispatch_called = False

    def fake_click(step):
        nonlocal dispatch_called
        dispatch_called = True
        return {"status": "ok"}

    monkeypatch.setitem(executor.ACTION_HANDLERS, "click", fake_click)

    class AlwaysOther(executor._DefaultWindowProvider):
        def get_foreground_window(self):
            return {"title": "Other", "class": "other", "pid": 2, "hwnd": 22}

    monkeypatch.setattr(executor, "_DefaultWindowProvider", AlwaysOther)

    client = TestClient(app)
    payload = {"task": "test", "steps": [{"action": "click", "params": {"title": "Notepad", "x": 1, "y": 2}}]}
    resp = client.post("/api/ai/execute_plan", json=payload)
    data = resp.json()

    assert dispatch_called is False
    assert data["overall_status"] == "error"
    entry = data["logs"][0]
    assert entry["reason"] == "foreground_mismatch"
    assert "request_id" in data
