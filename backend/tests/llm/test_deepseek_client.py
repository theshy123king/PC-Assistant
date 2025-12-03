import pytest

from backend.llm import deepseek_client


class DummyResponse:
    def __init__(self, json_data=None, status_code: int = 200):
        self._json_data = json_data or {
            "choices": [{"message": {"content": "dummy-reply"}}]
        }
        self.status_code = status_code
        self.reason_phrase = "OK"

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


class DummyClient:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return DummyResponse()


def test_call_deepseek_success(monkeypatch):
    dummy_client = DummyClient()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-valid")
    monkeypatch.setattr(deepseek_client.httpx, "Client", lambda: dummy_client)

    reply = deepseek_client.call_deepseek("ping")

    assert reply == "dummy-reply"
    assert dummy_client.calls[0]["json"]["messages"][0]["content"] == "ping"
    assert dummy_client.calls[0]["json"]["model"] == (
        deepseek_client.DEFAULT_DEEPSEEK_MODEL
    )
    assert (
        dummy_client.calls[0]["headers"]["Authorization"] == "Bearer sk-valid"
    )
    assert (
        dummy_client.calls[0]["url"] == deepseek_client.DEFAULT_DEEPSEEK_API_URL
    )


def test_call_deepseek_missing_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        deepseek_client.call_deepseek("ping")


def test_call_deepseek_invalid_key_format(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", ":sk-invalid")
    with pytest.raises(RuntimeError, match="does not look valid"):
        deepseek_client.call_deepseek("ping")


def test_call_deepseek_uses_messages(monkeypatch):
    dummy_client = DummyClient()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-valid")
    monkeypatch.setattr(deepseek_client.httpx, "Client", lambda: dummy_client)

    messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    deepseek_client.call_deepseek("fallback", messages=messages)

    assert dummy_client.calls[0]["json"]["messages"] == messages
