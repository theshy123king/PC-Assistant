import copy
from types import SimpleNamespace

import pytest

from backend.executor import executor
from backend.executor.actions_schema import ActionStep


class FakeInvokePattern:
    def __init__(self) -> None:
        self.invoked = False

    def Invoke(self) -> None:
        self.invoked = True


class FakeValuePattern:
    def __init__(self) -> None:
        self.values = []
        self.CurrentIsReadOnly = False

    def SetValue(self, text: str) -> None:
        self.values.append(text)


class FakeElement:
    def __init__(self, pattern=None) -> None:
        self.pattern = pattern
        self.Current = SimpleNamespace(IsEnabled=True, IsOffscreen=False)
        if pattern:
            # Make it discoverable by the pattern helper.
            self.InvokePattern = pattern if isinstance(pattern, FakeInvokePattern) else None
            self.ValuePattern = pattern if isinstance(pattern, FakeValuePattern) else None
        self.focused = False

    def GetCurrentPattern(self, _pattern_id):
        return self.pattern

    def SetFocus(self):
        self.focused = True


def _fake_locate_base():
    return {
        "status": "success",
        "method": "uia",
        "center": {"x": 10, "y": 20},
        "bounds": {"x": 0, "y": 0, "width": 20, "height": 10},
        "candidate": {
            "text": "OK",
            "source": {
                "runtime_id": [1, 2, 3],
                "locator_key": {"name": "OK", "control_type": "ButtonControl"},
                "control_type": "ButtonControl",
            },
        },
    }


def test_click_uses_invoke_pattern_when_available(monkeypatch):
    fake_element = FakeElement(pattern=FakeInvokePattern())

    monkeypatch.setattr(executor, "rebind_element", lambda ref, root=None: fake_element)
    monkeypatch.setattr(executor, "capture_screen", lambda: "dummy.png")
    monkeypatch.setattr(executor, "run_ocr_with_boxes", lambda path: ("", []))
    monkeypatch.setattr(executor, "_locate_from_params", lambda *args, **kwargs: copy.deepcopy(_fake_locate_base()))
    monkeypatch.setattr(executor.MOUSE, "click", lambda payload: "clicked")  # safety net

    step = ActionStep(action="click", params={"text": "OK"})
    result = executor.handle_click(step)

    assert result["method"] == "uia_pattern"
    assert result["message"]["pattern"] == "InvokePattern"
    assert fake_element.pattern.invoked is True


def test_click_fallbacks_to_focus_then_click_when_no_pattern(monkeypatch):
    focus_element = FakeElement(pattern=None)

    monkeypatch.setattr(executor, "rebind_element", lambda ref, root=None: focus_element)
    monkeypatch.setattr(executor, "capture_screen", lambda: "dummy.png")
    monkeypatch.setattr(executor, "run_ocr_with_boxes", lambda path: ("", []))
    monkeypatch.setattr(executor, "_locate_from_params", lambda *args, **kwargs: copy.deepcopy(_fake_locate_base()))
    click_calls = []

    def fake_click(payload):
        click_calls.append(payload)
        return "clicked"

    monkeypatch.setattr(executor.MOUSE, "click", fake_click)

    step = ActionStep(action="click", params={"text": "OK"})
    result = executor.handle_click(step)

    assert result["method"] == "focus_then_click"
    assert focus_element.focused is True
    assert click_calls, "expected physical click fallback"
    assert result["message"]["method"] == "focus_then_click"


def test_type_uses_value_pattern_then_fallbacks(monkeypatch):
    base_locate = _fake_locate_base()

    monkeypatch.setattr(executor, "capture_screen", lambda: "dummy.png")
    monkeypatch.setattr(executor, "run_ocr_with_boxes", lambda path: ("", []))

    def fake_locate(*args, **kwargs):
        return copy.deepcopy(base_locate)

    monkeypatch.setattr(executor, "_locate_from_params", fake_locate)
    monkeypatch.setattr(executor.MOUSE, "click", lambda payload: "clicked")

    value_pattern = FakeValuePattern()
    value_element = FakeElement(pattern=value_pattern)
    monkeypatch.setattr(executor, "rebind_element", lambda ref, root=None: value_element)

    step = ActionStep(action="browser_input", params={"text": "Name", "value": "abc"})
    result = executor.handle_browser_input(step)

    assert result["method"] == "uia_value"
    assert value_pattern.values == ["abc"]

    # Now force ValuePattern failure to exercise fallbacks.
    events = []
    fallback_element = FakeElement(pattern=None)

    monkeypatch.setattr(executor, "rebind_element", lambda ref, root=None: fallback_element)
    monkeypatch.setattr(executor, "try_set_value", lambda element, text: (events.append("value") or (False, "fail")))
    monkeypatch.setattr(executor, "_set_clipboard_text", lambda text: (events.append("clipboard") or (False, "no_clipboard")))
    monkeypatch.setattr(executor.input, "type_text", lambda params: events.append("type") or "typed")

    fallback = executor.handle_browser_input(ActionStep(action="browser_input", params={"text": "Name", "value": "xyz"}))

    assert fallback["method"] == "keyboard_type"
    assert events == ["value", "clipboard", "type"]
