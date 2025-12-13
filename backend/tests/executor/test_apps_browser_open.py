import backend.executor.apps as apps


def test_normalize_app_target_key_edge_browser():
    assert apps._normalize_app_target_key("edge") == "msedge"
    assert apps._normalize_app_target_key("msedge") == "msedge"
    assert apps._normalize_app_target_key("microsoft edge") == "msedge"
    assert apps._normalize_app_target_key("edge\u6d4f\u89c8\u5668") == "msedge"


def test_select_best_resolution_ignores_untrusted_edge_cache(monkeypatch):
    msedge_path = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    bad_cached_path = r"E:\edge\AS\bin\studio64.exe"

    def fake_get_cached_path(key: str):
        if key == "msedge":
            return {"path": bad_cached_path, "kind": "uwp"}
        return None

    def fake_get_cached_alias(query: str):
        if query == "msedge":
            return {"target": "msedge", "path": bad_cached_path, "kind": "uwp"}
        return None

    monkeypatch.setattr(apps, "get_cached_path", fake_get_cached_path)
    monkeypatch.setattr(apps, "get_cached_alias", fake_get_cached_alias)
    monkeypatch.setattr(apps, "_find_uwp_app", lambda _key: None)
    monkeypatch.setattr(apps, "_search_registry", lambda _target, _key: [])
    monkeypatch.setattr(apps, "APP_PATHS", {"msedge": msedge_path})
    monkeypatch.setattr(apps.os.path, "isfile", lambda p: str(p) == msedge_path)

    resolution, _terms, _logs = apps._select_best_resolution("edge", "edge", {})
    assert resolution and resolution["matches"]
    assert resolution["matches"][0]["path"] == msedge_path

