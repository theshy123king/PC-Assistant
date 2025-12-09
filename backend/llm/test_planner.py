"""
Deterministic planner used for local tests and user view scenarios.

This avoids external LLM calls and produces stable ActionPlans for known inputs.
"""

from pathlib import Path
from typing import Any, Dict

from backend.executor.actions_schema import validate_action_plan

DEFAULT_WORKSPACE = Path(__file__).resolve().parents[1] / "tests" / "test_data" / "workspace"


def _ws(rel: str) -> str:
    return str(DEFAULT_WORKSPACE / rel)


def build_test_plan(user_text: str, screenshot_base64: str | None = None) -> Dict[str, Any]:
    text = (user_text or "").lower()

    if "calculator" in text:
        plan = {
            "task": "open calculator",
            "steps": [{"action": "open_app", "params": {"target": "calculator"}}],
        }
    elif "sample note" in text and "backup" in text:
        plan = {
            "task": "copy a file",
            "steps": [
                {
                    "action": "copy_file",
                    "params": {"source": _ws("notes/readme.txt"), "destination_dir": _ws("backup")},
                }
            ],
        }
    elif "confirm" in text:
        plan = {"task": "click confirm button", "steps": [{"action": "click", "params": {"text": "Confirm"}}]}
    elif "download" in text:
        plan = {
            "task": "start download",
            "steps": [
                {
                    "action": "click",
                    "params": {"visual_description": "start download button", "strategy_hint": "vlm"},
                }
            ],
        }
    elif "settings" in text:
        plan = {
            "task": "open settings",
            "steps": [
                {"action": "click", "params": {"text": "Settings", "strategy_hint": "top_level"}},
            ],
        }
    elif "output.txt" in text or "hello pc assistant" in text:
        plan = {
            "task": "write and read",
            "steps": [
                {"action": "write_file", "params": {"path": _ws("output.txt"), "content": "Hello PC Assistant"}},
                {"action": "read_file", "params": {"path": _ws("output.txt")}},
            ],
        }
    elif "temp file" in text or "archive folder" in text:
        plan = {
            "task": "move and list",
            "steps": [
                {"action": "move_file", "params": {"source": _ws("temp/move_me.txt"), "destination_dir": _ws("archive")}},
                {"action": "list_files", "params": {"path": _ws("archive")}},
            ],
        }
    elif "delete c:/windows" in text or "delete c:\\windows" in text:
        return {"error": "dangerous_request", "error_type": "dangerous_request"}
    elif "search for qwen" in text:
        plan = {
            "task": "search qwen docs",
            "steps": [
                {"action": "open_url", "params": {"url": "https://www.google.com"}},
                {"action": "browser_input", "params": {"text": "Search", "value": "Qwen docs"}},
                {"action": "browser_click", "params": {"text": "Search"}},
            ],
        }
    elif "first search result" in text:
        plan = {"task": "read first search result", "steps": [{"action": "browser_extract_text", "params": {"text": "result title"}}]}
    elif "end-to-end" in text or "test_e2e" in text:
        plan = {
            "task": "file create move read",
            "steps": [
                {"action": "write_file", "params": {"path": _ws("test_e2e.txt"), "content": "end-to-end"}},
                {"action": "move_file", "params": {"source": _ws("test_e2e.txt"), "destination_dir": _ws("archive")}},
                {"action": "read_file", "params": {"path": _ws("archive/test_e2e.txt")}},
            ],
        }
    else:
        plan = {"task": "wait", "steps": [{"action": "wait", "params": {"seconds": 0.1}}]}

    validated = validate_action_plan(plan)
    return validated.model_dump()


__all__ = ["build_test_plan"]
