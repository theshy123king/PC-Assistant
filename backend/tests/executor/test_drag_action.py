from typing import Any, Dict

import backend.executor.executor as executor
from backend.executor import mouse
from backend.executor.actions_schema import ActionStep, DragAction


class FakeMouse:
    def __init__(self) -> None:
        self.calls = []

    def drag(self, start: Dict[str, Any], end: Dict[str, Any], duration: float = 0.0):
        self.calls.append((start, end, duration))
        return {
            "status": "success",
            "start": start,
            "end": end,
            "duration": duration,
            "performed": True,
        }


def test_drag_action_validates_coordinates():
    action = DragAction(
        start={"x": 1, "y": 2},
        end={"x": 3, "y": 4},
        duration=0.5,
    )

    assert action.duration == 0.5
    assert action.start["x"] == 1
    assert action.end["y"] == 4


def test_handle_drag_calls_controller_and_returns_metadata(monkeypatch):
    fake_mouse = FakeMouse()
    monkeypatch.setattr(executor, "MOUSE", fake_mouse)

    result = executor.handle_drag(
        ActionStep(
            action="drag",
            params={"start": {"x": 10, "y": 20}, "end": {"x": 30, "y": 40}, "duration": 0.1},
        )
    )

    assert fake_mouse.calls == [({"x": 10, "y": 20}, {"x": 30, "y": 40}, 0.1)]
    assert result["metadata"]["start"] == {"x": 10, "y": 20}
    assert result["metadata"]["end"] == {"x": 30, "y": 40}
    assert result["metadata"]["duration"] == 0.1
    assert result["status"] == "success"


def test_mouse_controller_drag_uses_pyautogui(monkeypatch):
    moves = []
    drags = []

    class FakePyAuto:
        def size(self):
            return (800, 600)

        def moveTo(self, x, y):
            moves.append((x, y))

        def dragTo(self, x, y, duration=None):
            drags.append((x, y, duration))

    monkeypatch.setattr(mouse, "pyautogui", FakePyAuto())

    result = mouse.controller.drag({"x": 1, "y": 2}, {"x": 5, "y": 6}, duration=0.25)

    assert moves == [(1, 2)]
    assert drags == [(5, 6, 0.25)]
    assert result["status"] == "success"
