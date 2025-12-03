import base64
import json
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from PIL import Image

from backend.executor import executor
from backend.executor.actions_schema import validate_action_plan
from backend.llm.action_parser import parse_action_plan
from backend.llm.deepseek_client import call_deepseek
from backend.llm.openai_client import call_openai
from backend.llm.planner_prompt import PromptBundle, format_prompt
from backend.llm.qwen_client import call_qwen
from backend.executor.task_context import TaskContext
from backend.vision.ocr import run_ocr, run_ocr_with_boxes
from backend.vision.screenshot import capture_screen

load_dotenv()


class CommandRequest(BaseModel):
    text: str


class AIQueryRequest(BaseModel):
    provider: str
    text: str
    screenshot_base64: str | None = None


app = FastAPI()


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
        if provider == "openai":
            reply = await asyncio.to_thread(call_openai, payload.text)
        elif provider == "deepseek":
            reply = await asyncio.to_thread(call_deepseek, payload.text)
        elif provider == "qwen":
            reply = await asyncio.to_thread(call_qwen, payload.text)
        else:
            raise HTTPException(status_code=400, detail="Unsupported provider")
    except Exception as exc:  # noqa: BLE001
        return {"provider": provider, "error": f"LLM call failed: {exc}"}
    return {"provider": provider, "response": reply}


@app.post("/api/ai/plan")
async def ai_plan(payload: AIQueryRequest):
    """
    Call the selected LLM provider with the planner prompt, parse the response
    as an action plan, and return it without executing any actions.
    """
    context = TaskContext(user_instruction=payload.text)
    prompt: PromptBundle = format_prompt(
        payload.text, ocr_text="", image_base64=payload.screenshot_base64
    )
    llm_messages = prompt.messages
    if provider != "deepseek":
        llm_messages = prompt.vision_messages or prompt.messages
    prompt_text = prompt.prompt_text
    context.set_prompt_text(prompt_text)

    provider = payload.provider.lower()
    try:
        if provider == "openai":
            reply = await asyncio.to_thread(call_openai, prompt_text, llm_messages)
        elif provider == "deepseek":
            reply = await asyncio.to_thread(call_deepseek, prompt_text, llm_messages)
        elif provider == "qwen":
            reply = await asyncio.to_thread(call_qwen, prompt_text, llm_messages)
        else:
            raise HTTPException(status_code=400, detail="Unsupported provider")
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
    context.record_plan(parsed)
    return {
        "provider": provider,
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

    result = executor.run_steps(plan, work_dir=work_dir)
    return result


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

    prompt: PromptBundle = format_prompt(
        user_text,
        ocr_text=ocr_text or "",
        manual_click=manual_click,
        screenshot_meta=screenshot_meta,
        image_base64=screenshot_base64,
    )
    llm_messages = prompt.messages
    if provider != "deepseek":
        llm_messages = prompt.vision_messages or prompt.messages
    prompt_text = prompt.prompt_text
    context.set_prompt_text(prompt_text)

    try:
        if provider == "openai":
            raw_reply = await asyncio.to_thread(call_openai, prompt_text, llm_messages)
        elif provider == "deepseek":
            raw_reply = await asyncio.to_thread(call_deepseek, prompt_text, llm_messages)
        elif provider == "qwen":
            raw_reply = await asyncio.to_thread(call_qwen, prompt_text, llm_messages)
        else:
            return {"error": "Unsupported provider", "provider": provider}
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
            "provider": provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan_error": parsed,
            "context": context.to_dict(),
        }

    plan_data = parsed.model_dump()
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
            "provider": provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan_error": f"invalid action plan: {exc}",
            "context": context.to_dict(),
        }

    if dry_run:
        return {
            "provider": provider,
            "user_text": user_text,
            "raw_reply": raw_reply,
            "plan": plan.model_dump(),
            "plan_after_injection": plan_data,
            "execution": None,
            "dry_run": True,
            "context": context.to_dict(),
        }

    exec_result = executor.run_steps(plan, context=context, planner_provider=provider, work_dir=work_dir)
    return {
        "provider": provider,
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

    prompt: PromptBundle = format_prompt(
        user_text,
        ocr_text=ocr_text or "",
        manual_click=manual_click,
        screenshot_meta=screenshot_meta,
        image_base64=screenshot_base64,
    )
    llm_messages = prompt.messages
    if provider != "deepseek":
        llm_messages = prompt.vision_messages or prompt.messages
    prompt_text = prompt.prompt_text
    context.set_prompt_text(prompt_text)

    try:
        if provider == "openai":
            raw_reply = await asyncio.to_thread(call_openai, prompt_text, llm_messages)
        elif provider == "deepseek":
            raw_reply = await asyncio.to_thread(call_deepseek, prompt_text, llm_messages)
        elif provider == "qwen":
            raw_reply = await asyncio.to_thread(call_qwen, prompt_text, llm_messages)
        else:
            return {"error": "Unsupported provider", "provider": provider}
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
            "provider": provider,
            "raw": raw_reply,
            "context": context.to_dict(),
        }
    context.record_plan(parsed)

    exec_result = executor.run_steps(
        parsed,
        context=context,
        planner_provider=provider,
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
