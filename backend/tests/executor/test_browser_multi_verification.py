import json

import backend.executor.executor as executor
from backend.executor.actions_schema import ActionPlan, ActionStep
from fastapi.testclient import TestClient


class StubHandler:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, step):
        self.calls += 1
        idx = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[idx]


def test_open_url_verifies_url(monkeypatch):
    handler = StubHandler([{"status": "success", "url": "https://example.com/home", "title": "Example Home"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "open_url", handler)
    plan = ActionPlan(task="browse", steps=[ActionStep(action="open_url", params={"url": "https://example.com"})])
    result = executor.run_steps(plan, consent_token=True, capture_observations=False)
    assert handler.calls == 1
    assert result["overall_status"] == "success"
    entry = result["logs"][-1]
    assert entry["verification"]["verifier"] == "browser_url"
    assert entry["evidence"]["actual"]["url"].startswith("https://example.com")


def test_browser_click_missing_expected_fails(monkeypatch):
    handler = StubHandler([{"status": "success", "url": "https://example.com"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_click", handler)
    plan = ActionPlan(task="click", steps=[ActionStep(action="browser_click", params={"text": "Go", "title": "Browser"})])
    wp = type("WP", (), {"get_foreground_window": lambda self: {"title": "Browser"}})()
    result = executor.run_steps(plan, consent_token=True, capture_observations=False, window_provider=wp)
    assert handler.calls >= 1
    entry = [log for log in result["logs"] if log.get("action") == "browser_click"][-1]
    assert entry["reason"] == "missing_expected_verify"


def test_browser_click_retries_and_succeeds(monkeypatch):
    responses = [
        {"status": "success", "url": ""},  # first attempt missing url/text
        {"status": "success", "url": "https://example.com/next", "text": "Ready"},
    ]
    handler = StubHandler(responses)
    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_click", handler)
    plan = ActionPlan(
        task="click",
        steps=[
            ActionStep(
                action="browser_click",
                params={"expected_url": "example.com/next", "verify_text": ["Ready"], "max_retries": 1, "title": "Browser"},
            )
        ],
    )
    wp = type("WP", (), {"get_foreground_window": lambda self: {"title": "Browser"}})()
    result = executor.run_steps(plan, consent_token=True, capture_observations=False, window_provider=wp)
    assert handler.calls >= 2
    entry = result["logs"][-1]
    assert entry["status"] == "success"
    assert len(entry["attempts"]) == 2
    assert entry["verification"]["verifier"] == "browser_text"


def test_browser_input_requires_value(monkeypatch):
    handler = StubHandler([{"status": "success", "value": "hello world"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_input", handler)
    plan = ActionPlan(task="input", steps=[ActionStep(action="browser_input", params={"value": "hello world", "title": "Browser"})])
    wp = type("WP", (), {"get_foreground_window": lambda self: {"title": "Browser"}})()
    result = executor.run_steps(plan, consent_token=True, capture_observations=False, window_provider=wp)
    assert handler.calls == 1
    entry = result["logs"][-1]
    assert entry["status"] == "success"
    assert entry["verification"]["verifier"] == "browser_text"


def test_browser_extract_text_nonempty(monkeypatch):
    handler = StubHandler([{"status": "success", "text": ""}, {"status": "success", "text": "Status Ready"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_extract_text", handler)
    plan = ActionPlan(
        task="extract",
        steps=[ActionStep(action="browser_extract_text", params={"text": "Status", "max_retries": 1})],
    )
    result = executor.run_steps(plan, consent_token=True, capture_observations=False)
    assert handler.calls == 2
    entry = result["logs"][-1]
    assert entry["status"] == "success"
    assert entry["verification"]["verifier"] == "browser_extract"


def test_execute_plan_retries_then_passes(monkeypatch):
    handler = StubHandler(
        [
            {"status": "success", "url": "", "text": ""},
            {"status": "success", "url": "https://example.com/form", "text": "Done"},
        ]
    )
    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_click", handler)
    from backend.app import app

    client = TestClient(app)
    payload = {
        "task": "browser",
        "steps": [
            {
                "action": "browser_click",
                "params": {"expected_url": "example.com/form", "verify_text": ["Done"], "max_retries": 1, "title": "Browser"},
            }
        ],
        "consent_token": True,
    }
    class AlwaysBrowser(executor._DefaultWindowProvider):
        def get_foreground_window(self):
            return {"title": "Browser"}

    monkeypatch.setattr(executor, "_DefaultWindowProvider", AlwaysBrowser)
    resp = client.post("/api/ai/execute_plan", json=payload)
    data = resp.json()
    assert handler.calls >= 2
    assert data["overall_status"] == "success"
    entry = data["logs"][-1]
    assert entry["verification"]["verifier"] == "browser_text"
    assert len(entry["attempts"]) == 2
    ev = entry["attempts"][-1]["evidence"]
    assert ev["capture_phase"] == "verify"
    assert "url" in (ev.get("actual") or {}) or ev.get("text_result") is not None


def test_dry_run_skips_browser_dispatch(monkeypatch):
    handler = StubHandler([{"status": "success", "url": "https://example.com"}])
    monkeypatch.setitem(executor.ACTION_HANDLERS, "browser_click", handler)
    plan = ActionPlan(task="dry", steps=[ActionStep(action="browser_click", params={"expected_url": "example.com"})])
    result = executor.run_steps(plan, dry_run=True, consent_token=True)
    assert handler.calls == 0
    assert result["overall_status"] == "dry_run"
