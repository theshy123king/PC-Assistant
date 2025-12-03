"""
Utilities to parse LLM output into an ActionPlan JSON structure.

This module extracts the first JSON object found in a text blob (including content
inside ```json code fences), parses it, and validates it against the ActionPlan
schema. No actions are executed here.
"""

import json
from typing import Any, Optional, Union

from backend.executor.actions_schema import ActionPlan, validate_action_plan


def _extract_json_from_fence(text: str) -> Optional[str]:
    marker = "```json"
    start = text.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = text.find("```", start)
    if end == -1:
        return None
    return text[start:end].strip()


def _extract_first_json_object(text: str) -> Optional[str]:
    """Extract the first JSON object substring by brace matching."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def parse_action_plan(llm_output: str) -> Union[ActionPlan, str]:
    """
    Extract and validate an ActionPlan from LLM output.

    Returns:
        ActionPlan on success, or an error string on failure.
    """
    if not isinstance(llm_output, str):
        return "error: llm_output must be a string"

    candidate = _extract_json_from_fence(llm_output) or _extract_first_json_object(
        llm_output
    )
    if not candidate:
        return "error: no JSON object found in LLM output"

    try:
        data: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return f"error: failed to parse JSON: {exc}"

    try:
        plan = validate_action_plan(data)
    except Exception as exc:  # noqa: BLE001
        return f"error: invalid action plan: {exc}"

    return plan
