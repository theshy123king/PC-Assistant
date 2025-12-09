import base64
import json
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from PIL import Image

from backend.executor import executor
from backend.executor.actions_schema import ActionPlan, ActionStep, validate_action_plan
from backend.executor.apps import get_cached_alias
from backend.executor.task_context import TaskContext
from backend.llm.action_parser import parse_action_plan
from backend.llm.deepseek_client import call_deepseek
from backend.llm.doubao_client import call_doubao
from backend.llm.planner_prompt import PromptBundle, format_prompt
from backend.llm.qwen_client import call_qwen
from backend.llm.test_planner import build_test_plan
from backend.vision.ocr import run_ocr, run_ocr_with_boxes
from backend.vision.screenshot import capture_screen
from backend.logging_setup import setup_logging

setup_logging()
load_dotenv()


class CommandRequest(BaseModel):
    text: str


class AIQueryRequest(BaseModel):
    provider: str
    text: str
    screenshot_base64: str | None = None


app = FastAPI()


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


def _call_llm_provider(provider: str, prompt_text: str, messages: list[dict] | None = None) -> tuple[str, str]:
    """Call the chosen provider; on failure, fall back to other available providers."""
    provider = (provider or "deepseek").lower()
    last_exc: Exception | None = None

    def _try_call(name: str) -> str:
        if name == "deepseek":
            return call_deepseek(prompt_text, messages)
        if name == "doubao":
            return call_doubao(prompt_text, messages)
        if name == "qwen":
            return call_qwen(prompt_text, messages)
        raise HTTPException(status_code=400, detail="Unsupported provider")

    # If requested provider is available, try only that; otherwise fall back.
    if _provider_available(provider):
        order = [provider]
    else:
        order = [p for p in [provider, "deepseek", "doubao", "qwen"] if p]
    seen = set()
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        if not _provider_available(name):
            continue
        try:
            return name, _try_call(name)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    raise RuntimeError(f"LLM call failed ({provider}): {last_exc or 'no providers available'}")


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
    context = TaskContext(user_instruction=payload.text)
    provider = (payload.provider or "deepseek").lower()

    dangerous = executor._detect_dangerous_request(payload.text)
    if dangerous:
        return {
            "provider": provider,
            "status": "error",
            "message": f"dangerous_request:{dangerous}",
            "context": context.to_dict(),
        }

    cached_plan = _cached_open_plan(payload.text)
    if cached_plan:
        context.record_plan(cached_plan.model_dump())
        return {
            "provider": "alias_cache",
            "status": "success",
            "plan": cached_plan.model_dump(),
            "raw": "alias_cache",
            "context": context.to_dict(),
        }

    if provider == "test":
        plan_data = build_test_plan(payload.text, payload.screenshot_base64)
        if isinstance(plan_data, dict) and plan_data.get("error_type") == "dangerous_request":
            return {
                "provider": provider,
                "status": "error",
                "message": "dangerous_request:block",
                "context": context.to_dict(),
            }
        try:
            plan = validate_action_plan(plan_data)
        except Exception as exc:  # noqa: BLE001
            return {
                "provider": provider,
                "status": "error",
                "message": f"invalid action plan: {exc}",
                "raw": plan_data,
                "context": context.to_dict(),
            }
        context.record_plan(plan.model_dump())
        return {
            "provider": provider,
            "status": "success",
            "plan": plan.model_dump(),
            "raw": "test_planner",
            "context": context.to_dict(),
        }

    prompt: PromptBundle = format_prompt(
        payload.text, ocr_text="", image_base64=payload.screenshot_base64
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
        return {
            "provider": provider,
            "status": "error",
            "message": f"LLM call failed: {exc}",
        }

    context.set_raw_reply(reply)
    parsed = parse_action_plan(reply)
    if isinstance(parsed, str):
        return {
            "provider": provider,
            "status": "error",
            "message": parsed,
            "raw": reply,
            "context": context.to_dict(),
        }
    plan_dict = parsed.model_dump()
    plan_dict = _maybe_enforce_open_app(payload.text, plan_dict)
    try:
        parsed = ActionPlan.model_validate(plan_dict)
    except Exception as exc:  # noqa: BLE001
        return {
            "provider": provider,
            "status": "error",
            "message": f"invalid action plan after normalization: {exc}",
            "raw": reply,
            "context": context.to_dict(),
        }
    context.record_plan(parsed)
    return {
        "provider": resolved_provider,
        "status": "success",
        "plan": parsed.model_dump(),
        "raw": reply,
        "context": context.to_dict(),
    }


@app.post("/api/ai/execute_plan")
async def ai_execute_plan(payload: dict):
    """
    Validate and execute an ActionPlan provided by the client.
    Does not call any LLMs.
    """
    work_dir = _resolve_work_dir(payload.get("work_dir")) if isinstance(payload, dict) else None
    try:
        plan = validate_action_plan(payload)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"invalid action plan: {exc}"}

    result = await asyncio.to_thread(executor.run_steps, plan, work_dir=work_dir)
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
    user_text = payload.get("user_text")
    ocr_text = payload.get("ocr_text", "")
    manual_click = payload.get("manual_click")
    screenshot_meta = payload.get("screenshot_meta") or {}
    screenshot_base64 = payload.get("screenshot_base64") or payload.get("image_base64")
    dry_run = bool(payload.get("dry_run"))
    provider = (payload.get("provider") or "deepseek").lower()
    work_dir = _resolve_work_dir(payload.get("work_dir"))
    context = TaskContext(user_instruction=user_text, screenshot_meta=screenshot_meta, ocr_text=ocr_text or "", work_dir=work_dir)

    if not user_text or not isinstance(user_text, str):
        return {"error": "user_text is required", "provider": provider}

    cached_plan = _cached_open_plan(user_text)
    if cached_plan:
        if dry_run:
            return {
                "provider": "alias_cache",
                "user_text": user_text,
                "raw_reply": "alias_cache",
                "plan": cached_plan.model_dump(),
                "plan_after_injection": cached_plan.model_dump(),
                "execution": None,
                "dry_run": True,
                "context": context.to_dict(),
            }
        exec_result = await asyncio.to_thread(
            executor.run_steps,
            cached_plan,
            context=context,
            planner_provider="alias_cache",
            work_dir=work_dir,
        )
        return {
            "provider": "alias_cache",
            "user_text": user_text,
            "raw_reply": "alias_cache",
            "plan": cached_plan.model_dump(),
            "plan_after_injection": cached_plan.model_dump(),
            "execution": exec_result,
            "context": context.to_dict(),
        }

    prompt: PromptBundle = format_prompt(
        user_text,
        ocr_text=ocr_text or "",
        manual_click=manual_click,
        screenshot_meta=screenshot_meta,
        image_base64=screenshot_base64,
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
        return {
            "error": f"LLM call failed: {exc}",
            "provider": provider,
            "user_text": user_text,
            "context": context.to_dict(),
        }

    context.set_raw_reply(raw_reply)
    parsed = parse_action_plan(raw_reply)
    if isinstance(parsed, str):
        return {
            "provider": resolved_provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan_error": parsed,
            "context": context.to_dict(),
        }

    plan_data = parsed.model_dump()
    plan_data = _maybe_enforce_open_app(user_text, plan_data)
    context.record_plan(plan_data)

    # Inject manual click as the first step if provided.
    if isinstance(manual_click, dict) and "x" in manual_click and "y" in manual_click:
        plan_data.setdefault("steps", [])
        plan_data["steps"].insert(
            0, {"action": "click", "params": {"x": manual_click["x"], "y": manual_click["y"]}}
        )

    try:
        plan = validate_action_plan(plan_data)
    except Exception as exc:  # noqa: BLE001
        context.add_error(f"invalid action plan: {exc}")
        return {
            "provider": resolved_provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan_error": f"invalid action plan: {exc}",
            "context": context.to_dict(),
        }

    if dry_run:
        return {
            "provider": resolved_provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan": plan.model_dump(),
            "plan_after_injection": plan_data,
            "execution": None,
            "dry_run": True,
            "context": context.to_dict(),
        }

    exec_result = await asyncio.to_thread(
        executor.run_steps,
        plan,
        context=context,
        planner_provider=resolved_provider,
        work_dir=work_dir,
    )
    return {
        "provider": resolved_provider,
        "user_text": user_text,
        "raw_reply": raw_reply,
        "plan": plan.model_dump(),
        "plan_after_injection": plan_data,
        "execution": exec_result,
        "context": context.to_dict(),
    }


@app.post("/api/ai/debug_run")
async def ai_debug_run(payload: dict):
    """
    Debug endpoint: plan + execute with full TaskContext returned.
    Optional debug flags:
    - debug_force_capture: force before/after screenshots + OCR summaries.
    - debug_disable_vlm: suppress VLM/multimodal calls for localization.
    """
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

    context = TaskContext(
        user_instruction=user_text,
        screenshot_meta=screenshot_meta,
        ocr_text=ocr_text or "",
        max_replans=max_replans,
        work_dir=work_dir,
    )

    if not user_text or not isinstance(user_text, str):
        return {"error": "user_text is required", "provider": provider}

    cached_plan = _cached_open_plan(user_text)
    if cached_plan:
        exec_result = await asyncio.to_thread(
            executor.run_steps,
            cached_plan,
            context=context,
            planner_provider="alias_cache",
            work_dir=work_dir,
        )
        return exec_result

    prompt: PromptBundle = format_prompt(
        user_text,
        ocr_text=ocr_text or "",
        manual_click=manual_click,
        screenshot_meta=screenshot_meta,
        image_base64=screenshot_base64,
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
        return {
            "error": f"LLM call failed: {exc}",
            "provider": provider,
            "user_text": user_text,
            "context": context.to_dict(),
        }

    context.set_raw_reply(raw_reply)
    parsed = parse_action_plan(raw_reply)
    if isinstance(parsed, str):
        return {
            "error": parsed,
            "provider": resolved_provider,
            "raw": raw_reply,
            "context": context.to_dict(),
        }
    context.record_plan(parsed)

    exec_result = await asyncio.to_thread(
        executor.run_steps,
        parsed,
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
    )

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
