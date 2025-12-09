from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# -----------------------------------------
# Paths
# -----------------------------------------

ROOT = Path(__file__).resolve().parent
TEST_DIR = ROOT / "backend" / "tests"
TEST_DATA_DIR = TEST_DIR / "test_data"
SCREENSHOT_DIR = TEST_DIR / "screenshots"
WORKSPACE_DIR = TEST_DATA_DIR / "workspace"
DESKTOP_DIR = TEST_DATA_DIR / "desktop"
MOCK_PAGES_DIR = TEST_DIR / "mock_pages"

# HTML templates for browser-focused cases
SEARCH_PAGE_HTML = """
<html>
<head><title>Mock Search Page</title></head>
<body>
  <input id="search" />
  <div class="result-title">Qwen docs overview</div>
  <div class="result-title">PC Assistant quickstart</div>
</body>
</html>
"""

IMAGE_SEARCH_PAGE_HTML = """
<html>
<head><title>Mock Image Search</title></head>
<body>
  <img src="mock_volcano.jpg" id="image0" />
  <img src="mock_extra.jpg" id="image1" />
</body>
</html>
"""


# -----------------------------------------
# Helpers
# -----------------------------------------

def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def write_binary(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def draw_text_image(label: str, path: Path, size=(360, 200)) -> None:
    """Create a small PNG with centered text for vision tests."""
    img = Image.new("RGB", size, color=(245, 245, 245))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:  # pragma: no cover - fallback only
        font = None
    text = label.strip()
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (size[0] - text_width) / 2
    y = (size[1] - text_height) / 2
    draw.text((x, y), text, fill=(30, 30, 30), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


# -----------------------------------------
# 1. Workspace data
# -----------------------------------------

def create_workspace() -> None:
    print("Creating workspace directory:", WORKSPACE_DIR)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    notes = WORKSPACE_DIR / "notes"
    backup = WORKSPACE_DIR / "backup"
    archive = WORKSPACE_DIR / "archive"
    temp = WORKSPACE_DIR / "temp"
    notes.mkdir(parents=True, exist_ok=True)
    backup.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)

    write_text(notes / "readme.txt", "hello, this is a test file.")
    write_text(temp / "move_me.txt", "move me to archive")


# -----------------------------------------
# 2. Desktop fixtures
# -----------------------------------------

def create_desktop_folders() -> None:
    print("Creating desktop fixtures...")
    (DESKTOP_DIR / "Folder1").mkdir(parents=True, exist_ok=True)
    (DESKTOP_DIR / "Folder2").mkdir(parents=True, exist_ok=True)


# -----------------------------------------
# 3. Mock browser pages
# -----------------------------------------

def create_mock_pages() -> None:
    print("Creating mock browser pages...")
    MOCK_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    write_text(MOCK_PAGES_DIR / "search.html", SEARCH_PAGE_HTML)
    write_text(MOCK_PAGES_DIR / "image_search.html", IMAGE_SEARCH_PAGE_HTML)
    write_binary(MOCK_PAGES_DIR / "mock_volcano.jpg", b"FAKEJPGDATA")
    write_binary(MOCK_PAGES_DIR / "mock_extra.jpg", b"FAKEJPGDATA2")


# -----------------------------------------
# 4. Screenshot placeholders
# -----------------------------------------

def create_screenshots() -> None:
    print("Generating placeholder screenshots...")
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    draw_text_image("Confirm", SCREENSHOT_DIR / "confirm_btn.png")
    draw_text_image("Start download", SCREENSHOT_DIR / "download_btn.png")
    draw_text_image("Settings | Advanced settings | System settings", SCREENSHOT_DIR / "settings_multi.png")


# -----------------------------------------
# Entrypoint
# -----------------------------------------

def main() -> None:
    print("\nPreparing test environment...\n")
    create_workspace()
    create_desktop_folders()
    create_mock_pages()
    create_screenshots()
    print("\nTest assets ready.")
    print(f"Screenshots: {SCREENSHOT_DIR}")
    print(f"Mock pages: {MOCK_PAGES_DIR}")
    print(f"Workspace: {WORKSPACE_DIR}")
    print(f"Desktop fixtures: {DESKTOP_DIR}")


if __name__ == "__main__":
    main()
