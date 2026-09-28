"""Typed data shapes for incidents, debug requests/responses, and recalls."""
from typing import List, Optional

from pydantic import BaseModel, Field, computed_field

from categories import categorize


class Incident(BaseModel):
    """One resolved third-party integration bug, as the team records it."""

    id: Optional[str] = None  # filled in by /incidents POST
    api_name: str
    error_signature: str
    text_id: str = ""  # Hindsight fact id of the retained text; "" = not yet retained
    root_cause: str
    fix: str
    resolved_by: Optional[str] = None
    date: Optional[str] = None

    @computed_field
    @property
    def category(self) -> str:
        """Root-cause bucket (keyword-based) used to link related incidents."""
        return categorize(self.api_name, self.error_signature, self.root_cause, self.fix)


class DebugRequest(BaseModel):
    error: str
    use_memory: bool = True
    api_name: Optional[str] = None  # optional provider hint for strict matching


class MatchInfo(BaseModel):
    """Public info about one matched incident."""

    incident_id: str
    api_name: str
    error_signature: str
    confidence: float


class DebugResponse(BaseModel):
    reply: str
    recalled_incidents: List[str]
    matches: List[MatchInfo] = []
    matched_incident: Optional[MatchInfo] = None
    memory_tier: str = "none"  # none | strict | recall
    match_status: str = "none"  # none | strict | recall | stale | rejected
    use_memory_active: bool = True


class UpdateIncident(BaseModel):
    """Partial update body for PUT /incidents/{id}."""

    api_name: Optional[str] = None
    error_signature: Optional[str] = None
    root_cause: Optional[str] = None
    fix: Optional[str] = None
    resolved_by: Optional[str] = None
    date: Optional[str] = None


class RecallItem(BaseModel):
    """One recalled Hindsight text, as surfaced to the agent/frontend."""

    id: Optional[str] = None
    content: str
    score: float = Field(default=0.0, description="1.0 for strict matches, else recall score")


class HistoryEntry(BaseModel):
    """One past /debug call, kept so the demo can revisit earlier runs."""

    ts: str
    error: str
    use_memory: bool
    memory_tier: str
    matched_api: Optional[str] = None
    matched_incident_id: Optional[str] = None
    confidence: Optional[float] = None
    reply: str = ""


def normalize_results(raw: object) -> List[RecallItem]:
    """Flatten Hindsight's varying return shapes into RecallItems."""
    if isinstance(raw, dict):
        raw = raw.get("results", [])
    else:
        raw = getattr(raw, "results", raw) or []
    items: List[RecallItem] = []
    for r in raw:
        if isinstance(r, dict):
            items.append(
                RecallItem(id=r.get("id"), content=r.get("content", ""), score=float(r.get("score", 0.0)))
            )
        elif isinstance(r, RecallItem):
            items.append(r)
        else:
            items.append(RecallItem(id=getattr(r, "id", None), content=str(r)))
    return items
