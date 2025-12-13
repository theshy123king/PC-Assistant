"""
App launching helpers for Windows.

Uses a small registry of common applications mapped to their executable paths.
All errors are reported as strings to keep callers resilient.
"""

import os
import subprocess
import shutil
import difflib
import time
import json
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

try:
    import winreg  # type: ignore
except Exception:  # pragma: no cover - non-Windows environments
    winreg = None  # type: ignore

ALIAS_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "app_alias_cache.json"
PATH_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "app_path_cache.json"
_ALIAS_LOCK = threading.Lock()
_PATH_LOCK = threading.Lock()

APP_PATHS: Dict[str, str] = {
    "chrome": r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "chrome_x86": r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "edge": r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "edge_x86": r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "msedge": r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "microsoft edge": r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "notepad": r"C:\\Windows\\System32\\notepad.exe",
    "记事本": r"C:\\Windows\\System32\\notepad.exe",
    "notepad.exe": r"C:\\Windows\\System32\\notepad.exe",
    "explorer": r"C:\\Windows\\explorer.exe",
    "explorer.exe": r"C:\\Windows\\explorer.exe",
    "file explorer": r"C:\\Windows\\explorer.exe",
    "windows explorer": r"C:\\Windows\\explorer.exe",
    "win explorer": r"C:\\Windows\\explorer.exe",
    "文件管理器": r"C:\\Windows\\explorer.exe",
    "资源管理器": r"C:\\Windows\\explorer.exe",
}

_local_app_data = os.environ.get("LOCALAPPDATA")
if _local_app_data:
    _chrome_user = str(Path(_local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe")
    APP_PATHS.setdefault("chrome_user", _chrome_user)

WECHAT_BRIDGE_APPID = r"{6D809377-6AF0-444B-8957-A3773F02200E}\\Tencent\\Weixin\\Weixin.exe"

def _normalize_alias_key(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    return value.strip().lower()

def _load_alias_cache() -> Dict[str, dict]:
    path = ALIAS_CACHE_PATH
    try:
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}

def _save_alias_cache(cache: Dict[str, dict]) -> None:
    try:
        if not ALIAS_CACHE_PATH.parent.exists():
            ALIAS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = ALIAS_CACHE_PATH.with_suffix(f".tmp.{uuid.uuid4().hex}")
        tmp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(ALIAS_CACHE_PATH)
    except Exception:
        return

def get_cached_alias(query: Optional[str]) -> Optional[dict]:
    key = _normalize_alias_key(query)
    if not key:
        return None
    with _ALIAS_LOCK:
        cache = _load_alias_cache()
        entry = cache.get(key)
        return dict(entry) if isinstance(entry, dict) else None

def set_cached_alias(query: Optional[str], target: str, path: str, kind: str) -> None:
    key = _normalize_alias_key(query)
    if not key:
        return
    entry = {"target": target, "path": path, "kind": kind}
    with _ALIAS_LOCK:
        cache = _load_alias_cache()
        cache[key] = entry
        _save_alias_cache(cache)

def _load_path_cache() -> Dict[str, dict]:
    path = PATH_CACHE_PATH
    try:
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}

def _save_path_cache(cache: Dict[str, dict]) -> None:
    try:
        if not PATH_CACHE_PATH.parent.exists():
            PATH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = PATH_CACHE_PATH.with_suffix(f".tmp.{uuid.uuid4().hex}")
        tmp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(PATH_CACHE_PATH)
    except Exception:
        return

def get_cached_path(query: Optional[str]) -> Optional[dict]:
    key = _normalize_alias_key(query)
    if not key:
        return None
    with _PATH_LOCK:
        cache = _load_path_cache()
        entry = cache.get(key)
        return dict(entry) if isinstance(entry, dict) else None

def set_cached_path(query: Optional[str], path: str, kind: str) -> None:
    key = _normalize_alias_key(query)
    if not key:
        return
    entry = {"path": path, "kind": kind}
    with _PATH_LOCK:
        cache = _load_path_cache()
        cache[key] = entry
        _save_path_cache(cache)

def _fuzzy_best_window(target: str, windows: List[Any]) -> Tuple[float, Any]:
    target_lower = target.lower()
    best_score = -1.0
    best_win = None
    for win in windows:
        title = (getattr(win, "title", "") or "").strip()
        if not title:
            continue
        title_lower = title.lower()
        score = 1.0 if target_lower in title_lower else difflib.SequenceMatcher(
            None, target_lower, title_lower
        ).ratio()
        if score > best_score:
            best_score = score
            best_win = win
    return best_score, best_win

def _best_window_for_terms(terms: List[str], windows: List[Any]) -> Tuple[float, Any, str]:
    best_score = -1.0
    best_win = None
    best_term = ""
    for term in terms:
        score, win = _fuzzy_best_window(term, windows)
        if score > best_score:
            best_score = score
            best_win = win
            best_term = term
    return best_score, best_win, best_term

def _run_powershell_json(command: str) -> Optional[Any]:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            check=False,
            text=True,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    raw = completed.stdout.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def _find_uwp_app(target_key: str) -> Optional[dict]:
    escaped = target_key.replace("'", "''")
    startapps_cmd = (
        f"$t=[regex]::Escape('{escaped}'); "
        "Get-StartApps | Where-Object { $_.Name -match $t -or $_.AppID -match $t } "
        "| Select-Object -First 1 Name,AppID | ConvertTo-Json -Compress"
    )
    result = _run_powershell_json(startapps_cmd)
    if isinstance(result, dict) and result.get("AppID"):
        appid = result.get("AppID")
        kind = "uwp"
        if target_key == "wechat" and appid and "\\" in appid and "weixin.exe" in appid.lower():
            kind = "bridge"
        return {"name": result.get("Name"), "appid": appid, "kind": kind}

    appx_cmd = (
        f"Get-AppxPackage *{escaped}* | "
        "Select-Object -First 1 Name,PackageFamilyName | ConvertTo-Json -Compress"
    )
    result = _run_powershell_json(appx_cmd)
    if isinstance(result, dict) and result.get("PackageFamilyName"):
        appid = f"{result['PackageFamilyName']}!App"
        return {"name": result.get("Name"), "appid": appid, "kind": "uwp"}
    return None

def _find_wechat_bridge_appid() -> Optional[str]:
    escaped_appid = WECHAT_BRIDGE_APPID.replace("'", "''")
    ps_cmd = (
        "$appid = '{appid}'; "
        "Get-StartApps | Where-Object {{ $_.AppID -eq $appid }} "
        "| Select-Object -First 1 -ExpandProperty AppID "
        "| ConvertTo-Json -Compress"
    ).format(appid=escaped_appid)
    data = _run_powershell_json(ps_cmd)
    if isinstance(data, str) and data:
        return data
    return None

def _score(name: str, needle: str) -> float:
    name_l = name.lower()
    if name_l == needle:
        return 3.0
    if name_l.startswith(needle):
        return 2.5
    if needle in name_l:
        return 2.0
    return difflib.SequenceMatcher(None, needle, name_l).ratio()

def _collect_roots() -> List[str]:
    roots = []
    for env_key in ["PROGRAMFILES", "PROGRAMFILES(X86)"]:
        val = os.getenv(env_key)
        if val and os.path.isdir(val):
            roots.append(val)
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        for sub in ["Programs", os.path.join("Microsoft", "WindowsApps")]:
            candidate = os.path.join(local_appdata, sub)
            if os.path.isdir(candidate):
                roots.append(candidate)
    return roots

def _looks_like_filesystem_path(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        if os.path.isabs(value):
            return True
    except Exception:
        pass
    lowered = value.lower()
    # Drive-letter paths like C:\..., UNC paths, or Unix-style paths.
    if len(lowered) >= 3 and lowered[1:3] == ":\\":
        return True
    if lowered.startswith("\\\\") or lowered.startswith("//"):
        return True
    if "/" in lowered or "\\" in lowered:
        return True
    return False


def _normalize_app_target_key(raw: str) -> str:
    """
    Normalize common app names into stable keys to reduce LLM ambiguity.

    In particular, treat "edge浏览器"/"Microsoft Edge" as "msedge" so it resolves
    reliably to the actual browser instead of unrelated apps containing "edge".
    """
    key = (raw or "").strip().lower()
    if not key:
        return key

    browser_hints = ("浏览器" in key) or ("browser" in key)

    if key in {"edge", "msedge", "microsoft edge"}:
        return "msedge"
    if ("微软edge" in key) or ("微软 edge" in key):
        return "msedge"

    if browser_hints and ("edge" in key or "msedge" in key or "microsoft edge" in key or "微软" in key):
        return "msedge"
    if browser_hints and ("chrome" in key or "google chrome" in key or "谷歌" in key or "google" in key):
        return "chrome"
    if browser_hints and ("firefox" in key or "火狐" in key):
        return "firefox"
    if browser_hints and "safari" in key:
        return "safari"

    return key


def _trusted_cached_match(target_key: str, cached_path: str, cached_kind: str) -> bool:
    key = (target_key or "").strip().lower()
    kind = (cached_kind or "win32").strip().lower()
    path = str(cached_path or "")
    if not path:
        return False
    path_lower = path.lower()
    basename = os.path.basename(path_lower)
    base_no_ext = os.path.splitext(basename)[0]

    if key in {"edge", "msedge", "microsoft edge"}:
        if kind == "win32":
            return "msedge" in base_no_ext
        if kind == "uwp":
            if _looks_like_filesystem_path(path):
                return False
            return ("msedge" in path_lower) or ("microsoftedge" in path_lower)
        return False

    if key in {"chrome", "google chrome"}:
        if kind == "win32":
            return "chrome" in base_no_ext
        if kind == "uwp":
            if _looks_like_filesystem_path(path):
                return False
            return "chrome" in path_lower
        return False

    return True


def _prefer_path_score(path: str, base_score: float, target_key: str) -> float:
    name = os.path.splitext(os.path.basename(path))[0].lower()
    penalty_keywords = ["update", "installer", "uninstall", "crash", "helper", "dbg", "launcher"]
    score = base_score
    for kw in penalty_keywords:
        if kw in name:
            score -= 0.5
    if target_key == "wechat":
        if name == "wechat":
            score += 1.5
        elif name.startswith("wechat") and "launcher" not in name:
            score += 0.5
        if "launcher" in name:
            score -= 1.5
    if target_key in {"edge", "msedge", "microsoft edge"} and name == "msedge":
        score += 4.0
    if target_key in {"chrome", "google chrome"} and name == "chrome":
        score += 4.0
    return score

def _search_registry(target: str, target_key: str) -> List[str]:
    if not winreg:
        return []
    paths = []
    key_names = [f"{target}.exe", f"{target_key}.exe", target, target_key]
    hives = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for hive, base in hives:
        try:
            with winreg.OpenKey(hive, base) as base_key:
                for key_name in key_names:
                    try:
                        with winreg.OpenKey(base_key, key_name) as sub:
                            val, _ = winreg.QueryValueEx(sub, None)
                            if val and os.path.isfile(val):
                                paths.append(val)
                    except OSError:
                        continue
        except OSError:
            continue
    return paths

def _gather_matches(target_key: str, user_query: str, params: dict) -> Tuple[List[dict], List[str], List[dict]]:
    logs: List[dict] = []
    params = dict(params or {})
    normalized_key = _normalize_app_target_key(target_key)
    if normalized_key and normalized_key != target_key:
        logs.append({"candidate": target_key, "source": "normalize_target", "normalized": normalized_key})
        target_key = normalized_key

    path_entry = get_cached_path(target_key)
    cached_path_candidates: List[Tuple[float, str, str]] = []
    if path_entry and isinstance(path_entry, dict):
        cached_path = path_entry.get("path")
        cached_kind = path_entry.get("kind") or "win32"
        if cached_path and (os.path.isfile(cached_path) or cached_kind in {"uwp", "bridge"}):
            if not _trusted_cached_match(target_key, str(cached_path), str(cached_kind)):
                logs.append(
                    {
                        "candidate": target_key,
                        "source": "path_cache_ignored",
                        "path": cached_path,
                        "kind": cached_kind,
                        "reason": "untrusted_for_target",
                    }
                )
            else:
                cached_path_candidates.append((6.0, cached_path, cached_kind))
                logs.append({"candidate": target_key, "source": "path_cache", "path": cached_path, "kind": cached_kind})

    cached = get_cached_alias(user_query) or get_cached_alias(target_key)
    cached_candidates: List[Tuple[float, str, str]] = []
    if cached:
        cached_path = cached.get("path")
        cached_kind = cached.get("kind") or "win32"
        cached_target = cached.get("target") or target_key
        if cached_path and _trusted_cached_match(target_key, str(cached_path), str(cached_kind)):
            if cached_target:
                target_key = str(cached_target).lower().strip()
            cached_candidates.append((5.0, cached_path, cached_kind))
            logs.append({"candidate": target_key, "source": "alias_cache", "path": cached_path, "kind": cached_kind})
        else:
            logs.append(
                {
                    "candidate": target_key,
                    "source": "alias_cache_ignored",
                    "path": cached_path,
                    "kind": cached_kind,
                    "reason": "untrusted_for_target",
                }
            )

    if target_key in {"notepad", "notepad.exe", "记事本"}:
        params["target"] = r"C:\\Windows\\System32\\notepad.exe"
        target_key = params["target"].lower()

    search_terms = [target_key]
    if target_key == "wechat":
        for alias in ["wechat", "微信", "weixin"]:
            if alias not in search_terms:
                search_terms.append(alias)
    if target_key in {"edge", "msedge", "microsoft edge"}:
        for alias in ["edge", "msedge", "microsoft edge"]:
            if alias not in search_terms:
                search_terms.append(alias)

    if target_key == "wechat":
        bridge_appid = _find_wechat_bridge_appid()
        if bridge_appid:
            matches = [{"score": 10.0, "path": bridge_appid, "kind": "bridge"}]
            logs.append({"candidate": target_key, "source": "wechat_bridge", "path": bridge_appid, "kind": "bridge"})
            return matches, search_terms, logs

    candidates: List[Tuple[float, str, str]] = []
    if cached_path_candidates:
        candidates.extend(cached_path_candidates)
    if cached_candidates:
        candidates.extend(cached_candidates)
    known_extra = [
        r"C:\\Program Files\\Tencent\\WeChat\\WeChat.exe",
        r"C:\\Program Files (x86)\\Tencent\\WeChat\\WeChat.exe",
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Tencent", "WeChat", "WeChat.exe"),
    ]
    for alias, path in APP_PATHS.items():
        if target_key in alias or target_key in os.path.basename(path).lower():
            if os.path.isfile(path):
                candidates.append((_score(alias, target_key), path, "win32"))
    for path in known_extra:
        if path and os.path.isfile(path):
            candidates.append((_score(os.path.basename(path), target_key), path, "win32"))

    if not candidates:
        time_budget = time.time() + 3.0
        for root in _collect_roots():
            for dirpath, _dirnames, filenames in os.walk(root):
                if time.time() > time_budget:
                    break
                for fname in filenames:
                    if not fname.lower().endswith(".exe"):
                        continue
                    name_no_ext = os.path.splitext(fname)[0]
                    if target_key in name_no_ext.lower():
                        full_path = os.path.join(dirpath, fname)
                        candidates.append((_score(name_no_ext, target_key), full_path, "win32"))
                _dirnames[:] = _dirnames[:20]
            if candidates:
                break

    which_path = shutil.which(target_key) or shutil.which(f"{target_key}.exe")
    if which_path:
        candidates.append((_score(os.path.basename(which_path), target_key), which_path, "win32"))

    for reg_path in _search_registry(target_key, target_key):
        candidates.append((_score(os.path.basename(reg_path), target_key), reg_path, "win32"))

    uwp_info = _find_uwp_app(target_key)
    if uwp_info and uwp_info.get("appid"):
        uwp_appid = uwp_info["appid"]
        uwp_name = str(uwp_info.get("name") or "")
        uwp_kind = uwp_info.get("kind") or "uwp"
        if uwp_name:
            lower_name = uwp_name.lower()
            if lower_name and lower_name not in search_terms:
                search_terms.append(lower_name)
        if target_key == "wechat":
            for alias in ["wechat", "微信", "weixin", uwp_name.lower()]:
                if alias and alias not in search_terms:
                    search_terms.append(alias)
        base_score = 4.5 if uwp_kind == "bridge" else 3.5
        candidates.append((base_score, uwp_appid, uwp_kind))

    dedup: Dict[str, Tuple[float, str, str]] = {}
    for sc, p, kind in candidates:
        if kind == "uwp":
            norm = f"uwp:{p.lower()}"
            adjusted_score = sc
        elif kind == "bridge":
            norm = f"bridge:{p.lower()}"
            adjusted_score = sc + 1.0
        else:
            norm = f"win32:{os.path.normcase(os.path.abspath(p))}"
            adjusted_score = _prefer_path_score(p, sc, target_key)
        if norm not in dedup or adjusted_score > dedup[norm][0]:
            dedup[norm] = (adjusted_score, p, kind)

    sorted_candidates = sorted(dedup.values(), key=lambda item: item[0], reverse=True)
    matches = [{"score": sc, "path": p, "kind": kind} for sc, p, kind in sorted_candidates]
    logs.append({"candidate": target_key, "source": "resolver", "match_count": len(matches)})
    return matches, search_terms, logs

def _build_candidate_keys(primary: str) -> List[Tuple[str, str]]:
    alias_map = {
        "file explorer": "explorer",
        "windows explorer": "explorer",
        "win explorer": "explorer",
        "文件管理器": "explorer",
        "资源管理器": "explorer",
        "explorer.exe": "explorer",
        "weixin": "wechat",
        "微信": "wechat",
    }
    primary_key = primary.strip().lower()
    localized = [k for k in alias_map if not k.isascii() and alias_map[k] == primary_key]
    english = [k for k in alias_map if k.isascii() and alias_map[k] == primary_key]
    abbreviations = [k for k in alias_map if len(k) <= 4 and alias_map[k] == primary_key]

    ordered: List[Tuple[str, str]] = []
    seen = set()
    for name, cat in [(primary_key, "primary")] + [(k, "localized") for k in localized] + [(k, "english") for k in english] + [(k, "abbr") for k in abbreviations]:
        if name in seen:
            continue
        seen.add(name)
        ordered.append((name, cat))
    return ordered or [(primary_key, "primary")]

def _select_best_resolution(target: str, user_query: str, params: dict) -> Tuple[Optional[dict], List[str], List[dict]]:
    target_key = _normalize_app_target_key(str(target))
    candidates = _build_candidate_keys(target_key)

    async def _run_all():
        import asyncio

        async def _resolve(name: str, cat: str):
            try:
                matches, terms, logs = await asyncio.to_thread(_gather_matches, name, user_query, params)
                return {"name": name, "category": cat, "matches": matches, "search_terms": terms, "logs": logs}
            except Exception as exc:  # noqa: BLE001
                return {"name": name, "category": cat, "matches": [], "search_terms": [name], "logs": [{"candidate": name, "error": str(exc)}]}

        tasks = [_resolve(name, cat) for name, cat in candidates]
        return await asyncio.gather(*tasks)

    import asyncio

    results = asyncio.run(_run_all())

    all_logs: List[dict] = []
    first_match = None
    search_terms: List[str] = []
    for name, cat in candidates:
        res = next((r for r in results if r["name"] == name and r["category"] == cat), None)
        if res:
            all_logs.extend(res.get("logs") or [])
            if res.get("matches"):
                first_match = {"matches": res["matches"], "name": name, "category": cat}
                search_terms = res.get("search_terms") or [name]
                break
    if not first_match:
        matches, search_terms, extra_logs = _gather_matches(target_key, user_query, params)
        all_logs.extend(extra_logs)
        if matches:
            first_match = {"matches": matches, "name": target_key, "category": "fallback"}
    return first_match, search_terms, all_logs

def open_app(params: dict) -> dict:
    target = (params or {}).get("target")
    user_query = (params or {}).get("user_query") or target
    if not target:
        return "error: 'target' param is required"

    normalized_target = _normalize_app_target_key(str(target))
    if normalized_target and normalized_target != str(target).lower().strip():
        params = dict(params or {})
        params["target"] = normalized_target
        target = normalized_target

    resolution, search_terms, logs = _select_best_resolution(target, user_query, params)
    if not resolution or not resolution.get("matches"):
        return {"status": "error", "message": f"no executable found for '{target}'", "matches": [], "logs": logs}

    matches = resolution["matches"]
    selected = matches[0]["path"]
    selected_kind = matches[0].get("kind", "win32")
    target_key = str(target).lower().strip()

    raw_count = (params or {}).get("count", 1)
    try:
        requested_count = int(raw_count)
    except (TypeError, ValueError):
        requested_count = 1
    requested_count = max(1, requested_count)
    allow_existing_activation = requested_count == 1

    gw = None  # type: ignore
    before_windows: List[Any] = []
    before_titles = set()
    try:
        import pygetwindow as gw  # local import to avoid import cycles
        before_windows = gw.getAllWindows()
        before_titles = {(getattr(w, "title", "") or "").strip() for w in before_windows}
        if allow_existing_activation:
            score, win, _ = _best_window_for_terms(search_terms, before_windows)
            if win and score >= 0.8:
                try:
                    if getattr(win, "isMinimized", False):
                        win.restore()
                    win.activate()
                    selected_window = (getattr(win, "title", "") or "").strip()
                    return {
                        "status": "activated_existing",
                        "target": target,
                        "selected_path": selected,
                        "matches": matches[:20],
                        "window_title": selected_window,
                        "method": "existing_window",
                        "requested_count": requested_count,
                        "launched_count": 0,
                        "selected_kind": selected_kind,
                        "logs": logs,
                    }
                except Exception:
                    pass
    except Exception:
        gw = None  # type: ignore

    launched_count = 0
    launch_error: Optional[str] = None
    for _ in range(requested_count):
        try:
            if selected_kind == "uwp":
                subprocess.Popen(
                    ["explorer.exe", f"shell:AppsFolder\\{selected}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif selected_kind == "bridge":
                ps_cmd = f"Start-Process 'shell:AppsFolder\\{selected}'"
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    [selected],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            launched_count += 1
        except Exception as exc:  # noqa: BLE001
            launch_error = str(exc)
            break

    if launched_count == 0:
        return {
            "status": "error",
            "message": f"failed to launch '{target}': {launch_error or 'unknown error'}",
            "selected_path": selected,
            "matches": matches[:20],
            "requested_count": requested_count,
            "launched_count": launched_count,
            "selected_kind": selected_kind,
            "logs": logs,
        }

    selected_window = None
    if gw:
        try:
            best_win = None
            search_pool = []
            poll_attempts = 10
            poll_sleep = 0.6
            if target_key == "wechat" or selected_kind == "uwp":
                poll_attempts = 20
                poll_sleep = 0.5
            for _ in range(poll_attempts):
                time.sleep(poll_sleep)
                after_windows = gw.getAllWindows()
                new_windows = [
                    w
                    for w in after_windows
                    if ((getattr(w, "title", "") or "").strip()) not in before_titles
                ]
                if new_windows:
                    search_pool = new_windows
                    break
                search_pool = after_windows

            if search_pool:
                _, best_win, _ = _best_window_for_terms(search_terms, search_pool)
                if not best_win:
                    best_win = next(
                        (w for w in search_pool if (getattr(w, "title", "") or "").strip()),
                        None,
                    )
            if not best_win and search_pool:
                _, best_win, _ = _best_window_for_terms(search_terms, search_pool)
            if not best_win and (target_key == "wechat" or selected_kind == "uwp"):
                for _ in range(5):
                    time.sleep(1.0)
                    _, best_win, _ = _best_window_for_terms(search_terms, gw.getAllWindows())
                    if best_win:
                        break

            if best_win:
                try:
                    if getattr(best_win, "isMinimized", False):
                        best_win.restore()
                    best_win.activate()
                except Exception:
                    pass
                selected_window = (getattr(best_win, "title", "") or "").strip()
        except Exception:
            selected_window = None

    def _should_cache() -> bool:
        if selected_kind != "win32":
            return True
        base = os.path.splitext(os.path.basename(selected))[0].lower()
        return target_key in base or base in target_key

    should_cache = _should_cache()

    try:
        if should_cache:
            set_cached_alias(user_query, target_key, selected, selected_kind)
            set_cached_alias(target_key, target_key, selected, selected_kind)
    except Exception:
        pass
    try:
        if should_cache:
            set_cached_path(user_query, selected, selected_kind)
            set_cached_path(target_key, selected, selected_kind)
    except Exception:
        pass

    return {
        "status": "launched",
        "target": target,
        "selected_path": selected,
        "matches": matches[:20],
        "window_title": selected_window,
        "method": "fuzzy_search",
        "activated_window": selected_window is not None,
        "requested_count": requested_count,
        "launched_count": launched_count,
        "selected_kind": selected_kind,
        "logs": logs,
        "message": (
            f"launched {launched_count}/{requested_count}: "
            f"{launch_error or 'unknown error'}"
            if launch_error and launched_count < requested_count
            else None
        ),
    }
