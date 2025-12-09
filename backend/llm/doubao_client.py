import os
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com"
CHAT_PATH = "/api/v3/chat/completions"
DEFAULT_DOUBAO_TEXT_MODEL = ""
DEFAULT_DOUBAO_VISION_MODEL = ""
DEFAULT_DOUBAO_CODE_MODEL = ""
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 3
DEFAULT_REASONING = "low"  # minimal|low|medium|high
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TOP_P = 0.95


def _get_api_key() -> str:
    api_key = (os.getenv("DOUBAO_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("DOUBAO_API_KEY environment variable is not set")
    return api_key


def _is_vision(messages: Optional[List[dict]]) -> bool:
    if not messages:
        return False
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") != "text":
                    return True
                if isinstance(item, dict) and item.get("image_url"):
                    return True
        elif isinstance(content, dict) and content.get("image_url"):
            return True
    return False


def _normalize_content(content: Any) -> Any:
    """
    Normalize message content for Doubao Chat API.
    Supports:
    - plain string content
    - OpenAI-style multimodal arrays: [{"type": "text", "text": ...}, {"type": "image_url", "image_url": {"url": ...}}]
    - shorthand dicts with type/image_url
    """
    if isinstance(content, list):
        normalized_items: List[Dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    normalized_items.append({"type": "text", "text": str(item.get("text", ""))})
                    continue
                if item_type == "image_url" or item.get("image_url"):
                    img = item.get("image_url") or item.get("url")
                    url = None
                    if isinstance(img, str):
                        url = img
                    elif isinstance(img, dict):
                        url = img.get("url") or img.get("image_url")
                    if not url:
                        raise RuntimeError("Invalid image_url payload for Doubao vision request")
                    normalized_items.append({"type": "image_url", "image_url": {"url": url}})
                    continue
            # Fallback: coerce unknown item to text
            normalized_items.append({"type": "text", "text": str(item)})
        return normalized_items

    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        if content.get("type") == "image_url" or content.get("image_url"):
            img = content.get("image_url") or content.get("url")
            url = None
            if isinstance(img, str):
                url = img
            elif isinstance(img, dict):
                url = img.get("url") or img.get("image_url")
            if not url:
                raise RuntimeError("Invalid image_url payload for Doubao vision request")
            return [{"type": "image_url", "image_url": {"url": url}}]

    if not isinstance(content, str):
        return str(content)
    return content


def _build_payload(
    prompt: str,
    model: str,
    messages: Optional[List[dict]],
    reasoning_effort: Optional[str] = None,
    stream: bool = False,
) -> Dict[str, Any]:
    payload_messages = messages or [{"role": "user", "content": prompt}]
    normalized_messages: List[Dict[str, Any]] = []
    for msg in payload_messages:
        role = msg.get("role", "user")
        content = _normalize_content(msg.get("content", ""))
        normalized_messages.append({"role": role, "content": content})

    temperature_env = os.getenv("DOUBAO_TEMPERATURE")
    try:
        temperature_val = float(temperature_env) if temperature_env is not None else DEFAULT_TEMPERATURE
    except Exception:
        temperature_val = DEFAULT_TEMPERATURE

    top_p_env = os.getenv("DOUBAO_TOP_P")
    try:
        top_p_val = float(top_p_env) if top_p_env is not None else DEFAULT_TOP_P
    except Exception:
        top_p_val = DEFAULT_TOP_P

    payload: Dict[str, Any] = {
        "model": model,
        "messages": normalized_messages,
        "stream": bool(stream),
        "temperature": temperature_val,
        "top_p": top_p_val,
    }
    effort = (reasoning_effort or os.getenv("DOUBAO_REASONING_EFFORT") or DEFAULT_REASONING).strip()
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def _send_chat_completion(
    client: httpx.Client,
    base_url: str,
    payload: Dict[str, Any],
    timeout_sec: float,
) -> httpx.Response:
    url = base_url.rstrip("/") + CHAT_PATH
    return client.post(url, json=payload, timeout=timeout_sec)


def _send_vision_completion(
    client: httpx.Client,
    base_url: str,
    payload: Dict[str, Any],
    timeout_sec: float,
) -> httpx.Response:
    # vision uses the same endpoint; payload should already include image content in messages.
    url = base_url.rstrip("/") + CHAT_PATH
    return client.post(url, json=payload, timeout=timeout_sec)


def call_doubao(
    prompt: str,
    messages: Optional[List[dict]] = None,
    model: Optional[str] = None,
    stream: bool = False,
) -> str:
    """Call Doubao chat completions (text or vision)."""
    api_key = _get_api_key()
    base_env = os.getenv("DOUBAO_BASE_URL")
    base_candidates = [base_env] if base_env else []
    base_candidates += [u for u in [DEFAULT_DOUBAO_BASE_URL] if u not in base_candidates]
    is_vision = _is_vision(messages)
    if model:
        model_name = _validate_model_id(model)
    elif is_vision:
        model_name = (
            os.getenv("DOUBAO_VISION_MODEL")
            or os.getenv("DOUBAO_MODEL")
            or DEFAULT_DOUBAO_VISION_MODEL
        )
    else:
        model_name = (
            os.getenv("DOUBAO_MODEL")
            or os.getenv("DOUBAO_TEXT_MODEL")
            or DEFAULT_DOUBAO_TEXT_MODEL
        )
    model_name = _validate_model_id(model_name)
    if is_vision and not model_name:
        raise RuntimeError("Vision request detected but no vision-capable DOUBAO_MODEL set")

    timeout_sec = float(os.getenv("DOUBAO_TIMEOUT", DEFAULT_TIMEOUT))
    max_retries = int(os.getenv("DOUBAO_RETRIES", DEFAULT_RETRIES))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = _build_payload(prompt, model_name, messages, stream=stream)

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        for base_url in base_candidates:
            try:
                with httpx.Client(headers=headers) as client:
                    sender = _send_vision_completion if is_vision else _send_chat_completion
                    response = sender(client, base_url, payload, timeout_sec)
                    response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response is not None and exc.response.status_code == 404:
                    # try next base
                    continue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                # try next base/attempt
                continue
        if attempt >= max_retries:
            break
    raise RuntimeError(f"Doubao API call failed: {last_exc}")
def _validate_model_id(model_name: str) -> str:
    """Ark requires exact model IDs; reject friendly names."""
    if not model_name or not isinstance(model_name, str):
        raise RuntimeError("DOUBAO model ID is not set; please provide an exact model ID from Ark console")
    normalized = model_name.strip()
    # Friendly names to reject explicitly.
    friendly = {
        "doubao-seed-1.6-lite",
        "doubao-seed-1.6-vision",
        "doubao-seed-code",
        "Doubao-1.6-lite",
        "Doubao-1.6-vision",
        "Doubao-Seed-1.6-lite",
        "Doubao-Seed-1.6-vision",
    }
    if normalized in friendly:
        raise RuntimeError(
            f"DOUBAO_MODEL must be an exact model ID from Ark (e.g., doubao-seed-1-6-lite-251015), "
            f"not friendly name '{normalized}'"
        )
    return normalized
