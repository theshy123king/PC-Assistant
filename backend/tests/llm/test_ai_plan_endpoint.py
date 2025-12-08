from fastapi.testclient import TestClient

from backend.app import app


def test_ai_plan_uses_provider_after_initialization(monkeypatch):
    """
    Regression: provider must be initialized before branching on it.
    """

    def fake_call_deepseek(prompt: str, messages=None):
        # Return a minimal valid action plan
        return '{"task":"demo","steps":[{"action":"wait","params":{"seconds":1}}]}'

    monkeypatch.setattr("backend.app.call_deepseek", fake_call_deepseek)

    client = TestClient(app)
    resp = client.post(
        "/api/ai/plan",
        json={"provider": "deepseek", "text": "do something", "screenshot_base64": None},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["provider"] == "deepseek"
    assert data["plan"]["steps"]
