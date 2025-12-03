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
from typing import Dict, List, Tuple, Any, Optional

try:
    import winreg  # type: ignore
except Exception:  # pragma: no cover - non-Windows environments
    winreg = None  # type: ignore

# Known application paths on typical Windows installations.
APP_PATHS: Dict[str, str] = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "chrome_x86": r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "edge_x86": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "msedge": r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "microsoft edge": r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "notepad": r"C:\Windows\System32\notepad.exe",
    "记事本": r"C:\Windows\System32\notepad.exe",
    "notepad.exe": r"C:\Windows\System32\notepad.exe",
    "explorer": r"C:\Windows\explorer.exe",
}

WECHAT_BRIDGE_APPID = r"{6D809377-6AF0-444B-8957-A3773F02200E}\Tencent\Weixin\Weixin.exe"


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
    """Pick the best window across multiple search terms (handles localized titles)."""
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
    """Run a PowerShell command and parse JSON output."""
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
    """
    Locate a UWP/Store app matching the target.

    Strategy:
    - Use Get-StartApps to fetch AUMID directly (preferred).
    - Fallback to Get-AppxPackage to build AUMID from PackageFamilyName.
    """
    # Escape single quotes for PowerShell single-quoted strings.
    escaped = target_key.replace("'", "''")
    # Prefer StartApps (returns AUMID in AppID).
    startapps_cmd = (
        f"$t=[regex]::Escape('{escaped}'); "
        "Get-StartApps | Where-Object { $_.Name -match $t -or $_.AppID -match $t } "
        "| Select-Object -First 1 Name,AppID | ConvertTo-Json -Compress"
    )
    result = _run_powershell_json(startapps_cmd)
    if isinstance(result, dict) and result.get("AppID"):
        appid = result.get("AppID")
        kind = "uwp"
        # Desktop Bridge (MSIX) WeChat exposes an AppID that looks like a path into AppsFolder.
        if target_key == "wechat" and appid and "\\" in appid and "weixin.exe" in appid.lower():
            kind = "bridge"
        return {"name": result.get("Name"), "appid": appid, "kind": kind}

    # Fallback to AppxPackage to build AUMID.
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
    """Detect the Desktop Bridge/MSIX WeChat AppID via Get-StartApps."""
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


def open_app(params: dict) -> dict:
    """
    Launch an application by target name using subprocess.Popen.

    Expected params:
        target: str - key in APP_PATHS.
        count: Optional[int] - number of instances to launch (defaults to 1).
    Supports Win32 executables, UWP/Store apps (via explorer.exe shell:AppsFolder\AUMID),
    and Desktop Bridge/MSIX apps (via Start-Process shell:AppsFolder\<AppID>).
    Returns a structured status dict (or string) describing the attempt.
    """
    target = (params or {}).get("target")
    if not target:
        return "error: 'target' param is required"

    target_key = str(target).lower().strip()
    # Force built-in Notepad for known aliases.
    if target_key in {"notepad", "notepad.exe", "记事本"}:
        params = dict(params or {})
        params["target"] = r"C:\Windows\System32\notepad.exe"
        target_key = params["target"].lower()
    # Include localized or alias terms for window detection (important for WeChat and UWP names).
    search_terms = [target_key]
    if target_key == "wechat":
        for alias in ["wechat", "微信", "weixin"]:
            if alias not in search_terms:
                search_terms.append(alias)
    if target_key in {"edge", "msedge", "microsoft edge"}:
        for alias in ["edge", "msedge", "microsoft edge"]:
            if alias not in search_terms:
                search_terms.append(alias)

    # Special-case the Desktop Bridge WeChat AppID and treat it as authoritative.
    if target_key == "wechat":
        bridge_appid = _find_wechat_bridge_appid()
        if bridge_appid:
            matches = [{"score": 10.0, "path": bridge_appid, "kind": "bridge"}]
            ps_launch = (
                f"Start-Process 'shell:AppsFolder\\{bridge_appid}' -PassThru "
                "| Select-Object -First 1 -ExpandProperty Id"
            )
            try:
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_launch],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "status": "error",
                    "message": f"failed to launch WeChat bridge app: {exc}",
                    "selected_path": bridge_appid,
                    "matches": matches,
                    "requested_count": 1,
                    "launched_count": 0,
                    "selected_kind": "bridge",
                    "method": "wechat_bridge_appid",
                }

            pid_val: Optional[int] = None
            if completed.stdout:
                try:
                    pid_val = int(str(completed.stdout).strip())
                except Exception:
                    pid_val = None

            if completed.returncode != 0:
                return {
                    "status": "error",
                    "message": (
                        f"failed to launch WeChat bridge app (exit {completed.returncode}): "
                        f"{completed.stderr.strip() if completed.stderr else ''}"
                    ),
                    "selected_path": bridge_appid,
                    "matches": matches,
                    "requested_count": 1,
                    "launched_count": 0,
                    "selected_kind": "bridge",
                    "method": "wechat_bridge_appid",
                    "pid": pid_val,
                }

            return {
                "status": "launched_with_bridge_appid",
                "target": target,
                "selected_path": bridge_appid,
                "matches": matches,
                "window_title": None,
                "method": "wechat_bridge_appid",
                "activated_window": False,
                "requested_count": 1,
                "launched_count": 1,
                "selected_kind": "bridge",
                "pid": pid_val,
            }

    raw_count = (params or {}).get("count", 1)
    try:
        requested_count = int(raw_count)
    except (TypeError, ValueError):
        requested_count = 1
    requested_count = max(1, requested_count)
    allow_existing_activation = requested_count == 1

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

    def _prefer_path_score(path: str, base_score: float) -> float:
        """Penalize obvious launchers/installers, favor exact exe name."""
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
        return score

    def _search_registry() -> List[str]:
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

    # Gather candidates from known paths (including common WeChat installs).
    candidates: List[Tuple[float, str, str]] = []  # (score, launch_target, kind)
    known_extra = [
        r"C:\Program Files\Tencent\WeChat\WeChat.exe",
        r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Tencent", "WeChat", "WeChat.exe"),
    ]
    for alias, path in APP_PATHS.items():
        if target_key in alias or target_key in os.path.basename(path).lower():
            if os.path.isfile(path):
                candidates.append((_score(alias, target_key), path, "win32"))
    for path in known_extra:
        if path and os.path.isfile(path):
            candidates.append((_score(os.path.basename(path), target_key), path, "win32"))

    # Walk common install roots for matching executables (time-bounded).
    if not candidates:
        time_budget = time.time() + 3.0  # seconds
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

    # Search PATH
    which_path = shutil.which(target) or shutil.which(f"{target}.exe")
    if which_path:
        candidates.append((_score(os.path.basename(which_path), target_key), which_path, "win32"))

    # Registry App Paths
    for reg_path in _search_registry():
        candidates.append((_score(os.path.basename(reg_path), target_key), reg_path, "win32"))

    # UWP/Store detection (includes Desktop Bridge/MSIX variants like WeChat AppEx).
    uwp_info = _find_uwp_app(target_key)
    if uwp_info and uwp_info.get("appid"):
        uwp_appid = uwp_info["appid"]
        uwp_name = str(uwp_info.get("name") or "")
        uwp_kind = uwp_info.get("kind") or "uwp"
        if uwp_name:
            lower_name = uwp_name.lower()
            if lower_name and lower_name not in search_terms:
                search_terms.append(lower_name)
        # Promote UWP/Bridge WeChat specific aliases.
        if target_key == "wechat":
            for alias in ["wechat", "微信", "weixin", uwp_name.lower()]:
                if alias and alias not in search_terms:
                    search_terms.append(alias)
        # Boost bridge variant to override Win32/UWP.
        base_score = 4.5 if uwp_kind == "bridge" else 3.5
        candidates.append((base_score, uwp_appid, uwp_kind))

    # Deduplicate by normalized path.
    dedup: Dict[str, Tuple[float, str, str]] = {}
    for sc, p, kind in candidates:
        if kind == "uwp":
            norm = f"uwp:{p.lower()}"
            adjusted_score = sc
        elif kind == "bridge":
            norm = f"bridge:{p.lower()}"
            adjusted_score = sc + 1.0  # prioritize bridge variant
        else:
            norm = f"win32:{os.path.normcase(os.path.abspath(p))}"
            adjusted_score = _prefer_path_score(p, sc)
        if norm not in dedup or adjusted_score > dedup[norm][0]:
            dedup[norm] = (adjusted_score, p, kind)

    sorted_candidates = sorted(dedup.values(), key=lambda item: item[0], reverse=True)
    matches = [{"score": sc, "path": p, "kind": kind} for sc, p, kind in sorted_candidates]

    if not matches:
        return {
            "status": "error",
            "message": f"no executable found for '{target}'",
            "matches": [],
        }

    selected = matches[0]["path"]
    selected_kind = matches[0].get("kind", "win32")
    gw = None  # type: ignore

    # Capture windows before launch and try to reuse existing instance.
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
        }

    selected_window = None
    if gw:
        try:
            best_win = None
            search_pool = []
            # Poll for UI creation; allow extra time for apps (like WeChat or UWP) that spawn via launchers.
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

            # If we detected new windows, prefer the first non-empty title.
            if search_pool:
                _, best_win, _ = _best_window_for_terms(search_terms, search_pool)
                if not best_win:
                    best_win = next(
                        (w for w in search_pool if (getattr(w, "title", "") or "").strip()),
                        None,
                    )
            # Fallback: fuzzy match over all windows if nothing new with a title.
            if not best_win and search_pool:
                _, best_win, _ = _best_window_for_terms(search_terms, search_pool)

            # Final fallback for WeChat: rescan all windows after a brief delay to catch tray activation.
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
        "message": (
            f"launched {launched_count}/{requested_count}: "
            f"{launch_error or 'unknown error'}"
            if launch_error and launched_count < requested_count
            else None
        ),
    }
