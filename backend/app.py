import base64
import json
import asyncio
import os
import re
from urllib.parse import parse_qs, quote_plus, urlparse, unquote
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from PIL import Image

from backend.executor import executor
from backend.executor.actions_schema import (
    ActionPlan,
    ActionStep,
    PlanValidationError,
    validate_plan_with_warnings,
)
from backend.executor.apps import get_cached_alias
from backend.executor.task_context import TaskContext
from backend.executor.task_registry import get_task, update_task, TaskStatus
from backend.llm.action_parser import parse_action_plan
from backend.llm.deepseek_client import call_deepseek
from backend.llm.doubao_client import call_doubao
from backend.llm.planner_prompt import PromptBundle, format_prompt
try:
    import pygetwindow as gw
except Exception:
    gw = None
from backend.llm.qwen_client import call_qwen
from backend.llm.test_planner import build_test_plan
from backend.vision.ocr import run_ocr, run_ocr_with_boxes
from backend.vision.screenshot import capture_screen
from backend.logging_setup import setup_logging
from backend.logging_utils import (
    generate_request_id,
    log_event,
    sanitize_payload,
    summarize_execution,
    summarize_plan,
)

setup_logging()
load_dotenv()


class CommandRequest(BaseModel):
    text: str


class AIQueryRequest(BaseModel):
    provider: str
    text: str
    screenshot_base64: str | None = None


app = FastAPI()


def _respond_invalid_plan(request_id: str, errors: list[dict], provider: str | None = None) -> dict:
    """Build a structured invalid-plan response."""
    base = {
        "error": "invalid action plan",
        "request_id": request_id,
        "validation_errors": errors,
    }
    if provider:
        base["provider"] = provider
    return base


def _validate_plan(plan_data: dict | ActionPlan, request_id: str) -> tuple[ActionPlan | None, list[dict] | None, list[dict]]:
    """
    Validate and normalize a plan, returning (plan, warnings, errors).

    Errors are structured per step; warnings capture conservative normalization (aliases/defaults).
    """
    try:
        plan, warnings = validate_plan_with_warnings(plan_data)
        return plan, warnings, []
    except PlanValidationError as exc:
        return None, None, exc.errors
    except Exception as exc:  # noqa: BLE001
        return None, None, [{"step_index": None, "action": None, "field": None, "reason": str(exc)}]


def _provider_available(name: str) -> bool:
    name = (name or "").lower()
    if name == "deepseek":
        return bool(os.getenv("DEEPSEEK_API_KEY"))
    if name == "doubao":
        return bool(os.getenv("DOUBAO_API_KEY"))
    if name == "qwen":
        return bool(os.getenv("QWEN_API_KEY"))
    return False


def _doubao_vision_enabled() -> bool:
    """Return True if a Doubao vision-capable model is configured."""
    vision_model = os.getenv("DOUBAO_VISION_MODEL")
    fallback_model = os.getenv("DOUBAO_MODEL")
    if vision_model:
        return True
    if fallback_model and "vision" in fallback_model.lower():
        return True
    return False


def _cached_open_plan(user_text: str) -> ActionPlan | None:
    cached = get_cached_alias(user_text)
    if not cached:
        return None
    target = cached.get("target") or cached.get("path") or user_text
    params = {"target": target, "user_query": user_text}
    return ActionPlan(task=user_text, steps=[ActionStep(action="open_app", params=params)])


def _maybe_enforce_open_app(user_text: str, plan_data: dict) -> dict:
    """Heuristically force open_app for common app-launch intents (e.g., WeChat) to avoid click-only plans."""
    text_lower = (user_text or "").lower()
    if not plan_data or not isinstance(plan_data, dict):
        return plan_data
    steps = plan_data.get("steps") or []
    if not isinstance(steps, list):
        return plan_data

    intents = {
        "wechat": ["wechat", "微信", "weixin"],
    }
    for target_key, keywords in intents.items():
        if not any(k in text_lower for k in keywords):
            continue
        has_open = any(
            (s.get("action") == "open_app" and isinstance(s.get("params"), dict) and target_key in str(s["params"].get("target", "")).lower())
            for s in steps
            if isinstance(s, dict)
        )
        if has_open:
            continue
        # Remove leading click/activate_window steps that try to activate the same app to avoid redundant actions.
        filtered: list[dict] = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            if s.get("action") in {"click", "activate_window"}:
                params = s.get("params") or {}
                combined = f"{params} {s.get('action')} {s.get('target', '')}".lower()
                if any(k in combined for k in keywords):
                    continue
            filtered.append(s)
        steps = filtered
        steps.insert(0, {"action": "open_app", "params": {"target": target_key, "user_query": user_text}})
        plan_data["steps"] = steps
        break
    return plan_data


def _maybe_rewrite_open_file(user_text: str, plan_data: dict) -> dict:
    """Convert read_file to open_file for pure 'open file' intents."""
    if not plan_data or not isinstance(plan_data, dict):
        return plan_data
    steps = plan_data.get("steps")
    if not isinstance(steps, list):
        return plan_data
    text_lower = (user_text or "").lower()
    open_hints = ["打开", "open"]
    read_hints = ["读取", "read", "内容", "查看", "look at", "read file"]
    wants_open = any(h in text_lower for h in open_hints)
    wants_read = any(h in text_lower for h in read_hints)
    if not wants_open or wants_read:
        return plan_data
    for step in steps:
        if isinstance(step, dict) and step.get("action") == "read_file":
            step["action"] = "open_file"
    plan_data["steps"] = steps
    return plan_data


def _normalize_workspace_paths(plan_data: dict, work_dir: str | None) -> dict:
    """Normalize LLM-produced path placeholders like 'current_directory' to '.' so base_dir can apply."""
    if not plan_data or not isinstance(plan_data, dict):
        return plan_data
    steps = plan_data.get("steps")
    if not isinstance(steps, list):
        return plan_data
    tokens = {"current_directory", "current dir", "current folder", "workspace", ".", "./", ""}
    for step in steps:
        if not isinstance(step, dict):
            continue
        params = step.get("params")
        if not isinstance(params, dict):
            continue
        for key in ("path", "destination_dir", "destination", "source"):
            raw = params.get(key)
            if isinstance(raw, str) and raw.strip().lower() in tokens:
                params[key] = "."
        step["params"] = params
    plan_data["steps"] = steps
    return plan_data


def _ensure_delete_confirm(plan_data: dict) -> dict:
    """Safety helper: require confirm=True for delete_file; auto-inject if missing."""
    if not plan_data or not isinstance(plan_data, dict):
        return plan_data
    steps = plan_data.get("steps")
    if not isinstance(steps, list):
        return plan_data
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("action") == "delete_file":
            params = step.get("params") or {}
            if "confirm" not in params:
                params["confirm"] = True
            step["params"] = params
    plan_data["steps"] = steps
    return plan_data


def _clean_query(text: str) -> str:
    """Extract core search term by truncating at common connector words."""
    stops = [
        " and",
        " then",
        " read",
        " return",
        " extract",
        " 并",
        "并",
        " 然后",
        "然后",
        " 返回",
        "返回",
        " 查看",
        "查看",
        " 告诉我",
        "告诉我",
        " 给我",
        "给我",
        " 找",
    ]
    candidate = text or ""
    candidate = candidate.strip().strip("'\"“”‘’").strip()
    lower = candidate.lower()
    cut = len(candidate)
    for stop in stops:
        pos = lower.find(stop)
        if pos != -1 and pos < cut:
            cut = pos
    candidate = candidate[:cut]
    return candidate.strip(" ，,。:.：;")


def _extract_query_from_user_text(user_text: str) -> str | None:
    """Extract a search query directly from the user instruction."""
    if not user_text or not isinstance(user_text, str):
        return None
    text = user_text.strip()
    patterns = [
        r"(?:搜索|搜一下|搜一搜|查一下|查找|查询|找一下|找一找)\s*[:：]?\s*(.+)",
        r"(?:search(?: for)?|find)\s*[:：]?\s*(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1)
        cleaned = _clean_query(candidate)
        if cleaned:
            return cleaned
    return None


def _normalize_browser_preference(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None

    edge_terms = [
        "edge",
        "msedge",
        "microsoft edge",
        "微软edge",
        "微软 edge",
        "微软浏览器",
    ]
    if any(term in text for term in edge_terms):
        return "edge"

    chrome_terms = [
        "chrome",
        "google chrome",
        "chrome.exe",
        "谷歌浏览器",
        "google浏览器",
        "google 浏览器",
        "chrome浏览器",
        "chrome 浏览器",
    ]
    if any(term in text for term in chrome_terms) or ("谷歌" in text and "浏览器" in text):
        return "chrome"

    firefox_terms = ["firefox", "火狐"]
    if any(term in text for term in firefox_terms):
        return "firefox"

    safari_terms = ["safari"]
    if any(term in text for term in safari_terms):
        return "safari"

    return None


def _maybe_rewrite_web_search(user_text: str, plan_data: dict) -> dict:
    """Rewrite search intents into a robust open_url + visual extract flow."""
    if not plan_data or not isinstance(plan_data, dict):
        return plan_data
    steps = plan_data.get("steps")
    if not isinstance(steps, list):
        return plan_data

    text_lower = (user_text or "").lower()
    search_hints = ["搜索", "search", "查一下", "find", "google", "bing", "baidu"]
    if not any(h in text_lower for h in search_hints):
        return plan_data

    web_context_hints = [
        "edge",
        "msedge",
        "microsoft edge",
        "chrome",
        "firefox",
        "safari",
        "浏览器",
        "网页",
        "bing",
        "google",
        "baidu",
    ]
    has_browser_actions = any(
        isinstance(s, dict)
        and s.get("action")
        in {
            "open_url",
            "browser_input",
            "browser_click",
            "browser_extract_text",
            "web_search",
        }
        for s in steps
    )
    mentions_web = any(h in text_lower for h in web_context_hints)
    if not has_browser_actions and not mentions_web:
        return plan_data

    query: str | None = None
    browser_pref: str | None = None

    # 1) browser_input value
    for step in steps:
        if not isinstance(step, dict):
            continue
        if not browser_pref and step.get("action") == "open_app":
            params = step.get("params") or {}
            target = params.get("target") or params.get("app")
            browser_pref = _normalize_browser_preference(target)
        if step.get("action") == "browser_input":
            params = step.get("params") or {}
            val = params.get("value")
            if isinstance(val, str) and val.strip():
                query = _clean_query(val)
        if step.get("action") == "open_url":
            params = step.get("params") or {}
            b = params.get("browser")
            if isinstance(b, str) and b.strip():
                browser_pref = _normalize_browser_preference(b) or b.strip()
        if query and browser_pref:
            break

    if not browser_pref:
        browser_pref = _normalize_browser_preference(user_text)

    # 2) open_url query params
    if not query:
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("action") != "open_url":
                continue
            params = step.get("params") or {}
            url_val = params.get("url")
            if not isinstance(url_val, str):
                continue
            try:
                parsed = urlparse(url_val)
                qs = parse_qs(parsed.query)
                raw_q = (qs.get("q") or qs.get("wd") or qs.get("text") or qs.get("query") or [None])[0]
                if raw_q:
                    query = _clean_query(unquote(raw_q))
                    break
            except Exception:
                continue

    if not query:
        query = _extract_query_from_user_text(user_text)

    if not query:
        return plan_data

    encoded = quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}"
    browser = browser_pref or "edge"
    new_steps = [
        {"action": "open_url", "params": {"url": url, "browser": browser, "verify_text": ["Results", "结果", "search results"]}},
        {"action": "wait", "params": {"seconds": 4.0}},
        {
            "action": "browser_extract_text",
            "params": {
                "text": query,
                "target": "第一条搜索结果标题",
                "visual_description": "What is the title of the first main search result? Return ONLY the title text.",
                "strategy_hint": "vlm_read",
                "prefer_top_line": True,
            },
        },
    ]
    plan_data["steps"] = new_steps
    plan_data["_rewrite_reason"] = "visual_web_search_v3_cleaned"
    return plan_data


def _summarize_logs(logs: list[dict] | None, limit: int = 10) -> list[dict]:
    """Lightweight step-level log summary for persistent logging."""
    if not isinstance(logs, list):
        return []
    summary: list[dict] = []
    for entry in logs[:limit]:
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message")
        if isinstance(msg, dict):
            msg = sanitize_payload(msg, keep_full={"text", "content"})
        elif msg is not None:
            msg = str(msg)
        summary.append(
            {
                "step_index": entry.get("step_index"),
                "action": entry.get("action"),
                "status": entry.get("status"),
                "message": msg,
            }
        )
    return summary


def _call_llm_provider(provider: str, prompt_text: str, messages: list[dict] | None = None) -> tuple[str, str]:
    """Call the chosen provider; on failure, fall back to other available providers."""
    provider = (provider or "deepseek").lower()
    last_exc: Exception | None = None
    attempted: list[str] = []

    def _try_call(name: str) -> str:
        if name == "deepseek":
            return call_deepseek(prompt_text, messages)
        if name == "doubao":
            return call_doubao(prompt_text, messages)
        if name == "qwen":
            return call_qwen(prompt_text, messages)
        raise HTTPException(status_code=400, detail="Unsupported provider")

    # Always try requested provider first, then fall back to others that are configured.
    order = []
    for name in [provider, "deepseek", "doubao", "qwen"]:
        if name and name not in order:
            order.append(name)

    for name in order:
        if not _provider_available(name):
            continue
        attempted.append(name)
        try:
            return name, _try_call(name)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    attempted_msg = f" after trying {attempted}" if attempted else ""
    raise RuntimeError(f"LLM call failed ({provider}){attempted_msg}: {last_exc or 'no providers available'}")


def _resolve_work_dir(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if path.exists() and path.is_dir():
            return str(path)
    except Exception:
        return None
    return None


def _get_open_windows_summary() -> str:
    if not gw:
        return "(unavailable)"
    try:
        titles = [t for t in gw.getAllTitles() if t and str(t).strip()]
        if not titles:
            return "(none)"
        return ", ".join(titles[:50])
    except Exception:
        return "(unavailable)"


@app.get("/")
async def read_root():
    return {"message": "backend running"}


@app.post("/api/command")
async def handle_command(payload: CommandRequest):
    return {"received": payload.text}


@app.post("/api/vision/screenshot")
async def vision_screenshot():
    path = capture_screen()
    image_base64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"image": image_base64}


@app.post("/api/vision/ocr")
async def vision_ocr():
    path = capture_screen()
    full_text, boxes = run_ocr_with_boxes(str(path))
    return {"text": full_text, "boxes": [b.to_dict() for b in boxes]}


@app.post("/api/screenshot/raw")
async def screenshot_raw():
    path = capture_screen()
    image_base64 = base64.b64encode(path.read_bytes()).decode("ascii")
    with Image.open(path) as img:
        width, height = img.size
    return {"image_base64": image_base64, "width": width, "height": height}


@app.post("/api/ai/query")
async def ai_query(payload: AIQueryRequest):
    provider = payload.provider.lower()
    try:
        resolved_provider, reply = await asyncio.to_thread(
            _call_llm_provider, provider, payload.text, None
        )
    except Exception as exc:  # noqa: BLE001
        return {"provider": provider, "error": f"LLM call failed: {exc}"}
    return {"provider": resolved_provider, "response": reply}


@app.post("/api/ai/plan")
async def ai_plan(payload: AIQueryRequest):
    """
    Call the selected LLM provider with the planner prompt, parse the response
    as an action plan, and return it without executing any actions.
    """
    request_id = generate_request_id()
    context = TaskContext(user_instruction=payload.text)
    provider = (payload.provider or "deepseek").lower()
    work_dir = None

    log_event(
        "ai_plan.start",
        request_id,
        {
            "provider": provider,
            "user_text": payload.text,
            "has_screenshot": bool(payload.screenshot_base64),
        },
    )

    dangerous = executor._detect_dangerous_request(payload.text)
    if dangerous:
        log_event(
            "ai_plan.blocked",
            request_id,
            {"reason": f"dangerous_request:{dangerous}", "user_text": payload.text},
        )
        return {
            "provider": provider,
            "status": "error",
            "message": f"dangerous_request:{dangerous}",
            "context": context.to_dict(),
            "request_id": request_id,
        }

    cached_plan = _cached_open_plan(payload.text)
    if cached_plan:
        plan, warnings, errors = _validate_plan(cached_plan, request_id)
        if errors:
            log_event(
                "ai_plan.invalid_plan",
                request_id,
                {"provider": "alias_cache", "errors": errors},
            )
            return _respond_invalid_plan(request_id, errors, provider="alias_cache")
        context.record_plan(plan.model_dump())
        log_event(
            "ai_plan.cached",
            request_id,
            {
                "provider": "alias_cache",
                "user_text": payload.text,
                "plan": summarize_plan(plan.model_dump()),
                "normalization_warnings": warnings,
            },
        )
        return {
            "provider": "alias_cache",
            "status": "success",
            "plan": plan.model_dump(),
            "raw": "alias_cache",
            "normalization_warnings": warnings,
            "context": context.to_dict(),
            "request_id": request_id,
        }

    if provider == "test":
        plan_data = build_test_plan(payload.text, payload.screenshot_base64)
        if isinstance(plan_data, dict) and plan_data.get("error_type") == "dangerous_request":
            log_event(
                "ai_plan.blocked",
                request_id,
                {"reason": "dangerous_request:block", "user_text": payload.text},
            )
            return {
                "provider": provider,
                "status": "error",
                "message": "dangerous_request:block",
                "context": context.to_dict(),
                "request_id": request_id,
            }
        plan, warnings, errors = _validate_plan(plan_data, request_id)
        if errors:
            log_event(
                "ai_plan.invalid_plan",
                request_id,
                {"error": errors, "raw_plan": sanitize_payload(plan_data)},
            )
            return _respond_invalid_plan(request_id, errors, provider=provider)
        context.record_plan(plan.model_dump())
        log_event(
            "ai_plan.success",
            request_id,
            {"provider": provider, "plan": summarize_plan(plan.model_dump()), "normalization_warnings": warnings},
        )
        return {
            "provider": provider,
            "status": "success",
            "plan": plan.model_dump(),
            "raw": "test_planner",
            "normalization_warnings": warnings,
            "context": context.to_dict(),
            "request_id": request_id,
        }

    prompt: PromptBundle = format_prompt(
        payload.text,
        ocr_text="",
        image_base64=payload.screenshot_base64,
        open_windows=_get_open_windows_summary(),
    )
    if provider == "deepseek":
        llm_messages = prompt.messages
    elif provider == "doubao":
        llm_messages = (
            prompt.vision_messages
            if (prompt.vision_messages and _doubao_vision_enabled())
            else prompt.messages
        )
    else:
        llm_messages = prompt.vision_messages or prompt.messages
    prompt_text = prompt.prompt_text
    context.set_prompt_text(prompt_text)
    try:
        resolved_provider, reply = await asyncio.to_thread(
            _call_llm_provider, provider, prompt_text, llm_messages
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "ai_plan.llm_error",
            request_id,
            {"provider": provider, "error": str(exc)},
        )
        return {
            "provider": provider,
            "status": "error",
            "message": f"LLM call failed: {exc}",
            "request_id": request_id,
        }

    context.set_raw_reply(reply)
    parsed = parse_action_plan(reply)
    if isinstance(parsed, str):
        log_event(
            "ai_plan.parse_error",
            request_id,
            {
                "provider": provider,
                "error": parsed,
                "raw_reply": reply,
            },
        )
        return {
            "provider": provider,
            "status": "error",
            "message": parsed,
            "raw": reply,
            "context": context.to_dict(),
            "request_id": request_id,
        }
    plan_dict = parsed.model_dump()
    plan_dict = _maybe_enforce_open_app(payload.text, plan_dict)
    plan_dict = _maybe_rewrite_open_file(payload.text, plan_dict)
    plan_dict = _normalize_workspace_paths(plan_dict, work_dir)
    plan_dict = _ensure_delete_confirm(plan_dict)
    plan_dict = _maybe_rewrite_web_search(payload.text, plan_dict)
    plan, warnings, errors = _validate_plan(plan_dict, request_id)
    if errors:
        log_event(
            "ai_plan.invalid_plan",
            request_id,
            {"provider": provider, "errors": errors, "raw_reply": reply},
        )
        return _respond_invalid_plan(request_id, errors, provider=provider)
    context.record_plan(plan)
    log_event(
        "ai_plan.success",
        request_id,
        {
            "provider": resolved_provider,
            "plan": summarize_plan(plan.model_dump()),
            "normalization_warnings": warnings,
        },
    )
    return {
        "provider": resolved_provider,
        "status": "success",
        "plan": plan.model_dump(),
        "normalization_warnings": warnings,
        "raw": reply,
        "context": context.to_dict(),
        "request_id": request_id,
    }


@app.post("/api/ai/execute_plan")
async def ai_execute_plan(payload: dict):
    """
    Validate and execute an ActionPlan provided by the client.
    Does not call any LLMs.
    """
    request_id = generate_request_id()
    consent_token = bool(payload.get("consent_token")) if isinstance(payload, dict) else False
    dry_run = bool(payload.get("dry_run")) if isinstance(payload, dict) else False
    work_dir = _resolve_work_dir(payload.get("work_dir")) if isinstance(payload, dict) else None
    log_event(
        "ai_execute_plan.start",
        request_id,
        {"work_dir": work_dir, "plan": summarize_plan(payload if isinstance(payload, dict) else {})},
    )
    plan, warnings, errors = _validate_plan(payload, request_id)
    if errors:
        log_event(
            "ai_execute_plan.invalid_plan",
            request_id,
            {"errors": errors},
        )
        return _respond_invalid_plan(request_id, errors)
    if dry_run:
        return {
            "plan": plan.model_dump(),
            "normalization_warnings": warnings,
            "dry_run": True,
            "mode": "dry_run",
            "note": "dry_run: no side effects executed",
            "request_id": request_id,
        }

    task_id = generate_request_id()
    result = await asyncio.to_thread(
        executor.run_steps,
        plan,
        work_dir=work_dir,
        task_id=task_id,
        request_id=request_id,
        consent_token=consent_token,
    )
    log_event(
        "ai_execute_plan.finished",
        request_id,
        {"execution": summarize_execution(result), "work_dir": work_dir},
    )
    result["request_id"] = request_id
    result["task_id"] = task_id
    return result


@app.post("/api/ai/execute")
async def ai_execute(payload: dict):
    """Alias for execute_plan to match external callers."""
    return await ai_execute_plan(payload)


@app.post("/api/ai/run")
async def ai_run(payload: dict):
    """
    Full natural-language automation: plan with LLM, parse, validate, execute.
    Does not raise; returns errors in the response body.
    """
    request_id = generate_request_id()
    user_text = payload.get("user_text")
    ocr_text = payload.get("ocr_text", "")
    manual_click = payload.get("manual_click")
    screenshot_meta = payload.get("screenshot_meta") or {}
    screenshot_base64 = payload.get("screenshot_base64") or payload.get("image_base64")
    dry_run = bool(payload.get("dry_run"))
    provider = (payload.get("provider") or "deepseek").lower()
    work_dir = _resolve_work_dir(payload.get("work_dir"))
    consent_token = bool(payload.get("consent_token"))
    context = TaskContext(user_instruction=user_text, screenshot_meta=screenshot_meta, ocr_text=ocr_text or "", work_dir=work_dir)

    log_event(
        "ai_run.start",
        request_id,
        {
            "provider": provider,
            "user_text": user_text,
            "dry_run": dry_run,
            "work_dir": work_dir,
            "manual_click": bool(manual_click),
            "screenshot_meta": sanitize_payload(screenshot_meta),
            "has_screenshot": bool(screenshot_base64),
        },
    )

    if not user_text or not isinstance(user_text, str):
        log_event(
            "ai_run.error",
            request_id,
            {"error": "user_text is required", "provider": provider},
        )
        return {"error": "user_text is required", "provider": provider, "request_id": request_id}

    open_windows = _get_open_windows_summary()

    cached_plan = _cached_open_plan(user_text)
    if cached_plan:
        plan, warnings, errors = _validate_plan(cached_plan, request_id)
        if errors:
            log_event(
                "ai_run.invalid_plan",
                request_id,
                {"provider": "alias_cache", "errors": errors},
            )
            return _respond_invalid_plan(request_id, errors, provider="alias_cache")
        if dry_run:
            log_event(
                "ai_run.cached",
                request_id,
                {
                    "provider": "alias_cache",
                    "plan": summarize_plan(plan.model_dump()),
                    "dry_run": True,
                    "normalization_warnings": warnings,
                },
            )
            return {
                "provider": "alias_cache",
                "user_text": user_text,
                "raw_reply": "alias_cache",
                "plan": plan.model_dump(),
                "plan_after_injection": plan.model_dump(),
                "normalization_warnings": warnings,
                "execution": None,
                "dry_run": True,
                "mode": "dry_run",
                "note": "dry_run: no side effects executed",
                "context": context.to_dict(),
                "request_id": request_id,
            }
        task_id = generate_request_id()
        exec_result = await asyncio.to_thread(
            executor.run_steps,
            plan,
            context=context,
            planner_provider="alias_cache",
            work_dir=work_dir,
            task_id=task_id,
            request_id=request_id,
            consent_token=consent_token,
        )
        log_event(
            "ai_run.cached",
            request_id,
            {
                "provider": "alias_cache",
                "plan": summarize_plan(plan.model_dump()),
                "execution": summarize_execution(exec_result),
                "normalization_warnings": warnings,
                "dry_run": False,
            },
        )
        return {
            "provider": "alias_cache",
            "user_text": user_text,
            "raw_reply": "alias_cache",
            "plan": plan.model_dump(),
            "plan_after_injection": plan.model_dump(),
            "normalization_warnings": warnings,
            "execution": exec_result,
            "context": context.to_dict(),
            "request_id": request_id,
            "task_id": task_id,
        }

    prompt: PromptBundle = format_prompt(
        user_text,
        ocr_text=ocr_text or "",
        manual_click=manual_click,
        screenshot_meta=screenshot_meta,
        image_base64=screenshot_base64,
        open_windows=open_windows,
    )
    if provider == "deepseek":
        llm_messages = prompt.messages
    elif provider == "doubao":
        llm_messages = (
            prompt.vision_messages
            if (prompt.vision_messages and _doubao_vision_enabled())
            else prompt.messages
        )
    else:
        llm_messages = prompt.vision_messages or prompt.messages
    prompt_text = prompt.prompt_text
    context.set_prompt_text(prompt_text)

    try:
        resolved_provider, raw_reply = await asyncio.to_thread(
            _call_llm_provider, provider, prompt_text, llm_messages
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "ai_run.llm_error",
            request_id,
            {"provider": provider, "error": str(exc)},
        )
        return {
            "error": f"LLM call failed: {exc}",
            "provider": provider,
            "user_text": user_text,
            "context": context.to_dict(),
            "request_id": request_id,
        }

    context.set_raw_reply(raw_reply)
    parsed = parse_action_plan(raw_reply)
    if isinstance(parsed, str):
        log_event(
            "ai_run.parse_error",
            request_id,
            {"provider": resolved_provider, "error": parsed, "raw_reply": raw_reply},
        )
        return {
            "provider": resolved_provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan_error": parsed,
            "context": context.to_dict(),
            "request_id": request_id,
        }

    plan_data = parsed.model_dump()
    plan_data = _maybe_enforce_open_app(user_text, plan_data)
    plan_data = _maybe_rewrite_open_file(user_text, plan_data)
    plan_data = _normalize_workspace_paths(plan_data, work_dir)
    plan_data = _ensure_delete_confirm(plan_data)
    plan_data = _maybe_rewrite_web_search(user_text, plan_data)
    context.record_plan(plan_data)

    # Inject manual click as the first step if provided.
    if isinstance(manual_click, dict) and "x" in manual_click and "y" in manual_click:
        plan_data.setdefault("steps", [])
        plan_data["steps"].insert(
            0, {"action": "click", "params": {"x": manual_click["x"], "y": manual_click["y"]}}
        )

    plan, warnings, errors = _validate_plan(plan_data, request_id)
    if errors:
        context.add_error("invalid action plan")
        log_event(
            "ai_run.invalid_plan",
            request_id,
            {"provider": resolved_provider, "errors": errors, "raw_reply": raw_reply},
        )
        return {
            "provider": resolved_provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan_error": "invalid action plan",
            "validation_errors": errors,
            "context": context.to_dict(),
            "request_id": request_id,
        }

    log_event(
        "ai_run.plan_ready",
        request_id,
        {
            "provider": resolved_provider,
            "plan": summarize_plan(plan.model_dump()),
            "dry_run": dry_run,
            "normalization_warnings": warnings,
        },
    )

    if dry_run:
        return {
            "provider": resolved_provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan": plan.model_dump(),
            "plan_after_injection": plan_data,
            "normalization_warnings": warnings,
            "execution": None,
            "dry_run": True,
            "mode": "dry_run",
            "note": "dry_run: no side effects executed",
            "context": context.to_dict(),
            "request_id": request_id,
        }

        task_id = generate_request_id()
    exec_result = await asyncio.to_thread(
        executor.run_steps,
        plan,
        context=context,
        planner_provider=resolved_provider,
        work_dir=work_dir,
        task_id=task_id,
        request_id=request_id,
        consent_token=consent_token,
    )
    log_event(
        "ai_run.finished",
        request_id,
        {
            "provider": resolved_provider,
            "plan": summarize_plan(plan.model_dump()),
            "execution": summarize_execution(exec_result),
            "step_logs": _summarize_logs(exec_result.get("logs")),
            "work_dir": work_dir,
        },
    )
    return {
        "provider": resolved_provider,
        "user_text": user_text,
        "raw_reply": raw_reply,
        "plan": plan.model_dump(),
        "plan_after_injection": plan_data,
        "execution": exec_result,
        "context": context.to_dict(),
        "request_id": request_id,
        "task_id": task_id,
    }


@app.post("/api/ai/debug_run")
async def ai_debug_run(payload: dict):
    """
    Debug endpoint: plan + execute with full TaskContext returned.
    Optional debug flags:
    - debug_force_capture: force before/after screenshots + OCR summaries.
    - debug_disable_vlm: suppress VLM/multimodal calls for localization.
    """
    request_id = generate_request_id()
    user_text = payload.get("user_text")
    ocr_text = payload.get("ocr_text", "")
    manual_click = payload.get("manual_click")
    screenshot_meta = payload.get("screenshot_meta") or {}
    screenshot_base64 = payload.get("screenshot_base64") or payload.get("image_base64")
    provider = (payload.get("provider") or "deepseek").lower()
    allow_replan = payload.get("allow_replan", True)
    max_replans = payload.get("max_replans")
    debug_force_capture = bool(payload.get("debug_force_capture"))
    debug_disable_vlm = bool(payload.get("debug_disable_vlm"))
    work_dir = _resolve_work_dir(payload.get("work_dir"))
    consent_token = bool(payload.get("consent_token"))

    context = TaskContext(
        user_instruction=user_text,
        screenshot_meta=screenshot_meta,
        ocr_text=ocr_text or "",
        max_replans=max_replans,
        work_dir=work_dir,
    )

    log_event(
        "ai_debug_run.start",
        request_id,
        {
            "provider": provider,
            "user_text": user_text,
            "allow_replan": allow_replan,
            "max_replans": max_replans,
            "debug_force_capture": debug_force_capture,
            "debug_disable_vlm": debug_disable_vlm,
            "work_dir": work_dir,
            "has_screenshot": bool(screenshot_base64),
            "screenshot_meta": sanitize_payload(screenshot_meta),
        },
    )

    if not user_text or not isinstance(user_text, str):
        log_event(
            "ai_debug_run.error",
            request_id,
            {"error": "user_text is required", "provider": provider},
        )
        return {"error": "user_text is required", "provider": provider, "request_id": request_id}

    open_windows = _get_open_windows_summary()

    cached_plan = _cached_open_plan(user_text)
    if cached_plan:
        plan, warnings, errors = _validate_plan(cached_plan, request_id)
        if errors:
            log_event(
                "ai_debug_run.invalid_plan",
                request_id,
                {"provider": "alias_cache", "errors": errors},
            )
            return _respond_invalid_plan(request_id, errors, provider="alias_cache")
        exec_result = await asyncio.to_thread(
            executor.run_steps,
            plan,
            context=context,
            planner_provider="alias_cache",
            work_dir=work_dir,
        )
        log_event(
            "ai_debug_run.cached",
            request_id,
            {
                "plan": summarize_plan(plan.model_dump()),
                "execution": summarize_execution(exec_result),
                "normalization_warnings": warnings,
            },
        )
        exec_result["request_id"] = request_id
        return exec_result

    prompt: PromptBundle = format_prompt(
        user_text,
        ocr_text=ocr_text or "",
        manual_click=manual_click,
        screenshot_meta=screenshot_meta,
        image_base64=screenshot_base64,
        open_windows=open_windows,
    )
    if provider == "deepseek":
        llm_messages = prompt.messages
    elif provider == "doubao":
        llm_messages = (
            prompt.vision_messages
            if (prompt.vision_messages and _doubao_vision_enabled())
            else prompt.messages
        )
    else:
        llm_messages = prompt.vision_messages or prompt.messages
    prompt_text = prompt.prompt_text
    context.set_prompt_text(prompt_text)

    try:
        resolved_provider, raw_reply = await asyncio.to_thread(
            _call_llm_provider, provider, prompt_text, llm_messages
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "ai_debug_run.llm_error",
            request_id,
            {"provider": provider, "error": str(exc)},
        )
        return {
            "error": f"LLM call failed: {exc}",
            "provider": provider,
            "user_text": user_text,
            "context": context.to_dict(),
            "request_id": request_id,
        }

    context.set_raw_reply(raw_reply)
    parsed = parse_action_plan(raw_reply)
    if isinstance(parsed, str):
        log_event(
            "ai_debug_run.parse_error",
            request_id,
            {"provider": resolved_provider, "error": parsed, "raw_reply": raw_reply},
        )
        return {
            "error": parsed,
            "provider": resolved_provider,
            "raw": raw_reply,
            "context": context.to_dict(),
            "request_id": request_id,
        }
    plan_dict = parsed.model_dump()
    plan_dict = _maybe_enforce_open_app(user_text, plan_dict)
    plan_dict = _maybe_rewrite_open_file(user_text, plan_dict)
    plan_dict = _normalize_workspace_paths(plan_dict, work_dir)
    plan_dict = _ensure_delete_confirm(plan_dict)
    plan_dict = _maybe_rewrite_web_search(user_text, plan_dict)
    plan_obj, warnings, errors = _validate_plan(plan_dict, request_id)
    if errors:
        log_event(
            "ai_debug_run.invalid_plan",
            request_id,
            {"provider": resolved_provider, "errors": errors, "raw_reply": raw_reply},
        )
        return _respond_invalid_plan(request_id, errors, provider=resolved_provider)
    context.record_plan(plan_obj)

    task_id = generate_request_id()
    exec_result = await asyncio.to_thread(
        executor.run_steps,
        plan_obj,
        context=context,
        planner_provider=resolved_provider,
        allow_replan=allow_replan,
        max_replans=max_replans,
        debug_capture_all=debug_force_capture,
        disable_vlm=debug_disable_vlm,
        force_capture=debug_force_capture,
        force_ocr=debug_force_capture,
        allow_vlm_override=False if debug_disable_vlm else None,
        capture_ocr=True if debug_force_capture else None,
        work_dir=work_dir,
        task_id=task_id,
        request_id=request_id,
        consent_token=consent_token,
    )

    log_event(
        "ai_debug_run.finished",
        request_id,
        {
            "provider": resolved_provider,
            "plan": summarize_plan(getattr(parsed, "model_dump", lambda: {})() if hasattr(parsed, "model_dump") else context.action_plan),
            "execution": summarize_execution(exec_result),
            "step_logs": _summarize_logs(exec_result.get("logs")),
            "work_dir": work_dir,
            "normalization_warnings": warnings,
        },
    )
    exec_result["request_id"] = request_id
    exec_result["task_id"] = task_id
    return exec_result


def _task_response(record):
    if not record:
        return None
    data = record.to_dict()
    return data


@app.get("/api/tasks/{task_id}/status")
async def get_task_status(task_id: str):
    record = get_task(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="task not found")
    return _task_response(record)


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str, payload: dict | None = None):
    record = get_task(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="task not found")
    if record.status != TaskStatus.AWAITING_USER:
        raise HTTPException(status_code=400, detail="task not awaiting user")
    try:
        plan = ActionPlan.model_validate(record.plan)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid stored plan: {exc}")
    context = TaskContext(user_instruction=record.user_text, work_dir=(record.context_snapshot or {}).get("work_dir"))
    context.record_plan(plan)
    try:
        context.step_results = list(record.step_results or [])
    except Exception:
        context.step_results = []
    update_task(task_id, status=TaskStatus.RUNNING, last_error=None)
    exec_result = await asyncio.to_thread(
        executor.run_steps,
        plan,
        context=context,
        task_id=task_id,
        request_id=request_id,
        start_index=record.step_index,
        consent_token=bool(payload.get("consent_token")) if isinstance(payload, dict) else False,
    )
    exec_result["task_id"] = task_id
    return exec_result


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "debug_wechat_activation":
        result = executor.debug_wechat_activation()
        print(json.dumps(result, indent=2, default=str))
    elif len(sys.argv) > 3 and sys.argv[1] == "demo_search_image_and_save":
        query_arg = sys.argv[2]
        folder_arg = sys.argv[3]
        result = asyncio.run(executor.demo_search_image_and_save(query_arg, folder_arg))
        print(json.dumps(result, indent=2, default=str))
