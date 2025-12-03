from dataclasses import asdict, dataclass
from typing import List, Tuple

import pytesseract
from PIL import Image


@dataclass
class OcrBox:
    text: str
    x: int
    y: int
    width: int
    height: int
    conf: float

    def to_dict(self) -> dict:
        return asdict(self)


def run_ocr_with_boxes(image_path: str) -> Tuple[str, List[OcrBox]]:
    """
    Extract text and bounding boxes from the given image.

    Returns:
        full_text: the OCR'd text as a single string.
        boxes: list of OcrBox with positions and confidence scores.
    """
    image = Image.open(image_path)

    full_text = pytesseract.image_to_string(image)
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

    boxes: List[OcrBox] = []
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
                x=int(data.get("left", [])[i]),
                y=int(data.get("top", [])[i]),
                width=int(data.get("width", [])[i]),
                height=int(data.get("height", [])[i]),
                conf=conf,
            )
            boxes.append(box)
        except Exception:
            continue

    return full_text, boxes


def run_ocr(image_path: str) -> str:
    """Extract text from the given image path using Tesseract OCR."""
    full_text, _ = run_ocr_with_boxes(image_path)
    return full_text
