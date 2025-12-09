"""
Multimodal locator using a vision-language model to propose bounding boxes.

Designed to work with OpenAI-compatible chat APIs that accept image content.
The actual provider call is injected to avoid hard dependencies and ease testing.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional


def _strip_fences(text: str) -> str:
    if "```" not in text:
        return text
    start = text.find("```")
    if start == -1:
        return text
    # find second fence
    end = text.find("```", start + 3)
    if end == -1:
        return text
    return text[start + 3 : end].strip()


def _parse_boxes(raw: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "boxes" in data:
        boxes = data.get("boxes")
    else:
        boxes = data if isinstance(data, list) else []

    parsed: List[Dict[str, Any]] = []
    for item in boxes or []:
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("name") or "target"
        bbox = item.get("bbox") or item.get("box") or item
        try:
            x = float(bbox.get("x"))
            y = float(bbox.get("y"))
            w = float(bbox.get("width") or bbox.get("w"))
            h = float(bbox.get("height") or bbox.get("h"))
        except Exception:
            continue
        parsed.append(
            {
                "label": str(label),
                "bounds": {"x": x, "y": y, "width": w, "height": h},
                "center": {"x": x + w / 2.0, "y": y + h / 2.0},
            }
        )
    return parsed


def locate_with_vlm(
    query: str,
    image_base64: str,
    provider_call: Callable[[str, Optional[List[dict]]], str],
    provider_name: str = "deepseek",
) -> Dict[str, Any]:
    """
    Use a multimodal LLM to locate a visually described target.

    provider_call should accept (prompt_text, messages) and return a string reply.
    """
    if not image_base64:
        return {"status": "error", "reason": "missing_image"}
    if not provider_call:
        return {"status": "error", "reason": "missing_provider"}

    system = (
        "You are a UI vision locator. Given an image and a target description, "
        "return JSON with a 'boxes' list of objects: "
        "[{'label': '<match>', 'bbox': {'x': <int>, 'y': <int>, 'width': <int>, 'height': <int>}}]. "
        "Use pixel coordinates relative to the provided image; return the best match first."
    )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Find: {query}"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
            ],
        },
    ]
    prompt_text = "\n".join(f"{m['role']}: {m['content']}" for m in messages if isinstance(m.get("content"), str))

    raw_reply = provider_call(prompt_text, messages)
    cleaned = _strip_fences(raw_reply or "")
    boxes = _parse_boxes(cleaned)

    if not boxes:
        return {"status": "error", "reason": "no_boxes", "provider": provider_name, "raw": raw_reply}

    top = boxes[0]
    return {
        "status": "success",
        "provider": provider_name,
        "raw": raw_reply,
        "boxes": boxes,
        "top": top,
    }


__all__ = ["locate_with_vlm"]
