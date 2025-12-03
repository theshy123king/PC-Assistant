"""
Lightweight template-matching icon locator.

Attempts OpenCV-based matchTemplate when available; otherwise falls back to a
minimal Pillow-based sliding window matcher suitable for small UI glyphs.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

from PIL import Image

try:  # pragma: no cover - optional dependency
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore


def _load_gray(path: str):
    if cv2:
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is not None:
            return img
    with Image.open(path) as img:
        return img.convert("L")


def _pil_match_template(image: Image.Image, template: Image.Image, threshold: float) -> List[dict]:
    """
    Simple normalized match based on mean squared error; sufficient for small icons.
    """
    img_w, img_h = image.size
    tpl_w, tpl_h = template.size
    if tpl_w == 0 or tpl_h == 0 or tpl_w > img_w or tpl_h > img_h:
        return []

    img_pixels = image.load()
    tpl_pixels = template.load()
    results: List[dict] = []
    norm_factor = tpl_w * tpl_h * 255.0

    for x in range(0, img_w - tpl_w + 1):
        for y in range(0, img_h - tpl_h + 1):
            diff = 0.0
            for dx in range(tpl_w):
                for dy in range(tpl_h):
                    diff += abs(img_pixels[x + dx, y + dy] - tpl_pixels[dx, dy])
            score = 1.0 - (diff / norm_factor)
            if score >= threshold:
                results.append(
                    {
                        "score": float(score),
                        "bounds": {"x": x, "y": y, "width": tpl_w, "height": tpl_h},
                        "center": {"x": x + tpl_w / 2.0, "y": y + tpl_h / 2.0},
                    }
                )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def locate_icons(
    image_path: str,
    templates: Dict[str, str],
    threshold: float = 0.9,
    max_results: int = 5,
) -> List[dict]:
    """
    Locate icons using template matching.

    Returns a list of matches with name, score, center, and bounds.
    """
    if not image_path or not templates:
        return []

    matches: List[dict] = []
    base = _load_gray(image_path)
    for name, tpl_path in templates.items():
        if not tpl_path:
            continue
        tpl_img = _load_gray(tpl_path)
        if tpl_img is None or base is None:
            continue
        try:
            if cv2 and not isinstance(base, Image.Image):
                res = cv2.matchTemplate(base, tpl_img, cv2.TM_CCOEFF_NORMED)
                loc = cv2.minMaxLoc(res)
                max_val, max_loc = loc[1], loc[3]
                if max_val >= threshold:
                    w = tpl_img.shape[1]
                    h = tpl_img.shape[0]
                    cx = max_loc[0] + w / 2.0
                    cy = max_loc[1] + h / 2.0
                    matches.append(
                        {
                            "name": name,
                            "score": float(max_val),
                            "center": {"x": cx, "y": cy},
                            "bounds": {"x": max_loc[0], "y": max_loc[1], "width": w, "height": h},
                            "method": "opencv",
                        }
                    )
            else:
                # Pillow fallback
                if not isinstance(base, Image.Image):
                    with Image.open(image_path) as img:
                        base_img = img.convert("L")
                else:
                    base_img = base
                if isinstance(tpl_img, Image.Image):
                    tpl_image = tpl_img
                else:
                    with Image.open(tpl_path) as img:
                        tpl_image = img.convert("L")
                pil_matches = _pil_match_template(base_img, tpl_image, threshold)
                for m in pil_matches:
                    matches.append(
                        {
                            "name": name,
                            "score": m["score"],
                            "center": m["center"],
                            "bounds": m["bounds"],
                            "method": "pillow",
                        }
                    )
        except Exception:
            continue

    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches[:max_results]


__all__ = ["locate_icons"]
