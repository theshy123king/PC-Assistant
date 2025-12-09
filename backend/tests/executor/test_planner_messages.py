from backend.executor.executor import _call_planner_with_fallback
from backend.llm.planner_prompt import format_prompt


def test_doubao_planner_uses_text_when_no_vision_model(monkeypatch):
    """Without a vision model, Doubao should fall back to text-only messages."""
    prompt_bundle = format_prompt("demo task", image_base64="abc123")
    monkeypatch.setenv("DOUBAO_API_KEY", "dummy-key")
    monkeypatch.delenv("DOUBAO_VISION_MODEL", raising=False)
    monkeypatch.setenv("DOUBAO_MODEL", "doubao-seed-1-6-lite-251015")

    captured = {}

    def fake_call(prompt_text, messages):
        captured["prompt"] = prompt_text
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr("backend.executor.executor.call_doubao", fake_call)

    provider, reply = _call_planner_with_fallback("doubao", prompt_bundle)

    assert provider == "doubao"
    assert captured["messages"] == prompt_bundle.messages
    assert reply == "ok"


def test_doubao_planner_uses_vision_when_configured(monkeypatch):
    """With a vision model configured, Doubao should receive vision payloads."""
    prompt_bundle = format_prompt("demo task", image_base64="abc123")
    monkeypatch.setenv("DOUBAO_API_KEY", "dummy-key")
    monkeypatch.setenv("DOUBAO_VISION_MODEL", "doubao-seed-1-6-vision-251015")

    captured = {}

    def fake_call(prompt_text, messages):
        captured["prompt"] = prompt_text
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr("backend.executor.executor.call_doubao", fake_call)

    provider, reply = _call_planner_with_fallback("doubao", prompt_bundle)

    assert provider == "doubao"
    assert captured["messages"] == prompt_bundle.vision_messages
    assert reply == "ok"
