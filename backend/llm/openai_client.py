import os
from typing import List, Optional

from openai import OpenAI


def call_openai(prompt: str, messages: Optional[List[dict]] = None, model: Optional[str] = None) -> str:
    """Call OpenAI chat completions and return the assistant message content."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set")

    client = OpenAI(api_key=api_key)
    payload_messages = messages or [{"role": "user", "content": prompt}]
    response = client.chat.completions.create(
        model=model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=payload_messages,
    )
    return response.choices[0].message.content or ""
