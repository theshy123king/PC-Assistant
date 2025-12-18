from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel


class ArtifactRef(BaseModel):
    artifact_id: str
    kind: Literal["image", "json"]
    mime: str
    bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    sha256: Optional[str] = None


class EvidenceEvent(BaseModel):
    request_id: str
    seq: int
    ts_ms: int
    type: str
    payload: Dict[str, Any]
    step_index: Optional[int] = None
    attempt: Optional[int] = None
    artifact: Optional[ArtifactRef] = None


__all__ = ["ArtifactRef", "EvidenceEvent"]
