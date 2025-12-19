import asyncio
from types import SimpleNamespace

import pytest

import backend.executor.executor as ex
from backend.executor.executor import ActionPlan, ActionStep, run_steps


class StubWindowProvider:
    def __init__(self, foreground):
        self._fg = foreground

    def get_foreground_window(self):
        return self._fg


def make_snapshot(title, class_name="app", pid=222, hwnd=333):
    return SimpleNamespace(
        hwnd=hwnd,
        title=title,
        pid=pid,
        is_visible=True,
        is_cloaked=False,
        has_owner=False,
        is_minimized=False,
        class_name=class_name,
        rect=(0, 0, 100, 100),
    )


@pytest.mark.anyio
async def test_open_app_skips_focus_gate(monkeypatch):
    # Mock handler to avoid real launch
    monkeypatch.setitem(ex.ACTION_HANDLERS, "open_app", lambda step: {"status": "success"})
    # Ensure window enumeration finds target for verification
    monkeypatch.setattr(ex, "_enum_top_windows", lambda: [make_snapshot("Notepad")])
    wp = StubWindowProvider({"title": "Assistant", "pid": 1, "hwnd": 10, "class": "App"})
    plan = ActionPlan(task="open notepad", steps=[ActionStep(action="open_app", params={"target": "Notepad", "verify_timeout": 0.2})])

    result = await asyncio.to_thread(run_steps, plan, window_provider=wp)
    assert result["overall_status"] == "success"
    assert result["logs"][0]["status"] == "success"
    assert result["logs"][0].get("reason") != "foreground_mismatch"


@pytest.mark.anyio
async def test_open_app_verification_failure(monkeypatch):
    monkeypatch.setitem(ex.ACTION_HANDLERS, "open_app", lambda step: {"status": "success"})
    monkeypatch.setattr(ex, "_enum_top_windows", lambda: [])
    wp = StubWindowProvider({"title": "Assistant", "pid": 1, "hwnd": 10, "class": "App"})
    plan = ActionPlan(task="open unknown", steps=[ActionStep(action="open_app", params={"target": "GhostApp", "verify_timeout": 0.2})])

    result = await asyncio.to_thread(run_steps, plan, window_provider=wp)
    assert result["overall_status"] == "error"
    entry = result["logs"][0]
    assert entry["reason"] == "verification_failed"
    assert entry["status"] == "error"
