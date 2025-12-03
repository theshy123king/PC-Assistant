from pathlib import Path

from PIL import Image, ImageDraw

from backend.vision.icon_locator import locate_icons


def test_locate_icons_matches_template(tmp_path: Path):
    base = tmp_path / "base.png"
    tpl = tmp_path / "tpl.png"

    img = Image.new("L", (20, 20), color=255)
    draw = ImageDraw.Draw(img)
    draw.rectangle((5, 5, 9, 9), fill=0)
    img.save(base)

    Image.new("L", (5, 5), color=0).save(tpl)

    matches = locate_icons(str(base), {"dot": str(tpl)}, threshold=0.8)

    assert matches
    assert matches[0]["name"] == "dot"
    assert matches[0]["score"] >= 0.8
