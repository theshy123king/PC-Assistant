"""
Central VLM provider/model configuration helper.
"""

import os
from typing import Callable, Optional, Tuple

from backend.llm.deepseek_client import call_deepseek
from backend.llm.qwen_client import call_qwen
from backend.llm.doubao_client import call_doubao

ProviderCall = Callable[[str, Optional[list], Optional[str]], str]


def get_vlm_config() -> Tuple[str, str]:
    provider = (os.getenv("VLM_PROVIDER") or "deepseek").lower()
    model = os.getenv("VLM_MODEL", "").strip()
    return provider, model


def get_vlm_call() -> Tuple[str, Callable[[str, Optional[list]], str]]:
    provider, model = get_vlm_config()

    def _strip_vision(messages: Optional[list]) -> Optional[list]:
        """Convert any multimodal content to plain text for providers that do not support images."""
        if not messages:
            return messages
        stripped = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            text = ""
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            parts.append(str(item.get("text", "")))
                        elif item.get("type") == "image_url":
                            # drop image parts for non-vision providers
                            continue
                        elif item.get("image_url"):
                            continue
                    else:
                        parts.append(str(item))
                text = "\n".join([p for p in parts if p])
            elif isinstance(content, dict):
                if content.get("type") == "text":
                    text = str(content.get("text", ""))
                elif content.get("type") == "image_url" or content.get("image_url"):
                    text = ""
                else:
                    text = str(content)
            elif isinstance(content, str):
                text = content
            else:
                text = str(content)
            stripped.append({"role": role, "content": text})
        return stripped

    def _build_provider_call(name: str) -> Tuple[str, Callable[[str, Optional[list]], str]]:
        if name == "qwen":
            return name, (lambda prompt, messages: call_qwen(prompt, messages, model=model or None))
        if name == "doubao":
            return name, (lambda prompt, messages: call_doubao(prompt, messages, model=model or None))
        # deepseek (text-only): strip images before calling
        return "deepseek", (lambda prompt, messages: call_deepseek(prompt, _strip_vision(messages), model=model or None))

    def _available(name: str) -> bool:
        if name == "qwen":
            return bool(os.getenv("QWEN_API_KEY"))
        if name == "doubao":
            return bool(os.getenv("DOUBAO_API_KEY"))
        if name == "deepseek":
            return bool(os.getenv("DEEPSEEK_API_KEY"))
        return False

    def _doubao_vision_available() -> bool:
        if not os.getenv("DOUBAO_API_KEY"):
            return False
        vision_model = os.getenv("DOUBAO_VISION_MODEL") or ""
        fallback_model = os.getenv("DOUBAO_MODEL") or ""
        return bool(vision_model or ("vision" in fallback_model.lower()))

    # If user specified provider, honor it (with stripping if deepseek), else auto-pick best available.
    preferred = provider if provider else None
    if preferred in {"deepseek", "qwen", "doubao"} and _available(preferred):
        return _build_provider_call(preferred)

    # Auto selection: prefer Qwen (supports vision), then Doubao vision, then DeepSeek (text-only).
    if _available("qwen"):
        return _build_provider_call("qwen")
    if _doubao_vision_available():
        return _build_provider_call("doubao")
    if _available("deepseek"):
        return _build_provider_call("deepseek")

    # Fallback: safest text-only call without credentials will error at call site.
    return _build_provider_call("deepseek")


__all__ = ["get_vlm_call", "get_vlm_config"]
