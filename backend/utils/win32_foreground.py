"""
Foreground enforcement helpers for Windows.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Dict, List, Optional

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_RESTORE = 9
SW_SHOWMAXIMIZED = 3
SW_SHOW = 5
VK_MENU = 0x12  # ALT key
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SWP_NOACTIVATE = 0x0010


def get_foreground_info() -> Dict[str, Optional[int]]:
    """Return hwnd/pid/title/class for current foreground window."""
    info: Dict[str, Optional[int]] = {"hwnd": None, "pid": None, "title": None, "class_name": None}
    try:
        fg = user32.GetForegroundWindow()
        info["hwnd"] = int(fg) if fg else None
    except Exception:
        fg = None
    if fg:
        try:
            pid_out = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(pid_out))
            info["pid"] = int(pid_out.value)
        except Exception:
            pass
        try:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(wintypes.HWND(fg), buf, 512)
            info["title"] = buf.value
        except Exception:
            pass
        try:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(wintypes.HWND(fg), buf, 256)
            info["class_name"] = buf.value
        except Exception:
            pass
    return info


def _window_state(hwnd: int) -> Dict[str, bool]:
    minimized = False
    maximized = False
    try:
        minimized = bool(user32.IsIconic(wintypes.HWND(hwnd)))
    except Exception:
        minimized = False
    try:
        maximized = bool(user32.IsZoomed(wintypes.HWND(hwnd)))
    except Exception:
        maximized = False
    return {"minimized": minimized, "maximized": maximized}


def _topmost_bounce(hwnd: int) -> None:
    try:
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE,
        )
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(HWND_NOTOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE,
        )
    except Exception:
        pass


def _send_alt_nudge() -> None:
    try:
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, 2, 0)
    except Exception:
        pass


def ensure_foreground(hwnd: int, timeout_ms: int = 1200, try_alt_nudge: bool = True) -> Dict[str, object]:
    """
    Try to force the given hwnd to foreground. Returns structured result with attempts and timing.
    """
    start = time.perf_counter()
    result: Dict[str, object] = {
        "ok": False,
        "success": False,
        "attempts": [],
        "final_foreground": None,
        "reason": None,
    }
    if not hwnd or int(hwnd) <= 0:
        result["reason"] = "invalid_hwnd"
        return result

    hwnd = int(hwnd)
    deadline = start + (timeout_ms / 1000.0)
    attempt_idx = 0
    alt_used = False

    while time.perf_counter() < deadline:
        attempt_idx += 1
        actions: List[str] = []
        fg_tid = None
        tgt_tid = None
        fg_info_before = get_foreground_info()
        try:
            # Show respecting current state.
            state = _window_state(hwnd)
            if state["minimized"]:
                user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
                actions.append("show:restore")
            elif state["maximized"]:
                user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOWMAXIMIZED)
                actions.append("show:maximized")
            else:
                user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)
                actions.append("show:normal")

            _topmost_bounce(hwnd)
            actions.append("topmost_bounce")

            try:
                fg_tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            except Exception:
                fg_tid = None
            try:
                tgt_tid = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
            except Exception:
                tgt_tid = None
            cur_tid = kernel32.GetCurrentThreadId()

            if fg_tid:
                user32.AttachThreadInput(cur_tid, fg_tid, True)
                actions.append("attach_fg")
            if tgt_tid:
                user32.AttachThreadInput(cur_tid, tgt_tid, True)
                actions.append("attach_tgt")

            user32.BringWindowToTop(wintypes.HWND(hwnd))
            actions.append("bring_to_top")
            user32.SetForegroundWindow(wintypes.HWND(hwnd))
            actions.append("set_fg")

            fg_now = get_foreground_info()
            if fg_now.get("hwnd") == hwnd:
                result["ok"] = True
                result["attempts"].append({"index": attempt_idx, "actions": actions, "fg": fg_now})
                result["final_foreground"] = fg_now
                result["reason"] = "foreground_acquired"
                break

            if try_alt_nudge and not alt_used:
                alt_used = True
                _send_alt_nudge()
                user32.SetForegroundWindow(wintypes.HWND(hwnd))
                actions.append("alt_nudge_set_fg")
                fg_now = get_foreground_info()
                if fg_now.get("hwnd") == hwnd:
                    result["ok"] = True
                    result["attempts"].append({"index": attempt_idx, "actions": actions, "fg": fg_now})
                    result["final_foreground"] = fg_now
                    result["reason"] = "foreground_acquired_alt"
                    break

            result["attempts"].append({"index": attempt_idx, "actions": actions, "fg_before": fg_info_before, "fg_after": fg_now})
        except Exception as exc:  # noqa: BLE001
            result["attempts"].append({"index": attempt_idx, "actions": actions, "error": str(exc), "fg_before": fg_info_before})
        finally:
            try:
                if fg_tid:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
            except Exception:
                pass
            try:
                if tgt_tid:
                    user32.AttachThreadInput(cur_tid, tgt_tid, False)
            except Exception:
                pass

    if not result["ok"]:
        result["final_foreground"] = get_foreground_info()
        if result["reason"] is None:
            result["reason"] = "foreground_not_acquired"
    result["duration_ms"] = (time.perf_counter() - start) * 1000.0
    result["success"] = result["ok"]
    return result


__all__ = ["ensure_foreground", "get_foreground_info"]
