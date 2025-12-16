from pathlib import Path

import pytest

from backend.executor import executor
from backend.executor.actions_schema import ActionStep
from backend.executor.task_context import TaskContext
from backend.executor.ui_locator import locate_target


def test_activate_window_failure_marks_step_error(monkeypatch):
    monkeypatch.setattr(executor, "activate_window", lambda params: {"success": False, "reason": "nope"})

    step = ActionStep(action="activate_window", params={"title_keywords": ["dummy"]})
    result = executor.handle_activate_window(step)

    assert result["status"] == "error"
    assert result.get("success") is False


def test_activate_window_success_sets_context_snapshot(monkeypatch):
    ctx = TaskContext()
    context_token = executor.CURRENT_CONTEXT.set(ctx)
    active_token = executor.ACTIVE_WINDOW.set(None)

    snapshot = {"hwnd": 123, "pid": 456, "title": "Notepad", "class": "Notepad"}

    def fake_activate(params):
        executor._store_active_window(snapshot)
        return {
            "success": True,
            "status": "success",
            "hwnd": snapshot["hwnd"],
            "pid": snapshot["pid"],
            "matched_title": snapshot["title"],
            "matched_class": snapshot["class"],
            "active_window": snapshot,
        }

    monkeypatch.setattr(executor, "activate_window", fake_activate)

    step = ActionStep(action="activate_window", params={"title_keywords": ["Notepad"]})
    result = executor.handle_activate_window(step)

    try:
        assert result["status"] == "success"
        assert executor.ACTIVE_WINDOW.get() == snapshot
        assert ctx.active_window == snapshot
    finally:
        executor.CURRENT_CONTEXT.reset(context_token)
        executor.ACTIVE_WINDOW.reset(active_token)


def test_strict_activation_fails_when_foreground_not_enforced(monkeypatch):
    active_token = executor.ACTIVE_WINDOW.set({"hwnd": 1})
    fake_snap = executor._WinSnapshot(101, "Note", 11, True, False, False, False, "Notepad", (0, 0, 100, 100))

    monkeypatch.setattr(executor, "locate_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(executor, "_enum_top_windows", lambda: [fake_snap])
    monkeypatch.setattr(executor, "_filter_windows_by_keywords", lambda snaps, tk, ck: snaps)
    monkeypatch.setattr(executor, "_score_window_candidate", lambda snap, terms: (1.0, terms[0] if terms else ""))
    monkeypatch.setattr(executor, "_foreground_window", lambda hwnd, pid, logs: True)
    monkeypatch.setattr(executor, "ensure_foreground", lambda hwnd: {"ok": False, "final_foreground": {"hwnd": 999}})

    try:
        result = executor.activate_window({"title_keywords": ["note"], "strict": True})

        assert result["status"] == "error"
        assert result["reason"] == "foreground_enforcement_failed"
        assert executor.ACTIVE_WINDOW.get() is None
    finally:
        executor.ACTIVE_WINDOW.reset(active_token)


def test_locator_uses_preferred_hwnd_for_uia(monkeypatch):
    pref = {"hwnd": 99, "pid": 77, "title": "Note"}
    ctx = TaskContext()
    ctx.active_window = pref
    context_token = executor.CURRENT_CONTEXT.set(ctx)
    active_token = executor.ACTIVE_WINDOW.set(pref)

    captured = {}

    def fake_locate_target(
        query,
        boxes,
        image_path=None,
        image_base64=None,
        icon_templates=None,
        vlm_call=None,
        vlm_provider="deepseek",
        high_threshold=0.9,
        medium_threshold=0.75,
        match_policy=None,
        preferred_hwnd=None,
        preferred_pid=None,
        preferred_title=None,
    ):
        captured["preferred_hwnd"] = preferred_hwnd
        captured["preferred_pid"] = preferred_pid
        captured["preferred_title"] = preferred_title
        return {"status": "error", "reason": "forced"}

    monkeypatch.setattr(executor, "locate_target", fake_locate_target)
    monkeypatch.setattr(executor, "_encode_image_base64", lambda path: None)

    _ = executor._locate_from_params("Save", {}, [], Path("dummy.png"), match_policy=executor.MatchPolicy.CONTROL_ONLY)

    try:
        assert captured["preferred_hwnd"] == pref["hwnd"]
        assert captured["preferred_pid"] == pref["pid"]
        assert captured["preferred_title"] == pref["title"]
    finally:
        executor.CURRENT_CONTEXT.reset(context_token)
        executor.ACTIVE_WINDOW.reset(active_token)


def test_preferred_root_mismatch_blocks_vlm_fallback(monkeypatch):
    def raise_root_error(*args, **kwargs):
        raise ValueError("preferred_root_unavailable:hwnd")

    monkeypatch.setattr("backend.executor.ui_locator.find_uia_element", raise_root_error)
    result = locate_target("btn", [], preferred_hwnd=12345)

    assert result["status"] == "error"
    assert "preferred_root_unavailable" in (result.get("reason", "") or result.get("log", [""])[-1])
