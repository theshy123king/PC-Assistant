"""
Helpers to locate UI text positions from OCR boxes.

Provides:
- find_best_text_match: choose the best OcrBox for a query using exact/substring/fuzzy match.
- get_box_center: compute the center point of a box.
- locate_text: convenience that returns (best_box, (cx, cy)) or None.
"""

import difflib
from typing import Iterable, List, Optional, Tuple

from backend.vision.ocr import OcrBox


def _score(query: str, candidate: str, conf: float) -> float:
    """Compute a composite score using exact/substring/fuzzy match plus confidence."""
    q = query.lower()
    c = candidate.lower()

    if not q or not c:
        return -1.0

    if q == c:
        base = 2.0
    elif q in c or c in q:
        base = 1.5
    else:
        base = difflib.SequenceMatcher(None, q, c).ratio()

    conf_bonus = max(0.0, conf) / 100.0 * 0.1  # small weight for OCR confidence
    return base + conf_bonus


def find_best_text_match(query: str, boxes: Iterable[OcrBox]) -> Optional[OcrBox]:
    """
    Return the OcrBox that best matches query (case-insensitive), or None if none match.
    """
    if not query:
        return None

    best: Optional[OcrBox] = None
    best_score = -1.0

    for box in boxes:
        text = (box.text or "").strip()
        if not text:
            continue
        score = _score(query, text, box.conf)
        if score > best_score:
            best_score = score
            best = box

    return best


def get_box_center(box: OcrBox) -> Tuple[float, float]:
    """Return (cx, cy) for the box center."""
    return box.x + box.width / 2.0, box.y + box.height / 2.0


def locate_text(query: str, boxes: List[OcrBox]) -> Optional[Tuple[OcrBox, Tuple[float, float]]]:
    """
    Find the best box and its center for the given query.

    Returns:
        (best_box, (cx, cy)) or None if no match found.
    """
    best = find_best_text_match(query, boxes)
    if not best:
        return None
    return best, get_box_center(best)


__all__ = ["find_best_text_match", "get_box_center", "locate_text"]
