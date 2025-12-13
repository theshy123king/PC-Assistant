from urllib.parse import parse_qs, unquote_plus, urlparse

from backend.app import _maybe_rewrite_web_search


def test_web_search_rewrite_falls_back_to_user_text():
    user_text = "打开 Edge 搜索 Python 教程并告诉我第一条结果的标题"
    plan = {
        "task": "demo",
        "steps": [{"action": "open_app", "params": {"target": "explorer"}}],
    }

    rewritten = _maybe_rewrite_web_search(user_text, plan)
    steps = rewritten["steps"]
    assert steps[0]["action"] == "open_url"
    assert steps[0]["params"]["browser"] == "edge"

    url = steps[0]["params"]["url"]
    parsed = urlparse(url)
    assert "bing.com" in parsed.netloc
    qs = parse_qs(parsed.query)
    assert unquote_plus(qs["q"][0]) == "Python 教程"
    assert rewritten.get("_rewrite_reason") == "visual_web_search_v3_cleaned"


def test_web_search_rewrite_skips_without_web_context():
    user_text = "在文档里搜索 Python 教程"
    plan = {
        "task": "search local doc",
        "steps": [{"action": "type_text", "params": {"text": "search inside document"}}],
    }
    original_steps = list(plan["steps"])

    rewritten = _maybe_rewrite_web_search(user_text, plan)
    assert rewritten["steps"] == original_steps


def test_web_search_rewrite_prefers_chrome_for_google_browser():
    user_text = "打开谷歌浏览器搜索python教程"
    plan = {
        "task": "demo",
        "steps": [{"action": "open_app", "params": {"target": "谷歌浏览器"}}],
    }

    rewritten = _maybe_rewrite_web_search(user_text, plan)
    steps = rewritten["steps"]
    assert steps[0]["action"] == "open_url"
    assert steps[0]["params"]["browser"] == "chrome"

    url = steps[0]["params"]["url"]
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert unquote_plus(qs["q"][0]) == "python教程"
