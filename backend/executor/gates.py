"""Gate evaluation skeleton for executor safety checks.

This module will host focus, consent, and file guard gate logic.
Currently only declares the contract and stubs; no behavior changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class GateDecision:
    """Represents the outcome of a gate evaluation."""

    allowed: bool
    needs_consent: bool = False
    reason: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


def evaluate_file_guard(
    step: Any,
    work_dir: Optional[str],
    dry_run: bool,
    allowed_roots: Optional[list],
    add_allowed_root,
    normalize_path_candidate,
    is_under_any_root,
    is_forbidden_path,
    coerce_bool,
) -> Dict[str, Any]:
    """
    Enforce file path guardrails for mutation and read-only actions.
    Mirrors executor._evaluate_file_guardrails return shape.
    """
    action = step.action
    params = step.params or {}
    roots = list(allowed_roots or [])
    if work_dir:
        add_allowed_root(work_dir)
        if work_dir not in roots:
            try:
                roots.append(os.path.abspath(work_dir))
            except Exception:
                roots.append(work_dir)

    def _decision(allow: bool, reason: str, original: Optional[str], normalized: Optional[str], rule: str) -> Dict[str, Any]:
        return {
            "allow": allow,
            "reason": reason,
            "rule": rule,
            "original_path": original,
            "normalized_path": normalized,
            "allowed_roots": roots,
        }

    mutation_actions = {"write_file", "delete_file", "move_file", "copy_file", "rename_file", "create_folder"}
    read_actions = {"read_file", "open_file", "list_files"}
    relevant_actions = mutation_actions | read_actions
    if action not in relevant_actions:
        return _decision(True, "not_applicable", None, None, "skip")

    primary = params.get("path") or params.get("source")
    destination = params.get("destination") or params.get("destination_dir") or params.get("new_name")

    norm_primary, err_primary, had_traversal = normalize_path_candidate(primary, work_dir)
    if err_primary:
        return _decision(False, err_primary if err_primary != "normalize_error" else "path_not_allowed", primary, None, err_primary)

    if had_traversal and norm_primary and not is_under_any_root(norm_primary, roots):
        return _decision(False, "traversal_detected", primary, norm_primary, "traversal_detected")

    if is_forbidden_path(norm_primary, roots):
        return _decision(False, "forbidden_path", primary, norm_primary, "forbidden_path")

    norm_dest = None
    if action in {"move_file", "copy_file", "rename_file"} and destination:
        norm_dest, err_dest, had_traversal_dest = normalize_path_candidate(destination, work_dir)
        if err_dest:
            return _decision(False, err_dest if err_dest != "normalize_error" else "path_not_allowed", destination, None, err_dest)
        if had_traversal_dest and norm_dest and not is_under_any_root(norm_dest, roots):
            return _decision(False, "traversal_detected", destination, norm_dest, "traversal_detected")
        if is_forbidden_path(norm_dest, roots):
            return _decision(False, "forbidden_path", destination, norm_dest, "forbidden_path")

    is_mutation = action in mutation_actions

    if is_mutation:
        if not is_under_any_root(norm_primary, roots):
            return _decision(False, "path_not_allowed", primary, norm_primary, "path_not_allowed")
        if norm_dest and not is_under_any_root(norm_dest, roots):
            return _decision(False, "path_not_allowed", destination, norm_dest, "path_not_allowed")
    else:
        if is_forbidden_path(norm_primary, roots):
            return _decision(False, "forbidden_path", primary, norm_primary, "forbidden_path")

    if is_mutation and norm_primary and not is_under_any_root(norm_primary, roots):
        return _decision(False, "symlink_escape", primary, norm_primary, "symlink_escape")
    if is_mutation and norm_dest and not is_under_any_root(norm_dest, roots):
        return _decision(False, "symlink_escape", destination, norm_dest, "symlink_escape")

    if not dry_run:
        try:
            overwrite_flag = coerce_bool(params.get("overwrite"), False)
        except Exception:
            overwrite_flag = False
        if action in {"write_file", "rename_file", "move_file", "copy_file"}:
            target_path = norm_dest if action in {"move_file", "copy_file"} else norm_primary
            if target_path and is_under_any_root(target_path, roots):
                try:
                    if Path(target_path).exists() and not overwrite_flag:
                        return _decision(False, "overwrite_blocked", target_path, target_path, "overwrite_blocked")
                except Exception:
                    return _decision(False, "overwrite_blocked", target_path, target_path, "overwrite_blocked")

    return _decision(True, "allow", primary, norm_primary, "allow")


def evaluate_risk_consent(*args: Any, **kwargs: Any) -> GateDecision:
    """
    Evaluate risk/consent requirement.

    Expects risk_info dict with a 'level' key and optional 'reason'.
    consent_token indicates whether user-level consent was provided.
    Returns a dict matching the executor's current expectations.
    """
    risk_info = kwargs.get("risk_info") if not args else args[0]
    consent_token = kwargs.get("consent_token") if len(args) < 2 else args[1]
    level = (risk_info or {}).get("level")
    reason = (risk_info or {}).get("reason")

    if level == "block":
        return {
            "allowed": False,
            "reason": "blocked",
            "message": reason or "blocked",
        }

    if level == "high" and not consent_token:
        return {
            "allowed": False,
            "reason": "needs_consent",
            "message": "consent required for high-risk action",
        }

    return {
        "allowed": True,
        "reason": None,
        "message": None,
    }


def evaluate_focus_gate(*args: Any, **kwargs: Any) -> GateDecision:
    """Placeholder for focus/foreground evaluation."""
    raise NotImplementedError
