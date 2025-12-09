from backend.executor.ui_locator import locate_target, rank_text_candidates
from backend.vision.ocr import OcrBox


def test_locate_target_prefers_ocr_match():
    boxes = [OcrBox(text="Save", x=10, y=10, width=10, height=10, conf=90.0)]

    result = locate_target("Save", boxes)

    assert result["method"] == "ocr"
    assert result["status"] == "success"


def test_locate_target_uses_icon_fallback(tmp_path):
    boxes = []
    base = tmp_path / "base.png"
    tpl = tmp_path / "tpl.png"
    # simple white image with small black square
    from PIL import Image, ImageDraw

    img = Image.new("L", (20, 20), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle((5, 5, 9, 9), fill=0)
    img.save(base)
    Image.new("L", (5, 5), color=0).save(tpl)

    result = locate_target(
        "nonexistent",
        boxes,
        image_path=str(base),
        icon_templates={"dot": str(tpl)},
    )

    assert result["method"] == "icon"
    assert result["status"] == "success"


def test_locate_target_uses_vlm_when_available(monkeypatch):
    boxes = []

    def fake_vlm(prompt, messages):
        return '{"boxes":[{"label":"btn","bbox":{"x":1,"y":2,"width":3,"height":4}}]}'

    result = locate_target(
        "describe button",
        boxes,
        image_base64="abc",
        vlm_call=fake_vlm,
        vlm_provider="doubao",
    )

    assert result["method"] == "vlm"
    assert result["status"] == "success"
    assert result["bounds"] == {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}
