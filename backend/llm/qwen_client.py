import os
from typing import Any, Dict, List, Optional

from dashscope import Generation
from dotenv import load_dotenv

load_dotenv()


def _get_api_key() -> str:
    api_key = (os.getenv("QWEN_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("QWEN_API_KEY environment variable is not set")
    return api_key


def _extract_text(response: Dict[str, Any]) -> str:
    return response["output"]["text"]


def _messages_to_prompt(messages: Optional[List[dict]], fallback: str) -> str:
    if not messages:
        return fallback
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
            content = "\n".join(text_parts) if text_parts else ""
        parts.append(f"{role}: {content}")
    return "\n".join(parts) if parts else fallback


def call_qwen(text: str, messages: Optional[List[dict]] = None, model: Optional[str] = None) -> str:
    """Call Qwen model via DashScope and return the output text (text-only fallback)."""
    api_key = _get_api_key()
    prompt = _messages_to_prompt(messages, text)
    model_name = model or os.getenv("QWEN_MODEL", "qwen-turbo")
    last_exc: Exception | None = None
    # Simple retry loop to mitigate transient SSL/EOF issues.
    for attempt in range(3):
        try:
            response = Generation.call(
                model=model_name,
                prompt=prompt,
                api_key=api_key,
            )
            return _extract_text(response)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    raise RuntimeError(f"Qwen API call failed after retries: {last_exc}")

    try:
        return _extract_text(response)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Qwen API response missing text: {exc}") from exc
