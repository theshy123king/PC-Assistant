"""
Action executor for the action DSL.

Provides:
- A dispatcher `run_steps` that iterates over an ActionPlan with a safety cap.
- Handlers for supported actions; some remain stubs and others call helper modules.

Future work: flesh out remaining stubs with OS-specific implementations.
"""

import base64
import ctypes
import difflib
import hashlib
import json
import os
import sys
import threading
import shutil
import subprocess
import time
import webbrowser
import yaml
from ctypes import wintypes
from contextvars import ContextVar
from datetime import datetime
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Tuple, Protocol
from urllib.parse import quote_plus, urlparse

from PIL import Image
import pytesseract
from pydantic import ValidationError

import pygetwindow as gw
import uiautomation as auto
try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

from backend.executor import apps, files, input, mouse
from backend.executor.actions_schema import (
    ActionPlan,
    ActionStep,
    ActivateWindowAction,
    DragAction,
    ScrollAction,
    WaitUntilAction,
)
from backend.vision.ocr import OcrBox
from backend.vision.ocr import run_ocr, run_ocr_with_boxes
from backend.llm.action_parser import parse_action_plan
from backend.llm.planner_prompt import format_prompt
import os

from backend.llm.deepseek_client import call_deepseek
from backend.llm.doubao_client import call_doubao
from backend.llm.qwen_client import call_qwen
from backend.vision.screenshot import capture_screen, capture_window
from backend.executor.ui_locator import locate_target, locate_text, rank_text_candidates
from backend.vision.uia_locator import MatchPolicy, find_element
from backend.llm.vlm_config import get_vlm_call
from backend.executor.task_registry import TaskStatus, create_task, get_task, update_task
from backend.executor.runtime_context import (
    ACTIVE_WINDOW,
    CURRENT_CONTEXT,
    _get_active_window_snapshot,
    _store_active_window,
    get_active_window,
    get_current_context,
    reset_active_window,
    reset_current_context,
    set_active_window,
    set_current_context,
)
from backend.executor.uia_rebind import rebind_element
from backend.executor.dispatch import (
    Dispatcher,
    handle_click as dispatch_handle_click,
    handle_hotkey,
    handle_open_app as dispatch_handle_open_app,
    handle_browser_click as dispatch_handle_browser_click,
    handle_browser_input as dispatch_handle_browser_input,
    handle_browser_extract_text as dispatch_handle_browser_extract_text,
    handle_open_url as dispatch_handle_open_url,
    handle_type,
    handle_wait_until as dispatch_handle_wait_until,
)
from backend.executor.evidence_emit import build_evidence, emit_context_event
from backend.executor.verify import _clip_text, verify_step_outcome
from backend.executor.uia_patterns import try_focus, try_invoke, try_select, try_set_value, try_toggle
from backend.utils.time_utils import now_iso_utc
from backend.utils.win32_foreground import ensure_foreground, get_foreground_info

MAX_STEPS = 10
LAST_WINDOW_TITLE: Optional[str] = None
LAST_OPEN_APP_CONTEXT: Dict[str, Any] = {}
MOUSE = mouse.controller

# Risk levels
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_BLOCK = "block"


class WindowProvider(Protocol):
    def get_foreground_window(self) -> Dict[str, Any]: ...


class _DefaultWindowProvider:
    def get_foreground_window(self) -> Dict[str, Any]:
        info = get_foreground_info()
        return {
            "hwnd": info.get("hwnd"),
            "pid": info.get("pid"),
            "title": info.get("title"),
            "class": info.get("class_name"),
            "class_name": info.get("class_name"),
        }


def _flag_from_env(var: str, default: bool) -> bool:
    value = os.getenv(var)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "off", "no", "none"}


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"", "0", "false", "off", "no", "none"}


def _coerce_nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(0, parsed)


def _foreground_snapshot() -> Dict[str, Any]:
    """Return hwnd/pid/title/class of current foreground window."""
    info = get_foreground_info()
    return {
        "hwnd": info.get("hwnd"),
        "pid": info.get("pid"),
        "title": info.get("title"),
        "class": info.get("class_name"),
        "class_name": info.get("class_name"),
    }


def _extract_focus_hints(step: ActionStep) -> Optional[Dict[str, Any]]:
    params = step.params or {}
    title = params.get("title") or params.get("target") or params.get("label")
    keywords = params.get("title_keywords") or []
    class_keywords = params.get("class_keywords") or []
    strict = params.get("strict_foreground")
    hints = {
        "title": title,
        "title_keywords": keywords if isinstance(keywords, list) else [],
        "class_keywords": class_keywords if isinstance(class_keywords, list) else [],
        "strict_foreground": strict,
    }
    has_hint = any(
        [
            hints["title"],
            hints["title_keywords"],
            hints["class_keywords"],
        ]
    )
    return hints if has_hint else None


def _window_matches(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    if not actual:
        return False
    # Prefer stable identifiers.
    if expected.get("hwnd") and actual.get("hwnd") and expected["hwnd"] == actual["hwnd"]:
        return True
    if expected.get("pid") and actual.get("pid") and expected["pid"] == actual["pid"]:
        return True
    expected_class = (expected.get("class") or expected.get("class_name") or "").lower()
    actual_class = (actual.get("class") or actual.get("class_name") or "").lower()
    if expected_class and actual_class and expected_class in actual_class:
        return True
    exp_keywords = [str(k).lower() for k in expected.get("class_keywords") or []]
    if exp_keywords and actual_class and any(k in actual_class for k in exp_keywords):
        return True
    exp_title = (expected.get("title") or "").lower()
    actual_title = (actual.get("title") or "").lower()
    if exp_title and exp_title in actual_title:
        return True
    exp_title_keywords = [str(k).lower() for k in expected.get("title_keywords") or []]
    if exp_title_keywords and actual_title and any(k in actual_title for k in exp_title_keywords):
        return True
    return False


def _set_last_focus_target(context, target: Optional[Dict[str, Any]]) -> None:
    if context is None:
        return
    try:
        context.last_focus_target = target
    except Exception:
        try:
            setattr(context, "last_focus_target", target)
        except Exception:
            pass


def _is_path_under(base_dir: Optional[str], path_value: Optional[str]) -> bool:
    if not base_dir or not path_value:
        return False
    try:
        base = Path(base_dir).resolve()
        target = Path(path_value).resolve()
        return base in target.parents or base == target
    except Exception:
        return False


def _score_risk(step: ActionStep, work_dir: Optional[str], last_focus_target: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Rule-based risk scoring."""
    action = step.action
    params = step.params or {}
    tags: List[str] = []

    # Default level.
    level = RISK_LOW
    reason = "ok"

    if action in RISKY_FILE_ACTIONS:
        tags.append("file_op")
        path_candidates = [
            params.get("path"),
            params.get("source"),
            params.get("destination_dir"),
            params.get("destination"),
            params.get("new_name"),
        ]
        in_scope = any(_is_path_under(work_dir, p) for p in path_candidates)
        if not in_scope or action == "delete_file":
            level = RISK_HIGH
            reason = "file_op_high_risk"
        else:
            level = RISK_MEDIUM
            reason = "file_op_in_scope"
    elif action in RISKY_INPUT_ACTIONS:
        tags.append("input")
        if not last_focus_target:
            level = RISK_HIGH
            reason = "input_no_focus_context"
        else:
            level = RISK_MEDIUM
            reason = "input_with_focus_context"
    elif action == "open_app":
        tags.append("app_launch")
        target = (params.get("target") or "").lower()
        risky_targets = {"powershell", "cmd", "regedit", "taskmgr"}
        if any(t in target for t in risky_targets):
            level = RISK_HIGH
            reason = "app_launch_sensitive"
        else:
            level = RISK_MEDIUM
            reason = "app_launch"
    else:
        level = RISK_LOW
        reason = "low_risk"

    return {"level": level, "reason": reason, "tags": tags}


def _foreground_matches(preferred: Dict[str, Any], current: Dict[str, Any]) -> bool:
    """Check whether the current foreground matches the preferred window."""
    if not preferred:
        return True
    pref_hwnd = preferred.get("hwnd") or preferred.get("handle")
    try:
        pref_hwnd = int(pref_hwnd) if pref_hwnd is not None else None
    except Exception:
        pref_hwnd = None
    if pref_hwnd and current.get("hwnd") and pref_hwnd == current.get("hwnd"):
        return True
    pref_pid = preferred.get("pid")
    if pref_pid and current.get("pid") and pref_pid == current.get("pid"):
        return True
    pref_title = str(preferred.get("title") or "").strip().lower()
    cur_title = str(current.get("title") or "").strip().lower()
    if pref_title and cur_title and pref_title in cur_title:
        return True
    return False


def _ensure_foreground(preferred: Dict[str, Any], strict: bool, logs: Optional[List[str]] = None) -> Tuple[bool, Dict[str, Any]]:
    """
    Ensure the preferred window is in foreground. Attempts one re-activation if needed.
    """
    preferred = preferred or {}
    pref_hwnd = preferred.get("hwnd") or preferred.get("handle")
    try:
        pref_hwnd = int(pref_hwnd) if pref_hwnd is not None else None
    except Exception:
        pref_hwnd = None

    fg_snapshot = _foreground_snapshot()
    if pref_hwnd and fg_snapshot.get("hwnd") == pref_hwnd:
        return True, fg_snapshot
    if not pref_hwnd and _foreground_matches(preferred, fg_snapshot):
        return True, fg_snapshot

    if pref_hwnd:
        try:
            enforce_result = ensure_foreground(int(pref_hwnd))
            if logs is not None:
                logs.append(f"foreground_enforce:{enforce_result}")
            fg_snapshot = enforce_result.get("final_foreground") or _foreground_snapshot()
            if enforce_result.get("ok") and fg_snapshot.get("hwnd") == pref_hwnd:
                return True, fg_snapshot
        except Exception as exc:  # noqa: BLE001
            if logs is not None:
                logs.append(f"foreground_enforce_error:{exc}")

    if not pref_hwnd and _foreground_matches(preferred, fg_snapshot):
        return True, fg_snapshot

    if strict:
        if logs is not None:
            logs.append(f"foreground_mismatch:preferred:{preferred} current:{fg_snapshot}")
        return False, fg_snapshot
    return True, fg_snapshot


def _enforce_strict_foreground_once(
    preferred: Dict[str, Any], logs: Optional[List[str]] = None
) -> Tuple[bool, Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Attempt to enforce a strict foreground match for a preferred hwnd exactly once.

    Returns (success, foreground_snapshot, enforcement_result).
    """
    preferred = preferred or {}
    pref_hwnd = preferred.get("hwnd") or preferred.get("handle")
    try:
        pref_hwnd = int(pref_hwnd) if pref_hwnd is not None else None
    except Exception:
        pref_hwnd = None
    fg_snapshot = _foreground_snapshot()
    if pref_hwnd and fg_snapshot.get("hwnd") == pref_hwnd:
        return True, fg_snapshot, None
    if not pref_hwnd:
        return False, fg_snapshot, None
    enforcement = ensure_foreground(int(pref_hwnd))
    if logs is not None:
        logs.append(f"foreground_enforce:{enforcement}")
    fg_after = enforcement.get("final_foreground") or _foreground_snapshot()
    ok = bool(enforcement.get("ok")) and fg_after.get("hwnd") == pref_hwnd
    return ok, fg_after, enforcement


def _capture_for_interaction(preferred: Dict[str, Any], strict: bool) -> Tuple[Optional[Path], Dict[str, Any], Optional[str]]:
    """
    Capture screenshot with preferred window awareness. Returns (path, fg_snapshot, error_reason).
    """
    preferred = preferred or {}
    if not preferred.get("hwnd"):
        active = ACTIVE_WINDOW.get(None)
        if isinstance(active, dict) and active.get("hwnd"):
            preferred = active
    if strict and not preferred.get("hwnd"):
        fg_hint = _foreground_snapshot()
        if fg_hint.get("hwnd"):
            preferred = {**preferred, **fg_hint}
    pref_hwnd = preferred.get("hwnd") or preferred.get("handle")
    try:
        pref_hwnd = int(pref_hwnd) if pref_hwnd is not None else None
    except Exception:
        pref_hwnd = None
    fg_snapshot = _foreground_snapshot()
    if strict and pref_hwnd and fg_snapshot.get("hwnd") != pref_hwnd:
        return None, fg_snapshot, "capture_foreground_mismatch"
    if strict and not pref_hwnd and preferred and not _foreground_matches(preferred, fg_snapshot):
        return None, fg_snapshot, "capture_foreground_mismatch"

    if preferred.get("hwnd"):
        try:
            path = capture_window(int(preferred["hwnd"]))
            return Path(path), fg_snapshot, None
        except Exception as exc:  # noqa: BLE001
            if strict:
                return None, fg_snapshot, f"capture_window_failed:{exc}"

    try:
        path = capture_screen()
        return Path(path), fg_snapshot, None
    except Exception as exc:  # noqa: BLE001
        return None, fg_snapshot, f"capture_failed:{exc}"


DEFAULT_STEP_MAX_RETRIES = _coerce_nonnegative_int(os.getenv("EXECUTOR_MAX_STEP_RETRIES", "1"), 1)
DEFAULT_CAPTURE_BEFORE = _flag_from_env("EXECUTOR_CAPTURE_BEFORE", True)
DEFAULT_CAPTURE_AFTER = _flag_from_env("EXECUTOR_CAPTURE_AFTER", True)
DEFAULT_CAPTURE_OCR = _flag_from_env("EXECUTOR_CAPTURE_OCR", False)
DEFAULT_UI_OCR = _flag_from_env("EXECUTOR_CAPTURE_UI_OCR", True)
DEFAULT_MAX_REPLANS = _coerce_nonnegative_int(os.getenv("EXECUTOR_MAX_REPLANS", "1"), 1)
DEFAULT_REPLAN_CAPTURE = _flag_from_env("EXECUTOR_REPLAN_CAPTURE_SCREENSHOT", True)
DEFAULT_DISABLE_VLM = _flag_from_env("EXECUTOR_DISABLE_VLM", False)
ALLOWED_ROOTS = [
    os.path.abspath(root)
    for root in (
        os.getenv("EXECUTOR_ALLOWED_ROOTS", str(Path.cwd()))
        .split(os.pathsep)
        if os.getenv("EXECUTOR_ALLOWED_ROOTS") is not None
        else [str(Path.cwd())]
    )
    if str(root).strip()
]
SYSTEM_FORBIDDEN_DIRS = [
    os.path.abspath(os.path.expandvars(path))
    for path in [
        r"C:\Windows",
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
        r"%WINDIR%",
        r"%SYSTEMROOT%",
    ]
]
USER_SENSITIVE_DIRS = [
    os.path.abspath(os.path.expandvars(path))
    for path in [
        r"%USERPROFILE%\AppData",
        r"%USERPROFILE%\AppData\Local",
        r"%USERPROFILE%\AppData\Roaming",
        r"%USERPROFILE%\Desktop",
        r"%USERPROFILE%\Documents",
        r"%USERPROFILE%\Pictures",
        r"%USERPROFILE%\Downloads",
    ]
]


def _add_allowed_root(path: str) -> None:
    """Append an allowed root if not already covered."""
    try:
        resolved = os.path.abspath(path)
    except Exception:
        return
    for root in ALLOWED_ROOTS:
        try:
            common = os.path.commonpath([resolved, root])
            if common == root:
                return
        except Exception:
            continue
    ALLOWED_ROOTS.append(resolved)


def _normalize_case(path_value: str) -> str:
    try:
        return path_value.casefold()
    except Exception:
        return path_value.lower()


def _normalize_path_candidate(path_value: str, base_dir: Optional[str]) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Normalize a user-provided path string into an absolute, resolved path.

    Returns (normalized_path, error_reason, had_traversal_hint).
    """
    if not path_value or not isinstance(path_value, str):
        return None, "invalid_path", False
    if "*" in path_value or "?" in path_value:
        return None, "wildcard_blocked", False
    had_traversal = ".." in Path(path_value).parts
    try:
        expanded = os.path.expanduser(path_value)
        if not os.path.isabs(expanded):
            if base_dir:
                expanded = os.path.join(base_dir, expanded)
            else:
                expanded = os.path.abspath(expanded)
        abs_path = os.path.abspath(expanded)
        # Resolve symlinks/junctions if possible but allow non-existent targets.
        resolved = Path(abs_path).resolve(strict=False)
        return str(resolved), None, had_traversal
    except Exception:
        return None, "normalize_error", had_traversal


def _is_under_any_root(path_value: str, roots: List[str]) -> bool:
    for root in roots:
        try:
            common = os.path.commonpath([path_value, root])
            if common == root:
                return True
        except Exception:
            continue
    return False


def _is_forbidden_path(path_value: str, allowed_roots: List[str]) -> bool:
    # If already under allowed root, do not treat as forbidden.
    if _is_under_any_root(path_value, allowed_roots):
        return False
    lower_path = _normalize_case(path_value)
    forbidden_bases = [*SYSTEM_FORBIDDEN_DIRS, *USER_SENSITIVE_DIRS]
    for base in forbidden_bases:
        try:
            if _normalize_case(base) and _normalize_case(path_value).startswith(_normalize_case(base)):
                return True
        except Exception:
            continue
    # Block drive roots (e.g., C:\) unless explicitly allowed.
    drive, tail = os.path.splitdrive(path_value)
    if drive and not tail.strip("\\/"):
        return True
    # Block UNC unless explicitly allowed via roots.
    if path_value.startswith("\\\\") and not _is_under_any_root(path_value, allowed_roots):
        return True
    return False
OCR_PREVIEW_LIMIT = 1200
OCR_CAPTURE_ACTIONS = {
    "browser_click",
    "browser_input",
    "browser_extract_text",
    "click",
    "click_text",
    "double_click",
    "right_click",
    "scroll",
    "drag",
}
VLM_DISABLED: ContextVar[bool] = ContextVar("VLM_DISABLED", default=DEFAULT_DISABLE_VLM)
CURRENT_TASK_ID: ContextVar[Optional[str]] = ContextVar("CURRENT_TASK_ID", default=None)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    dwmapi = ctypes.windll.dwmapi
except Exception:  # pragma: no cover - dwmapi may be missing
    dwmapi = None  # type: ignore

DWMWA_CLOAKED = 14
SW_RESTORE = 9
SW_SHOWMAXIMIZED = 3
GW_OWNER = 4
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
HWND_BOTTOM = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
_WECHAT_CLASS_NAMES = {"WeChatMainWndForPC", "WeChatPreviewWndForPC"}


class _WinInfo:
    __slots__ = ("hwnd", "title", "pid")

    def __init__(self, hwnd: int, title: str, pid: int) -> None:
        self.hwnd = hwnd
        self.title = title
        self.pid = pid


class _WinSnapshot:
    __slots__ = (
        "hwnd",
        "title",
        "pid",
        "is_visible",
        "is_cloaked",
        "has_owner",
        "is_minimized",
        "class_name",
        "rect",
    )

    def __init__(
        self,
        hwnd: int,
        title: str,
        pid: int,
        is_visible: bool,
        is_cloaked: bool,
        has_owner: bool,
        is_minimized: bool,
        class_name: str,
        rect: Tuple[int, int, int, int],
    ) -> None:
        self.hwnd = hwnd
        self.title = title
        self.pid = pid
        self.is_visible = is_visible
        self.is_cloaked = is_cloaked
        self.has_owner = has_owner
        self.is_minimized = is_minimized
        self.class_name = class_name
        self.rect = rect


def _run_powershell_json(command: str) -> Optional[Any]:
    """Run PowerShell and parse JSON output; return None on any failure."""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    raw = (completed.stdout or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _pids_for_process_names(names: List[str]) -> List[int]:
    target = {n.lower() for n in names if n}
    pids: List[int] = []
    if psutil:
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name")
                if name and name.lower() in target:
                    pids.append(proc.pid)
            except Exception:
                continue
    if pids:
        return pids

    # Fallback via PowerShell
    name_args = ",".join({n for n in names if n})
    ps_cmd = (
        f"Get-Process -ErrorAction SilentlyContinue {name_args} "
        "| Select-Object -ExpandProperty Id | ConvertTo-Json -Compress"
    )
    data = _run_powershell_json(ps_cmd)
    if isinstance(data, list):
        for item in data:
            try:
                pids.append(int(item))
            except Exception:
                continue
    elif isinstance(data, (int, float)):
        try:
            pids.append(int(data))
        except Exception:
            pass
    return pids


def _get_process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    if psutil:
        try:
            proc = psutil.Process(pid)
            return proc.name() or ""
        except Exception:
            pass
    # Fallback PowerShell for name
    ps_cmd = (
        f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty ProcessName) | ConvertTo-Json -Compress"
    )
    data = _run_powershell_json(ps_cmd)
    if isinstance(data, str):
        return data
    return ""


def _is_cloaked(hwnd: int) -> bool:
    if not dwmapi:
        return False
    cloaked = wintypes.DWORD()
    try:
        res = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(DWMWA_CLOAKED),
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        if res == 0:
            return cloaked.value != 0
    except Exception:
        return False
    return False


def _get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length == 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
    return buffer.value.strip()


def _get_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    try:
        length = user32.GetClassNameW(wintypes.HWND(hwnd), buffer, 255)
        if length > 0:
            return buffer.value.strip()
    except Exception:
        pass
    return ""


def _get_window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT()
    try:
        if user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return (0, 0, 0, 0)


def _enum_wechat_windows() -> List[_WinSnapshot]:
    snapshots: List[_WinSnapshot] = []
    blocked_classes = {"applicationframewindow", "applicationframeinputsinkwindow"}
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def _callback(hwnd, _lparam):
        title = _get_window_title(hwnd)
        class_name = _get_class_name(hwnd)
        title_l = title.lower()
        class_l = class_name.lower()
        if "wechat" not in title_l and "微信" not in title_l and "wechat" not in class_l:
            return True
        if class_l in blocked_classes:
            return True
        pid_out = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
        is_visible = bool(user32.IsWindowVisible(hwnd))
        has_owner = bool(user32.GetWindow(hwnd, GW_OWNER))
        if has_owner:
            return True
        is_cloaked = _is_cloaked(hwnd)
        try:
            is_minimized = bool(user32.IsIconic(hwnd))
        except Exception:
            is_minimized = False
        rect = _get_window_rect(hwnd)
        # Reject only if both hidden and minimized (likely non-interactive host).
        if not is_visible and is_minimized:
            return True
        snapshots.append(
            _WinSnapshot(
                int(hwnd),
                title,
                int(pid_out.value),
                is_visible,
                is_cloaked,
                has_owner,
                is_minimized,
                class_name,
                rect,
            )
        )
        return True

    try:
        user32.EnumWindows(_callback, 0)
    except Exception:
        return snapshots
    return snapshots


def _probe_windows_for_pid(pid: int) -> List[_WinSnapshot]:
    snapshots: List[_WinSnapshot] = []
    if pid <= 0:
        return snapshots
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def _callback(hwnd, _lparam):
        pid_out = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
        if pid_out.value != pid:
            return True
        title = _get_window_title(hwnd)
        is_visible = bool(user32.IsWindowVisible(hwnd))
        has_owner = bool(user32.GetWindow(hwnd, GW_OWNER))
        is_cloaked = _is_cloaked(hwnd)
        try:
            is_minimized = bool(user32.IsIconic(hwnd))
        except Exception:
            is_minimized = False
        class_name = _get_class_name(hwnd)
        rect = _get_window_rect(hwnd)
        snapshots.append(
            _WinSnapshot(
                int(hwnd),
                title,
                int(pid),
                is_visible,
                is_cloaked,
                has_owner,
                is_minimized,
                class_name,
                rect,
            )
        )
        return True

    try:
        user32.EnumWindows(_callback, 0)
    except Exception:
        return snapshots
    return snapshots


def _filter_interactive_windows(snapshots: List[_WinSnapshot]) -> List[_WinInfo]:
    windows: List[_WinInfo] = []
    for snap in snapshots:
        if not snap.title:
            continue
        if snap.is_cloaked or snap.has_owner:
            continue
        if not snap.is_visible and not snap.is_minimized:
            continue
        windows.append(_WinInfo(snap.hwnd, snap.title, snap.pid))
    return windows


def _filter_wechat_windows(snapshots: List[_WinSnapshot]) -> List[_WinInfo]:
    windows: List[_WinInfo] = []
    for snap in snapshots:
        if snap.is_cloaked or snap.has_owner:
            continue
        if not snap.title:
            continue
        if not snap.class_name:
            continue
        class_lower = snap.class_name.lower()
        if "wechat" not in class_lower:
            continue
        left, top, right, bottom = snap.rect
        width = max(0, right - left)
        height = max(0, bottom - top)
        min_width = 140 if snap.is_minimized else 200
        min_height = 120 if snap.is_minimized else 150
        size_ok = width >= min_width and height >= min_height
        if width > 2000 or height > 2000:
            continue
        class_ok = snap.class_name in _WECHAT_CLASS_NAMES or (
            snap.class_name and snap.class_name.lower().startswith("wechat")
        )
        if not size_ok and not class_ok:
            continue
        windows.append(_WinInfo(snap.hwnd, snap.title or snap.class_name or "", snap.pid))
    return windows


def _activate_hwnd(hwnd: int, pid: int) -> bool:
    try:
        target_pid = pid if pid and pid > 0 else -1
        user32.AllowSetForegroundWindow(target_pid)
    except Exception:
        pass
    try:
        fg_result = ensure_foreground(int(hwnd))
        return bool(fg_result.get("ok"))
    except Exception:
        return False


def _force_foreground_wechat(hwnd: int, pid: int) -> bool:
    success = False
    try:
        user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    try:
        res = user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        success = success or bool(res)
    except Exception:
        pass
    try:
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        success = True
    except Exception:
        pass
    try:
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(HWND_NOTOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        success = True
    except Exception:
        pass
    try:
        res = user32.SetForegroundWindow(wintypes.HWND(hwnd))
        success = success or bool(res)
    except Exception:
        pass
    try:
        res = user32.BringWindowToTop(wintypes.HWND(hwnd))
        success = success or bool(res)
    except Exception:
        pass
    try:
        res = user32.SetActiveWindow(wintypes.HWND(hwnd))
        success = success or bool(res)
    except Exception:
        pass
    return success


def _best_window_for_terms(terms: List[str], windows: List[_WinInfo]) -> Tuple[Optional[_WinInfo], str, float]:
    best_win: Optional[_WinInfo] = None
    best_term = ""
    best_score = -1.0
    for term in terms:
        term_lower = term.lower()
        for win in windows:
            win_lower = win.title.lower()
            score = 1.0 if term_lower in win_lower else difflib.SequenceMatcher(
                None, term_lower, win_lower
            ).ratio()
            if score > best_score:
                best_score = score
                best_win = win
                best_term = term
    return best_win, best_term, best_score


def _enum_top_windows() -> List[_WinSnapshot]:
    """Enumerate top-level windows with basic metadata for activation."""
    snapshots: List[_WinSnapshot] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def _callback(hwnd, _lparam):
        title = _get_window_title(hwnd)
        class_name = _get_class_name(hwnd)
        pid_out = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
        is_visible = bool(user32.IsWindowVisible(hwnd))
        has_owner = bool(user32.GetWindow(hwnd, GW_OWNER))
        is_cloaked = _is_cloaked(hwnd)
        try:
            is_minimized = bool(user32.IsIconic(hwnd))
        except Exception:
            is_minimized = False
        rect = _get_window_rect(hwnd)
        snapshots.append(
            _WinSnapshot(
                int(hwnd),
                title,
                int(pid_out.value),
                is_visible,
                is_cloaked,
                has_owner,
                is_minimized,
                class_name,
                rect,
            )
        )
        return True

    try:
        user32.EnumWindows(_callback, 0)
    except Exception:
        return snapshots
    return snapshots


def _filter_windows_by_keywords(
    snapshots: List[_WinSnapshot],
    title_keywords: List[str],
    class_keywords: Optional[List[str]] = None,
) -> List[_WinSnapshot]:
    """
    Filter windows whose title or class contains any keyword (case-insensitive).
    Skips cloaked/owned windows and ignores fully hidden non-minimized windows.
    """
    title_terms = [t.lower() for t in title_keywords if isinstance(t, str) and t.strip()]
    class_terms = [
        t.lower() for t in (class_keywords or []) if isinstance(t, str) and t.strip()
    ]
    if not title_terms and not class_terms:
        return []

    matches: List[_WinSnapshot] = []
    for snap in snapshots:
        if snap.is_cloaked or snap.has_owner:
            continue
        if not snap.is_visible and not snap.is_minimized:
            continue
        title_l = (snap.title or "").lower()
        class_l = (snap.class_name or "").lower()
        title_hit = any(term in title_l for term in title_terms) if title_terms else False
        class_hit = any(term in class_l for term in class_terms) if class_terms else False
        if title_hit or class_hit:
            matches.append(snap)
    return matches


def _score_window_candidate(snap: _WinSnapshot, terms: List[str]) -> Tuple[float, str]:
    """
    Return the best fuzzy score for the window across the provided terms.
    """
    best_score = -1.0
    best_term = ""
    title_l = (snap.title or "").lower()
    class_l = (snap.class_name or "").lower()

    for term in terms:
        score_title = 1.0 if term in title_l else difflib.SequenceMatcher(None, term, title_l).ratio()
        score_class = 1.0 if term in class_l else difflib.SequenceMatcher(None, term, class_l).ratio()
        candidate_score = max(score_title, score_class)
        if candidate_score > best_score:
            best_score = candidate_score
            best_term = term
    return best_score, best_term


def _foreground_window(hwnd: int, pid: int, logs: List[str]) -> bool:
    """
    Restore and foreground a window using topmost bounce and thread attachment.
    """
    try:
        user32.AllowSetForegroundWindow(pid if pid and pid > 0 else -1)
        logs.append("AllowSetForegroundWindow:ok")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"AllowSetForegroundWindow:error:{exc}")

    # Preserve window state in logs for diagnostics.
    try:
        was_minimized = bool(user32.IsIconic(wintypes.HWND(hwnd)))
    except Exception:
        was_minimized = False
    try:
        was_maximized = bool(user32.IsZoomed(wintypes.HWND(hwnd)))
    except Exception:
        was_maximized = False
    logs.append(f"WindowState:minimized:{was_minimized}:maximized:{was_maximized}")

    fg_result = ensure_foreground(int(hwnd))
    logs.append(f"ensure_foreground:{fg_result}")
    return bool(fg_result.get("ok"))


def activate_window(params: Dict[str, Any]) -> dict:
    """
    Activate a window by fuzzy-matching title/class keywords and foregrounding it.
    """
    global LAST_WINDOW_TITLE
    logs: List[str] = []
    try:
        action = ActivateWindowAction.model_validate(params)
    except ValidationError as exc:  # noqa: BLE001
        return {
            "success": False,
            "status": "error",
            "reason": f"invalid activate_window params: {exc}",
            "hwnd": None,
            "log": logs,
        }

    title_keywords = list(action.title_keywords)
    class_keywords = list(action.class_keywords)
    logs.append(f"title_keywords:{title_keywords} class_keywords:{class_keywords} strict:{action.strict}")

    if not title_keywords and not class_keywords:
        return {"success": False, "status": "error", "reason": "title_keywords or class_keywords required", "hwnd": None, "log": logs}

    primary_query = title_keywords[0] if title_keywords else ""
    if primary_query:
        try:
            uia_probe = locate_target(
                primary_query,
                [],
                match_policy=MatchPolicy.WINDOW_FIRST,
                preferred_title=primary_query,
            )
        except Exception as exc:  # noqa: BLE001
            logs.append(f"uia:error:{exc}")
            uia_probe = None
        if isinstance(uia_probe, dict) and uia_probe.get("method") == "uia":
            logs.append("uia:match")
            source = (uia_probe.get("candidate") or {}).get("source") or {}
            handle = source.get("handle")
            pid = source.get("pid")
            if handle:
                activated = _activate_hwnd(int(handle), int(source.get("pid") or -1))
                logs.append(f"uia:activate:{activated}")
                if activated:
                    LAST_WINDOW_TITLE = source.get("name") or primary_query
                    snapshot = {
                        "hwnd": int(handle),
                        "pid": pid,
                        "title": source.get("name") or primary_query,
                        "class": source.get("control_type"),
                    }
                    _store_active_window(snapshot)
                    return {
                        "success": True,
                        "status": "success",
                        "reason": "activated",
                        "hwnd": int(handle),
                        "pid": pid,
                        "matched_title": source.get("name") or primary_query,
                        "matched_class": source.get("control_type"),
                        "matched_term": primary_query,
                        "score": uia_probe.get("score", 1.0),
                        "active_window": snapshot,
                        "log": logs,
                    }
            logs.append("uia:no_handle_or_activation")

    snapshots = _enum_top_windows()
    logs.append(f"enum_windows:{len(snapshots)}")

    candidates = _filter_windows_by_keywords(snapshots, title_keywords, class_keywords)
    logs.append(f"candidates:{len(candidates)}")
    if not candidates:
        return {"success": False, "reason": "no matching window", "hwnd": None, "log": logs}

    terms = [t.lower() for t in title_keywords + class_keywords]
    best_snap: Optional[_WinSnapshot] = None
    best_score = -1.0
    matched_term = ""
    for snap in candidates:
        score, term = _score_window_candidate(snap, terms)
        if score > best_score:
            best_score = score
            best_snap = snap
            matched_term = term

    if not best_snap:
        return {"success": False, "reason": "no best candidate", "hwnd": None, "log": logs}

    logs.append(
        f"selected_hwnd:{best_snap.hwnd} pid:{best_snap.pid} title:{best_snap.title} class:{best_snap.class_name} score:{best_score} term:{matched_term}"
    )

    activated = _foreground_window(best_snap.hwnd, best_snap.pid, logs)
    snapshot = {
        "hwnd": best_snap.hwnd,
        "pid": best_snap.pid,
        "title": best_snap.title,
        "class": best_snap.class_name,
        "class_name": best_snap.class_name,
    }

    # Strict foreground enforcement
    if action.strict:
        fg_result = ensure_foreground(best_snap.hwnd)
        logs.append(f"foreground_enforce:{fg_result}")
        activated = bool(fg_result.get("ok")) and (fg_result.get("final_foreground") or {}).get("hwnd") == best_snap.hwnd
        if not activated:
            _store_active_window(None)
            return {
                "success": False,
                "status": "error",
                "reason": "foreground_enforcement_failed",
                "hwnd": best_snap.hwnd,
                "pid": best_snap.pid,
                "matched_title": best_snap.title,
                "matched_class": best_snap.class_name,
                "matched_term": matched_term,
                "score": best_score,
                "active_window": None,
                "foreground": fg_result.get("final_foreground"),
                "log": logs,
            }
        snapshot["foreground"] = fg_result.get("final_foreground")
    elif activated:
        LAST_WINDOW_TITLE = best_snap.title

    if activated:
        _store_active_window(snapshot)
        LAST_WINDOW_TITLE = best_snap.title

    return {
        "success": bool(activated),
        "status": "success" if activated else "error",
        "reason": "activated" if activated else "foreground_failed",
        "hwnd": best_snap.hwnd,
        "pid": best_snap.pid,
        "matched_title": best_snap.title,
        "matched_class": best_snap.class_name,
        "matched_term": matched_term,
        "score": best_score,
        "active_window": snapshot if activated else None,
        "log": logs,
    }


def _activate_wechat_bridge_window(
    search_terms: List[str],
    requested: str,
    prefer_pids: Optional[List[int]] = None,
    debug: Optional[Dict[str, Any]] = None,
) -> dict:
    debug_info: Dict[str, Any] = debug or {}
    debug_info.setdefault("requested", requested)
    debug_info.setdefault("search_terms", search_terms)

    prefer_pid_list: List[int] = []
    for pid in prefer_pids or []:
        try:
            pid_int = int(pid)
        except Exception:
            continue
        if pid_int > 0:
            prefer_pid_list.append(pid_int)
    debug_info["prefer_pids"] = prefer_pid_list

    detected_pids = _pids_for_process_names(["WeChatAppEx"])
    combined: List[int] = []
    seen: set[int] = set()
    for pid in prefer_pid_list + detected_pids:
        if pid not in seen and pid > 0:
            combined.append(pid)
            seen.add(pid)
    debug_info["wechat_process_pids"] = combined
    if not combined:
        return {
            "status": "error",
            "message": "wechat bridge process not found",
            "requested": requested,
            "method": "wechat_bridge_pid",
            "debug": debug_info,
        }

    all_snapshots: List[_WinSnapshot] = []
    window_probe: List[dict] = []
    for pid in combined:
        snaps = _probe_windows_for_pid(pid)
        window_probe.append(
            {
                "pid": pid,
                "windows": [
                    {
                        "hwnd": snap.hwnd,
                        "title": snap.title,
                        "class_name": snap.class_name,
                        "rect": snap.rect,
                        "is_visible": snap.is_visible,
                        "is_cloaked": snap.is_cloaked,
                        "has_owner": snap.has_owner,
                        "is_minimized": snap.is_minimized,
                    }
                    for snap in snaps
                ],
            }
        )
        all_snapshots.extend(snaps)
    debug_info["window_probe"] = window_probe

    snapshot_lookup = {(snap.pid, snap.hwnd): snap for snap in all_snapshots}
    candidates = _filter_wechat_windows(all_snapshots)
    candidate_debug: List[Dict[str, Any]] = []
    for win in candidates:
        snap = snapshot_lookup.get((win.pid, win.hwnd))
        candidate_debug.append(
            {
                "hwnd": win.hwnd,
                "title": win.title,
                "pid": win.pid,
                "is_visible": snap.is_visible if snap else None,
                "is_cloaked": snap.is_cloaked if snap else None,
                "has_owner": snap.has_owner if snap else None,
                "is_minimized": snap.is_minimized if snap else None,
                "class_name": snap.class_name if snap else None,
                "rect": snap.rect if snap else None,
                "width": (snap.rect[2] - snap.rect[0]) if snap else None,
                "height": (snap.rect[3] - snap.rect[1]) if snap else None,
            }
        )
    debug_info["candidate_windows"] = candidate_debug
    if not candidates:
        return {
            "status": "error",
            "message": "wechat bridge windows not found",
            "requested": requested,
            "method": "wechat_bridge_pid",
            "debug": debug_info,
        }

    best_win, matched_term, score = _best_window_for_terms(search_terms, candidates)
    if not best_win:
        return {
            "status": "error",
            "message": "wechat bridge window scoring failed",
            "requested": requested,
            "method": "wechat_bridge_pid",
            "debug": debug_info,
        }

    debug_info["selected_window"] = {
        "hwnd": best_win.hwnd,
        "pid": best_win.pid,
        "title": best_win.title,
        "matched_term": matched_term,
        "score": score,
    }

    activated = _force_foreground_wechat(best_win.hwnd, best_win.pid)
    debug_info["activation_attempted"] = {
        "hwnd": best_win.hwnd,
        "pid": best_win.pid,
        "activated": activated,
    }
    if activated:
        global LAST_WINDOW_TITLE
        LAST_WINDOW_TITLE = best_win.title
    return {
        "status": "activated" if activated else "found_not_activated",
        "requested": requested,
        "matched_title": best_win.title,
        "matched_term": matched_term,
        "score": score,
        "method": "wechat_bridge_pid",
        "pid": best_win.pid,
        "hwnd": best_win.hwnd,
        "debug": debug_info,
    }

def handle_open_url(step: ActionStep) -> Any:
    return dispatch_handle_open_url(step, provider=sys.modules[__name__])


def handle_browser_click(step: ActionStep) -> Any:
    return dispatch_handle_browser_click(step, provider=sys.modules[__name__])


def handle_switch_window(step: ActionStep) -> str:
    title = (step.params or {}).get("title") or (step.params or {}).get("name")
    if not title or not isinstance(title, str):
        return "error: 'title' param is required"

    target = title.strip()
    target_lower = target.lower()
    global LAST_WINDOW_TITLE

    try:
        uia_probe = locate_target(target, [], match_policy=MatchPolicy.WINDOW_FIRST)
    except Exception:
        uia_probe = None
    if isinstance(uia_probe, dict) and uia_probe.get("method") == "uia":
        candidate = uia_probe.get("candidate") or {}
        source = candidate.get("source") or {}
        handle = source.get("handle")
        if handle:
            activated = _activate_hwnd(int(handle), int(source.get("pid") or -1))
            if activated:
                LAST_WINDOW_TITLE = source.get("name") or target
                return {
                    "status": "activated",
                    "requested": target,
                    "matched_title": source.get("name") or candidate.get("text") or target,
                    "score": uia_probe.get("score", 1.0),
                    "method": "uia",
                    "locator": uia_probe,
                }
    try:
        windows = gw.getAllWindows()
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to query windows: {exc}"

    # Prefer last launched/activated window if available.
    if LAST_WINDOW_TITLE:
        for win in windows:
            if (getattr(win, "title", "") or "").strip() == LAST_WINDOW_TITLE:
                try:
                    if getattr(win, "isMinimized", False):
                        win.restore()
                    win.activate()
                    return {
                        "status": "activated",
                        "requested": target,
                        "matched_title": LAST_WINDOW_TITLE,
                        "score": 1.0,
                        "source": "last_window_title",
                    }
                except Exception:
                    break

    best_score = -1.0
    best_win = None
    for win in windows:
        win_title = (getattr(win, "title", "") or "").strip()
        if not win_title:
            continue
        win_lower = win_title.lower()
        score = 1.0 if target_lower in win_lower else difflib.SequenceMatcher(
            None, target_lower, win_lower
        ).ratio()
        if score > best_score:
            best_score = score
            best_win = win

    if not best_win:
        return f"error: window not found matching '{target}'"

    try:
        if getattr(best_win, "isMinimized", False):
            best_win.restore()
        best_win.activate()
        LAST_WINDOW_TITLE = (getattr(best_win, "title", "") or "").strip()
        return {
            "status": "activated",
            "requested": target,
            "matched_title": getattr(best_win, "title", "") or "",
            "score": best_score,
        }
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to activate window: {exc}"


def handle_activate_window(step: ActionStep) -> Any:
    result = activate_window(step.params or {})
    if result.get("success"):
        result.setdefault("status", "success")
    else:
        result.setdefault("status", "error")
        _store_active_window(None)
    return result


def handle_click(step: ActionStep) -> Any:
    return dispatch_handle_click(step, provider=sys.modules[__name__])


def handle_key_press(step: ActionStep) -> str:
    return input.key_press(step.params)


def handle_move_file(step: ActionStep) -> Dict[str, Any]:
    return files.move_file(step.params)


def handle_rename_file(step: ActionStep) -> Dict[str, Any]:
    return files.rename_file(step.params)


def handle_open_file(step: ActionStep) -> Dict[str, Any]:
    return files.open_file(step.params)


def handle_create_folder(step: ActionStep) -> Dict[str, Any]:
    return files.create_folder(step.params)


def handle_wait(step: ActionStep) -> str:
    seconds = (step.params or {}).get("seconds", 0)
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "error: 'seconds' must be a number"
    if seconds < 0:
        return "error: 'seconds' must be non-negative"

    time.sleep(seconds)
    return f"waited {seconds} seconds"


def _get_active_window_rect() -> Optional[Tuple[int, int, int, int]]:
    """Return bounding rect of the foreground top-level window."""
    try:
        with auto.UIAutomationInitializerInThread(debug=False):
            fg = auto.GetForegroundControl()
            if not fg:
                return None
            top = fg.GetTopLevelControl()
            rect = getattr(top, "BoundingRectangle", None)
            if not rect:
                return None
            left, top_v, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
            if right <= left or bottom <= top_v:
                return None
            return left, top_v, right, bottom
    except Exception:
        return None


def _hash_active_window_region(rect: Tuple[int, int, int, int]) -> Tuple[Optional[str], Optional[str]]:
    """Compute a SHA1 hash of the active window region to track stability."""
    try:
        screenshot_path = capture_screen()
    except Exception as exc:  # noqa: BLE001
        return None, f"screenshot_error:{exc}"
    try:
        with Image.open(screenshot_path) as img:
            left, top, right, bottom = rect
            left = max(0, left)
            top = max(0, top)
            right = min(img.width, right)
            bottom = min(img.height, bottom)
            if right <= left or bottom <= top:
                return None, "empty_rect"
            region = img.crop((left, top, right, bottom))
            buf = BytesIO()
            region.save(buf, format="PNG")
            digest = hashlib.sha1(buf.getvalue()).hexdigest()
            return digest, None
    except Exception as exc:  # noqa: BLE001
        return None, f"hash_error:{exc}"


def _build_context_snapshot(context) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {}
    if not context:
        return snapshot
    try:
        snapshot["last_ui_fingerprint"] = context.get_ui_fingerprint(lite_only=True)
    except Exception:
        snapshot["last_ui_fingerprint"] = None
    try:
        snapshot["last_screenshot_path"] = getattr(context, "last_screenshot_path", None)
    except Exception:
        snapshot["last_screenshot_path"] = None
    try:
        snapshot["work_dir"] = getattr(context, "work_dir", None)
    except Exception:
        pass
    try:
        snapshot["active_window"] = getattr(context, "active_window", None)
    except Exception:
        snapshot["active_window"] = None
    return snapshot


def handle_wait_until(step: ActionStep) -> Dict[str, Any]:
    return dispatch_handle_wait_until(step, provider=sys.modules[__name__])


def handle_take_over(step: ActionStep) -> Dict[str, Any]:
    """
    Placeholder handler that signals user takeover.
    """
    return {"status": "awaiting_user", "message": "User takeover required", "ok": True}


def _require_number(value, name: str) -> Optional[str]:
    if not isinstance(value, (int, float)):
        return f"error: '{name}' must be a number"
    return None


def handle_mouse_move(step: ActionStep) -> str:
    x = (step.params or {}).get("x")
    y = (step.params or {}).get("y")
    if (msg := _require_number(x, "x")) or (msg := _require_number(y, "y")):
        return msg
    return f"stub: mouse_move to ({x}, {y}) not implemented yet"


def _handle_click_variant(step: ActionStep, variant: str) -> str:
    params = step.params or {}
    x = params.get("x")
    y = params.get("y")
    button = "left"
    clicks = 1
    if variant == "right_click":
        button = "right"
    elif variant == "double_click":
        clicks = 2
    # Resolve coordinates
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        cx, cy = x, y
        locator_meta = {"method": "absolute"}
    else:
        query = params.get("text") or params.get("target") or params.get("label") or params.get("visual_description")
        if not query:
            return "error: 'x'/'y' or 'text/target/visual_description' is required"
        try:
            screenshot_path = capture_screen()
            _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
        except Exception as exc:  # noqa: BLE001
            return f"error: locate failed: {exc}"
        try:
            locate_result = _locate_from_params(
                query, params, boxes, Path(screenshot_path), match_policy=MatchPolicy.CONTROL_ONLY
            )
        except ValueError as exc:
            return {"status": "error", "reason": str(exc)}
        if locate_result.get("status") != "success":
            reason = (locate_result or {}).get("reason") if isinstance(locate_result, dict) else None
            return {"status": "error", "reason": reason or "locate_failed", "locator": locate_result}
        center = locate_result.get("center") or {}
        cx = center.get("x")
        cy = center.get("y")
        if cx is None or cy is None:
            return {"status": "error", "reason": "locate_missing_center", "locator": locate_result}
        center = {"x": cx, "y": cy}
        valid_center, center_reason = _validate_locator_center(center, locate_result)
        if not valid_center:
            return {"status": "error", "reason": center_reason, "locator": locate_result}
        cx = center["x"]
        cy = center["y"]
        locator_meta = locate_result

    try:
        import pyautogui  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return f"error: pyautogui unavailable: {exc}"

    try:
        if clicks == 2:
            pyautogui.click(x=cx, y=cy, button=button, clicks=2, interval=0.12)
        else:
            pyautogui.click(x=cx, y=cy, button=button)
        return {
            "status": "success",
            "method": locator_meta.get("method"),
            "center": {"x": cx, "y": cy},
            "locator": locator_meta,
            "variant": variant,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"failed to {variant}: {exc}"}


def handle_right_click(step: ActionStep) -> str:
    return _handle_click_variant(step, "right_click")


def handle_double_click(step: ActionStep) -> str:
    return _handle_click_variant(step, "double_click")


def handle_scroll(step: ActionStep) -> Dict[str, Any]:
    """
    Scroll via MouseController using direction/amount or explicit deltas.

    Returns a metadata-rich dict for feedback loops.
    """
    params = step.params or {}
    try:
        scroll_action = ScrollAction.model_validate(params)
    except ValidationError as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"invalid scroll params: {exc}"}

    dx, dy = scroll_action.to_deltas()
    controller_result = MOUSE.scroll(dx=dx, dy=dy)
    controller_result.setdefault("status", "success")
    controller_result["action"] = "scroll"
    controller_result["metadata"] = {
        "direction": scroll_action.direction,
        "amount": scroll_action.amount if scroll_action.direction else None,
        "delta": {"dx": dx, "dy": dy},
    }
    return controller_result


def handle_drag(step: ActionStep) -> Dict[str, Any]:
    params = step.params or {}
    try:
        drag_action = DragAction.model_validate(params)
    except ValidationError as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"invalid drag params: {exc}"}

    start = drag_action.start
    end = drag_action.end
    start_coords = None
    end_coords = None
    locator_meta: Dict[str, Any] = {}

    def _extract_query(point: Dict[str, Any]) -> Optional[str]:
        return (
            point.get("text")
            or point.get("target")
            or point.get("label")
            or point.get("visual_description")
            or drag_action.visual_description
        )

    needs_locate = not (isinstance(start.get("x"), (int, float)) and isinstance(start.get("y"), (int, float))) or not (
        isinstance(end.get("x"), (int, float)) and isinstance(end.get("y"), (int, float))
    )

    boxes: List[OcrBox] = []
    screenshot_path: Optional[Path] = None
    if needs_locate:
        try:
            screenshot_path = capture_screen()
            _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "reason": f"locate failed: {exc}"}

    def _resolve_point(point: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Dict[str, Any]]:
        if isinstance(point.get("x"), (int, float)) and isinstance(point.get("y"), (int, float)):
            return float(point["x"]), float(point["y"]), {"method": "absolute"}
        query = _extract_query(point)
        if not query or not screenshot_path:
            return None, None, {"status": "error", "reason": "missing_target"}
        merged_params = {**params, **point}
        try:
            loc_result = _locate_from_params(
                query, merged_params, boxes, Path(screenshot_path), match_policy=MatchPolicy.CONTROL_ONLY
            )
        except ValueError as exc:
            return None, None, {"status": "error", "reason": str(exc)}
        if loc_result.get("status") != "success":
            return None, None, loc_result
        center = loc_result.get("center") or {}
        return center.get("x"), center.get("y"), loc_result

    sx, sy, start_meta = _resolve_point(start)
    ex, ey, end_meta = _resolve_point(end)
    locator_meta["start"] = start_meta
    locator_meta["end"] = end_meta

    if sx is None or sy is None or ex is None or ey is None:
        return {"status": "error", "reason": "locate_failed", "locator": locator_meta}

    result = MOUSE.drag({"x": sx, "y": sy}, {"x": ex, "y": ey}, duration=drag_action.duration)
    result.setdefault("status", "success")
    result["action"] = "drag"
    result["metadata"] = {
        "start": {"x": sx, "y": sy},
        "end": {"x": ex, "y": ey},
        "duration": drag_action.duration,
        "locator": locator_meta,
    }
    return result


def handle_list_windows(step: ActionStep) -> str:
    try:
        windows = gw.getAllWindows()
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to list windows: {exc}"

    result: List[dict] = []
    for win in windows:
        title = getattr(win, "title", "") or ""
        result.append(
            {
                "title": title,
                "is_active": bool(getattr(win, "isActive", False)),
                "is_visible": bool(getattr(win, "isVisible", False)),
                "is_minimized": bool(getattr(win, "isMinimized", False)),
            }
        )
    return {"windows": result, "count": len(result)}


def handle_get_active_window(step: ActionStep) -> str:
    try:
        win = gw.getActiveWindow()
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to get active window: {exc}"
    if not win:
        return {"active_window": None, "message": "no active window"}
    return {
        "active_window": getattr(win, "title", "") or "",
        "is_visible": bool(getattr(win, "isVisible", False)),
        "is_minimized": bool(getattr(win, "isMinimized", False)),
    }


def handle_fuzzy_switch_window(step: ActionStep) -> str:
    title = (step.params or {}).get("title")
    if not title or not isinstance(title, str):
        return "error: 'title' param is required"
    target = title.strip()

    try:
        windows = gw.getAllWindows()
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to list windows: {exc}"

    candidates: List[Tuple[float, object]] = []
    target_lower = target.lower()
    for win in windows:
        win_title = (getattr(win, "title", "") or "").strip()
        if not win_title:
            continue
        win_lower = win_title.lower()
        score = 1.0 if target_lower in win_lower else difflib.SequenceMatcher(
            None, target_lower, win_lower
        ).ratio()
        candidates.append((score, win))

    if not candidates:
        return f"error: window not found matching '{target}'"

    # Pick the best score
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_win = candidates[0]

    try:
        if getattr(best_win, "isMinimized", False):
            best_win.restore()
        best_win.activate()
    except Exception as exc:  # noqa: BLE001
        return f"error: failed to activate '{best_win.title}': {exc}"

    return {
        "matched_title": getattr(best_win, "title", "") or "",
        "score": best_score,
        "requested": target,
    }


def handle_list_files(step: ActionStep) -> Dict[str, Any]:
    return files.list_files(step.params)


def handle_delete_file(step: ActionStep) -> Dict[str, Any]:
    return files.delete_file(step.params)


def handle_copy_file(step: ActionStep) -> Dict[str, Any]:
    return files.copy_file(step.params)


def handle_create_folder(step: ActionStep) -> str:
    return files.create_folder(step.params)


def handle_read_file(step: ActionStep) -> str:
    path = (step.params or {}).get("path")
    if not path or not isinstance(path, str):
        return "error: 'path' param is required"
    return files.read_file(step.params)


def handle_write_file(step: ActionStep) -> str:
    return files.write_file(step.params)


def handle_adjust_volume(step: ActionStep) -> str:
    params = step.params or {}
    if "level" in params:
        try:
            level = float(params.get("level"))
        except (TypeError, ValueError):
            return "error: 'level' must be a number"
        return f"stub: adjust_volume to {level}% not implemented yet"
    if "delta" in params:
        try:
            delta = float(params.get("delta"))
        except (TypeError, ValueError):
            return "error: 'delta' must be a number"
        return f"stub: adjust_volume by {delta} not implemented yet"
    return "error: 'level' or 'delta' param is required"


def _coerce_boxes(raw_boxes: Any) -> List[OcrBox]:
    boxes: List[OcrBox] = []
    if not raw_boxes:
        return boxes
    if isinstance(raw_boxes, list):
        for item in raw_boxes:
            if isinstance(item, OcrBox):
                boxes.append(item)
            elif isinstance(item, dict):
                try:
                    boxes.append(
                        OcrBox(
                            text=str(item.get("text", "")),
                            x=int(item.get("x", 0)),
                            y=int(item.get("y", 0)),
                            width=int(item.get("width", 0)),
                            height=int(item.get("height", 0)),
                            conf=float(item.get("conf", -1.0)),
                        )
                    )
                except Exception:
                    continue
    return boxes


def handle_click_text(step: ActionStep) -> Any:
    params = step.params or {}
    query = params.get("query")
    if not query or not isinstance(query, str):
        return "error: 'query' param is required"

    boxes = _coerce_boxes(params.get("boxes"))
    if not boxes:
        return "error: no OCR boxes provided"

    match = locate_text(query, boxes)
    if not match:
        return f"error: text '{query}' not found"

    best_box, center = match
    click_params = {
        "x": center[0],
        "y": center[1],
        "button": params.get("button", "left"),
    }
    click_step = ActionStep(action="click", params=click_params)
    click_result = handle_click(click_step)

    box_payload = (
        best_box.to_dict()
        if isinstance(best_box, OcrBox)
        else best_box
        if isinstance(best_box, dict)
        else getattr(best_box, "__dict__", {})
    )
    matched_text = (
        best_box.text
        if isinstance(best_box, OcrBox)
        else best_box.get("text")
        if isinstance(best_box, dict)
        else getattr(best_box, "text", None)
    )

    return {
        "matched_text": matched_text,
        "box": box_payload,
        "center": {"x": center[0], "y": center[1]},
        "click_result": click_result,
        "status": "clicked",
    }


def _normalize_target_list(raw: Any) -> List[str]:
    targets: List[str] = []
    if isinstance(raw, str) and raw.strip():
        targets.append(raw.strip())
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                targets.append(item.strip())
    return targets


def _extract_targets(params: Dict[str, Any]) -> List[str]:
    targets: List[str] = []
    primary = params.get("text") or params.get("query") or params.get("label")
    targets.extend(_normalize_target_list(primary))
    targets.extend(_normalize_target_list(params.get("variants") or []))
    deduped: List[str] = []
    seen = set()
    for term in targets:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(term)
    return deduped


def _summarize_browser_extract_params(params: Dict[str, Any]) -> Dict[str, Any]:
    summary = {
        "text": params.get("text"),
        "target": params.get("target"),
        "visual_description": params.get("visual_description"),
        "query": params.get("query"),
        "label": params.get("label"),
        "variants": params.get("variants"),
        "strategy_hint": params.get("strategy_hint"),
        "prefer_top_line": params.get("prefer_top_line"),
        "attempts": params.get("attempts"),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _wait_for_ocr_targets(targets: List[str], attempts: int = 2, delay: float = 0.8) -> Dict[str, Any]:
    """
    Capture + OCR loop to find any of the target strings. Returns success flag and match details.
    """
    logs: List[str] = []
    candidates: List[Dict[str, Any]] = []
    attempts = max(1, min(5, int(attempts or 1)))
    for attempt in range(1, attempts + 1):
        logs.append(f"attempt:{attempt}")
        try:
            screenshot_path = capture_screen()
            logs.append(f"screenshot:{screenshot_path}")
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "reason": f"screenshot failed: {exc}", "log": logs}

        try:
            full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
            logs.append(f"ocr_boxes:{len(boxes)}")
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "reason": f"ocr failed: {exc}", "log": logs}

        best = None
        best_term = None
        for term in targets:
            ranked = rank_text_candidates(term, boxes)
            if ranked:
                candidates.extend(ranked[:5])
            if not ranked:
                continue
            top = ranked[0]
            if not best or top.get("high_enough") and not best.get("high_enough"):
                best = top
                best_term = term
            elif top.get("high_enough") == best.get("high_enough") and top["score"] > best["score"]:
                best = top
                best_term = term
            if top.get("high_enough"):
                break

        if best and (best.get("high_enough") or best.get("medium_enough")):
            center = best.get("center") or {}
            return {
                "success": True,
                "matched_text": best.get("text"),
                "matched_term": best_term,
                "center": {"x": center.get("x"), "y": center.get("y")},
                "bounds": best.get("bounds"),
                "log": logs,
                "candidates": candidates[:10],
                "full_text": full_text,
            }

        if attempt < attempts and delay > 0:
            try:
                time.sleep(delay)
            except Exception:
                pass

    return {
        "success": False,
        "reason": "text_not_found",
        "targets": targets,
        "log": logs,
        "candidates": candidates[:10],
    }


def handle_browser_input(step: ActionStep) -> Any:
    return dispatch_handle_browser_input(step, provider=sys.modules[__name__])


def handle_browser_extract_text(step: ActionStep) -> Any:
    return dispatch_handle_browser_extract_text(step, provider=sys.modules[__name__])


def _build_vlm_read_prompt(target_desc: str, search_query: Optional[str] = None) -> str:
    context_line = (
        f"The page shows search results for query: '{search_query}'."
        if search_query
        else "The page shows search results in a browser."
    )
    return "\n\n".join(
        [
            "You are looking at a screenshot of a browser.",
            context_line,
            f"User Target: '{target_desc}' (e.g., 'first search result title').",
            "Instructions:",
            "1) Focus on the main search results list (below the search box).",
            "2) Ignore browser UI, tabs, address bar, search box, suggestions, ads, and side panels.",
            "3) Identify the FIRST main organic result title and read it EXACTLY as shown.",
            "4) Return ONLY that title text (no URL, no extra words, no quotes, no markdown).",
            "5) Keep the answer under 120 characters.",
        ]
    )


def handle_browser_extract_text(step: ActionStep) -> Any:
    """
    Extract text from browser. Supports two modes:
    1. VLM Direct Read (strategy_hint="vlm_read"): Sends screenshot to VLM to read specific content.
    2. OCR/Locator (default): Finds a label (e.g. "Price") and extracts text near it.
    """
    params = step.params or {}
    params_summary = _summarize_browser_extract_params(params)

    # === [Commit 4.8] 新增 VLM 直读分支 ===
    # 只要 strategy_hint 包含 'vlm' 且是 'read' 意图，就优先尝试直读
    hint = str(params.get("strategy_hint", "")).lower()
    if "vlm_read" in hint or ("vlm" in hint and "read" in hint):
        target_desc = None
        target_desc_source = None
        for key in ("visual_description", "target", "text", "query", "label"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                target_desc = value.strip()
                target_desc_source = key
                break
        debug = {
            "branch": "vlm_read",
            "strategy_hint": hint,
            "params": params_summary,
            "target_desc_source": target_desc_source,
        }
        if not target_desc:
            return {
                "status": "error",
                "reason": "visual_description/target/text required for vlm_read",
                "debug": debug,
            }

        # 1. 截图
        try:
            screenshot_path = capture_screen()
        except Exception as exc:
            return {"status": "error", "reason": f"screenshot failed: {exc}", "debug": debug}
        debug.update({"screenshot_path": str(screenshot_path), "target_desc": target_desc})

        # 2. 构造直读 Prompt (严格约束模式)
        search_query = None
        for key in ("text", "query", "label"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                search_query = value.strip()
                break

        prompt = _build_vlm_read_prompt(target_desc, search_query)
        debug["prompt"] = prompt
        if search_query:
            debug["search_query"] = search_query

        # 3. 调用 VLM (复用现有的 VLM 配置)
        provider_name, provider_call = get_vlm_call()
        if not provider_call:
            debug["vlm_provider"] = provider_name
            return {"status": "error", "reason": "No VLM provider configured (VLM_DISABLED?)", "debug": debug}

        try:
            print(f"[EXECUTOR] VLM Direct Read: {target_desc} using {provider_name}")
            # image_base64 转换（可选裁剪顶部区域以聚焦首条搜索结果）
            prefer_top = bool(params.get("prefer_top_line")) or ("first" in target_desc.lower()) or ("第一" in target_desc)
            image_b64 = _encode_image_base64(Path(screenshot_path))
            crop_info = None
            if prefer_top and image_b64:
                try:
                    with Image.open(screenshot_path) as img:
                        width, height = img.size
                        top = int(height * 0.18)
                        bottom = int(height * 0.55)
                        if bottom - top > 20:
                            cropped = img.crop((0, top, width, bottom))
                            buf = BytesIO()
                            cropped.save(buf, format="PNG")
                            image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                            crop_info = {"top": top, "bottom": bottom, "width": width, "height": height}
                except Exception as exc:  # noqa: BLE001
                    debug["crop_error"] = str(exc)

            debug["vlm_provider"] = provider_name
            debug["image_base64_ok"] = bool(image_b64)
            if crop_info:
                debug["crop"] = crop_info

            # 构造符合接口的消息结构
            messages = [
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
                ]}
            ]

            # 直接调用 provider
            vlm_response = provider_call(prompt, messages)

            # 清理结果 (去掉可能存在的引号或换行)
            cleaned_text = vlm_response.strip().strip('"').strip("'").strip()

            return {
                "status": "success",
                "matched_text": cleaned_text,
                "matched_term": target_desc,
                "method": "vlm_direct_read",
                "log": [f"vlm_provider:{provider_name}", f"prompt:{target_desc}"],
                "debug": debug,
            }

        except Exception as exc:
            return {"status": "error", "reason": f"vlm_read failed: {exc}", "debug": debug}

    # === [旧逻辑] OCR/Locator 分支 (保持不变) ===
    targets = _extract_targets(params)
    debug = {
        "branch": "ocr_locator",
        "strategy_hint": hint,
        "params": params_summary,
        "targets": targets,
    }
    if not targets:
        return {"status": "error", "reason": "text param is required", "debug": debug}

    attempts = params.get("attempts", 2)
    try:
        attempts = int(attempts)
    except Exception:
        attempts = 2
    attempts = max(1, min(5, attempts))
    debug["attempts"] = attempts

    logs: List[str] = []
    last_candidates: List[Dict[str, Any]] = []
    logs.append(f"strategy_hint:{hint}")

    for attempt in range(1, attempts + 1):
        logs.append(f"attempt:{attempt}")
        try:
            screenshot_path = capture_screen()
            logs.append(f"screenshot:{screenshot_path}")
        except Exception as exc:  # noqa: BLE001
            debug["attempt"] = attempt
            return {"status": "error", "reason": f"screenshot failed: {exc}", "debug": debug}

        try:
            full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
            logs.append(f"ocr_boxes:{len(boxes)}")
        except Exception as exc:  # noqa: BLE001
            debug["attempt"] = attempt
            return {"status": "error", "reason": f"ocr failed: {exc}", "debug": debug}

        best = None
        best_term = None
        for term in targets:
            ranked = rank_text_candidates(term, boxes)
            if ranked:
                last_candidates.extend(ranked[:5])
            if not ranked:
                continue
            top = ranked[0]
            if not best or top.get("high_enough") and not best.get("high_enough"):
                best = top
                best_term = term
            elif top.get("high_enough") == best.get("high_enough") and top["score"] > best["score"]:
                best = top
                best_term = term
            if top.get("high_enough"):
                break

        if best and (best.get("high_enough") or best.get("medium_enough")):
            center = best.get("center") or {}
            bounds = best.get("bounds") or {}
            debug["attempt"] = attempt
            result = {
                "status": "ok",
                "method": "ocr_locator",
                "matched_text": best.get("text"),
                "matched_term": best_term,
                "center": {"x": center.get("x"), "y": center.get("y")},
                "bounds": bounds,
                "full_text": full_text,
                "log": logs,
                "debug": debug,
            }
            if last_candidates:
                result["candidates"] = last_candidates[:10]
            return result

        logs.append("no_match_found")

    return {
        "status": "error",
        "reason": "text_not_found",
        "targets": targets,
        "log": logs,
        "candidates": last_candidates,
        "debug": debug,
    }


def _clamp_point(x: float, y: float, width: int, height: int) -> Tuple[float, float]:
    return max(0.0, min(x, float(width - 1))), max(0.0, min(y, float(height - 1)))


def _encode_image_base64(path: Path) -> Optional[str]:
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:
        return None


def _icon_templates_from_params(params: Dict[str, Any]) -> Dict[str, str]:
    templates: Dict[str, str] = {}
    icon = params.get("target_icon")
    if isinstance(icon, str):
        templates["target_icon"] = icon
    elif isinstance(icon, dict):
        for k, v in icon.items():
            if isinstance(v, str):
                templates[str(k)] = v
    elif isinstance(icon, (list, tuple)):
        for idx, v in enumerate(icon):
            if isinstance(v, str):
                templates[f"icon_{idx}"] = v
    return templates


def _use_vlm(params: Dict[str, Any]) -> bool:
    if VLM_DISABLED.get():
        return False
    hint = str(params.get("strategy_hint", "")).lower()
    if hint in {"vlm", "vision", "image", "color", "icon"}:
        return True
    return bool(params.get("visual_description"))


def _reject_window_match(locate_result: Dict[str, Any], query: str) -> None:
    """
    Prevent accidental interactions on entire windows when UIA matches titles.
    """
    if not locate_result or not isinstance(locate_result, dict) or locate_result.get("method") != "uia":
        return
    candidate = locate_result.get("candidate") or {}
    source = candidate.get("source") or {}
    kind = candidate.get("kind") or source.get("kind")
    if kind == "window":
        raise ValueError(f"Refusing window match for interaction: '{query}' (kind=window)")


def _preferred_window_hint() -> Dict[str, Any]:
    """Return preferred window metadata from the current context, if any."""
    snapshot = _get_active_window_snapshot() or {}
    if not isinstance(snapshot, dict):
        return {}
    hwnd = snapshot.get("hwnd") or snapshot.get("handle")
    try:
        hwnd = int(hwnd) if hwnd is not None else None
    except Exception:
        hwnd = None
    return {
        "preferred_hwnd": hwnd,
        "preferred_pid": snapshot.get("pid"),
        "preferred_title": snapshot.get("title"),
        "preferred_class": snapshot.get("class") or snapshot.get("class_name"),
    }


def _locate_from_params(
    query: str,
    params: Dict[str, Any],
    boxes: List[OcrBox],
    screenshot_path: Path,
    match_policy: MatchPolicy = MatchPolicy.HYBRID,
) -> Dict[str, Any]:
    icon_templates = _icon_templates_from_params(params)
    image_b64 = _encode_image_base64(screenshot_path) if (icon_templates or _use_vlm(params)) else None
    provider_name, provider_call = get_vlm_call()
    vlm_call = provider_call if _use_vlm(params) else None
    preferred = _preferred_window_hint()
    result = locate_target(
        query=query,
        boxes=boxes,
        image_path=str(screenshot_path),
        image_base64=image_b64,
        icon_templates=icon_templates if icon_templates else None,
        vlm_call=vlm_call,
        vlm_provider=provider_name,
        match_policy=match_policy,
        preferred_hwnd=preferred.get("preferred_hwnd"),
        preferred_pid=preferred.get("preferred_pid"),
        preferred_title=preferred.get("preferred_title"),
    )
    if match_policy == MatchPolicy.CONTROL_ONLY:
        _reject_window_match(result, query)
    return result


def _extract_target_ref_from_locator(locate_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize TargetRef from locator output (candidate/source/top-level)."""
    if not isinstance(locate_result, dict):
        return None
    candidate = locate_result.get("candidate") or {}
    if not isinstance(candidate, dict):
        candidate = {}
    source = candidate.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    target_ref = locate_result.get("target_ref") or candidate.get("target_ref") or source.get("target_ref")
    runtime_id = locate_result.get("runtime_id") or candidate.get("runtime_id") or source.get("runtime_id")
    locator_key = locate_result.get("locator_key") or candidate.get("locator_key") or source.get("locator_key")
    if target_ref and isinstance(target_ref, dict):
        if runtime_id and "runtime_id" not in target_ref:
            target_ref = {**target_ref, "runtime_id": runtime_id}
        if locator_key and "locator_key" not in target_ref:
            target_ref = {**target_ref, "locator_key": locator_key}
        return target_ref
    if runtime_id or locator_key:
        return {"runtime_id": runtime_id, "locator_key": locator_key}
    return None


def _rebind_with_meta(target_ref: Optional[Dict[str, Any]]) -> Tuple[Optional[Any], Dict[str, bool]]:
    """Best-effort rebind with meta describing inputs used."""
    meta = {"used_runtime_id": False, "used_locator_key": False, "success": False}
    if not target_ref:
        return None, meta
    meta["used_runtime_id"] = bool(target_ref.get("runtime_id"))
    meta["used_locator_key"] = bool(target_ref.get("locator_key"))
    element = None
    try:
        element = rebind_element(target_ref, root=None)
    except Exception:
        element = None
    meta["success"] = element is not None
    return element, meta


def _extract_center_from_locator(locate_result: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Return a normalized center dict from locator result."""
    if not isinstance(locate_result, dict):
        return None
    center = locate_result.get("center") or {}
    if center and center.get("x") is not None and center.get("y") is not None:
        try:
            return {"x": float(center.get("x")), "y": float(center.get("y"))}
        except Exception:
            pass
    candidate = locate_result.get("candidate") or {}
    if not isinstance(candidate, dict):
        candidate = {}
    candidate_center = candidate.get("center") or {}
    if candidate_center and candidate_center.get("x") is not None and candidate_center.get("y") is not None:
        try:
            return {"x": float(candidate_center.get("x")), "y": float(candidate_center.get("y"))}
        except Exception:
            pass
    bounds = locate_result.get("bounds") or candidate.get("bounds") or {}
    if bounds and all(k in bounds for k in ("x", "y", "width", "height")):
        try:
            return {
                "x": float(bounds["x"]) + float(bounds["width"]) / 2.0,
                "y": float(bounds["y"]) + float(bounds["height"]) / 2.0,
            }
        except Exception:
            pass
    source = candidate.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    source_center = source.get("center") if isinstance(source, dict) else None
    if source_center and source_center.get("x") is not None and source_center.get("y") is not None:
        try:
            return {"x": float(source_center.get("x")), "y": float(source_center.get("y"))}
        except Exception:
            pass
    source_bbox = source.get("bbox") if isinstance(source, dict) else None
    if source_bbox and all(k in source_bbox for k in ("x", "y", "width", "height")):
        try:
            return {
                "x": float(source_bbox["x"]) + float(source_bbox["width"]) / 2.0,
                "y": float(source_bbox["y"]) + float(source_bbox["height"]) / 2.0,
            }
        except Exception:
            pass
    return None


def _validate_locator_center(center: Optional[Dict[str, float]], locate_result: Dict[str, Any]) -> Tuple[bool, str]:
    """Validate click center to avoid defaulting to unsafe coordinates."""
    if not center or center.get("x") is None or center.get("y") is None:
        return False, "locate_missing_center"
    try:
        x = float(center.get("x"))
        y = float(center.get("y"))
    except Exception:
        return False, "invalid_center"

    bounds = locate_result.get("bounds") or (locate_result.get("candidate") or {}).get("bounds") or {}
    if bounds:
        try:
            width = float(bounds.get("width", 0))
            height = float(bounds.get("height", 0))
            if width <= 0 or height <= 0:
                return False, "invalid_bounds"
        except Exception:
            pass
    if x == 0 and y == 0 and not bounds:
        return False, "suspicious_origin_center"
    try:
        import pyautogui  # type: ignore

        screen_w, screen_h = pyautogui.size()
        if x < 0 or y < 0 or x >= screen_w or y >= screen_h:
            return False, "center_out_of_bounds"
    except Exception:
        # If pyautogui is unavailable, rely on downstream mouse validation.
        pass
    return True, ""


def _extract_control_type(locate_result: Dict[str, Any]) -> str:
    """Return control_type string for heuristic pattern selection."""
    if not isinstance(locate_result, dict):
        return ""
    candidate = locate_result.get("candidate") or {}
    if not isinstance(candidate, dict):
        candidate = {}
    source = candidate.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    control_type = (
        candidate.get("control_type")
        or source.get("control_type")
        or source.get("ControlTypeName")
        or candidate.get("ControlTypeName")
    )
    return str(control_type or "").strip()


def _click_pattern_sequence(control_type: str) -> List[Tuple[str, Callable]]:
    """Return ordered UIA pattern attempts for click-like actions."""
    ctype = (control_type or "").lower()
    if ctype in {"checkboxcontrol", "checkbox", "switch"}:
        return [("TogglePattern", try_toggle)]
    if ctype in {"tabitemcontrol", "tabitem", "listitemcontrol", "listitem", "treeitemcontrol", "treeitem"}:
        return [("SelectionItemPattern", try_select), ("InvokePattern", try_invoke)]
    if ctype in {"hyperlinkcontrol", "hyperlink", "buttoncontrol", "button"}:
        return [("InvokePattern", try_invoke)]
    return [("InvokePattern", try_invoke), ("SelectionItemPattern", try_select), ("TogglePattern", try_toggle)]


class InteractionStrategyError(RuntimeError):
    """Raised when all interaction strategies fail."""

    def __init__(
        self,
        reason: str,
        rebind_meta: Dict[str, bool],
        target_ref: Optional[Dict[str, Any]],
        locator: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(reason)
        self.rebind_meta = rebind_meta
        self.target_ref = target_ref
        self.locator = locator


def _execute_click_strategies(locate_result: Dict[str, Any], button: str = "left") -> Dict[str, Any]:
    """
    Deterministic click execution with UIA patterns first, then focus+click.
    """
    reasons: List[str] = []
    target_ref = _extract_target_ref_from_locator(locate_result)
    element, rebind_meta = _rebind_with_meta(target_ref)
    control_type = _extract_control_type(locate_result)
    center = _extract_center_from_locator(locate_result)
    pattern_used: Optional[str] = None

    if element:
        for pattern_name, func in _click_pattern_sequence(control_type):
            ok, detail = func(element)
            if ok:
                message = {
                    "ok": True,
                    "status": "invoked",
                    "method": "uia_pattern",
                    "pattern": pattern_name,
                    "rebind": rebind_meta,
                    "reason": detail,
                }
                return {
                    "status": "success",
                    "method": "uia_pattern",
                    "pattern": pattern_name,
                    "locator": locate_result,
                    "target_ref": target_ref,
                    "message": message,
                    "reason": detail,
                    "center": center,
                }
            reasons.append(f"{pattern_name}:{detail}")
    elif target_ref:
        reasons.append("rebind_failed")

    focus_detail: Optional[str] = None
    if element:
        focus_ok, focus_detail = try_focus(element)
        if focus_ok:
            reasons.append("focus:ok")
        elif focus_detail:
            reasons.append(f"focus:{focus_detail}")
    elif target_ref:
        focus_detail = "rebind_missing_for_focus"
        reasons.append(f"focus:{focus_detail}")

    if not center:
        raise InteractionStrategyError(f"click_failed:no_center; reasons={'|'.join(reasons)}", rebind_meta, target_ref, locate_result)
    valid_center, center_reason = _validate_locator_center(center, locate_result)
    if not valid_center:
        raise InteractionStrategyError(f"click_failed:{center_reason}", rebind_meta, target_ref, locate_result)

    click_reason = MOUSE.click({"x": center["x"], "y": center["y"], "button": button})
    click_status = "success"
    if isinstance(click_reason, str) and click_reason.lower().startswith("error"):
        click_status = "error"
        reasons.append(click_reason)
        raise InteractionStrategyError(f"click_failed:{'|'.join(reasons)}", rebind_meta, target_ref, locate_result)
    method_label = "focus_then_click" if (locate_result.get("method") == "uia") else (
        "ocr_then_click" if locate_result.get("method") == "ocr" else "focus_then_click"
    )
    message = {
        "ok": True,
        "status": click_status,
        "method": method_label,
        "pattern": pattern_used,
        "rebind": rebind_meta,
        "reason": click_reason,
    }
    return {
        "status": "success",
        "method": method_label,
        "locator": locate_result,
        "center": center,
        "target_ref": target_ref,
        "reason": click_reason,
        "message": message,
    }


def _set_clipboard_text(text: str) -> Tuple[bool, str]:
    """Set clipboard content using available mechanisms without new deps."""
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        return True, "pyperclip"
    except Exception as exc:  # noqa: BLE001
        last_error = f"pyperclip:{exc}"
    else:  # pragma: no cover
        last_error = "unknown_clipboard_error"
    try:
        import tkinter  # type: ignore

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True, "tkinter"
    except Exception as exc:  # noqa: BLE001
        last_error = f"tkinter:{exc}"
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value @'{text}'@"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                return True, "powershell"
            last_error = f"powershell:exit:{proc.returncode}:{proc.stderr.strip()}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"powershell:{exc}"
    return False, last_error or "clipboard_unavailable"


def _type_with_strategies(
    value: str, locate_result: Dict[str, Any], button: str = "left", auto_enter: bool = True
) -> Dict[str, Any]:
    """
    Deterministic typing: ValuePattern, clipboard paste, then keyboard typing.
    """
    reasons: List[str] = []
    target_ref = _extract_target_ref_from_locator(locate_result)
    element, rebind_meta = _rebind_with_meta(target_ref)
    center = _extract_center_from_locator(locate_result)
    if center:
        valid_center, center_reason = _validate_locator_center(center, locate_result)
        if not valid_center:
            raise InteractionStrategyError(center_reason, rebind_meta, target_ref, locate_result)

    if element:
        ok, detail = try_set_value(element, value)
        if ok:
            message = {
                "ok": True,
                "status": "set_value",
                "method": "uia_value",
                "pattern": "ValuePattern",
                "rebind": rebind_meta,
                "reason": detail,
            }
            return {
                "status": "success",
                "method": "uia_value",
                "locator": locate_result,
                "target_ref": target_ref,
                "message": message,
                "center": center,
            }
        reasons.append(f"ValuePattern:{detail}")
    elif target_ref:
        reasons.append("rebind_failed")

    focus_ok = False
    focus_reason: Optional[str] = None
    if element:
        focus_ok, focus_reason = try_focus(element)
    elif center:
        click_reason = MOUSE.click({"x": center["x"], "y": center["y"], "button": button})
        focus_reason = click_reason if isinstance(click_reason, str) else str(click_reason)
        focus_ok = not (isinstance(click_reason, str) and click_reason.lower().startswith("error"))
    if focus_reason and not focus_ok:
        reasons.append(f"focus:{focus_reason}")

    if focus_ok and (element is not None or target_ref):
        clipboard_ok, clipboard_detail = _set_clipboard_text(value)
        if clipboard_ok:
            try:
                paste_reason = input.key_press({"keys": ["ctrl", "v"], "post_delay": 0.0})
            except Exception as exc:  # noqa: BLE001
                paste_reason = f"error:paste_failed:{exc}"
            if isinstance(paste_reason, str) and paste_reason.lower().startswith("error"):
                reasons.append(f"paste:{paste_reason}")
            else:
                message = {
                    "ok": True,
                    "status": "pasted",
                    "method": "clipboard_paste",
                    "pattern": None,
                    "rebind": rebind_meta,
                    "reason": paste_reason,
                }
                return {
                    "status": "success",
                    "method": "clipboard_paste",
                    "locator": locate_result,
                    "target_ref": target_ref,
                    "message": message,
                    "center": center,
                }
        else:
            reasons.append(f"clipboard:{clipboard_detail}")

    type_reason = input.type_text({"text": value, "auto_enter": auto_enter})
    if isinstance(type_reason, str) and type_reason.lower().startswith("error"):
        reasons.append(type_reason)
        raise InteractionStrategyError(f"type_failed:{'|'.join(reasons)}", rebind_meta, target_ref, locate_result)
    message = {
        "ok": True,
        "status": "typed",
        "method": "keyboard_type",
        "pattern": None,
        "rebind": rebind_meta,
        "reason": type_reason,
    }
    return {
        "status": "success",
        "method": "keyboard_type",
        "locator": locate_result,
        "target_ref": target_ref,
        "message": message,
        "center": center,
    }


def _run_region_ocr(image_path: Path, bounds: Dict[str, float], padding: int = 40) -> List[OcrBox]:
    """
    Run OCR on a padded region around the given bounds and return boxes in global coordinates.
    """
    boxes: List[OcrBox] = []
    with Image.open(image_path) as img:
        width, height = img.size
        left = max(0, int(bounds["x"] - padding))
        top = max(0, int(bounds["y"] - padding))
        right = min(width, int(bounds["x"] + bounds["width"] + padding))
        bottom = min(height, int(bounds["y"] + bounds["height"] + padding))
        region = img.crop((left, top, right, bottom))
        data = pytesseract.image_to_data(region, output_type=pytesseract.Output.DICT)
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = float(data.get("conf", [])[i])
            except Exception:
                conf = -1.0
            try:
                box = OcrBox(
                    text=text,
                    x=int(data.get("left", [])[i]) + left,
                    y=int(data.get("top", [])[i]) + top,
                    width=int(data.get("width", [])[i]),
                    height=int(data.get("height", [])[i]),
                    conf=conf,
                )
                boxes.append(box)
            except Exception:
                continue
    return boxes


def _select_best_candidate(target: str, boxes: List[OcrBox], logs: List[str]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    candidates = rank_text_candidates(target, boxes)
    summary = [
        {"text": c["text"], "score": round(c["score"], 3), "match": c["match_type"]} for c in candidates[:5]
    ]
    logs.append(f"candidates:{summary}")
    best: Optional[Dict[str, Any]] = candidates[0] if candidates else None
    # Prioritize by tiers: exact > high fuzzy > medium fuzzy.
    for cand in candidates:
        if cand["match_type"] == "exact":
            best = cand
            break
    if not best:
        for cand in candidates:
            if cand["score"] >= 0.9:
                best = cand
                break
    if not best:
        for cand in candidates:
            if cand["score"] >= 0.75:
                best = cand
                break
    if best and not best.get("high_enough") and not best.get("medium_enough"):
        logs.append(
            f"best_below_threshold:score:{round(best.get('score', 0.0),3)} match:{best.get('match_type')}"
        )
        best = None
    return best, candidates


def _maximize_active_window(logs: List[str]) -> None:
    try:
        import pygetwindow as gw  # type: ignore

        win = gw.getActiveWindow()
        if win:
            try:
                win.maximize()
                logs.append("active_window_maximized")
                return
            except Exception as exc:  # noqa: BLE001
                logs.append(f"active_window_maximize:error:{exc}")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"active_window_fetch:error:{exc}")

    # Fallback hotkey maximize (Win+Up on Windows).
    try:
        import pyautogui  # type: ignore

        pyautogui.hotkey("win", "up")
        logs.append("active_window_maximized_fallback")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"active_window_maximize_fallback:error:{exc}")


async def click_text(target: str) -> dict:
    """
    Capture the screen, OCR it (with regional refinement), locate target text, and click its center.

    Returns a structured dict with success flag, reason, selected box, candidates, retries, and logs.
    """
    logs: List[str] = []
    screenshot_paths: List[str] = []
    retries = 0
    if not target or not isinstance(target, str) or not target.strip():
        return {"success": False, "reason": "target is required", "chosen_box": None, "candidates": [], "retries": retries, "screenshot_paths": screenshot_paths, "log": logs}

    target_norm = target.strip()
    max_attempts = 3
    scroll_attempts = 2
    scroll_pixels = 240
    chosen = None
    all_candidates: List[Dict[str, Any]] = []

    prev_failsafe = None
    try:
        import pyautogui  # type: ignore

        prev_failsafe = getattr(pyautogui, "FAILSAFE", None)
        pyautogui.FAILSAFE = False
    except Exception:
        pyautogui = None  # type: ignore

    try:
        for attempt in range(1, max_attempts + 1):
            retries = attempt - 1
            logs.append(f"attempt:{attempt}")
            try:
                screenshot_path = capture_screen()
                screenshot_paths.append(str(screenshot_path))
            except Exception as exc:  # noqa: BLE001
                return {
                    "success": False,
                    "reason": f"screenshot failed: {exc}",
                    "chosen_box": None,
                    "candidates": all_candidates,
                    "retries": retries,
                    "screenshot_paths": screenshot_paths,
                    "log": logs,
                }

            try:
                _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
                logs.append(f"ocr_boxes:{len(boxes)}")
            except Exception as exc:  # noqa: BLE001
                return {
                    "success": False,
                    "reason": f"ocr failed: {exc}",
                    "chosen_box": None,
                    "candidates": all_candidates,
                    "retries": retries,
                    "screenshot_paths": screenshot_paths,
                    "log": logs,
                }

            best, ranked = _select_best_candidate(target_norm, boxes, logs)
            all_candidates.extend(ranked)

            # Region-focused refinement around the top candidates when confidence is low.
            if (not best or best["score"] < 0.9) and ranked:
                top_regions = ranked[:3]
                refined_boxes: List[OcrBox] = []
                for cand in top_regions:
                    refined_boxes.extend(_run_region_ocr(Path(screenshot_path), cand["bounds"], padding=60))
                if refined_boxes:
                    best_refined, ranked_refined = _select_best_candidate(target_norm, boxes + refined_boxes, logs)
                    all_candidates.extend(ranked_refined)
                    if best_refined:
                        best = best_refined

            if best:
                chosen = best
                bounds = best["bounds"]
                with Image.open(screenshot_path) as img:
                    width, height = img.size
                cx, cy = _clamp_point(bounds["x"] + bounds["width"] / 2.0, bounds["y"] + bounds["height"] / 2.0, width, height)
                logs.append(f"selected:{best['text']} score:{round(best['score'],3)} match:{best['match_type']} center:({cx},{cy})")
                try:
                    if pyautogui:
                        pyautogui.moveTo(cx, cy)
                        logs.append("mouse_move:done")
                except Exception as exc:  # noqa: BLE001
                    logs.append(f"mouse_move:error:{exc}")
                click_result = mouse.click({"x": cx, "y": cy, "button": "left"})
                if isinstance(click_result, str) and click_result.startswith("error"):
                    return {
                        "success": False,
                        "reason": click_result,
                        "chosen_box": best,
                        "candidates": all_candidates,
                        "retries": retries,
                        "screenshot_paths": screenshot_paths,
                        "log": logs,
                    }
                return {
                    "success": True,
                    "reason": click_result if isinstance(click_result, str) else "clicked",
                    "chosen_box": best,
                    "candidates": all_candidates,
                    "retries": retries,
                    "screenshot_paths": screenshot_paths,
                    "log": logs,
                }

            # Auto-scroll and retry when allowed.
            if scroll_attempts > 0 and pyautogui:
                try:
                    pyautogui.scroll(-scroll_pixels)
                    scroll_attempts -= 1
                    logs.append(f"auto_scroll:-{scroll_pixels}")
                    time.sleep(0.4)
                    continue
                except Exception as exc:  # noqa: BLE001
                    logs.append(f"auto_scroll:error:{exc}")
                    scroll_attempts = 0

        return {
            "success": False,
            "reason": "text_not_found",
            "chosen_box": None,
            "candidates": all_candidates,
            "retries": retries,
            "screenshot_paths": screenshot_paths,
            "log": logs,
        }
    finally:
        try:
            if pyautogui and prev_failsafe is not None:
                pyautogui.FAILSAFE = prev_failsafe
        except Exception:
            pass


def _safe_filename_from_query(query: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in query.strip()) or "image"
    return cleaned.strip("_") or "image"


async def demo_search_image_and_save(query: str, target_folder: str) -> dict:
    """
    Demo helper to search an image in Edge and save it to a target folder.
    Uses only generic actions (open_app, activate_window, hotkey, type_text, key_press,
    wait, click_text, right_click) plus OCR and screenshot utilities.
    Not exposed to the planner; intended for manual/demo use.
    """
    logs: List[str] = []
    saved_path: Optional[str] = None
    target_folder = str(target_folder or "").strip()
    if not query or not isinstance(query, str):
        return {"success": False, "reason": "query_required", "saved_path": None, "log": logs}
    if not target_folder:
        return {"success": False, "reason": "target_folder_required", "saved_path": None, "log": logs}

    # Ensure folder exists.
    try:
        os.makedirs(target_folder, exist_ok=True)
        logs.append(f"ensure_dir:{target_folder}")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"ensure_dir_failed:{exc}", "saved_path": None, "log": logs}

    # 1) Launch/foreground Chrome.
    try:
        open_result = handle_open_app(ActionStep(action="open_app", params={"target": "chrome"}))
        logs.append(f"open_chrome:{open_result}")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"open_chrome:error:{exc}")
    try:
        activate_result = activate_window({"title_keywords": ["chrome", "google chrome"]})
        logs.append(f"activate_chrome:{activate_result}")
        if isinstance(activate_result, dict) and not activate_result.get("success"):
            logs.append("activate_chrome:failed")
        _maximize_active_window(logs)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"activate_chrome:error:{exc}")

    # 2) Focus address bar, type query, hit enter.
    try:
        hotkey_result = handle_hotkey(ActionStep(action="hotkey", params={"keys": ["ctrl", "l"]}))
        logs.append(f"hotkey_ctrl_l:{hotkey_result}")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"hotkey_ctrl_l:error:{exc}")
    try:
        type_result = input.type_text({"text": query})
        logs.append(f"type_query:{type_result}")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"type_query_failed:{exc}", "saved_path": None, "log": logs}
    try:
        enter_result = input.key_press({"keys": ["enter"]})
        logs.append(f"press_enter:{enter_result}")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"press_enter:error:{exc}")
    time.sleep(3)
    logs.append("wait_after_search:3s")
    try:
        recheck_activate = activate_window({"title_keywords": ["chrome", "google chrome"]})
        logs.append(f"recheck_activate_chrome:{recheck_activate}")
        _maximize_active_window(logs)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"recheck_activate_chrome:error:{exc}")
    time.sleep(0.6)
    logs.append("wait_after_reactivate:0.6s")

    # 2b) Force navigate directly to Bing Images to avoid mis-OCR on the Images tab.
    images_nav_done = False
    images_url = f"https://www.bing.com/images/search?q={quote_plus(query)}"
    # Prefer launching navigation via shell to avoid keyboard focus issues.
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", images_url],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        logs.append("open_url_via_start:ok")
        images_nav_done = True
    except Exception as exc:  # noqa: BLE001
        logs.append(f"open_url_via_start:error:{exc}")

    if not images_nav_done:
        try:
            nav_hotkey = handle_hotkey(ActionStep(action="hotkey", params={"keys": ["ctrl", "l"]}))
            logs.append(f"hotkey_ctrl_l_images:{nav_hotkey}")
            nav_type = input.type_text({"text": images_url})
            logs.append(f"type_images_url:{images_url}:{nav_type}")
            nav_enter = input.key_press({"keys": ["enter"]})
            logs.append(f"confirm_images_url:{nav_enter}")
            images_nav_done = True
        except Exception as exc:  # noqa: BLE001
            logs.append(f"navigate_images_url:error:{exc}")

    time.sleep(3)
    logs.append("wait_after_images_nav:3s")
    try:
        recheck_activate2 = activate_window({"title_keywords": ["chrome", "google chrome"]})
        logs.append(f"recheck_activate_chrome_after_nav:{recheck_activate2}")
        _maximize_active_window(logs)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"recheck_activate_chrome_after_nav:error:{exc}")
    time.sleep(0.6)
    logs.append("wait_after_reactivate_images:0.6s")

    # 3) Ensure we are on the images results page; if the direct nav worked, skip OCR tab click.
    images_label_used = None
    if images_nav_done:
        logs.append("images_tab_skip:direct_nav")
    else:
        for label in ["图片", "Images"]:
            try:
                click_res = await click_text(label)
                logs.append(f"click_images_tab:{label}:{click_res}")
                if click_res.get("success"):
                    images_label_used = label
                    break
            except Exception as exc:  # noqa: BLE001
                logs.append(f"click_images_tab:{label}:error:{exc}")
        if not images_label_used:
            return {"success": False, "reason": "images_tab_not_found", "saved_path": None, "log": logs}
        time.sleep(2)
        logs.append("wait_after_images_tab:2s")

    # 4) Heuristic first-image click point: screen center offset slightly up/left.
    active_rect = None
    try:
        import pygetwindow as gw  # type: ignore

        win = gw.getActiveWindow()
        if win:
            active_rect = (
                getattr(win, "left", 0),
                getattr(win, "top", 0),
                getattr(win, "width", 0),
                getattr(win, "height", 0),
            )
            logs.append(f"active_window_rect:{getattr(win, 'title', '')}:{active_rect}")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"active_window_rect:error:{exc}")

    try:
        screenshot_path = capture_screen()
        logs.append(f"screenshot_for_heuristic:{screenshot_path}")
        with Image.open(screenshot_path) as img:
            width, height = img.size
        if active_rect:
            left, top, w, h = active_rect
            # Aim near the upper-left grid area of the image results.
            click_x = left + w * 0.30
            click_y = top + h * 0.35
            center_x, center_y = left + w / 2.0, top + h / 2.0
            logs.append(f"use_active_center:{center_x},{center_y}")
            logs.append(f"heuristic_from_window:({click_x},{click_y}) window:{active_rect}")
        else:
            center_x, center_y = width / 2.0, height / 2.0
            dx, dy = 220, -140
            click_x, click_y = _clamp_point(center_x - dx, center_y + dy, int(width), int(height))
            logs.append(
                f"heuristic_click_point:({click_x},{click_y}) center:({center_x},{center_y}) offset:({-dx},{dy})"
            )
        rc_result = handle_right_click(ActionStep(action="right_click", params={"x": click_x, "y": click_y}))
        logs.append(f"right_click_result:{rc_result}")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"right_click_failed:{exc}", "saved_path": None, "log": logs}

    def _click_menu_label(label: str) -> dict:
        region_size = 640
        half = region_size / 2.0
        region_bounds = {
            "x": max(0.0, click_x - half),
            "y": max(0.0, click_y - half),
            "width": region_size,
            "height": region_size,
        }
        try:
            screenshot_path_local = capture_screen()
            boxes_local = _run_region_ocr(Path(screenshot_path_local), region_bounds, padding=10)
            logs.append(f"menu_region_boxes:{len(boxes_local)} label:{label} bounds:{region_bounds}")
            best_local, ranked_local = _select_best_candidate(label, boxes_local, logs)
            if best_local:
                bounds_local = best_local["bounds"]
                with Image.open(screenshot_path_local) as img_local:
                    w_local, h_local = img_local.size
                cx_local, cy_local = _clamp_point(
                    bounds_local["x"] + bounds_local["width"] / 2.0,
                    bounds_local["y"] + bounds_local["height"] / 2.0,
                    w_local,
                    h_local,
                )
                click_outcome = mouse.click({"x": cx_local, "y": cy_local, "button": "left"})
                return {
                    "success": not (isinstance(click_outcome, str) and click_outcome.startswith("error")),
                    "reason": click_outcome,
                    "chosen_box": best_local,
                    "candidates": ranked_local,
                }
            return {"success": False, "reason": "menu_label_not_found", "candidates": ranked_local}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "reason": f"menu_click_error:{exc}", "candidates": []}

    # 5) Context menu: choose "Save image as".
    menu_labels = ["图片另存为", "Save image as"]
    menu_clicked = False
    for label in menu_labels:
        try:
            save_click = _click_menu_label(label)
            logs.append(f"click_save_menu:{label}:{save_click}")
            if save_click.get("success"):
                menu_clicked = True
                break
        except Exception as exc:  # noqa: BLE001
            logs.append(f"click_save_menu:{label}:error:{exc}")
    if not menu_clicked:
        # Fallback: use keyboard to choose the first "Save image as" entry in the context menu.
        try:
            kb_try = input.key_press({"keys": ["v"]})
            logs.append(f"fallback_menu_key_v:{kb_try}")
            time.sleep(0.3)
            kb_enter = input.key_press({"keys": ["enter"]})
            logs.append(f"fallback_menu_enter:{kb_enter}")
            menu_clicked = True  # best effort; continue to save dialog
        except Exception as exc:  # noqa: BLE001
            logs.append(f"fallback_menu_key:error:{exc}")
            # Final fallback: down-arrow a few times then enter.
            try:
                _ = input.key_press({"keys": ["down"]})
                _ = input.key_press({"keys": ["down"]})
                _ = input.key_press({"keys": ["enter"]})
                logs.append("fallback_menu_down_enter:sent")
                menu_clicked = True
            except Exception as exc2:  # noqa: BLE001
                logs.append(f"fallback_menu_down_enter:error:{exc2}")
                return {"success": False, "reason": "save_menu_not_found", "saved_path": None, "log": logs}

    # 6) Save dialog: type path and confirm.
    time.sleep(1.2)
    logs.append("wait_before_save:1.2s")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{_safe_filename_from_query(query)}_{timestamp}.png"
    full_path = str(Path(target_folder) / filename)
    try:
        select_name = input.key_press({"keys": ["ctrl", "a"]})
        logs.append(f"select_filename_box:{select_name}")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"select_filename_box:error:{exc}")
    try:
        type_path_res = input.type_text({"text": full_path})
        logs.append(f"type_save_path:{full_path}:{type_path_res}")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"type_save_path_failed:{exc}", "saved_path": None, "log": logs}
    try:
        enter_save = input.key_press({"keys": ["enter"]})
        logs.append(f"confirm_save_enter1:{enter_save}")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"confirm_save_enter1:error:{exc}")
    time.sleep(1.2)
    try:
        enter_save2 = input.key_press({"keys": ["enter"]})
        logs.append(f"confirm_save_enter2:{enter_save2}")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"confirm_save_enter2:error:{exc}")
    time.sleep(2.5)
    logs.append("wait_after_save:2.5s")

    # 7) Optional verification.
    exists = os.path.exists(full_path)
    if not exists:
        try:
            alt_save = input.key_press({"keys": ["alt", "s"]})
            logs.append(f"fallback_alt_s:{alt_save}")
        except Exception as exc:  # noqa: BLE001
            logs.append(f"fallback_alt_s:error:{exc}")
        time.sleep(2.0)
        exists = os.path.exists(full_path)

    if exists:
        saved_path = full_path
        logs.append(f"file_exists:{full_path}")
    else:
        logs.append(f"file_not_found:{full_path}")

    return {
        "success": bool(saved_path),
        "reason": "saved" if saved_path else "save_unconfirmed",
        "saved_path": saved_path,
        "log": logs,
    }


async def search_and_open_contact(contact_name: str) -> dict:
    """
    Open a contact in WeChat by searching and clicking the contact entry.

    Steps:
    1) Click search box ("搜索" or "Search").
    2) Type contact name.
    3) Wait briefly for results.
    4) OCR screen and locate the contact name.
    5) Move mouse and click the contact entry.
    """
    logs: List[str] = []
    if not contact_name or not isinstance(contact_name, str) or not contact_name.strip():
        return {"success": False, "reason": "contact_name is required", "boxes": None, "log": logs}

    # Step 1: click search box (Chinese first, fallback to English).
    search_attempt = await click_text("搜索")
    logs.append(f"click_search_zh:{search_attempt}")
    if not search_attempt.get("success"):
        search_attempt_en = await click_text("Search")
        logs.append(f"click_search_en:{search_attempt_en}")
        if not search_attempt_en.get("success"):
            return {
                "success": False,
                "reason": "search box not found",
                "boxes": None,
                "log": logs,
            }

    # Step 2: type contact name.
    try:
        input.type_text({"text": contact_name})
        logs.append("typed_contact")
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "reason": f"failed to type contact: {exc}",
            "boxes": None,
            "log": logs,
        }

    # Step 3: wait for results.
    time.sleep(0.8)
    logs.append("waited:0.8s")

    # Step 4: OCR the screen.
    try:
        screenshot_path = capture_screen()
        logs.append(f"screenshot:{screenshot_path}")
        _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
        logs.append(f"ocr_boxes:{len(boxes)}")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"ocr failed: {exc}", "boxes": None, "log": logs}

    # Step 5: locate contact name.
    match = locate_text(contact_name.strip(), boxes)
    if not match:
        return {
            "success": False,
            "reason": f"contact '{contact_name}' not found",
            "boxes": [b.to_dict() for b in boxes],
            "log": logs,
        }

    best_box, center = match
    x, y = center
    logs.append(f"match:{best_box} center:({x},{y})")

    # Step 6: click contact entry.
    try:
        import pyautogui  # type: ignore

        pyautogui.moveTo(x, y)
        logs.append("mouse_move:done")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"mouse_move:error:{exc}")

    click_result = mouse.click({"x": x, "y": y, "button": "left"})
    if isinstance(click_result, str) and click_result.startswith("error"):
        return {
            "success": False,
            "reason": click_result,
            "boxes": [b.to_dict() for b in boxes],
            "log": logs,
        }

    return {
        "success": True,
        "reason": click_result if isinstance(click_result, str) else "contact opened",
        "boxes": [b.to_dict() for b in boxes],
        "log": logs,
    }


async def locate_message_input_box() -> dict:
    """
    Capture screen and heuristically locate the message input box using hint text.
    """
    logs: List[str] = []
    hints = ["发送", "Send", "输入", "Aa"]
    try:
        screenshot_path = capture_screen()
        logs.append(f"screenshot:{screenshot_path}")
        _full_text, boxes = run_ocr_with_boxes(str(screenshot_path))
        logs.append(f"ocr_boxes:{len(boxes)}")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"ocr failed: {exc}", "box": None, "log": logs}

    best_match = None
    for hint in hints:
        match = locate_text(hint, boxes)
        logs.append(f"locate_hint:{hint}:{'hit' if match else 'miss'}")
        if match:
            best_match = match
            break

    if not best_match:
        return {"success": False, "reason": "input box not found", "box": None, "log": logs}

    box, center = best_match
    try:
        bx = box.x if isinstance(box, OcrBox) else float(getattr(box, "x", box.get("x", 0)))
        by = box.y if isinstance(box, OcrBox) else float(getattr(box, "y", box.get("y", 0)))
        bw = box.width if isinstance(box, OcrBox) else float(getattr(box, "width", box.get("width", 0)))
        bh = box.height if isinstance(box, OcrBox) else float(getattr(box, "height", box.get("height", 0)))
    except Exception:
        bx = by = bw = bh = 0.0

    # Expand downward to cover the text entry region.
    margin_x = 30
    margin_y = 80
    inferred_box = {
        "x": bx - margin_x,
        "y": by,
        "width": bw + margin_x * 2,
        "height": bh + margin_y,
        "center": {"x": center[0], "y": center[1] + bh / 2 + margin_y / 2},
        "hint": getattr(box, "text", None) if hasattr(box, "text") else getattr(box, "get", lambda k, default=None: None)("text", None),
    }
    logs.append(f"inferred_box:{inferred_box}")

    return {"success": True, "reason": "input box inferred", "box": inferred_box, "log": logs}


async def send_message(message: str) -> dict:
    """
    Locate the message input box, click it, type the message, and press Enter.
    """
    logs: List[str] = []
    if not message or not isinstance(message, str):
        return {"success": False, "reason": "message is required", "box": None, "log": logs}

    locate_result = await locate_message_input_box()
    logs.append(f"locate_input:{locate_result}")
    if not locate_result.get("success"):
        return {"success": False, "reason": locate_result.get("reason"), "box": None, "log": logs}

    box = locate_result.get("box") or {}
    center = box.get("center") or {}
    cx = center.get("x")
    cy = center.get("y")
    if cx is None or cy is None:
        return {"success": False, "reason": "invalid input center", "box": box, "log": logs}

    try:
        import pyautogui  # type: ignore

        pyautogui.moveTo(cx, cy)
        logs.append("mouse_move:done")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"mouse_move:error:{exc}")

    click_result = mouse.click({"x": cx, "y": cy, "button": "left"})
    logs.append(f"click_input:{click_result}")
    if isinstance(click_result, str) and click_result.startswith("error"):
        return {"success": False, "reason": click_result, "box": box, "log": logs}

    try:
        input.type_text({"text": message})
        logs.append("typed_message")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"failed to type message: {exc}", "box": box, "log": logs}

    try:
        input.key_press({"keys": ["enter"]})
        logs.append("pressed_enter")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"failed to press enter: {exc}", "box": box, "log": logs}

    return {"success": True, "reason": "message sent", "box": box, "log": logs}


async def send_wechat_message(contact: str, message: str) -> dict:
    """
    Activate WeChat, open a contact, and send a message.
    """
    logs: List[str] = []
    if not contact or not isinstance(contact, str):
        return {
            "success": False,
            "reason": "contact is required",
            "contact_result": None,
            "message_result": None,
            "log": logs,
        }
    if not message or not isinstance(message, str):
        return {
            "success": False,
            "reason": "message is required",
            "contact_result": None,
            "message_result": None,
            "log": logs,
        }

    # Step 1: activate WeChat window.
    try:
        activation = activate_wechat_window()
        logs.append(f"activate_wechat:{activation}")
        activation_log = activation.get("log") if isinstance(activation, dict) else None
        if isinstance(activation_log, list):
            logs.extend([f"activation_log:{entry}" for entry in activation_log])
        elif activation_log:
            logs.append(f"activation_log:{activation_log}")
        if isinstance(activation, dict) and not activation.get("success"):
            return {
                "success": False,
                "reason": activation.get("reason") or "failed to activate wechat",
                "contact_result": None,
                "message_result": None,
                "log": logs,
            }
    except Exception as exc:  # noqa: BLE001
        logs.append(f"activate_wechat:error:{exc}")
        try:
            activation = handle_switch_window(ActionStep(action="switch_window", params={"title": "wechat"}))
            logs.append(f"activate:{activation}")
            if isinstance(activation, str) and activation.startswith("error"):
                return {
                    "success": False,
                    "reason": activation,
                    "contact_result": None,
                    "message_result": None,
                    "log": logs,
                }
            if isinstance(activation, dict) and activation.get("status") == "error":
                return {
                    "success": False,
                    "reason": activation.get("message") or "failed to activate wechat",
                    "contact_result": None,
                    "message_result": None,
                    "log": logs,
                }
        except Exception as switch_exc:  # noqa: BLE001
            return {
                "success": False,
                "reason": f"activation failed: {switch_exc}",
                "contact_result": None,
                "message_result": None,
                "log": logs,
            }

    # Step 2: open contact.
    contact_result = await search_and_open_contact(contact)
    logs.append(f"contact:{contact_result}")
    if not contact_result.get("success"):
        return {
            "success": False,
            "reason": contact_result.get("reason") or "failed to open contact",
            "contact_result": contact_result,
            "message_result": None,
            "log": logs,
        }

    # Step 3: send message.
    message_result = await send_message(message)
    logs.append(f"message:{message_result}")
    if not message_result.get("success"):
        return {
            "success": False,
            "reason": message_result.get("reason") or "failed to send message",
            "contact_result": contact_result,
            "message_result": message_result,
            "log": logs,
        }

    return {
        "success": True,
        "reason": "message sent",
        "contact_result": contact_result,
        "message_result": message_result,
        "log": logs,
    }


def _pick_best_wechat_window(candidates: List[_WinSnapshot]) -> Optional[_WinSnapshot]:
    best: Optional[_WinSnapshot] = None
    best_score: Optional[float] = None
    blocked_classes = {"applicationframewindow", "applicationframeinputsinkwindow"}
    preferred_classes = {"wechatmainwndforpc", "chrome_widgetwin_0"}
    for snap in candidates:
        left, top, right, bottom = snap.rect
        width = max(0, right - left)
        height = max(0, bottom - top)
        if width > 2000 or height > 2000:
            continue
        if snap.is_cloaked or snap.has_owner:
            continue
        title = snap.title or ""
        class_name = snap.class_name or ""
        class_l = class_name.lower()
        title_l = title.lower()
        if class_l in blocked_classes:
            continue
        # Reject if fully hidden and minimized (likely non-interactive host container).
        if not snap.is_visible and snap.is_minimized:
            continue
        base_class_score = 0
        if class_name in _WECHAT_CLASS_NAMES:
            base_class_score = 3
        elif class_l == "chrome_widgetwin_0":
            base_class_score = 2
        elif "wechat" in class_l:
            base_class_score = 1
        title_score = 1.0 if ("wechat" in title_l or "微信" in title_l) else 0.0
        preferred_flag = 1 if class_l in preferred_classes else 0
        area = width * height
        score = (
            preferred_flag * 1_000_000_000
            + base_class_score * 10_000_000
            + (1 if snap.is_visible else 0) * 5_000_000
            + area
            + title_score * 10_000
        )
        if best_score is None or score > best_score:
            best_score = score
            best = snap
    return best


def _foreground_snapshot() -> Dict[str, Any]:
    """Collect foreground window details for diagnostics."""
    info: Dict[str, Any] = {
        "hwnd": None,
        "title": "",
        "class_name": "",
        "pid": None,
        "is_wechat_ui": False,
    }
    try:
        hwnd = user32.GetForegroundWindow()
        info["hwnd"] = int(hwnd)
        info["title"] = _get_window_title(hwnd)
        info["class_name"] = _get_class_name(hwnd)
        pid_out = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid_out))
        info["pid"] = int(pid_out.value)
        class_l = info["class_name"].lower()
        title_l = info["title"].lower()
        info["is_wechat_ui"] = bool(
            "wechat" in class_l or "wechat" in title_l or "微信" in info["title"]
        )
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return info


def _collect_wechat_candidates_debug(relax_hidden: bool, extra_terms: Optional[List[str]] = None) -> List[_WinSnapshot]:
    """Enumerate WeChat-like windows with adjustable filters for debugging."""
    snapshots: List[_WinSnapshot] = []
    search_terms = ["wechat", "微信", "weixin"]
    for term in extra_terms or []:
        if term and term not in search_terms:
            search_terms.append(term)
    blocked_classes = {"applicationframewindow", "applicationframeinputsinkwindow"}
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def _callback(hwnd, _lparam):
        title = _get_window_title(hwnd)
        class_name = _get_class_name(hwnd)
        title_l = title.lower()
        class_l = class_name.lower()
        hit = any(term in title_l or term in class_l for term in search_terms)
        if not hit:
            return True
        if class_l in blocked_classes:
            return True
        pid_out = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
        is_visible = bool(user32.IsWindowVisible(hwnd))
        has_owner = bool(user32.GetWindow(hwnd, GW_OWNER))
        if has_owner:
            return True
        is_cloaked = _is_cloaked(hwnd)
        try:
            is_minimized = bool(user32.IsIconic(hwnd))
        except Exception:
            is_minimized = False
        rect = _get_window_rect(hwnd)
        if not relax_hidden and not is_visible and is_minimized:
            return True
        snapshots.append(
            _WinSnapshot(
                int(hwnd),
                title,
                int(pid_out.value),
                is_visible,
                is_cloaked,
                has_owner,
                is_minimized,
                class_name,
                rect,
            )
        )
        return True

    try:
        user32.EnumWindows(_callback, 0)
    except Exception:
        return snapshots
    return snapshots


def _analyze_window_white(rect: Tuple[int, int, int, int]) -> Dict[str, Any]:
    """Heuristic to detect if a window region is mostly white/blank."""
    result: Dict[str, Any] = {"is_white": False}
    try:
        from PIL import Image, ImageStat  # type: ignore
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"pil_missing:{exc}"
        return result

    try:
        screenshot_path = capture_screen()
        with Image.open(Path(screenshot_path)) as im:
            left, top, right, bottom = rect
            width = max(0, right - left)
            height = max(0, bottom - top)
            if width <= 0 or height <= 0:
                result["reason"] = "invalid_rect"
                return result
            # Clamp to image bounds.
            clamped_left = max(0, min(left, im.width))
            clamped_top = max(0, min(top, im.height))
            clamped_right = max(clamped_left + 1, min(right, im.width))
            clamped_bottom = max(clamped_top + 1, min(bottom, im.height))
            crop = im.crop((clamped_left, clamped_top, clamped_right, clamped_bottom))
            gray = crop.convert("L")
            hist = gray.histogram()
            total = sum(hist)
            if total <= 0:
                result["reason"] = "empty_hist"
                return result
            white_pixels = sum(hist[245:])
            white_ratio = white_pixels / float(total)
            stat = ImageStat.Stat(crop)
            mean = sum(stat.mean) / len(stat.mean)
            stddev = max(stat.stddev)
            result.update(
                {
                    "white_ratio": white_ratio,
                    "mean": mean,
                    "stddev": stddev,
                    "reason": "ok",
                    "is_white": bool(
                        white_ratio >= 0.97
                        or (white_ratio >= 0.95 and mean >= 240 and stddev <= 20)
                    ),
                }
            )
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"analysis_error:{exc}"
    return result


def _debug_activation_attempt(
    strategy: str,
    relax_hidden: bool,
    force_foreground: bool,
    extra_terms: Optional[List[str]],
) -> dict:
    """
    Run a configurable activation attempt by enumerating windows, picking the best
    candidate, and foregrounding it with optional force.
    """
    logs: List[str] = [f"strategy:{strategy}", f"relax_hidden:{relax_hidden}", f"force_foreground:{force_foreground}"]
    snapshots = _collect_wechat_candidates_debug(relax_hidden=relax_hidden, extra_terms=extra_terms)
    logs.append(f"candidates:{len(snapshots)}")
    if not snapshots:
        return {"success": False, "reason": "no candidates", "hwnd": None, "log": logs}

    best = _pick_best_wechat_window(snapshots)
    if not best:
        return {"success": False, "reason": "no best candidate", "hwnd": None, "log": logs}

    hwnd = best.hwnd
    pid = best.pid
    logs.append(f"selected_hwnd:{hwnd} pid:{pid} title:{best.title} class:{best.class_name}")

    activated = False
    try:
        if force_foreground:
            activated = _force_foreground_wechat(hwnd, pid)
            logs.append("foreground_logic:force")
        else:
            activated = _activate_hwnd(hwnd, pid)
            logs.append("foreground_logic:standard")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"activation_error:{exc}")

    if activated:
        try:
            global LAST_WINDOW_TITLE
            LAST_WINDOW_TITLE = best.title
        except Exception:
            pass
    return {
        "success": bool(activated),
        "reason": "activated" if activated else "foreground_failed",
        "hwnd": hwnd,
        "pid": pid,
        "log": logs,
    }


def debug_wechat_activation() -> dict:
    """
    Automated self-test-and-repair for WeChat activation with up to 3 attempts.

    Each cycle runs activation, inspects results, captures a screenshot, and applies
    progressively relaxed filters and foregrounding logic when necessary.
    Prints a diagnostic report and returns structured results.
    """
    strategies = [
        {"name": "default_activate", "mode": "activate", "relax_hidden": False, "force_foreground": False, "extra_terms": ["wechat", "微信", "weixin"]},
        {"name": "relaxed_filters_force", "mode": "manual", "relax_hidden": True, "force_foreground": True, "extra_terms": ["wechat", "微信", "weixin"]},
        {"name": "manual_force_broad_terms", "mode": "manual", "relax_hidden": True, "force_foreground": True, "extra_terms": ["wechat", "微信", "weixin", "wx"]},
    ]

    attempts: List[Dict[str, Any]] = []

    for idx, strat in enumerate(strategies, start=1):
        attempt_info: Dict[str, Any] = {"attempt": idx, "strategy": strat["name"]}
        try:
            if strat["mode"] == "activate":
                activation = activate_wechat_window()
            else:
                activation = _debug_activation_attempt(
                    strategy=strat["name"],
                    relax_hidden=strat["relax_hidden"],
                    force_foreground=strat["force_foreground"],
                    extra_terms=strat["extra_terms"],
                )
        except Exception as exc:  # noqa: BLE001
            activation = {"success": False, "reason": f"exception:{exc}", "hwnd": None, "log": []}

        attempt_info["activation"] = activation
        attempt_info["activation_success"] = bool(activation.get("success") if isinstance(activation, dict) else False)
        attempt_info["activation_hwnd"] = activation.get("hwnd") if isinstance(activation, dict) else None
        attempt_info["activation_reason"] = activation.get("reason") if isinstance(activation, dict) else None
        attempt_info["activation_log"] = activation.get("log") if isinstance(activation, dict) else None

        fg_snapshot = _foreground_snapshot()
        attempt_info["foreground"] = fg_snapshot
        attempt_info["wechat_ui_detected"] = bool(fg_snapshot.get("is_wechat_ui"))
        attempt_info["foreground_matches_activation"] = (
            bool(fg_snapshot.get("hwnd")) and fg_snapshot.get("hwnd") == attempt_info["activation_hwnd"]
        )

        try:
            screenshot_path = capture_screen()
            attempt_info["screenshot"] = str(screenshot_path)
        except Exception as exc:  # noqa: BLE001
            attempt_info["screenshot_error"] = f"screenshot failed: {exc}"

        attempts.append(attempt_info)
        if attempt_info["wechat_ui_detected"]:
            break

    final_detected = bool(attempts and attempts[-1].get("wechat_ui_detected"))
    summary = {"attempts": attempts, "final_wechat_ui_foreground": final_detected}

    # Human-readable diagnostic report.
    print("=== WeChat Activation Debug Report ===")
    for att in attempts:
        print(
            f"[Attempt {att['attempt']} - {att['strategy']}] "
            f"activation_success={att.get('activation_success')} "
            f"wechat_ui_detected={att.get('wechat_ui_detected')} "
            f"foreground_matches_activation={att.get('foreground_matches_activation')}"
        )
        fg = att.get("foreground", {}) or {}
        print(
            f"  Foreground hwnd={fg.get('hwnd')} "
            f"title='{fg.get('title')}' class='{fg.get('class_name')}' visible_wechat={fg.get('is_wechat_ui')}"
        )
        if att.get("activation_reason"):
            print(f"  Activation reason: {att.get('activation_reason')}")
        if att.get("screenshot"):
            print(f"  Screenshot: {att.get('screenshot')}")
        if att.get("screenshot_error"):
            print(f"  Screenshot error: {att.get('screenshot_error')}")
    print(f"Final visible WeChat UI: {final_detected}")

    return summary


def activate_wechat_window() -> dict:
    """
    Deprecated WeChat-specific helper kept for compatibility.
    Routes to the generic activate_window using WeChat keywords.
    """
    return activate_window(
        {"title_keywords": ["wechat", "微信", "weixin"], "class_keywords": ["wechat"]}
    )


def _capture_observation(capture_screenshot: bool, capture_ocr: bool) -> Dict[str, Any]:
    """
    Capture an observation snapshot (screenshot + optional OCR) for feedback loops.
    """
    observation: Dict[str, Any] = {"captured": False, "timestamp": now_iso_utc()}
    if not capture_screenshot:
        observation["capture_enabled"] = False
        return observation

    try:
        observation["foreground"] = _foreground_snapshot()
        screenshot_path = capture_screen()
        observation.update({"captured": True, "screenshot_path": str(screenshot_path), "capture_enabled": True})
        if capture_ocr:
            try:
                full_text = run_ocr(str(screenshot_path))
                observation["ocr_excerpt"] = full_text[:OCR_PREVIEW_LIMIT]
                observation["ocr_char_count"] = len(full_text)
            except Exception as exc:  # noqa: BLE001
                observation["ocr_error"] = f"ocr failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        observation["error"] = f"screenshot failed: {exc}"
    return observation


def _normalize_handler_status(message: Any, default_status: str = "success") -> Tuple[str, Optional[str]]:
    status = default_status
    reason = None
    if isinstance(message, dict):
        msg_status = message.get("status")
        if isinstance(msg_status, str):
            status = msg_status.lower()
        reason = message.get("reason") or message.get("message")
    elif isinstance(message, str):
        lowered = message.lower()
        if lowered.startswith("error"):
            status = "error"
        elif lowered.startswith("stub"):
            status = "noop"
        reason = message
    return status, reason


def _map_reason_category(reason: Optional[str]) -> str:
    reason = (reason or "").lower()
    mapping = {
        "foreground_mismatch": "focus_gate",
        "no_target_hint": "focus_gate",
        "needs_consent": "consent_gate",
        "blocked": "consent_gate",
        "path_not_allowed": "file_guardrail",
        "forbidden_path": "file_guardrail",
        "traversal_detected": "file_guardrail",
        "symlink_escape": "file_guardrail",
        "wildcard_blocked": "file_guardrail",
        "overwrite_blocked": "file_guardrail",
        "missing_expected_verify": "verification",
        "verification_failed": "verification",
        "verification_retry": "verification",
        "handler_error": "handler",
        "handler_exception": "handler",
        "timeout": "timeout",
        "dangerous_request": "unsafe_policy",
        "confirm_required": "unsafe_policy",
        "path_outside_workspace": "unsafe_policy",
        "plan_validation_error": "plan_validation_error",
    }
    return mapping.get(reason, "handler" if reason.startswith("handler") else "verification" if reason.startswith("verification") else "unsafe_policy" if "unsafe" in reason else "handler")


def _build_diagnostics_summary(logs: List[dict], overall_status: str) -> Optional[Dict[str, Any]]:
    if not logs:
        return None
    priority = [
        "plan_validation_error",
        "file_guardrail",
        "consent_gate",
        "focus_gate",
        "verification",
        "handler",
        "timeout",
        "unsafe_policy",
    ]

    def pick_entry() -> Optional[dict]:
        for category in priority:
            for log in logs:
                reason = log.get("reason") or (log.get("verification") or {}).get("reason") or (log.get("safety") or {}).get("code")
                mapped = _map_reason_category(reason)
                if mapped == category and (log.get("status") in {"error", "unsafe"} or mapped == "plan_validation_error"):
                    return log
        # fallback: first error/unsafe
        for log in logs:
            if log.get("status") in {"error", "unsafe"}:
                return log
        return None

    entry = pick_entry()
    if not entry:
        return None

    reason = entry.get("reason") or (entry.get("verification") or {}).get("reason") or (entry.get("safety") or {}).get("code")
    category = _map_reason_category(reason)
    attempts = entry.get("attempts") or []
    attempt_count = len(attempts)
    verification = entry.get("verification") or {}
    retry_exhausted = False
    if verification:
        try:
            retry_exhausted = verification.get("decision") != "success" and (verification.get("attempt") or 0) >= (
                verification.get("max_attempts") or verification.get("attempt") or 0
            )
        except Exception:
            retry_exhausted = False
    evidence = entry.get("evidence") or verification.get("evidence") or {}
    highlights = {
        "focus_expected_title": _clip_text((evidence.get("focus_expected") or {}).get("title")),
        "focus_actual_title": _clip_text((evidence.get("focus_actual") or {}).get("title")),
        "risk_level": (evidence.get("risk") or {}).get("level"),
        "file_path": evidence.get("file_check", {}).get("normalized_path") if isinstance(evidence.get("file_check"), dict) else None,
        "browser_url": (evidence.get("actual") or {}).get("url"),
        "browser_title": (evidence.get("actual") or {}).get("title"),
        "text_result": _clip_text(evidence.get("text_result")),
        "verifier_expected": evidence.get("expected"),
        "verifier_actual": evidence.get("actual"),
    }
    return {
        "overall_status": overall_status,
        "primary_failure_category": category,
        "primary_reason_code": reason,
        "failed_step_index": entry.get("step_index"),
        "action": entry.get("action"),
        "attempt_count": attempt_count,
        "retry_exhausted": bool(retry_exhausted),
        "evidence_highlights": highlights,
    }


def _evaluate_file_guardrails(
    step: ActionStep,
    work_dir: Optional[str],
    dry_run: bool,
    allowed_roots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Enforce file path guardrails for mutation and read-only actions.
    """
    action = step.action
    params = step.params or {}
    roots = list(allowed_roots or ALLOWED_ROOTS)
    if work_dir:
        _add_allowed_root(work_dir)
        if work_dir not in roots:
            roots.append(os.path.abspath(work_dir))

    def _decision(allow: bool, reason: str, original: Optional[str], normalized: Optional[str], rule: str) -> Dict[str, Any]:
        return {
            "allow": allow,
            "reason": reason,
            "rule": rule,
            "original_path": original,
            "normalized_path": normalized,
            "allowed_roots": roots,
        }

    mutation_actions = {"write_file", "delete_file", "move_file", "copy_file", "rename_file", "create_folder"}
    read_actions = {"read_file", "open_file", "list_files"}
    relevant_actions = mutation_actions | read_actions
    if action not in relevant_actions:
        return _decision(True, "not_applicable", None, None, "skip")

    # Collect source/target paths.
    primary = params.get("path") or params.get("source")
    destination = params.get("destination") or params.get("destination_dir") or params.get("new_name")

    # Normalize primary
    norm_primary, err_primary, had_traversal = _normalize_path_candidate(primary, work_dir)
    if err_primary:
        return _decision(False, err_primary if err_primary != "normalize_error" else "path_not_allowed", primary, None, err_primary)

    # Wildcard already handled above.
    if had_traversal and norm_primary and not _is_under_any_root(norm_primary, roots):
        return _decision(False, "traversal_detected", primary, norm_primary, "traversal_detected")

    if _is_forbidden_path(norm_primary, roots):
        return _decision(False, "forbidden_path", primary, norm_primary, "forbidden_path")

    # Destination normalization for move/copy/rename.
    norm_dest = None
    if action in {"move_file", "copy_file", "rename_file"} and destination:
        norm_dest, err_dest, had_traversal_dest = _normalize_path_candidate(destination, work_dir)
        if err_dest:
            return _decision(False, err_dest if err_dest != "normalize_error" else "path_not_allowed", destination, None, err_dest)
        if had_traversal_dest and norm_dest and not _is_under_any_root(norm_dest, roots):
            return _decision(False, "traversal_detected", destination, norm_dest, "traversal_detected")
        if _is_forbidden_path(norm_dest, roots):
            return _decision(False, "forbidden_path", destination, norm_dest, "forbidden_path")

    is_mutation = action in mutation_actions

    # Allowed roots enforcement for mutations
    if is_mutation:
        if not _is_under_any_root(norm_primary, roots):
            return _decision(False, "path_not_allowed", primary, norm_primary, "path_not_allowed")
        if norm_dest and not _is_under_any_root(norm_dest, roots):
            return _decision(False, "path_not_allowed", destination, norm_dest, "path_not_allowed")

    else:
        # Read-only policy: allow outside roots unless forbidden
        if _is_forbidden_path(norm_primary, roots):
            return _decision(False, "forbidden_path", primary, norm_primary, "forbidden_path")

    # Symlink/junction escape: resolved path outside roots even if apparent path is under root.
    if is_mutation and norm_primary and not _is_under_any_root(norm_primary, roots):
        return _decision(False, "symlink_escape", primary, norm_primary, "symlink_escape")
    if is_mutation and norm_dest and not _is_under_any_root(norm_dest, roots):
        return _decision(False, "symlink_escape", destination, norm_dest, "symlink_escape")

    # Overwrite guard (runtime only)
    if not dry_run:
        try:
            overwrite_flag = _coerce_bool(params.get("overwrite"), False)
        except Exception:
            overwrite_flag = False
        if action in {"write_file", "rename_file", "move_file", "copy_file"}:
            target_path = norm_dest if action in {"move_file", "copy_file"} else norm_primary
            if target_path and _is_under_any_root(target_path, roots):
                try:
                    if Path(target_path).exists() and not overwrite_flag:
                        return _decision(False, "overwrite_blocked", target_path, target_path, "overwrite_blocked")
                except Exception:
                    # If stat fails, err on side of blocking overwrite.
                    return _decision(False, "overwrite_blocked", target_path, target_path, "overwrite_blocked")

    return _decision(True, "allow", primary, norm_primary, "allow")



def _build_step_feedback_config(step: ActionStep, base_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge base feedback settings with per-step overrides embedded in params.
    """
    params = step.params if isinstance(step.params, dict) else {}
    feedback_overrides = params.get("_feedback") or params.get("feedback") or {}
    if not isinstance(feedback_overrides, dict):
        feedback_overrides = {}

    capture_before = _coerce_bool(
        params.get("capture_before", feedback_overrides.get("capture_before")), base_config["capture_before"]
    )
    capture_after = _coerce_bool(
        params.get("capture_after", feedback_overrides.get("capture_after")), base_config["capture_after"]
    )
    capture_ocr_default = base_config["capture_ocr"]
    if not capture_ocr_default and DEFAULT_UI_OCR and step.action in OCR_CAPTURE_ACTIONS:
        capture_ocr_default = True
    capture_ocr = _coerce_bool(
        params.get("capture_ocr", feedback_overrides.get("capture_ocr")), capture_ocr_default
    )
    capture_ocr = bool(capture_ocr and (capture_before or capture_after))
    run_ocr_after = _coerce_bool(
        params.get("run_ocr_after", feedback_overrides.get("run_ocr_after")), base_config.get("run_ocr_after", capture_ocr)
    )
    # Structural wait_until should never trigger OCR/VLM by default.
    if step.action == "wait_until":
        condition = (params.get("condition") or "").lower()
        if condition in {"window_exists", "process_exists", "foreground_matches", "title_contains"}:
            capture_ocr = False
            run_ocr_after = False
    max_retries = _coerce_nonnegative_int(
        params.get("max_retries", feedback_overrides.get("max_retries", base_config["max_retries"])),
        base_config["max_retries"],
    )
    verify_mode = str(
        params.get("verify_mode", feedback_overrides.get("verify_mode", base_config.get("verify_mode", "auto")))
    ).lower()
    if verify_mode not in {"auto", "never", "always"}:
        verify_mode = "auto"
    allow_vlm = _coerce_bool(
        params.get("allow_vlm", feedback_overrides.get("allow_vlm", base_config.get("allow_vlm", True))),
        base_config.get("allow_vlm", True),
    )

    return {
        "capture_before": capture_before,
        "capture_after": capture_after,
        "capture_ocr": capture_ocr,
        "run_ocr_after": run_ocr_after,
        "max_retries": max_retries,
        "verify_mode": verify_mode,
        "allow_vlm": allow_vlm,
        "max_attempts": 1 + max_retries,
    }


def _summarize_steps_for_prompt(step_results: List[Dict[str, Any]], limit: int = 5) -> str:
    """Condense recent step outcomes for planner prompts."""
    if not step_results:
        return ""
    lines: List[str] = []
    for entry in step_results[-limit:]:
        action = entry.get("action", "unknown")
        status = entry.get("status", "unknown")
        idx = entry.get("step_index", "?")
        msg = entry.get("message")
        reason = None
        if isinstance(msg, dict):
            reason = msg.get("reason") or msg.get("message") or msg.get("status")
        elif isinstance(msg, str):
            reason = msg
        verification = entry.get("verification") or {}
        if isinstance(verification, dict) and verification.get("reason"):
            reason = f"{verification.get('reason')} | {reason}" if reason else verification.get("reason")
        reason = (str(reason or "no details")).strip()
        if len(reason) > 220:
            reason = reason[:217] + "..."
        lines.append(f"[{idx}] {action} -> {status}: {reason}")
    return "\n".join(lines)


def _provider_available(name: str) -> bool:
    name = (name or "").lower()
    if name == "deepseek":
        return bool(os.getenv("DEEPSEEK_API_KEY"))
    if name == "doubao":
        return bool(os.getenv("DOUBAO_API_KEY"))
    if name == "qwen":
        return bool(os.getenv("QWEN_API_KEY"))
    return False


def _call_planner_with_fallback(
    provider: Optional[str], prompt_bundle: "PromptBundle"
) -> Tuple[str, str]:
    mapping = {
        "deepseek": call_deepseek,
        "doubao": call_doubao,
        "qwen": call_qwen,
    }
    normalized = (provider or "deepseek").lower()
    if _provider_available(normalized):
        order = [normalized]
    else:
        order = [normalized, "deepseek", "doubao", "qwen"]
    seen = set()
    last_exc: Exception | None = None
    doubao_can_use_vision = bool(
        os.getenv("DOUBAO_VISION_MODEL") or (os.getenv("DOUBAO_MODEL") and "vision" in os.getenv("DOUBAO_MODEL").lower())
    )
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        if name not in mapping:
            continue
        if not _provider_available(name):
            continue
        try:
            if name == "doubao":
                selected_messages = (
                    prompt_bundle.vision_messages
                    if (prompt_bundle.vision_messages and doubao_can_use_vision)
                    else prompt_bundle.messages
                )
            else:
                selected_messages = prompt_bundle.vision_messages or prompt_bundle.messages
            return name, mapping[name](prompt_bundle.prompt_text, selected_messages)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    raise RuntimeError(f"planner call failed: {last_exc or 'no providers available'}")


def _maybe_capture_replan_image(enable: bool) -> Dict[str, Any]:
    if not enable:
        return {"enabled": False}
    payload: Dict[str, Any] = {"enabled": True}
    try:
        path = capture_screen()
        payload["path"] = str(path)
        payload["image_base64"] = _encode_image_base64(path)
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"screenshot failed: {exc}"
    return payload


def _build_failure_summary(step_entry: Dict[str, Any], replan_count: int, max_replans: int) -> str:
    """
    Produce a concise failure string for planner context.
    """
    action = step_entry.get("action", "unknown")
    status = step_entry.get("status", "error")
    msg = step_entry.get("message")
    reason = None
    if isinstance(msg, dict):
        reason = msg.get("reason") or msg.get("message") or msg.get("status")
    elif isinstance(msg, str):
        reason = msg
    verification = step_entry.get("verification") or {}
    if isinstance(verification, dict) and verification.get("reason"):
        reason = f"{verification.get('reason')} | {reason}" if reason else verification.get("reason")
    reason = (str(reason or "no details")).strip()
    if len(reason) > 240:
        reason = reason[:237] + "..."
    return (
        f"Step '{action}' ended with status '{status}' after retries. "
        f"Reason: {reason}. Replan attempt {replan_count}/{max_replans}."
    )


def _rewrite_save_pattern(
    steps: List[ActionStep], base_dir: Optional[str] = None
) -> Tuple[List[ActionStep], Optional[Dict[str, Any]]]:
    """
    Detect a UI-based save sequence (type content -> ctrl+s -> type filename) and
    replace it with a direct write_file action to avoid IME/shortcut issues.
    """
    rewritten: List[ActionStep] = []
    rewrite_log: Optional[Dict[str, Any]] = None
    idx = 0
    cwd = Path(base_dir).expanduser() if base_dir else Path.cwd()
    if not cwd.exists() or not cwd.is_dir():
        cwd = Path.cwd()

    def _is_ctrl_s(step: ActionStep) -> bool:
        if step.action not in {"key_press", "hotkey"}:
            return False
        keys = step.params.get("keys") or step.params.get("key")
        if isinstance(keys, str):
            keys = [k.strip().lower() for k in keys.split("+") if k]
        elif isinstance(keys, (list, tuple)):
            keys = [str(k).strip().lower() for k in keys if str(k).strip()]
        else:
            return False
        return set(keys) == {"ctrl", "s"} or keys == ["ctrl", "s"]

    while idx < len(steps):
        # Look ahead for pattern: type_text -> ctrl+s -> type_text (filename)
        if (
            idx + 2 < len(steps)
            and steps[idx].action == "type_text"
            and _is_ctrl_s(steps[idx + 1])
            and steps[idx + 2].action == "type_text"
        ):
            content_step = steps[idx]
            filename_step = steps[idx + 2]
            content = (content_step.params or {}).get("text")
            filename = (filename_step.params or {}).get("text")
            if isinstance(content, str) and isinstance(filename, str) and content and filename:
                path = Path(filename)
                if not path.is_absolute():
                    path = cwd / filename
                write_params = {"path": str(path), "content": content}
                try:
                    new_step = ActionStep(action="write_file", params=write_params)
                    rewritten.append(new_step)
                    rewrite_log = {
                        "pattern": "type_text+ctrl+s+type_text",
                        "replacement": "write_file",
                        "path": str(path),
                    }
                    idx += 3
                    continue
                except Exception:
                    # fall back to original steps on validation failure
                    pass
        # Drop preceding notepad open/activate if we already rewrote?
        rewritten.append(steps[idx])
        idx += 1

    # If we rewrote, optionally strip trailing notepad open/activate that became redundant.
    if rewrite_log:
        cleaned: List[ActionStep] = []
        for step in rewritten:
            if step.action in {"open_app", "activate_window"}:
                params = step.params or {}
                target = str(params.get("target") or "").lower()
                title_kw = params.get("title_keywords") or []
                title_kw = [str(t).lower() for t in title_kw] if isinstance(title_kw, list) else []
                if "notepad" in target or "记事本" in target or any("notepad" in t or "记事本" in t for t in title_kw):
                    continue
            cleaned.append(step)
        rewritten = cleaned
    return rewritten, rewrite_log


def _build_execution_summary(
    logs: List[Dict[str, Any]], context, final_status: Optional[str] = None, recovered_failures: int = 0
) -> Dict[str, Any]:
    """
    Aggregate execution metrics for analytics/debugging.
    """
    total_steps = len(logs)
    successes = sum(1 for log in logs if log.get("status") == "success")
    failures = sum(1 for log in logs if log.get("status") in {"error", "unsafe"})
    retries = 0
    ocr_steps = 0
    icon_steps = 0
    vlm_steps = 0
    uia_steps = 0
    capture_steps = 0
    unsafe_steps = sum(1 for log in logs if log.get("status") == "unsafe")
    replan_count = getattr(context, "replan_count", 0) if context else 0
    failure_messages: List[str] = []

    for log in logs:
        attempts = log.get("attempts") or []
        if attempts:
            retries += max(0, len(attempts) - 1)
        feedback = log.get("feedback") or {}
        if feedback.get("capture_ocr") or feedback.get("run_ocr_after"):
            ocr_steps += 1
        params = log.get("params") or {}
        if isinstance(params, dict) and params.get("target_icon"):
            icon_steps += 1
        step_modalities: set = set()
        step_capture = False
        for att in attempts:
            ev = att.get("evidence") or {}
            modality = (ev.get("actual") or {}).get("modality_used") or ev.get("modality_used")
            if modality:
                step_modalities.add(modality)
            if ev.get("before_obs_ref") or ev.get("after_obs_ref"):
                step_capture = True
        if step_modalities:
            if "vlm" in step_modalities:
                vlm_steps += 1
            if "uia" in step_modalities:
                uia_steps += 1
        if step_capture:
            capture_steps += 1
        if log.get("status") in {"error", "unsafe"}:
            reason = log.get("message")
            if isinstance(reason, dict):
                reason = reason.get("reason") or reason.get("message")
            failure_messages.append(str(reason or "unknown"))

    summary_text = (
        f"Final: {final_status or 'unknown'}. Executed {total_steps} steps: {successes} succeeded, {failures} failed/unsafe, "
        f"{retries} retries, {replan_count} replans. Modalities -> UIA:{uia_steps}, OCR:{ocr_steps}, icons:{icon_steps}, VLM:{vlm_steps}, captures:{capture_steps}."
    )

    return {
        "steps": {
            "total": total_steps,
            "success": successes,
            "failed": failures,
            "unsafe": unsafe_steps,
            "retries": retries,
        },
        "modalities": {
            "ocr_steps": ocr_steps,
            "icon_steps": icon_steps,
            "vlm_steps": vlm_steps,
            "uia_steps": uia_steps,
            "capture_steps": capture_steps,
        },
        "failures": failure_messages,
        "summary_text": summary_text,
        "final_status": final_status,
        "recovered_failures": recovered_failures,
        "replans": {
            "count": replan_count,
            "history": getattr(context, "replan_history", []) if context else [],
        },
    }


def _is_path_within_allowed_roots(path: str) -> bool:
    normalized = os.path.abspath(path)
    for root in ALLOWED_ROOTS:
        try:
            common = os.path.commonpath([normalized, root])
        except Exception:
            continue
        if common == root:
            return True
    return False


def _detect_dangerous_request(user_instruction: Optional[str]) -> Optional[str]:
    if not user_instruction:
        return None
    policy = _load_safety_policy()
    keywords = policy.get("danger_keywords", [])
    lowered = user_instruction.lower()
    for term in keywords:
        if term in lowered:
            return term
    return None


_SAFETY_POLICY_CACHE: Dict[str, Any] = {}
_SAFETY_POLICY_MTIME: Optional[float] = None
_SAFETY_POLICY_LOCK = threading.Lock()


def _load_safety_policy() -> Dict[str, Any]:
    """
    Load the safety policy with mtime-based caching for hot-reload.
    Returns the last known-good policy on parse errors.
    """
    global _SAFETY_POLICY_CACHE, _SAFETY_POLICY_MTIME

    path = Path(__file__).resolve().parent.parent / "config" / "safety_policy.yaml"
    if not path.exists():
        with _SAFETY_POLICY_LOCK:
            _SAFETY_POLICY_CACHE = {}
            _SAFETY_POLICY_MTIME = None
        return {}

    try:
        mtime = path.stat().st_mtime
    except Exception as exc:
        print(f"Error loading safety policy: {exc}")
        with _SAFETY_POLICY_LOCK:
            return _SAFETY_POLICY_CACHE or {}
        return {}

    with _SAFETY_POLICY_LOCK:
        if _SAFETY_POLICY_MTIME is not None and mtime == _SAFETY_POLICY_MTIME:
            return _SAFETY_POLICY_CACHE

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            data = {}
        with _SAFETY_POLICY_LOCK:
            _SAFETY_POLICY_CACHE = data
            _SAFETY_POLICY_MTIME = mtime
        return data
    except Exception as exc:
        print(f"Error loading safety policy: {exc}")
        with _SAFETY_POLICY_LOCK:
            return _SAFETY_POLICY_CACHE or {}


def _normalize_process_name(name: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Normalize a process identifier or path to lowercase basename and a no-extension variant.
    Returns (normalized, no_ext) where either can be None.
    """
    try:
        cleaned = str(name).strip().strip("\"'")
        if not cleaned:
            return None, None
        lower = cleaned.lower()
        try:
            base = os.path.basename(lower) or lower
        except Exception:
            base = lower
        no_ext = base[:-4] if base.endswith(".exe") else base
        return base, no_ext
    except Exception:
        return None, None


def _match_blocked_process(requested: str, rules: List[str]) -> Optional[Dict[str, str]]:
    req_norm, req_no_ext = _normalize_process_name(requested)
    if not req_norm:
        return None

    req_candidates = {req_norm}
    if req_no_ext:
        req_candidates.add(req_no_ext)
        req_candidates.add(f"{req_no_ext}.exe")

    for rule in rules:
        rule_norm, rule_no_ext = _normalize_process_name(rule)
        if not rule_norm:
            continue
        rule_candidates = {rule_norm}
        if rule_no_ext:
            rule_candidates.add(rule_no_ext)
            rule_candidates.add(f"{rule_no_ext}.exe")
        if req_candidates & rule_candidates:
            matched_rule = next(iter(req_candidates & rule_candidates))
            return {
                "requested": requested,
                "normalized": req_norm,
                "matched_rule": matched_rule,
            }
    return None

    with _SAFETY_POLICY_LOCK:
        if _SAFETY_POLICY_MTIME is not None and mtime == _SAFETY_POLICY_MTIME:
            return _SAFETY_POLICY_CACHE

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            data = {}
        with _SAFETY_POLICY_LOCK:
            _SAFETY_POLICY_CACHE = data
            _SAFETY_POLICY_MTIME = mtime
        return data
    except Exception as exc:
        print(f"Error loading safety policy: {exc}")
        with _SAFETY_POLICY_LOCK:
            return _SAFETY_POLICY_CACHE or {}


def _evaluate_step_safety(step: ActionStep) -> Dict[str, Any]:
    """
    Enforce safety gates before executing a step.

    Returns dict with safe flag and metadata.
    """
    action = step.action
    params = step.params or {}
    base_dir = params.get("base_dir")
    policy = _load_safety_policy()

    def _unsafe(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"safe": False, "code": code, "message": message}
        if details:
            payload["details"] = details
        return payload

    # Keyword check on any string parameter.
    danger_keywords = policy.get("danger_keywords", []) if isinstance(policy, dict) else []
    if danger_keywords:
        for val in params.values():
            try:
                if isinstance(val, str) and any(k.lower() in val.lower() for k in danger_keywords):
                    return _unsafe("danger_keyword", f"param contains danger keyword for action '{action}'")
            except Exception:
                continue

    # Blocked process enforcement for open_app.
    if action == "open_app":
        requested = None
        for key in ("target", "app", "name", "path"):
            if isinstance(params.get(key), str):
                requested = params.get(key)
                break
        if requested:
            blocked_rules = policy.get("blocked_processes", []) if isinstance(policy, dict) else []
            match_info = _match_blocked_process(requested, blocked_rules) if blocked_rules else None
            if match_info:
                return _unsafe(
                    "process_blocked",
                    f"Blocked by safety policy: {requested}",
                    match_info,
                )

    # Action level check.
    sensitivity = {}
    if isinstance(policy, dict):
        sensitivity = policy.get("sensitive_actions", {}) or {}
    level = sensitivity.get(action)
    if level == "high" and params.get("confirm") is not True:
        return _unsafe("confirm_required", f"{action} requires confirm=True due to high risk", {"action": action})

    file_paths: List[str] = []

    def _resolve_file_path(raw: Any) -> Optional[str]:
        if not isinstance(raw, str):
            return None
        return files._resolve_path(raw, base_dir)

    if action in {"list_files", "delete_file", "read_file", "write_file", "open_file"}:
        path = _resolve_file_path(params.get("path"))
        if path:
            file_paths.append(path)
    if action in {"move_file", "copy_file"}:
        src = _resolve_file_path(params.get("source"))
        dest = _resolve_file_path(params.get("destination_dir") or params.get("destination"))
        if src:
            file_paths.append(src)
        if dest:
            file_paths.append(dest)
    if action == "rename_file":
        src = _resolve_file_path(params.get("source"))
        if src:
            file_paths.append(src)

    blocked_paths = policy.get("blocked_paths", []) if isinstance(policy, dict) else []

    for path in file_paths:
        if not _is_path_within_allowed_roots(path):
            return _unsafe("path_outside_workspace", f"path not allowed: {path}", {"path": path})
        if not files._is_path_safe(path):
            return _unsafe("path_blocked", f"path blocked by safety rules: {path}", {"path": path})
        for blocked in blocked_paths:
            try:
                if os.path.abspath(path).startswith(os.path.abspath(blocked)):
                    return _unsafe("path_blocked_policy", f"path blocked by policy: {path}", {"path": path})
            except Exception:
                continue

    return {"safe": True}


def _invoke_replan(
    user_text: str,
    context,
    recent_steps: str,
    failure_info: str,
    screenshot_meta: Optional[dict],
    provider: Optional[str],
    replan_image: Dict[str, Any],
    planner_override=None,
) -> Dict[str, Any]:
    """
    Call the planner to generate a follow-up ActionPlan after a failure.
    """
    image_b64 = replan_image.get("image_base64")
    prompt_bundle = format_prompt(
        user_text=user_text,
        ocr_text=getattr(context, "ocr_text", "") if context else "",
        manual_click=None,
        screenshot_meta=screenshot_meta or {},
        image_base64=image_b64,
        recent_steps=recent_steps,
        failure_info=failure_info,
    )

    if planner_override:
        try:
            override_result = planner_override(prompt_bundle)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"planner override failed: {exc}", "prompt": prompt_bundle}
        if isinstance(override_result, ActionPlan):
            return {"success": True, "plan": override_result, "prompt": prompt_bundle, "raw": override_result}
        if isinstance(override_result, dict):
            try:
                validated = ActionPlan.model_validate(override_result)
                return {"success": True, "plan": validated, "prompt": prompt_bundle, "raw": override_result}
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": f"invalid override plan: {exc}", "prompt": prompt_bundle}
        if isinstance(override_result, str):
            parsed = parse_action_plan(override_result)
            if isinstance(parsed, ActionPlan):
                return {"success": True, "plan": parsed, "prompt": prompt_bundle, "raw": override_result}
            return {"success": False, "error": parsed, "prompt": prompt_bundle}
        return {"success": False, "error": "planner override returned unsupported type", "prompt": prompt_bundle}

    try:
        provider_name, raw_reply = _call_planner_with_fallback(provider, prompt_bundle)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"planner call failed: {exc}", "prompt": prompt_bundle}

    parsed = parse_action_plan(raw_reply)
    if isinstance(parsed, str):
        return {"success": False, "error": parsed, "prompt": prompt_bundle, "raw": raw_reply}
    return {"success": True, "plan": parsed, "prompt": prompt_bundle, "raw": raw_reply}


ACTION_HANDLERS: Dict[str, Callable[[ActionStep], Any]] = {
    "open_app": lambda step, _prov=None: dispatch_handle_open_app(step, provider=sys.modules[__name__]),
    "open_url": handle_open_url,
    "switch_window": handle_switch_window,
    "activate_window": handle_activate_window,
    "type_text": handle_type,
    "key_press": handle_key_press,
    "click": handle_click,
    "move_file": handle_move_file,
    "copy_file": handle_copy_file,
    "rename_file": handle_rename_file,
    "open_file": handle_open_file,
    "mouse_move": handle_mouse_move,
    "right_click": handle_right_click,
    "double_click": handle_double_click,
    "scroll": handle_scroll,
    "drag": handle_drag,
    "hotkey": handle_hotkey,
    "list_windows": handle_list_windows,
    "get_active_window": handle_get_active_window,
    "fuzzy_switch_window": handle_fuzzy_switch_window,
    "list_files": handle_list_files,
    "delete_file": handle_delete_file,
    "create_folder": handle_create_folder,
    "read_file": handle_read_file,
    "write_file": handle_write_file,
    "wait": handle_wait,
    "wait_until": handle_wait_until,
    "adjust_volume": handle_adjust_volume,
    "click_text": handle_click_text,
    "browser_click": lambda step, _prov=None: dispatch_handle_browser_click(step, provider=sys.modules[__name__]),
    "browser_input": handle_browser_input,
    "browser_extract_text": lambda step, _prov=None: dispatch_handle_browser_extract_text(step, provider=sys.modules[__name__]),
    "take_over": handle_take_over,
}

FILE_PATH_ACTIONS = {
    "move_file",
    "copy_file",
    "rename_file",
    "list_files",
    "delete_file",
    "create_folder",
    "open_file",
    "read_file",
    "write_file",
}

STUB_UI_ACTIONS = {
    "open_app",
    "open_url",
    "switch_window",
    "activate_window",
    "type_text",
    "key_press",
    "click",
    "mouse_move",
    "right_click",
    "double_click",
    "scroll",
    "drag",
    "hotkey",
    "list_windows",
    "get_active_window",
    "fuzzy_switch_window",
    "click_text",
    "browser_click",
    "browser_input",
    "browser_extract_text",
    "adjust_volume",
    "take_over",
}

INTERACTIVE_ACTIONS = {
    "click",
    "right_click",
    "double_click",
    "mouse_move",
    "drag",
    "scroll",
    "type_text",
    "key_press",
    "hotkey",
    "browser_click",
    "browser_input",
    "browser_extract_text",
}

INPUT_ACTIONS = {
    "click",
    "right_click",
    "double_click",
    "mouse_move",
    "drag",
    "scroll",
    "type_text",
    "key_press",
    "hotkey",
    "browser_click",
    "browser_input",
    "adjust_volume",
}

RISKY_FILE_ACTIONS = {"delete_file", "move_file", "copy_file", "rename_file", "write_file"}
RISKY_INPUT_ACTIONS = {"type_text", "key_press", "hotkey", "browser_input"}

# Actions that must have the preferred/target window in foreground before dispatch.
FOREGROUND_REQUIRED_ACTIONS = {
    "click",
    "right_click",
    "double_click",
    "mouse_move",
    "drag",
    "scroll",
    "type_text",
    "key_press",
    "hotkey",
    "browser_click",
    "browser_input",
    "adjust_volume",
}


def _requires_foreground(action: str) -> bool:
    return action in FOREGROUND_REQUIRED_ACTIONS


def _stub_handler(step: ActionStep) -> Dict[str, Any]:
    """Return a success result without touching the real UI."""
    return {
        "status": "success",
        "method": "stub",
        "action": step.action,
        "params": dict(step.params or {}),
    }


TEST_MODE_HANDLERS: Dict[str, Callable[[ActionStep], Any]] = {name: _stub_handler for name in STUB_UI_ACTIONS}


def run_steps(
    action_plan: ActionPlan,
    context=None,
    max_retries: Optional[int] = None,
    capture_observations: bool = True,
    capture_ocr: Optional[bool] = None,
    allow_replan: bool = True,
    planner_provider: Optional[str] = None,
    planner_override=None,
    max_replans: Optional[int] = None,
    capture_replan_screenshot: Optional[bool] = None,
    debug_capture_all: bool = False,
    disable_vlm: bool = False,
    force_capture: Optional[bool] = None,
    force_ocr: Optional[bool] = None,
    allow_vlm_override: Optional[bool] = None,
    verify_mode_override: Optional[str] = None,
    work_dir: Optional[str] = None,
    task_id: Optional[str] = None,
    start_index: int = 0,
    dry_run: bool = False,
    window_provider: Optional[WindowProvider] = None,
    request_id: Optional[str] = None,
    consent_token: bool = False,
) -> Dict[str, Any]:
    """
    Execute an ActionPlan with a global observe-execute-observe-verify loop and optional replanning.

    Behavior:
    - Enforces a maximum number of steps (MAX_STEPS, scaled when replanning is enabled).
    - Wraps each step with optional before/after screenshots (and OCR summaries) recorded in the context.
    - Uses a generic verifier to decide success, retry (up to max_retries), or failure.
    - When a step exhausts retries and replanning is allowed, call the planner with recent history to
      generate additional follow-up steps appended to the remaining plan, up to max_replans.
    """
    logs: List[dict] = []
    plan_rewrites: List[Dict[str, Any]] = []
    if context is None:
        try:
            from backend.executor.task_context import TaskContext

            context = TaskContext(work_dir=work_dir)
        except Exception:
            context = None

    if work_dir:
        _add_allowed_root(work_dir)
    elif work_dir and getattr(context, "work_dir", None) is None:
        try:
            context.work_dir = work_dir
        except Exception:
            pass

    use_stub_handlers = _flag_from_env("EXECUTOR_TEST_MODE", False)
    base_max_retries = (
        DEFAULT_STEP_MAX_RETRIES
        if max_retries is None
        else _coerce_nonnegative_int(max_retries, DEFAULT_STEP_MAX_RETRIES)
    )
    capture_observations = True if debug_capture_all else capture_observations
    base_capture_before = True if debug_capture_all else (DEFAULT_CAPTURE_BEFORE if capture_observations else False)
    base_capture_after = True if debug_capture_all else (DEFAULT_CAPTURE_AFTER if capture_observations else False)
    base_capture_ocr = True if debug_capture_all else (_coerce_bool(capture_ocr, DEFAULT_CAPTURE_OCR) if capture_observations else False)
    if force_capture is not None:
        base_capture_before = bool(force_capture)
        base_capture_after = bool(force_capture)
    if force_ocr is not None:
        base_capture_ocr = bool(force_ocr)
    if use_stub_handlers:
        capture_observations = False
        base_capture_before = False
        base_capture_after = False
        base_capture_ocr = False
    base_max_replans = DEFAULT_MAX_REPLANS if max_replans is None else _coerce_nonnegative_int(max_replans, DEFAULT_MAX_REPLANS)
    base_replan_capture = DEFAULT_REPLAN_CAPTURE if capture_replan_screenshot is None else _coerce_bool(
        capture_replan_screenshot, DEFAULT_REPLAN_CAPTURE
    )
    base_allow_vlm = not (disable_vlm or DEFAULT_DISABLE_VLM)
    if allow_vlm_override is not None:
        base_allow_vlm = bool(allow_vlm_override)
    base_verify_mode = (verify_mode_override or "auto").lower()
    if base_verify_mode not in {"auto", "never", "always"}:
        base_verify_mode = "auto"
    window_provider = window_provider or _DefaultWindowProvider()

    base_feedback_config = {
        "capture_before": base_capture_before,
        "capture_after": base_capture_after,
        "capture_ocr": base_capture_ocr,
        "run_ocr_after": base_capture_ocr,
        "max_retries": base_max_retries,
        "verify_mode": base_verify_mode,
        "allow_vlm": base_allow_vlm,
    }
    if context and hasattr(context, "set_feedback_config"):
        try:
            context.set_feedback_config(base_feedback_config)
        except Exception:
            pass
    if context and getattr(context, "max_replans", None) is None:
        try:
            context.set_max_replans(base_max_replans)
        except Exception:
            pass
    if context and getattr(context, "max_replans", None) is not None:
        try:
            base_max_replans = int(context.max_replans)
        except Exception:
            pass

    # Per-run toggle for VLM usage; restore after run to avoid affecting other calls.
    context_token = CURRENT_CONTEXT.set(context)
    base_vlm_token = VLM_DISABLED.set(not base_allow_vlm)
    active_window_token = ACTIVE_WINDOW.set(None)

    dispatcher = Dispatcher(ACTION_HANDLERS, TEST_MODE_HANDLERS, test_mode=use_stub_handlers)
    steps: List[ActionStep] = list(action_plan.steps[:MAX_STEPS])
    last_focus_target = getattr(context, "last_focus_target", None)
    task_record = None
    task_token = CURRENT_TASK_ID.set(task_id) if task_id else None
    if task_id:
        task_record = get_task(task_id) or create_task(
            getattr(context, "user_instruction", None) if context else None,
            action_plan.model_dump() if hasattr(action_plan, "model_dump") else {},
            status=TaskStatus.RUNNING,
            task_id=task_id,
        )
        try:
            CURRENT_TASK_ID.set(task_id)
        except Exception:
            pass
    if dry_run:
        for idx, step in enumerate(steps):
            risk_info = _score_risk(step, work_dir, last_focus_target)
            evidence = build_evidence(
                request_id,
                idx,
                0,
                getattr(step, "action", None),
                "skipped",
                "dry_run",
                "preflight",
                before_obs=None,
                after_obs=None,
                foreground=last_focus_target,
                risk=risk_info,
                dry_run=True,
            )
            entry = {
                "step_index": idx,
                "action": getattr(step, "action", None),
                "params": getattr(step, "params", {}),
                "status": "skipped",
                "message": "dry_run: no side effects executed",
                "timestamp": now_iso_utc(),
                "duration_ms": 0.0,
                "risk": risk_info,
                "evidence": evidence,
                "attempts": [
                    {
                        "attempt": 0,
                        "status": "skipped",
                        "reason": "dry_run",
                        "message": "dry_run: no side effects executed",
                        "verification": {
                            "decision": "success",
                            "reason": "dry_run",
                            "status": "skipped",
                            "attempt": 0,
                            "max_attempts": 0,
                            "verifier": "none",
                            "expected": {},
                            "actual": {},
                            "evidence": evidence,
                            "should_retry": False,
                        },
                        "evidence": evidence,
                    }
                ],
            }
            logs.append(entry)
            if context:
                try:
                    context.record_step_result(entry)
                except Exception:
                    pass
        summary = _build_execution_summary(logs, context)
        diagnostics = _build_diagnostics_summary(logs, "dry_run")
        result = {"overall_status": "dry_run", "logs": logs, "summary": summary}
        if diagnostics:
            result["diagnostics_summary"] = diagnostics
        if context:
            result["context"] = context.to_dict()
            if hasattr(context, "set_summary"):
                try:
                    context.set_summary(summary)
                except Exception:
                    pass
        if task_record:
            update_task(
                task_id,
                status=TaskStatus.COMPLETED,
                step_index=0,
                last_error=None,
                context_snapshot=_build_context_snapshot(context),
            )
            result["task_id"] = task_id
        return result
    # Rewrite UI save patterns to direct write_file when possible.
    steps, rewrite_log = _rewrite_save_pattern(steps, base_dir=work_dir)
    if rewrite_log:
        plan_rewrites.append(rewrite_log)
    # Allow additional steps when replanning to avoid premature truncation.
    max_total_steps = max(len(steps), MAX_STEPS * (1 + max(0, base_max_replans)))
    overall_status = "success"
    idx = max(0, int(start_index))
    executed_steps = 0

    # Plan-level safety checks before executing anything.
    dangerous_term = _detect_dangerous_request(getattr(context, "user_instruction", None) if context else None)
    if dangerous_term:
        entry = {
            "step_index": -1,
            "action": "safety_check",
            "params": {},
            "status": "unsafe",
            "message": f"user request flagged as dangerous ('{dangerous_term}')",
            "timestamp": now_iso_utc(),
            "duration_ms": 0.0,
            "safety": {"code": "dangerous_request", "term": dangerous_term},
        }
        logs.append(entry)
        if context:
            try:
                context.record_step_result(entry)
                context.add_error(entry["message"], {"code": "dangerous_request", "term": dangerous_term})
            except Exception:
                pass
        diagnostics = _build_diagnostics_summary(logs, "unsafe")
        result = {"overall_status": "unsafe", "logs": logs, "context": context.to_dict() if context else None}
        if diagnostics:
            result["diagnostics_summary"] = diagnostics
        return result

    # Pre-validate all planned steps for safety before execution starts.
    for pre_idx, pre_step in enumerate(steps):
        safety_check = _evaluate_step_safety(pre_step)
        if not safety_check.get("safe"):
            entry = {
                "step_index": pre_idx,
                "action": pre_step.action,
                "params": pre_step.params,
                "status": "unsafe",
                "message": safety_check.get("message"),
                "timestamp": now_iso_utc(),
                "duration_ms": 0.0,
                "safety": safety_check,
            }
            logs.append(entry)
            if context:
                try:
                    context.record_step_result(entry)
                    context.add_error(entry["message"], safety_check)
                except Exception:
                    pass
            diagnostics = _build_diagnostics_summary(logs, "unsafe")
            result = {"overall_status": "unsafe", "logs": logs, "context": context.to_dict() if context else None}
            if diagnostics:
                result["diagnostics_summary"] = diagnostics
            return result

    try:
        while idx < len(steps) and executed_steps < max_total_steps:
            step = steps[idx]
            if work_dir and step.action in FILE_PATH_ACTIONS:
                step.params = dict(step.params or {})
                step.params.setdefault("base_dir", work_dir)
            expected_window = None
            needs_foreground = _requires_foreground(step.action)
            if step.action in INPUT_ACTIONS and needs_foreground:
                expected_window = last_focus_target or _extract_focus_hints(step)
            executed_steps += 1
            handler = dispatcher.get_handler(step.action)
            if not handler:
                entry = {
                    "step_index": idx,
                    "action": step.action,
                    "params": step.params,
                    "status": "error",
                    "message": f"No handler for action '{step.action}'",
                    "timestamp": now_iso_utc(),
                    "duration_ms": 0.0,
                }
                logs.append(entry)
                if context:
                    try:
                        context.record_step_result(entry)
                    except Exception:
                        pass
                overall_status = "error"
                break

            # Safety gate for newly appended steps (e.g., via replanning).
            step_safety = _evaluate_step_safety(step)
            if not step_safety.get("safe"):
                entry = {
                    "step_index": idx,
                    "action": step.action,
                    "params": step.params,
                    "status": "unsafe",
                    "message": step_safety.get("message"),
                    "timestamp": now_iso_utc(),
                    "duration_ms": 0.0,
                    "safety": step_safety,
                }
                logs.append(entry)
                if context:
                    try:
                        context.record_step_result(entry)
                        context.add_error(entry["message"], step_safety)
                    except Exception:
                        pass
                overall_status = "unsafe"
                break

            risk_info = _score_risk(step, work_dir, last_focus_target)

            if not dry_run and step.action in INPUT_ACTIONS and needs_foreground:
                expected = expected_window
                if not expected:
                    evidence = build_evidence(
                        request_id,
                        idx,
                        0,
                        step.action,
                        "error",
                        "no_target_hint",
                        "gate",
                        before_obs=None,
                        after_obs=None,
                        foreground=None,
                        focus_expected=None,
                        focus_actual=None,
                        risk=risk_info,
                    )
                    entry = {
                        "step_index": idx,
                        "action": step.action,
                        "params": step.params,
                        "status": "error",
                        "reason": "no_target_hint",
                        "message": "no target hint for focus safety",
                        "request_id": request_id,
                        "timestamp": now_iso_utc(),
                        "duration_ms": 0.0,
                        "expected_window": None,
                        "actual_window": None,
                        "evidence": evidence,
                        "attempts": [
                            {
                                "attempt": 0,
                                "status": "error",
                                "reason": "no_target_hint",
                                "message": "no target hint for focus safety",
                                "verification": {
                                    "decision": "failed",
                                    "reason": "no_target_hint",
                                    "status": "error",
                                    "attempt": 0,
                                    "max_attempts": 0,
                                    "verifier": "focus_gate",
                                    "expected": {},
                                    "actual": {},
                                    "evidence": evidence,
                                    "should_retry": False,
                                },
                                "evidence": evidence,
                            }
                        ],
                    }
                    logs.append(entry)
                    if context:
                        try:
                            context.record_step_result(entry)
                            context.add_error(entry["message"])
                        except Exception:
                            pass
                    overall_status = "error"
                    break
                actual_window = window_provider.get_foreground_window()
                if not _window_matches(expected, actual_window):
                    evidence = build_evidence(
                        request_id,
                        idx,
                        0,
                        step.action,
                        "error",
                        "foreground_mismatch",
                        "gate",
                        before_obs=None,
                        after_obs=None,
                        foreground=actual_window,
                        focus_expected=expected,
                        focus_actual=actual_window,
                        risk=risk_info,
                    )
                    entry = {
                        "step_index": idx,
                        "action": step.action,
                        "params": step.params,
                        "status": "error",
                        "reason": "foreground_mismatch",
                        "message": "foreground window mismatch",
                        "request_id": request_id,
                        "timestamp": now_iso_utc(),
                        "duration_ms": 0.0,
                        "expected_window": expected,
                        "actual_window": actual_window,
                        "evidence": evidence,
                        "attempts": [
                            {
                                "attempt": 0,
                                "status": "error",
                                "reason": "foreground_mismatch",
                                "message": "foreground window mismatch",
                                "verification": {
                                    "decision": "failed",
                                    "reason": "foreground_mismatch",
                                    "status": "error",
                                    "attempt": 0,
                                    "max_attempts": 0,
                                    "verifier": "focus_gate",
                                    "expected": {"target": expected},
                                    "actual": {"foreground": actual_window},
                                    "evidence": evidence,
                                    "should_retry": False,
                                },
                                "evidence": evidence,
                            }
                        ],
                    }
                    logs.append(entry)
                    if context:
                        try:
                            context.record_step_result(entry)
                            context.add_error(entry["message"], {"expected_window": expected, "actual_window": actual_window})
                        except Exception:
                            pass
                    overall_status = "error"
                    break
            elif not dry_run and step.action in INPUT_ACTIONS and not needs_foreground:
                try:
                    foreground_snapshot = window_provider.get_foreground_window()
                except Exception:
                    foreground_snapshot = None
                emit_context_event(
                    context,
                    "gate",
                    {
                        "reason": "focus_check_skipped",
                        "action": step.action,
                        "foreground": foreground_snapshot,
                    },
                )

            if risk_info["level"] == RISK_BLOCK:
                evidence = build_evidence(
                    request_id,
                    idx,
                    0,
                    step.action,
                    "error",
                    "blocked",
                    "gate",
                    before_obs=None,
                    after_obs=None,
                    foreground=None,
                    risk=risk_info,
                )
                entry = {
                    "step_index": idx,
                    "action": step.action,
                    "params": step.params,
                    "status": "error",
                    "reason": "blocked",
                    "message": risk_info["reason"],
                    "risk": risk_info,
                    "request_id": request_id,
                    "timestamp": now_iso_utc(),
                    "duration_ms": 0.0,
                    "evidence": evidence,
                    "attempts": [
                        {
                            "attempt": 0,
                            "status": "error",
                            "reason": "blocked",
                            "message": risk_info["reason"],
                            "verification": {
                                "decision": "failed",
                                "reason": "blocked",
                                "status": "error",
                                "attempt": 0,
                                "max_attempts": 0,
                                "verifier": "risk_gate",
                                "expected": {},
                                "actual": {},
                                "evidence": evidence,
                                "should_retry": False,
                            },
                            "evidence": evidence,
                        }
                    ],
                }
                logs.append(entry)
                if context:
                    try:
                        context.record_step_result(entry)
                        context.add_error(entry["message"], risk_info)
                    except Exception:
                        pass
                overall_status = "error"
                break

            if risk_info["level"] == RISK_HIGH and not consent_token:
                evidence = build_evidence(
                    request_id,
                    idx,
                    0,
                    step.action,
                    "error",
                    "needs_consent",
                    "gate",
                    before_obs=None,
                    after_obs=None,
                    foreground=None,
                    risk=risk_info,
                )
                entry = {
                    "step_index": idx,
                    "action": step.action,
                    "params": step.params,
                    "status": "error",
                    "reason": "needs_consent",
                    "message": "consent required for high-risk action",
                    "risk": risk_info,
                    "request_id": request_id,
                    "timestamp": now_iso_utc(),
                    "duration_ms": 0.0,
                    "evidence": evidence,
                    "attempts": [
                        {
                            "attempt": 0,
                            "status": "error",
                            "reason": "needs_consent",
                            "message": "consent required for high-risk action",
                            "verification": {
                                "decision": "failed",
                                "reason": "needs_consent",
                                "status": "error",
                                "attempt": 0,
                                "max_attempts": 0,
                                "verifier": "risk_gate",
                                "expected": {},
                                "actual": {},
                                "evidence": evidence,
                                "should_retry": False,
                            },
                            "evidence": evidence,
                        }
                    ],
                }
                logs.append(entry)
                if context:
                    try:
                        context.record_step_result(entry)
                        context.add_error(entry["message"], risk_info)
                    except Exception:
                        pass
                overall_status = "error"
                break

            # File guardrails (mutation + read)
            file_guard = _evaluate_file_guardrails(step, work_dir, dry_run, allowed_roots=ALLOWED_ROOTS)
            if not file_guard.get("allow"):
                reason_code = file_guard.get("reason") or "path_not_allowed"
                evidence = build_evidence(
                    request_id,
                    idx,
                    0,
                    step.action,
                    "error",
                    reason_code,
                    "gate",
                    before_obs=None,
                    after_obs=None,
                    foreground=None,
                    file_check={
                        "original_path": file_guard.get("original_path"),
                        "normalized_path": file_guard.get("normalized_path"),
                        "allowed_roots": file_guard.get("allowed_roots"),
                        "rule": file_guard.get("rule"),
                        "decision": "deny",
                    },
                    dry_run=dry_run,
                )
                entry = {
                    "step_index": idx,
                    "action": step.action,
                    "params": step.params,
                    "status": "error",
                    "reason": reason_code,
                    "message": reason_code.replace("_", " "),
                    "request_id": request_id,
                    "timestamp": now_iso_utc(),
                    "duration_ms": 0.0,
                    "evidence": evidence,
                    "attempts": [
                        {
                            "attempt": 0,
                            "status": "error",
                            "reason": reason_code,
                            "message": reason_code.replace("_", " "),
                            "verification": {
                                "decision": "failed",
                                "reason": reason_code,
                                "status": "error",
                                "attempt": 0,
                                "max_attempts": 0,
                                "verifier": "file_guard",
                                "expected": {},
                                "actual": {},
                                "evidence": evidence,
                                "should_retry": False,
                            },
                            "evidence": evidence,
                        }
                    ],
                }
                logs.append(entry)
                if context:
                    try:
                        context.record_step_result(entry)
                        context.add_error(entry["message"], {"file_guard": file_guard})
                    except Exception:
                        pass
                overall_status = "error"
                break

            step_feedback = _build_step_feedback_config(step, base_feedback_config)
            attempt_logs: List[Dict[str, Any]] = []
            combined_duration_ms = 0.0
            step_status = "error"
            last_message: Any = None
            last_verification: Dict[str, Any] = {}
            # Per-step VLM toggle (cannot override global disable).
            step_allow_vlm = base_allow_vlm and bool(step_feedback.get("allow_vlm", True))
            step_vlm_token = VLM_DISABLED.set(not step_allow_vlm)

            try:
                for attempt in range(1, step_feedback["max_attempts"] + 1):
                    pre_fingerprint = None
                    if context and hasattr(context, "get_ui_fingerprint"):
                        try:
                            pre_fingerprint = context.get_ui_fingerprint(lite_only=True)
                        except Exception:
                            pre_fingerprint = None

                    before_obs = _capture_observation(
                        step_feedback["capture_before"],
                        step_feedback["capture_before"] and step_feedback["capture_ocr"],
                    )
                    start = perf_counter()
                    try:
                        message = handler(step)
                        handler_status = "success"
                    except Exception as exc:  # noqa: BLE001
                        message = f"Handler failed: {exc}"
                        handler_status = "error"
                    duration_ms = (perf_counter() - start) * 1000.0
                    combined_duration_ms += duration_ms
                    post_fingerprint = None
                    if context and hasattr(context, "get_ui_fingerprint"):
                        try:
                            post_fingerprint = context.get_ui_fingerprint(lite_only=True)
                        except Exception:
                            post_fingerprint = None

                    after_obs = _capture_observation(
                        step_feedback["capture_after"],
                        step_feedback["capture_after"] and step_feedback.get("run_ocr_after", step_feedback["capture_ocr"]),
                    )
                    normalized_status, normalized_reason = _normalize_handler_status(message, handler_status)
                    verification = verify_step_outcome(
                        step,
                        normalized_status,
                        message,
                        attempt,
                        step_feedback["max_attempts"],
                        expected_window if step.action in INPUT_ACTIONS else None,
                        before_obs,
                        after_obs,
                        work_dir,
                        verify_mode=step_feedback.get("verify_mode", "auto"),
                        request_id=request_id,
                        step_index=idx,
                        window_provider=window_provider,
                        input_actions=INPUT_ACTIONS,
                        risky_file_actions=RISKY_FILE_ACTIONS,
                        window_enumerator=_enum_top_windows,
                        window_filter=_filter_windows_by_keywords,
                    )

                    attempt_logs.append(
                        {
                            "attempt": attempt,
                            "status": normalized_status,
                            "reason": normalized_reason,
                            "message": message,
                            "duration_ms": duration_ms,
                            "observation": {
                                "before": before_obs if step_feedback["capture_before"] else {"capture_enabled": False},
                                "after": after_obs if step_feedback["capture_after"] else {"capture_enabled": False},
                            },
                            "verification": verification,
                            "evidence": verification.get("evidence"),
                        }
                    )
                    last_message = message
                    last_verification = verification

                    if (
                        pre_fingerprint
                        and post_fingerprint
                        and pre_fingerprint == post_fingerprint
                        and step.action in INTERACTIVE_ACTIONS
                    ):
                        logs.append({"warning": "UI State unchanged", "fingerprint": pre_fingerprint, "step_index": idx, "action": step.action})

                    if verification["decision"] == "success":
                        step_status = "success"
                        last_message = verification.get("reason") or last_message
                        break
                    if verification["decision"] == "retry":
                        last_message = verification.get("reason") or last_message
                        continue

                    step_status = "error"
                    last_message = verification.get("reason") or last_message
                    break
            finally:
                VLM_DISABLED.reset(step_vlm_token)

            entry = {
                "step_index": idx,
                "action": step.action,
                "params": step.params,
                "status": step_status,
                "message": last_message,
                "timestamp": now_iso_utc(),
                "duration_ms": combined_duration_ms,
                "attempts": attempt_logs,
                "verification": last_verification,
                "feedback": step_feedback,
                "reason": last_verification.get("reason") if last_verification else normalized_reason,
            }
            if last_verification:
                entry["evidence"] = last_verification.get("evidence")
            if attempt_logs:
                entry["observations"] = attempt_logs[-1].get("observation")

            if (
                not dry_run
                and step_status == "success"
                and step.action in {"activate_window", "fuzzy_switch_window", "switch_window"}
            ):
                new_focus = window_provider.get_foreground_window()
                last_focus_target = new_focus
                _set_last_focus_target(context, new_focus)
            if dry_run:
                entry["risk"] = risk_info

            replan_successful = False
            replan_log: Dict[str, Any] = {}
            should_replan = (
                allow_replan
                and step_status == "error"
                and base_max_replans > 0
                and (getattr(context, "replan_count", 0) < base_max_replans if context else True)
            )

            if should_replan:
                next_replan_attempt = 1 + (getattr(context, "replan_count", 0) if context else 0)
                failure_info = _build_failure_summary(entry, next_replan_attempt, base_max_replans)
                recent_steps = _summarize_steps_for_prompt(logs + [entry], limit=6)
                replan_image = _maybe_capture_replan_image(base_replan_capture)
                replan_result = _invoke_replan(
                    user_text=getattr(context, "user_instruction", ""),
                    context=context,
                    recent_steps=recent_steps,
                    failure_info=failure_info,
                    screenshot_meta=getattr(context, "screenshot_meta", {}) if context else {},
                    provider=planner_provider,
                    replan_image=replan_image,
                    planner_override=planner_override,
                )
                replan_log = {
                    "attempt": next_replan_attempt,
                    "provider": (planner_provider or "deepseek"),
                    "success": bool(replan_result.get("success")),
                    "error": replan_result.get("error"),
                    "screenshot_path": replan_image.get("path"),
                    "used_screenshot": bool(replan_image.get("image_base64")),
                }
                if replan_result.get("success"):
                    try:
                        plan_obj = replan_result.get("plan")
                        new_steps = list(plan_obj.steps) if plan_obj else []
                        added = 0
                        for new_step in new_steps:
                            if len(steps) >= max_total_steps:
                                break
                            steps.append(new_step)
                            added += 1
                        replan_log["appended_steps"] = added
                        replan_log["total_steps_now"] = len(steps)
                        replan_log["replan_prompt"] = getattr(replan_result.get("prompt"), "prompt_text", None)
                        replan_successful = added > 0
                        if not replan_successful:
                            replan_log["error"] = replan_log.get("error") or "no new steps appended"
                    except Exception as exc:  # noqa: BLE001
                        replan_log["error"] = f"failed to append replan steps: {exc}"
                        replan_successful = False
                if context and hasattr(context, "record_replan"):
                    try:
                        context.record_replan(replan_log)
                        try:
                            context.plan_iteration = getattr(context, "plan_iteration", 0) + 1
                        except Exception:
                            pass
                    except Exception:
                        pass
                entry["replan"] = replan_log

            # Record step result after any replan adjustments.
            logs.append(entry)
            if context:
                try:
                    context.record_step_result(entry)
                except Exception:
                    pass
            if task_record:
                update_task(
                    task_id,
                    step_index=idx + 1,
                    step_results=(context.step_results if context else logs),
                    status=TaskStatus.RUNNING,
                    last_error=None,
                    context_snapshot=_build_context_snapshot(context),
                )

            if step.action == "take_over":
                if task_record:
                    update_task(
                        task_id,
                        status=TaskStatus.AWAITING_USER,
                        step_index=idx + 1,
                        last_error=None,
                        context_snapshot=_build_context_snapshot(context),
                    )
                overall_status = "awaiting_user"
                break

            if step_status == "error" and not replan_successful:
                overall_status = "error"
                break
            if replan_successful and overall_status == "success":
                overall_status = "replanned"

            idx += 1
    finally:
        CURRENT_CONTEXT.reset(context_token)
        VLM_DISABLED.reset(base_vlm_token)
        ACTIVE_WINDOW.reset(active_window_token)
        if task_token is not None:
            try:
                CURRENT_TASK_ID.reset(task_token)
            except Exception:
                pass

    result = {"overall_status": overall_status, "logs": logs}
    if context:
        result["context"] = context.to_dict()
    if plan_rewrites:
        result["plan_rewrites"] = plan_rewrites
    replan_count = getattr(context, "replan_count", 0) if context else 0
    if overall_status in {"success", "replanned"}:
        final_status = "success_with_replan" if replan_count > 0 or overall_status == "replanned" else "success"
    elif overall_status == "awaiting_user":
        final_status = "awaiting_user"
    else:
        final_status = "failed"
    recovered_failures = sum(1 for log in logs if log.get("status") in {"error", "unsafe"}) if final_status.startswith("success") else 0
    summary = _build_execution_summary(logs, context, final_status=final_status, recovered_failures=recovered_failures)
    result["summary"] = summary
    result["final_status"] = final_status
    diagnostics = _build_diagnostics_summary(logs, overall_status)
    if diagnostics:
        result["diagnostics_summary"] = diagnostics
    if context and hasattr(context, "set_summary"):
        try:
            context.set_summary(summary)
        except Exception:
            pass
    if task_record:
        final_status = (
            TaskStatus.COMPLETED
            if overall_status == "success"
            else TaskStatus.FAILED
            if overall_status == "error"
            else TaskStatus.AWAITING_USER
        )
        step_cursor = idx + 1 if final_status == TaskStatus.AWAITING_USER else idx
        update_task(
            task_id,
            status=final_status,
            step_index=step_cursor,
            last_error=None if final_status != TaskStatus.FAILED else str(summary),
            context_snapshot=_build_context_snapshot(context),
        )
        result["task_id"] = task_id
    return result


__all__ = ["run_steps", "ACTION_HANDLERS", "debug_wechat_activation"]
