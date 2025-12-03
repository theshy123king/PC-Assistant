import pytest
from pydantic import ValidationError

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionStep, ScrollAction, validate_action_plan


class FakeMouse:
    def __init__(self) -> None:
        self.calls = []

    def scroll(self, dx: int = 0, dy: int = 0):
        self.calls.append((dx, dy))
        applied = []
        if dy:
            applied.append("vertical")
        if dx:
            applied.append("horizontal")
        return {
            "status": "success",
            "dx": dx,
            "dy": dy,
            "applied": applied,
            "performed": bool(applied),
        }


def test_scroll_action_direction_to_deltas():
    scroll = ScrollAction(direction="left", amount=5)

    assert scroll.to_deltas() == (-5, 0)


def test_scroll_action_requires_direction_or_delta():
    with pytest.raises(ValidationError):
        ScrollAction()


def test_handle_scroll_maps_direction_and_metadata(monkeypatch):
    fake_mouse = FakeMouse()
    monkeypatch.setattr(executor, "MOUSE", fake_mouse)

    result = executor.handle_scroll(
        ActionStep(action="scroll", params={"direction": "up", "amount": 4})
    )

    assert fake_mouse.calls == [(0, 4)]
    assert result["metadata"]["delta"] == {"dx": 0, "dy": 4}
    assert result["metadata"]["direction"] == "up"
    assert result["metadata"]["amount"] == 4
    assert result["status"] == "success"


def test_handle_scroll_accepts_raw_deltas(monkeypatch):
    fake_mouse = FakeMouse()
    monkeypatch.setattr(executor, "MOUSE", fake_mouse)

    result = executor.handle_scroll(
        ActionStep(action="scroll", params={"dx": 10, "dy": -20})
    )

    assert fake_mouse.calls[-1] == (10, -20)
    assert result["metadata"]["delta"] == {"dx": 10, "dy": -20}
    assert result["metadata"]["direction"] is None
    assert result["metadata"]["amount"] is None
    assert result["status"] == "success"


def test_validate_action_plan_rejects_empty_scroll():
    with pytest.raises(ValueError):
        validate_action_plan({"task": "scroll", "steps": [{"action": "scroll", "params": {}}]})
