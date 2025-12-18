from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClarificationOption(BaseModel):
    label: str
    value: str


class ClarificationPayload(BaseModel):
    question: str
    options: List[ClarificationOption]
    hint: Optional[str] = None


class PlanError(BaseModel):
    category: str
    message: str
    detail: Optional[Any] = None


KEYWORDS = {"google", "谷歌", "bing", "必应", "baidu", "百度", "homepage", "首页", "browser", "浏览器"}


def maybe_short_circuit_to_clarification(user_text: str) -> Optional[Dict[str, Any]]:
    """
    Detect vague browser open intent lacking a concrete URL or target and return a clarification payload.
    """
    if not user_text or not isinstance(user_text, str):
        return None
    text = user_text.lower()
    if "http://" in text or "https://" in text:
        return None
    if any(kw in text for kw in KEYWORDS):
        options = [
            ClarificationOption(label="Open Google", value="Open https://www.google.com"),
            ClarificationOption(label="Open Bing", value="Open https://www.bing.com"),
            ClarificationOption(label="Open Baidu", value="Open https://www.baidu.com"),
        ]
        clarification = ClarificationPayload(
            question="Which homepage should I open?",
            options=options,
            hint="Select a homepage to proceed or provide a specific URL.",
        )
        return {"plan_status": "awaiting_user", "clarification": clarification.model_dump()}
    return None


def ensure_plan_outcome(user_text: str, plan_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure a plan has an explicit outcome; convert empty steps to awaiting_user or error.
    """
    steps = (plan_data or {}).get("steps") or []
    if steps:
        return {"plan_status": "ok"}

    # Fallback clarification when plan is empty.
    options = [
        ClarificationOption(label="Open Google", value="Open https://www.google.com"),
        ClarificationOption(label="Open Bing", value="Open https://www.bing.com"),
        ClarificationOption(label="Open Baidu", value="Open https://www.baidu.com"),
    ]
    clarification = ClarificationPayload(
        question="No actionable steps were generated. Choose a homepage or provide a URL.",
        options=options,
        hint="Respond with one of the options or a specific URL to continue.",
    )
    return {"plan_status": "awaiting_user", "clarification": clarification.model_dump()}


def build_plan_error(category: str, message: str, detail: Optional[Any] = None) -> Dict[str, Any]:
    return {
        "plan_status": "error",
        "plan_error": PlanError(category=category, message=message, detail=detail).model_dump(),
    }
