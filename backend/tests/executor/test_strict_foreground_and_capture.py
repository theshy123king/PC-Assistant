from pathlib import Path

import pytest

from backend.executor import executor
from backend.executor.actions_schema import ActionStep


def test_foreground_mismatch_blocks_click(monkeypatch):
    preferred = {"hwnd": 10, "pid": 1, "title": "Notepad"}
    executor.ACTIVE_WINDOW.set(preferred)
    monkeypatch.setattr(
        executor,
        "_enforce_strict_foreground_once",
        lambda pref, logs=None: (False, {"hwnd": 20, "pid": 2, "title": "Chrome"}, {"ok": False}),
    )

    result = executor.handle_click(ActionStep(action="click", params={"text": "File", "strict_foreground": True}))

    assert result["status"] == "error"
    assert result["reason"] == "foreground_mismatch"
    assert executor.ACTIVE_WINDOW.get() is None


def test_strict_click_enforces_foreground_once(monkeypatch):
    preferred = {"hwnd": 10, "pid": 1, "title": "Notepad"}
    executor.ACTIVE_WINDOW.set(preferred)
    calls = {"count": 0}

    def fake_enforce(pref, logs=None):
        calls["count"] += 1
        return False, {"hwnd": 20, "pid": 3}, {"ok": False}

    monkeypatch.setattr(executor, "_enforce_strict_foreground_once", fake_enforce)

    result = executor.handle_click(ActionStep(action="click", params={"text": "File", "strict_foreground": True}))

    assert calls["count"] == 1
    assert result["status"] == "error"
    assert result["reason"] == "foreground_mismatch"


def test_capture_prefers_window_when_available(monkeypatch, tmp_path):
    preferred = {"hwnd": 99, "pid": 1, "title": "Notepad"}
    executor.ACTIVE_WINDOW.set(preferred)
    monkeypatch.setattr(executor, "_foreground_snapshot", lambda: preferred)
    monkeypatch.setattr(
        executor, "_enforce_strict_foreground_once", lambda pref, logs=None: (True, preferred, {"ok": True})
    )
    monkeypatch.setattr(executor, "run_ocr_with_boxes", lambda path: ("", []))

    called = {}

    def fake_capture_window(hwnd):
        called["hwnd"] = hwnd
        path = tmp_path / "win.png"
        path.write_bytes(b"dummy")
        return path

    monkeypatch.setattr(executor, "capture_window", fake_capture_window)
    monkeypatch.setattr(executor, "_locate_from_params", lambda *args, **kwargs: {"status": "error", "reason": "locate_failed"})

    result = executor.handle_click(ActionStep(action="click", params={"text": "File", "strict_foreground": True}))

    assert result["status"] == "error"
    assert called["hwnd"] == preferred["hwnd"]


def test_activation_preserves_maximize(monkeypatch):
    calls = {}

    class FakeUser32:
        def AllowSetForegroundWindow(self, arg):
            calls["allow"] = arg

        def IsIconic(self, hwnd):
            return False

        def IsZoomed(self, hwnd):
            return True

    fake_user32 = FakeUser32()
    monkeypatch.setattr(executor, "user32", fake_user32)
    monkeypatch.setattr(
        executor, "ensure_foreground", lambda hwnd: {"ok": True, "final_foreground": {"hwnd": hwnd}, "reason": "ok"}
    )

    logs: list[str] = []
    ok = executor._foreground_window(100, 1, logs)

    assert ok is True
    assert any("WindowState:minimized:False:maximized:True" in l for l in logs)


def test_click_does_not_default_to_origin_on_locate_failure(monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"dummy")
    clicked = {"called": False}

    monkeypatch.setattr(executor, "_enforce_strict_foreground_once", lambda pref, logs=None: (True, {"hwnd": 1}, None))
    monkeypatch.setattr(executor, "_capture_for_interaction", lambda pref, strict: (shot, {"hwnd": 1}, None))
    monkeypatch.setattr(executor, "run_ocr_with_boxes", lambda path: ("", []))
    monkeypatch.setattr(executor, "_locate_from_params", lambda *args, **kwargs: {"status": "error", "reason": "forced"})
    monkeypatch.setattr(
        executor.MOUSE,
        "click",
        lambda params: clicked.__setitem__("called", True) or "clicked",
    )

    result = executor.handle_click(ActionStep(action="click", params={"text": "File", "strict_foreground": True}))

    assert result["status"] == "error"
    assert result["reason"] == "forced"
    assert clicked["called"] is False


def test_uia_locator_no_property_condition(monkeypatch):
    # PropertyCondition missing should not break traversal
    import backend.vision.uia_locator as locator

    monkeypatch.setattr(locator.auto, "PropertyCondition", None, raising=False)
    monkeypatch.setattr(locator.auto, "OrCondition", None, raising=False)
    monkeypatch.setattr(locator.auto, "AndCondition", None, raising=False)

    class FakeElem:
        def __init__(self):
            self.Name = ""

        def GetChildren(self):
            return []

    monkeypatch.setattr(locator.auto, "GetRootControl", lambda: FakeElem())
    result = locator.find_element("anything", policy=locator.MatchPolicy.CONTROL_ONLY)
    assert result is None


def test_uia_rebind_traversal_without_property_condition(monkeypatch):
    import backend.executor.uia_rebind as rebind

    class FakeElem:
        def __init__(self, name="", children=None, automation_id=None, class_name=None, ctrl="ButtonControl"):
            self.Name = name
            self.AutomationId = automation_id
            self.ClassName = class_name
            self.ControlTypeName = ctrl
            self._children = children or []

        def GetChildren(self):
            return list(self._children)

    target = FakeElem(name="Target", automation_id="auto1", class_name="cls")
    root = FakeElem(name="Root", children=[target])

    monkeypatch.setattr(rebind.auto, "GetForegroundControl", lambda: root)
    monkeypatch.setattr(rebind.auto, "GetRootControl", lambda: root)
    monkeypatch.setattr(rebind.auto, "PropertyCondition", None, raising=False)

    ref = {"locator_key": {"name": "Target", "automation_id": "auto1", "control_type": "ButtonControl", "class_name": "cls"}}
    found = rebind.rebind_element(ref, root=None)
    assert found is target
