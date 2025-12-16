from pathlib import Path
import tempfile
import ctypes
from ctypes import wintypes

import mss
from PIL import Image


def _window_rect(hwnd: int):
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):  # type: ignore[arg-type]
        raise RuntimeError("get_window_rect_failed")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError("invalid_window_rect")
    return rect.left, rect.top, rect.right, rect.bottom


def capture_window(hwnd: int) -> Path:
    """
    Capture a single window by hwnd. Raises on failure.
    """
    left, top, right, bottom = _window_rect(hwnd)
    output_path = Path(tempfile.gettempdir()) / "screenshot.png"
    with mss.mss() as sct:
        bbox = {"left": int(left), "top": int(top), "width": int(right - left), "height": int(bottom - top)}
        raw = sct.grab(bbox)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        img.save(output_path)
    return output_path


def capture_screen() -> Path:
    """Capture all monitors and save temporarily as screenshot.png."""
    output_path = Path(tempfile.gettempdir()) / "screenshot.png"
    with mss.mss() as sct:
        try:
            sct.shot(mon=-1, output=str(output_path))
        except Exception:
            # Fallback to primary monitor if all-monitors capture fails.
            sct.shot(output=str(output_path))
    return output_path
