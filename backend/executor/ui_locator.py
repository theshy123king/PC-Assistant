"""
Enhanced UI locator for executor-layer actions.

Provides:
- Fuzzy OCR text ranking.
- Template-based icon matching.
- Optional multimodal VLM fallback.
"""

import difflib
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from backend.vision.ocr import OcrBox
from backend.vision.icon_locator import locate_icons
from backend.vision.vlm_locator import locate_with_vlm

Box = Any
Center = Tuple[float, float]
Candidate = Dict[str, Any]


def _extract_text(box: Box) -> str:
    if isinstance(box, OcrBox):
        return (box.text or "").strip()
    if isinstance(box, dict):
        return str(box.get("text", "") or "").strip()
    return str(getattr(box, "text", "") or "").strip()


def _extract_bounds(box: Box) -> Tuple[float, float, float, float]:
    # Preferred keys
    if isinstance(box, OcrBox):
        return float(box.x), float(box.y), float(box.width), float(box.height)
    if isinstance(box, dict):
        x = float(box.get("x", 0))
        y = float(box.get("y", 0))
        w = float(box.get("width", 0))
        h = float(box.get("height", 0))
        # Support (left, top, right, bottom) form.
        if not w and "right" in box and "left" in box:
            w = float(box.get("right", 0)) - float(box.get("left", 0))
        if not h and "bottom" in box and "top" in box:
            h = float(box.get("bottom", 0)) - float(box.get("top", 0))
        return x, y, w, h
    # Fallback to attribute lookup.
    x = float(getattr(box, "x", 0) or 0)
    y = float(getattr(box, "y", 0) or 0)
    w = float(getattr(box, "width", 0) or 0)
    h = float(getattr(box, "height", 0) or 0)
    return x, y, w, h


def _center(bounds: Tuple[float, float, float, float]) -> Center:
    x, y, w, h = bounds
    return x + w / 2.0, y + h / 2.0


def _normalize_text(text: str) -> str:
    return (text or "").strip()


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _prepare_box(box: Box) -> Dict[str, Any]:
    text = _extract_text(box)
    x, y, w, h = _extract_bounds(box)
    return {
        "text": text,
        "norm_text": _normalize_text(text).lower(),
        "x": float(x),
        "y": float(y),
        "width": float(w),
        "height": float(h),
        "conf": float(getattr(box, "conf", getattr(box, "confidence", -1)) or -1),
        "source": box,
    }


def merge_similar_boxes(boxes: Iterable[Box], proximity: float = 6.0) -> List[Dict[str, Any]]:
    """
    Merge boxes that share the same normalized text into a union rectangle.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for raw in boxes:
        prepared = _prepare_box(raw)
        key = prepared["norm_text"]
        if not key:
            continue
        if key not in merged:
            merged[key] = {**prepared, "count": 1}
            continue
        entry = merged[key]
        # Union bounds with a small proximity padding.
        left = min(entry["x"], prepared["x"]) - proximity
        top = min(entry["y"], prepared["y"]) - proximity
        right = max(entry["x"] + entry["width"], prepared["x"] + prepared["width"]) + proximity
        bottom = max(entry["y"] + entry["height"], prepared["y"] + prepared["height"]) + proximity
        entry["x"] = left
        entry["y"] = top
        entry["width"] = max(0.0, right - left)
        entry["height"] = max(0.0, bottom - top)
        entry["count"] += 1
        # Average confidence.
        prev_conf = entry.get("conf", -1)
        prev_conf = prev_conf if prev_conf >= 0 else 0.0
        new_conf = max(-1.0, prepared.get("conf", -1))
        entry["conf"] = ((prev_conf * (entry["count"] - 1)) + new_conf) / entry["count"]
    return list(merged.values())


def score_text_match(target: str, candidate: str, conf: float = -1.0) -> Tuple[float, str, Dict[str, float]]:
    """
    Compute a composite fuzzy score with match category.
    """
    target_norm = _normalize_text(target).lower()
    cand_norm = _normalize_text(candidate).lower()
    ratio = difflib.SequenceMatcher(None, target_norm, cand_norm).ratio()

    substring_bonus = 0.25 if target_norm and (target_norm in cand_norm or cand_norm in target_norm) else 0.0
    prefix_bonus = 0.15 if target_norm and cand_norm.startswith(target_norm) else 0.0
    suffix_bonus = 0.1 if target_norm and cand_norm.endswith(target_norm) else 0.0
    exact = target_norm == cand_norm
    exact_bonus = 0.4 if exact else 0.0
    chinese_bonus = 0.2 if exact and _has_chinese(target_norm) else 0.0
    conf_bonus = max(0.0, conf) / 100.0 * 0.1

    score = ratio + substring_bonus + prefix_bonus + suffix_bonus + exact_bonus + chinese_bonus + conf_bonus

    if exact:
        match_type = "exact"
    elif substring_bonus > 0:
        match_type = "substring"
    elif ratio >= 0.88:
        match_type = "high_fuzzy"
    elif ratio >= 0.72:
        match_type = "medium_fuzzy"
    else:
        match_type = "low"

    details = {
        "ratio": ratio,
        "substring_bonus": substring_bonus,
        "prefix_bonus": prefix_bonus,
        "suffix_bonus": suffix_bonus,
        "exact_bonus": exact_bonus,
        "chinese_bonus": chinese_bonus,
        "conf_bonus": conf_bonus,
    }
    return score, match_type, details


def rank_text_candidates(
    target: str,
    boxes: Iterable[Box],
    high_threshold: float = 0.9,
    medium_threshold: float = 0.75,
) -> List[Candidate]:
    """
    Rank OCR boxes for a target string with enhanced scoring and fusion.
    """
    fused = merge_similar_boxes(boxes)
    candidates: List[Candidate] = []
    for item in fused:
        score, match_type, details = score_text_match(target, item["text"], conf=item.get("conf", -1))
        cx, cy = _center((item["x"], item["y"], item["width"], item["height"]))
        candidates.append(
            {
                "text": item["text"],
                "norm_text": item["norm_text"],
                "score": score,
                "match_type": match_type,
                "center": {"x": cx, "y": cy},
                "bounds": {
                    "x": item["x"],
                    "y": item["y"],
                    "width": item["width"],
                    "height": item["height"],
                },
                "conf": item.get("conf", -1),
                "count": item.get("count", 1),
                "details": details,
                "high_enough": match_type == "exact" or score >= high_threshold,
                "medium_enough": score >= medium_threshold,
                "source": item.get("source"),
            }
        )

    candidates.sort(key=lambda c: (c["high_enough"], c["medium_enough"], c["score"]), reverse=True)
    return candidates


def _pick_best_candidate(
    candidates: List[Candidate], high_threshold: float = 0.9, medium_threshold: float = 0.75
) -> Optional[Candidate]:
    for cand in candidates:
        if cand["match_type"] == "exact":
            return cand
    for cand in candidates:
        if cand["score"] >= high_threshold:
            return cand
    for cand in candidates:
        if cand["score"] >= medium_threshold and cand.get("medium_enough"):
            return cand
    return candidates[0] if candidates else None


def locate_text(target: str, boxes: Iterable[Box]) -> Optional[Tuple[Box, Center]]:
    """
    Return the best matching box and its center for the target string.

    Uses enhanced fuzzy scoring with soft thresholds; returns None when no reasonable match.
    """
    if not target:
        return None
    target_norm = target.strip()
    if not target_norm:
        return None

    candidates = rank_text_candidates(target_norm, boxes)
    best = _pick_best_candidate(candidates)
    if not best:
        return None

    bounds = best["bounds"]
    center = (bounds["x"] + bounds["width"] / 2.0, bounds["y"] + bounds["height"] / 2.0)
    return best, center


def locate_target(
    query: str,
    boxes: Iterable[Box],
    image_path: Optional[str] = None,
    image_base64: Optional[str] = None,
    icon_templates: Optional[Dict[str, str]] = None,
    vlm_call: Optional[Callable[[str, Optional[List[dict]]], str]] = None,
    vlm_provider: str = "deepseek",
    high_threshold: float = 0.9,
    medium_threshold: float = 0.75,
) -> Dict[str, Any]:
    """
    Unified locator that tries OCR text, then icon templates, then VLM.
    """
    logs: List[str] = []
    candidates = rank_text_candidates(query, boxes, high_threshold=high_threshold, medium_threshold=medium_threshold)
    if candidates:
        best = candidates[0]
        if best["high_enough"] or best["medium_enough"]:
            logs.append("method:ocr")
            return {
                "status": "success",
                "method": "ocr",
                "candidate": best,
                "center": best.get("center"),
                "bounds": best.get("bounds"),
                "score": best.get("score"),
                "log": logs,
            }
    logs.append("ocr:no_match")

    if image_path and icon_templates:
        matches = locate_icons(image_path, icon_templates)
        if matches:
            best = matches[0]
            logs.append(f"method:icon:{best.get('name')}")
            return {
                "status": "success",
                "method": "icon",
                "candidate": best,
                "center": best.get("center"),
                "bounds": best.get("bounds"),
                "score": best.get("score"),
                "log": logs,
            }
        logs.append("icon:no_match")

    if image_base64 and vlm_call:
        vlm_result = locate_with_vlm(query, image_base64, vlm_call, provider_name=vlm_provider)
        if vlm_result.get("status") == "success":
            top = vlm_result.get("top") or {}
            logs.append("method:vlm")
            return {
                "status": "success",
                "method": "vlm",
                "candidate": top,
                "center": top.get("center"),
                "bounds": top.get("bounds"),
                "provider": vlm_provider,
                "raw": vlm_result.get("raw"),
                "log": logs,
            }
        logs.append(f"vlm:{vlm_result.get('reason', 'failed')}")

    return {
        "status": "error",
        "method": None,
        "reason": "no_match_found",
        "log": logs,
    }


__all__ = ["locate_text", "rank_text_candidates", "merge_similar_boxes", "score_text_match", "locate_target"]
