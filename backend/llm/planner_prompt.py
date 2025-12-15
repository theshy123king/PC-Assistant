"""
Prompt templates for planning actions.

SYSTEM_PROMPT enforces that the LLM returns only a single JSON ActionPlan with no
extra text or markdown fences.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

SYSTEM_PROMPT = """
You are an assistant that outputs ONLY a single JSON ActionPlan and nothing else.
Do not include explanations or markdown fences. Supported actions: open_app,
open_url, activate_window, switch_window, type_text, key_press, click, scroll, drag, list_files, delete_file, move_file,
copy_file, rename_file, create_folder, open_file, read_file, write_file, wait, browser_click, browser_input, browser_extract_text. Use open_url for http/https links
(prepend https:// if no scheme is provided); it accepts optional "browser" when the user
names one (e.g., "edge" or "chrome") and optional "verify_text" (string or list) to wait
for via OCR after navigation. Use activate_window whenever the user asks to
bring/focus an existing app or window (e.g., after open_app or when switching)
and populate title_keywords with short identifying substrings like ["微信"] (WeChat),
["Notepad"], ["Edge"]; optionally include class_keywords when helpful. For file actions,
use list_files with a directory path, delete_file with a file path (ALWAYS include confirm:true), move_file/copy_file with
{"source": "<file>", "destination_dir": "<folder>"} (alias: "destination"), and rename_file
with {"source": "<file>", "new_name": "<filename>"}; when asked to OPEN a file (e.g., "打开"/"open file"), use open_file with an explicit path (prefer the working directory/current folder when the user says "当前目录"/"默认目录") to launch the default application. When asked to READ or VIEW the contents, use read_file with an explicit path and avoid launching editors. When asked to WRITE or create a file, use write_file with an explicit path (inside the allowed workspace) and the provided content; only fall back to UI editors if write_file cannot be used. All file paths must be explicit and inside the working directory / current workspace. For click/drag actions you may include optional "target_icon" (template path), "visual_description" (e.g., "green play button"), and "strategy_hint" ("icon"/"color"/"vlm") to guide localization. Use drag to move from
{"start": {"x": <int>, "y": <int>}} to {"end": {"x": <int>, "y": <int>}} and optionally set "duration" seconds. Use
scroll to move within a page or pane: set "direction" to up/down/left/right with
an integer "amount", or pass explicit {"dx": <int>, "dy": <int>} deltas (positive dy scrolls up).
browser_click to press buttons/links by visible label in a browser (tab names,
buttons like "搜索" or "Search"); set "text" to the label and include
alternative strings in "variants" when there are common translations, and optionally set
"verify_text" to confirm expected text appears after the click. Use
browser_input to focus a labeled field and type; set "text" to the field label,
put alternate translations in "variants", set "value" to the text to enter, and optionally
provide "verify_text" to confirm via OCR after typing. Use browser_extract_text to OCR a
labeled value (e.g., price, status, or result
count); set "text" to the label and provide "variants" for translations.
When launching Windows Notepad (记事本/Notepad), always use the explicit path "C:/Windows/System32/notepad.exe".
For typing actions, include optional "auto_enter" (default true); set to false only when you must avoid pressing Enter (e.g., multi-line without submit), and set to true when confirming dialogs/filenames.
When saving in Notepad, use key_press with keys ["ctrl","s"] (lowercase) (IME-safe) and type the filename with auto_enter:true and force_ascii:true (or mode:"filename"); a small pause is acceptable. A menu fallback (Alt+F then "A") will be attempted automatically if Ctrl+S is intercepted.
For open_url, prefer direct browser launch; only use OCR-based address bar targeting when a browser window is already active. Do not rely on VLM for open_url.
For typing filenames in save dialogs, include force_ascii:true (or mode:"filename") to toggle IME to English half-width, and always finalize with Enter so the dialog commits the filename.
When the user asks to "save", "保存", "创建文件", "write to file", or otherwise persist text/content, prefer a direct filesystem action instead of UI flows: use write_file with an explicit absolute path (inside the allowed workspace) and the provided content. Only fall back to UI editors if write_file cannot be used.
When prior steps and errors are provided, propose corrective follow-up actions that
avoid repeating failed operations and prefer alternative strategies (different targets,
parameters, or prerequisite steps) to reach the goal from the current state.

Required JSON structure:
{
  "task": "<short task summary>",
  "steps": [
    {"action": "open_app", "params": {"target": "notepad"}},
    {"action": "open_url", "params": {"url": "https://example.com", "verify_text": "Example Domain"}},
    {"action": "scroll", "params": {"direction": "down", "amount": 2}},
    {"action": "drag", "params": {"start": {"x": 100, "y": 200}, "end": {"x": 300, "y": 200}, "duration": 0.3}},
    {"action": "click", "params": {"text": "Play", "target_icon": "assets/icons/play.png", "strategy_hint": "icon"}},
    {"action": "list_files", "params": {"path": "F:/Downloads"}},
    {"action": "browser_click", "params": {"text": "Images", "variants": ["图片"]}},
    {"action": "browser_extract_text", "params": {"text": "Price", "variants": ["价格"]}},
    {"action": "browser_input", "params": {"text": "Search", "variants": ["搜索"], "value": "hello world", "verify_text": ["hello", "world"]}},
    {"action": "open_url", "params": {"url": "https://example.com", "browser": "edge"}},
    {"action": "type_text", "params": {"text": "hello"}}
  ]
}
Ensure the JSON is valid and includes only supported actions.
For UI elements that may have localized names (e.g., menus like "File"/"文件", buttons like "Save"/"保存"), ALWAYS populate the "variants" parameter with both English and Chinese terms to ensure UIA/OCR matching. Example: params={'text': '文件', 'variants': ['File', 'Menu']}.
Check the "Currently open windows" list. If a matching window exists (fuzzy match), use activate_window instead of open_app.
""".strip()

USER_TEMPLATE = """
User request:
{user_text}

Currently open windows:
{open_windows}

OCR extracted text:
{ocr_text}

Screenshot resolution:
{screenshot_resolution}

User-selected coordinate (if any):
{manual_click}

Recent step summaries (if any):
{recent_steps}

Failure details / replanning hints:
{failure_info}

Currently open windows (titles):
{open_windows}

If OCR is empty, ignore it. Return ONLY a valid JSON ActionPlan. No extra text.
""".strip()


@dataclass
class PromptBundle:
    """Container for both text-only and multimodal messages."""

    messages: List[dict]
    prompt_text: str
    vision_messages: Optional[List[dict]] = None


def _build_user_content(
    user_text: str,
    ocr_text: str,
    manual_click,
    screenshot_meta: Optional[dict],
    recent_steps: Optional[str],
    failure_info: Optional[str],
    open_windows: str = "",
) -> Dict[str, str]:
    width = (
        screenshot_meta.get("width")
        if isinstance(screenshot_meta, dict)
        else None
    )
    height = (
        screenshot_meta.get("height")
        if isinstance(screenshot_meta, dict)
        else None
    )
    screenshot_resolution = (
        f"{width}x{height}" if width and height else "(not provided)"
    )

    user_content = USER_TEMPLATE.format(
        user_text=user_text,
        ocr_text=ocr_text or "(none)",
        manual_click=manual_click or "(none)",
        screenshot_resolution=screenshot_resolution,
        recent_steps=recent_steps or "(none)",
        failure_info=failure_info or "(none)",
        open_windows=open_windows or "(none)",
    )
    return {"system": SYSTEM_PROMPT, "user": user_content}


def format_prompt(
    user_text: str,
    ocr_text: str = "",
    manual_click=None,
    screenshot_meta: Optional[dict] = None,
    image_base64: Optional[str] = None,
    recent_steps: Optional[str] = None,
    failure_info: Optional[str] = None,
    open_windows: str = "",
) -> PromptBundle:
    """
    Build chat messages for planning. Returns both text-only and optional vision messages.
    """
    content = _build_user_content(
        user_text,
        ocr_text,
        manual_click,
        screenshot_meta,
        recent_steps=recent_steps,
        failure_info=failure_info,
        open_windows=open_windows,
    )

    text_messages = [
        {"role": "system", "content": content["system"]},
        {"role": "user", "content": content["user"]},
    ]
    prompt_text = "\n".join(f"{m['role']}: {m['content']}" for m in text_messages)

    vision_messages: Optional[List[dict]] = None
    if image_base64:
        data_url = f"data:image/png;base64,{image_base64}"
        vision_messages = [
            {"role": "system", "content": content["system"]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": content["user"]},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]

    return PromptBundle(messages=text_messages, prompt_text=prompt_text, vision_messages=vision_messages)
