"""
Mouse helpers for simulated clicks and scrolling.

Provides a thin wrapper around pyautogui operations with basic validation.
"""

from typing import Any, Dict, List

import pyautogui


def _validate_coords(x: Any, y: Any) -> bool:
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return False
    width, height = pyautogui.size()
    return 0 <= x < width and 0 <= y < height


class MouseController:
    """Centralized mouse operations."""

    def click(self, params: Dict[str, Any]) -> str:
        """
        Click at the given screen coordinates.

        Expected params:
            x: int/float - x coordinate
            y: int/float - y coordinate
            button: str (optional) - 'left', 'right', or 'middle'; defaults to 'left'

        Returns:
            A status string describing success or the encountered error.
        """
        x = (params or {}).get("x")
        y = (params or {}).get("y")
        button = (params or {}).get("button", "left")

        if not _validate_coords(x, y):
            return "error: 'x' and 'y' must be within screen bounds"

        try:
            pyautogui.click(x=x, y=y, button=button)
            return f"clicked at ({x}, {y}) with {button}"
        except Exception as exc:  # noqa: BLE001
            return f"error: failed to click: {exc}"

    def scroll(self, dx: int = 0, dy: int = 0) -> Dict[str, Any]:
        """
        Scroll the mouse wheel horizontally/vertically based on deltas.

        Positive dy scrolls up; negative dy scrolls down.
        Positive dx scrolls right; negative dx scrolls left.
        """
        delta_x = int(dx)
        delta_y = int(dy)
        actions: List[str] = []
        result: Dict[str, Any] = {
            "dx": delta_x,
            "dy": delta_y,
            "applied": actions,
            "performed": False,
        }

        if delta_x == 0 and delta_y == 0:
            result["status"] = "noop"
            result["reason"] = "zero_delta"
            return result

        try:
            if delta_y:
                pyautogui.scroll(delta_y)
                actions.append("vertical")
            if delta_x:
                pyautogui.hscroll(delta_x)
                actions.append("horizontal")
            result["status"] = "success"
            result["performed"] = bool(actions)
            return result
        except Exception as exc:  # noqa: BLE001
            result["status"] = "error"
            result["error"] = str(exc)
            return result

    def drag(self, start: Dict[str, Any], end: Dict[str, Any], duration: float = 0.2) -> Dict[str, Any]:
        """
        Drag from start to end coordinates with an optional duration.
        """
        sx, sy = start.get("x"), start.get("y")
        ex, ey = end.get("x"), end.get("y")
        result: Dict[str, Any] = {
            "start": {"x": sx, "y": sy},
            "end": {"x": ex, "y": ey},
            "duration": duration,
        }

        if not _validate_coords(sx, sy) or not _validate_coords(ex, ey):
            result["status"] = "error"
            result["reason"] = "coordinates out of bounds or invalid"
            return result

        try:
            pyautogui.moveTo(sx, sy)
            pyautogui.dragTo(ex, ey, duration=duration)
            result["status"] = "success"
            return result
        except Exception as exc:  # noqa: BLE001
            result["status"] = "error"
            result["reason"] = f"failed to drag: {exc}"
            return result


controller = MouseController()


def click(params: Dict[str, Any]) -> str:
    return controller.click(params)


def scroll(dx: int = 0, dy: int = 0) -> Dict[str, Any]:
    return controller.scroll(dx=dx, dy=dy)
