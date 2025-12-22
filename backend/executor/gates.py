"""Gate evaluation skeleton for executor safety checks.

This module will host focus, consent, and file guard gate logic.
Currently only declares the contract and stubs; no behavior changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class GateDecision:
    """Represents the outcome of a gate evaluation."""

    allowed: bool
    needs_consent: bool = False
    reason: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


def evaluate_file_guard(*args: Any, **kwargs: Any) -> GateDecision:
    """Placeholder for file guard evaluation."""
    raise NotImplementedError


def evaluate_risk_consent(*args: Any, **kwargs: Any) -> GateDecision:
    """Placeholder for risk/consent evaluation."""
    raise NotImplementedError


def evaluate_focus_gate(*args: Any, **kwargs: Any) -> GateDecision:
    """Placeholder for focus/foreground evaluation."""
    raise NotImplementedError
