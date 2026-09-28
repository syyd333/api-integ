"""Editable index of incidents, mirrored into the team's Hindsight memory bank.

Hindsight's retain/recall is append-only, but teams need to correct and
retract bad memories. This store keeps the authoritative, editable record in
incidents_local.json and mirrors incident text into Hindsight:

  - add/update  -> (re-)retains the incident text, remembers its text id
  - delete      -> tombstones the old text id, so semantic recall that still
                   surfaces the stale text is filtered out of every answer
  - seed        -> idempotent, and if Hindsight was down during a previous
                   seed it backfills the missing retain on the next run
  - history     -> every /debug call is recorded to debug_history.json so the
                   demo can revisit past queries
  - reset       -> wipes everything and re-seeds fresh demo data

Hindsight is accessed defensively (any server hiccup degrades to an empty
text id, never a crashed request), so the demo works even while Hindsight
is still booting.
"""

import json
import os
import tempfile
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from matching import StrictMatcher
from models import Incident, UpdateIncident

DEFAULT_BANK_ID = "team-integration-debugging"


def _today() -> str:
    return date.today().isoformat()


def _resolve_writable(p: Path) -> Path:
    """Serverless-safe path resolution.

    Vercel/Lambda filesystems are read-only except /tmp. If the requested
    directory isn't writable, fall back to the temp dir so the app still
    runs (in-memory for the request's lifetime) instead of 500-ing.
    """
    d = p.parent
    try:
        if d.exists() and os.access(d, os.W_OK):
            return p
    except OSError:
        pass
    return Path(tempfile.gettempdir()) / p.name


class IncidentStore:
    def __init__(
        self,
        hindsight,
        bank_id: str = DEFAULT_BANK_ID,
        path="incidents_local.json",
        history_path="debug_history.json",
    ):
        self.hindsight = hindsight
        self.bank_id = bank_id
        self.path = _resolve_writable(Path(path))
        self.history_path = _resolve_writable(Path(history_path))
        self._lock = threading.RLock()
        self._incidents: Dict[str, Incident] = {}
        self._tombstones: set = set()
        self._history: List[dict] = []
        self._matcher = StrictMatcher()
        self._load()
        self._load_history()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for item in data.get("incidents", []):
            try:
                incident = Incident(**item)
            except Exception:
                continue
            self._incidents[incident.id] = incident
            self._matcher.update(incident)
        self._tombstones = set(data.get("tombstones", []))

    def _load_history(self) -> None:
        if not self.history_path.exists():
            return
        try:
            self._history = json.loads(self.history_path.read_text()).get("history", [])
        except (json.JSONDecodeError, OSError):
            self._history = []

    def _save_history(self) -> None:
        try:
            tmp = self.history_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"history": self._history}, indent=2))
            os.replace(tmp, self.history_path)
        except OSError:
            pass  # read-only FS: keep history in memory

    def _save(self) -> None:
        try:
            data = {
                "incidents": [i.model_dump() for i in self._incidents.values()],
                "tombstones": sorted(self._tombstones),
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.path)
        except OSError:
            # Read-only FS (e.g. Vercel): keep serving from memory.
            pass

    # ---------- hindsight ----------

    @staticmethod
    def content(incident: Incident) -> str:
        return (
            f"API: {incident.api_name}\n"
            f"Error: {incident.error_signature}\n"
            f"Root cause: {incident.root_cause}\n"
            f"Fix: {incident.fix}\n"
            f"Resolved by: {incident.resolved_by or 'unknown'} on {incident.date or 'unknown date'}"
        )

    def _retain(self, incident: Incident) -> str:
        """Retain into Hindsight; returns the retained text id ('' if unavailable)."""
        try:
            resp = self.hindsight.retain(bank_id=self.bank_id, content=self.content(incident))
        except Exception:
            return ""
        items = getattr(resp, "results", None)
        if items is None and isinstance(resp, dict):
            items = resp.get("results", [])
        for item in items or []:
            if isinstance(item, dict):
                for key in ("id", "text_id", "memory_id"):
                    if item.get(key):
                        return str(item[key])
            else:
                for attr in ("id", "text_id", "memory_id"):
                    value = getattr(item, attr, None)
                    if value:
                        return str(value)
        return ""

    def _forget_text(self, text_id: str) -> None:
        """Best-effort delete in Hindsight + tombstone so recall skips stale text."""
        if not text_id:
            return
        self._tombstones.add(text_id)
        delete = getattr(self.hindsight, "delete", None)
        if not callable(delete):
            return
        for kwargs in ({"bank_id": self.bank_id, "text_id": text_id}, {"bank_id": self.bank_id, "id": text_id}):
            try:
                delete(**kwargs)
                return
            except TypeError:
                continue
            except Exception:
                return

    # ---------- public API ----------

    def all(self) -> List[Incident]:
        with self._lock:
            return list(self._incidents.values())

    def get(self, incident_id: str) -> Optional[Incident]:
        with self._lock:
            return self._incidents.get(incident_id)

    def seed(self, incidents: List[Incident]) -> dict:
        """Load demo incidents; skip known ones, backfill retains missed while
        Hindsight was down (text_id empty). Idempotent."""
        loaded = skipped = backfilled = 0
        with self._lock:
            known = {(i.api_name.lower(), i.error_signature.lower()): i for i in self._incidents.values()}
            for incident in incidents:
                key = (incident.api_name.lower(), incident.error_signature.lower())
                existing = known.get(key)
                if existing is not None:
                    if not existing.text_id:  # retained text never made it to Hindsight
                        existing.text_id = self._retain(existing)
                        backfilled += 1
                    skipped += 1
                    continue
                stored = self._register(incident)
                self._index(stored)
                known[key] = stored
                loaded += 1
            self._save()
        return {"loaded": loaded, "skipped": skipped, "backfilled": backfilled}

    def add(self, incident: Incident) -> Incident:
        with self._lock:
            stored = self._register(incident)
            self._index(stored)
            self._save()
            return stored

    def update(self, incident_id: str, patch: UpdateIncident) -> Optional[Incident]:
        with self._lock:
            old = self._incidents.get(incident_id)
            if old is None:
                return None
            changes = patch.model_dump(exclude_none=True)
            if not changes:
                return old
            updated = old.model_copy(update={**changes, "text_id": ""})
            self._forget_text(old.text_id)
            updated.text_id = self._retain(updated)
            self._incidents[incident_id] = updated
            self._matcher.update(updated)
            self._save()
            return updated

    def delete(self, incident_id: str) -> bool:
        with self._lock:
            old = self._incidents.pop(incident_id, None)
            if old is None:
                return False
            self._forget_text(old.text_id)
            self._matcher.remove(incident_id)
            self._save()
            return True

    def is_live(self, text_id: Optional[str]) -> bool:
        """Recalled Hindsight text is only usable if its id is not tombstoned."""
        return not (text_id and text_id in self._tombstones)

    def reset_to_seed(self, seed_incidents: List[Incident]) -> dict:
        """Wipe all incidents + history, then re-seed from scratch (fresh ids,
        fresh retains). Used by the demo's Reset button."""
        with self._lock:
            for incident_id in list(self._incidents):
                self._forget_text(self._incidents[incident_id].text_id)
                self._matcher.remove(incident_id)
            self._incidents.clear()
            for incident in seed_incidents:
                self._index(self._register(incident))
            self._history = []
            self._save()
            self._save_history()
            return {"loaded": len(self._incidents)}

    # ---------- debug history ----------

    HISTORY_CAP = 100

    def add_history(self, entry: dict) -> None:
        with self._lock:
            self._history.append(entry)
            self._history = self._history[-self.HISTORY_CAP:]
            self._save_history()

    def history(self, limit: int = 50) -> List[dict]:
        with self._lock:
            return list(reversed(self._history[-limit:]))  # newest first

    def clear_history(self) -> None:
        with self._lock:
            self._history = []
            self._save_history()

    def match(self, error: str, api_name: str = "") -> Tuple[Optional[Incident], float, str]:
        """Verified match against the editable index (see matching.py)."""
        with self._lock:
            return self._matcher.match(error, api_name)

    # ---------- helpers ----------

    def _register(self, incident: Incident) -> Incident:
        """Assign id/date, retain the text, return the stored copy."""
        stored = incident.model_copy(
            update={
                "id": incident.id or f"inc-{uuid.uuid4().hex[:8]}",
                "date": incident.date or _today(),
                "text_id": "",
            }
        )
        stored.text_id = self._retain(stored)
        return stored

    def _index(self, incident: Incident) -> None:
        self._incidents[incident.id] = incident
        self._matcher.update(incident)
