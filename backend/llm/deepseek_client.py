import os
import time
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 3
MAX_RETRIES = 6
MAX_TIMEOUT = 90.0
BACKOFF_SECONDS = 2.0


def _get_api_key() -> str:
    """Return the configured API key or raise with a helpful error."""
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY environment variable is not set")
    if not api_key.startswith("sk-"):
        raise RuntimeError(
            "DEEPSEEK_API_KEY is set but does not look valid "
            "(expected to start with 'sk-')"
        )
    return api_key


def _build_payload(prompt: str, model: str, messages: Optional[List[dict]]) -> Dict[str, Any]:
    """Construct the request payload for DeepSeek chat completions."""
    payload_messages = messages or [{"role": "user", "content": prompt}]
    return {"model": model, "messages": payload_messages}


def call_deepseek(prompt: str, messages: Optional[List[dict]] = None, model: Optional[str] = None) -> str:
    """Call DeepSeek chat completions and return the assistant message content."""
    api_key = _get_api_key()
    url = os.getenv("DEEPSEEK_API_URL", DEFAULT_DEEPSEEK_API_URL)
    model_name = model or os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
    timeout_sec = min(float(os.getenv("DEEPSEEK_TIMEOUT", DEFAULT_TIMEOUT)), MAX_TIMEOUT)
    max_retries = min(int(os.getenv("DEEPSEEK_RETRIES", DEFAULT_RETRIES)), MAX_RETRIES)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = _build_payload(prompt, model_name, messages)

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client() as client:
                response = client.post(url, headers=headers, json=payload, timeout=timeout_sec)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response else "unknown"
                    reason = exc.response.reason_phrase if exc.response else ""
                    detail = ""
                    try:
                        detail_json = exc.response.json()
                        detail = detail_json.get("error") or detail_json
                    except Exception:
                        detail = (exc.response.text or "").strip() if exc.response else ""
                    raise RuntimeError(
                        f"DeepSeek API error {status} {reason}: {detail}".strip(": "),
                    ) from exc
                # Success, break loop
                break
        except httpx.TimeoutException as exc:
            last_error = exc
            if attempt >= max_retries:
                raise RuntimeError("DeepSeek API request timed out") from exc
            time.sleep(BACKOFF_SECONDS * attempt)
            continue
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt >= max_retries:
                raise RuntimeError("Failed to contact DeepSeek API") from exc
            time.sleep(BACKOFF_SECONDS * attempt)
            continue
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries:
                raise
            time.sleep(BACKOFF_SECONDS * attempt)
            continue
    else:
        if last_error:
            raise last_error
        raise RuntimeError("DeepSeek API call failed for unknown reasons")

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Failed to decode DeepSeek API response as JSON") from exc

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("DeepSeek API response is missing expected content") from exc
