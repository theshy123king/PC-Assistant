"""
Central VLM provider/model configuration helper.
"""

import os
from typing import Callable, Optional, Tuple

from backend.llm.deepseek_client import call_deepseek
from backend.llm.openai_client import call_openai
from backend.llm.qwen_client import call_qwen

ProviderCall = Callable[[str, Optional[list], Optional[str]], str]


def get_vlm_config() -> Tuple[str, str]:
    provider = (os.getenv("VLM_PROVIDER") or "openai").lower()
    model = os.getenv("VLM_MODEL", "").strip()
    return provider, model


def get_vlm_call() -> Tuple[str, Callable[[str, Optional[list]], str]]:
    provider, model = get_vlm_config()
    if provider == "deepseek":
        return provider, (lambda prompt, messages: call_deepseek(prompt, messages, model=model or None))
    if provider == "qwen":
        return provider, (lambda prompt, messages: call_qwen(prompt, messages, model=model or None))
    # default to openai
    return "openai", (lambda prompt, messages: call_openai(prompt, messages, model=model or None))


__all__ = ["get_vlm_call", "get_vlm_config"]
