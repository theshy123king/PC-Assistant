import pytest

from backend.executor.actions_schema import ActionStep
from backend.executor.executor import handle_wait_until


def test_wait_until_timeout_ok_false(monkeypatch):
    calls = {"count": 0}

    def fake_find_element(_query, policy=None):
        calls["count"] += 1
        return None

    monkeypatch.setattr("backend.executor.executor.find_element", fake_find_element)

    step = ActionStep(
        action="wait_until",
        params={"condition": "window_exists", "target": "Demo", "timeout": 0.01, "poll_interval": 0.005},
    )

    result = handle_wait_until(step)

    assert result["status"] == "timeout"
    assert result["ok"] is False
    assert result["condition"] == "window_exists"
    assert calls["count"] >= 1


def test_wait_until_timeout_with_require_raises(monkeypatch):
    def fake_find_element(_query, policy=None):
        return None

    monkeypatch.setattr("backend.executor.executor.find_element", fake_find_element)

    step = ActionStep(
        action="wait_until",
        params={"condition": "window_exists", "target": "Demo", "timeout": 0.01, "poll_interval": 0.005, "require": True},
    )

    with pytest.raises(RuntimeError) as excinfo:
        handle_wait_until(step)

    assert str(excinfo.value) == "wait_until failed: condition 'window_exists' not met within 0.01s"


def test_wait_until_success_sets_ok_true(monkeypatch):
    def fake_find_element(_query, policy=None):
        return {"kind": "window", "name": "Demo"}

    monkeypatch.setattr("backend.executor.executor.find_element", fake_find_element)

    step = ActionStep(
        action="wait_until",
        params={"condition": "window_exists", "target": "Demo", "timeout": 1.0, "poll_interval": 0.005, "require": True},
    )

    result = handle_wait_until(step)

    assert result["status"] == "success"
    assert result["ok"] is True
    assert result["condition"] == "window_exists"
