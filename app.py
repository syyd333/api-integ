"""
API/Integration Debugging Memory Agent
HackwithHyderabad 3.0

Every time an engineer resolves a third-party integration bug (a webhook
signature quirk, a weird token-refresh edge case, an undocumented rate
limit), it gets retained as an Incident. The next time anyone on the team
pastes a similar error, the agent recalls the matching incident instead of
sending them back to Slack archaeology or Stack Overflow.

Endpoints:
  GET  /                     - the web frontend (single HTML file)
  POST /seed                 - load the synthetic incident history (idempotent)
  POST /incidents            - log a newly resolved incident            (Retain)
  GET  /incidents            - list the authoritative incident index
  PUT  /incidents/{id}       - correct a bad memory                       (Retain)
  DELETE /incidents/{id}     - retract a bad memory                       (Tombstone)
  POST /debug                - paste an error, get help              (Match + Recall)
      body: { "error": "...", "use_memory": true|false }
      use_memory=false is the "before" side of the demo: no recall, generic
      advice only. use_memory=true is the "after" side: the agent verifies a
      strict match first (same provider + same error structure) and only
      surfaces the exact past fix when one clears the bar.

Run:
  pip install -r requirements.txt
  export OPENAI_API_KEY=...
  export ANTHROPIC_API_KEY=...
  hindsight-api &                  # starts Hindsight on :8888
  uvicorn app:app --reload --port 8000
"""
import json
import os
import socket
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

# Load .env from the repo folder so keys work without manual exports.
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from hindsight_client import Hindsight

from models import (
    DebugRequest,
    DebugResponse,
    HistoryEntry,
    Incident,
    MatchInfo,
    RecallItem,
    UpdateIncident,
    normalize_results,
)
from store import DEFAULT_BANK_ID, IncidentStore

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Integration Debugging Memory Agent")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

hindsight = Hindsight(base_url=os.environ.get("HINDSIGHT_URL", "http://localhost:8888"))
store = IncidentStore(
    hindsight,
    bank_id=DEFAULT_BANK_ID,
    path=BASE_DIR / "incidents_local.json",
    history_path=BASE_DIR / "debug_history.json",
)


@lru_cache(maxsize=1)
def _claude():
    """Created on first use so the module imports cleanly without a key."""
    return anthropic.Anthropic()


def _hindsight_reachable() -> bool:
    """Cheap TCP probe; Hindsight may still be booting its docker container."""
    url = urlparse(os.environ.get("HINDSIGHT_URL", "http://localhost:8888"))
    try:
        with socket.create_connection((url.hostname or "localhost", url.port or 8888), timeout=0.25):
            return True
    except OSError:
        return False


def _lazy_hindsight():
    """Skip recall entirely when Hindsight isn't reachable; degrade, don't crash."""
    return hindsight if _hindsight_reachable() else None


class ClaudeLLM:
    """The reasoning model. Falls back to a deterministic template if unreachable."""

    def reply(self, error: str, system_prompt: str) -> str:
        try:
            completion = _claude().messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                system=system_prompt,
                messages=[{"role": "user", "content": error}],
            )
            return "".join(b.text for b in completion.content if b.type == "text")
        except Exception as exc:  # API key missing / no network - stay useful
            idx = system_prompt.find("Past incident")  # skip the instruction preamble
            tail = system_prompt[idx:] if idx != -1 else system_prompt
            return (
                f"[offline mode: {type(exc).__name__} - set ANTHROPIC_API_KEY for full reasoning.]\n"
                f"Team memory says:\n{tail}"
            )


llm = ClaudeLLM()


@app.get("/")
def frontend():
    """The web frontend - a single HTML file."""
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/seed")
def seed():
    """Load synthetic past incidents into memory - for the demo (idempotent,
    and backfills retains that were missed while Hindsight was down)."""
    path = BASE_DIR / "incidents_seed.json"
    incidents = [Incident(**inc) for inc in json.loads(path.read_text())]
    result = store.seed(incidents)
    return {"status": "seeded", **result, "total": len(store.all())}


@app.post("/reset-demo")
def reset_demo():
    """Wipe incidents + history, then re-seed fresh demo data with new ids."""
    path = BASE_DIR / "incidents_seed.json"
    incidents = [Incident(**inc) for inc in json.loads(path.read_text())]
    result = store.reset_to_seed(incidents)
    return {"status": "reset", **result}


@app.post("/incidents")
def log_incident(incident: Incident):
    """A team member logs a bug they just resolved. (Retain)"""
    stored = store.add(incident)
    return {"status": "retained", "incident": stored.model_dump()}


@app.get("/incidents")
def list_incidents():
    """Browse the authoritative, editable incident index."""
    return {"incidents": [i.model_dump() for i in store.all()]}


@app.put("/incidents/{incident_id}")
def edit_incident(incident_id: str, patch: UpdateIncident):
    """Correct a bad memory: re-retain the new text, tombstone the old. (Retain)"""
    updated = store.update(incident_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown incident id: {incident_id}")
    return {"status": "updated", "incident": updated.model_dump()}


@app.delete("/incidents/{incident_id}")
def remove_incident(incident_id: str):
    """Retract a bad memory: tombstone its retained text so recall skips it."""
    if not store.delete(incident_id):
        raise HTTPException(status_code=404, detail=f"Unknown incident id: {incident_id}")
    return {"status": "deleted", "id": incident_id}


@app.post("/debug", response_model=DebugResponse)
def debug(req: DebugRequest):
    """
    Paste an error. With use_memory=True, the agent first verifies a strict
    match (same provider + same error structure); only then does it surface
    the exact past fix. If no strict match exists, it still uses semantic
    recall as *leads* but clearly labels them. With use_memory=False, it
    answers cold, like a plain chatbot - the "before / after" demo toggle.
    """
    recalled: List[RecallItem] = []
    matches: List[MatchInfo] = []
    memory_tier = "none"
    matched_incident: Optional[MatchInfo] = None
    match_status = "none"

    if req.use_memory:
        incident, score, reason = store.match(req.error, req.api_name or "")
        if incident:
            matched_incident = MatchInfo(
                incident_id=incident.id,
                api_name=incident.api_name,
                error_signature=incident.error_signature,
                confidence=score,
            )
            matches.append(matched_incident)
            memory_tier = "strict"
            match_status = "strict"

    if req.use_memory:
        hs = _lazy_hindsight()
        if hs is not None:
            try:
                recalled = normalize_results(hs.recall(bank_id=store.bank_id, query=req.error))
            except Exception:
                recalled = []
        live = [r for r in recalled if store.is_live(r.id)]
        stale_dropped = len(recalled) - len(live)

        if memory_tier != "strict" and live:
            memory_tier = "recall"
            match_status = "recall"
        if memory_tier == "strict":
            match_status = "recall" if (live and stale_dropped) else "strict"

        recalled = live[:5]
    else:
        stale_dropped = 0

    # ---- build the reply ----
    if not req.use_memory:
        system_prompt = (
            "You help engineers debug third-party API/integration errors. "
            "You have no memory of past incidents - give your best general "
            "debugging advice from first principles."
        )
    elif memory_tier == "strict":
        inc = store.get(matched_incident.incident_id)
        system_prompt = (
            "You help engineers debug third-party API/integration errors. "
            "The team has hit this exact issue before. Lead with the verified "
            "past fix, name the past incident, and note the root cause. Do not "
            "hedge with generic advice unless the fix genuinely does not apply.\n\n"
            f"Past incident (verified match):\n{store.content(inc)}"
        )
    else:
        memory_context = (
            "\n\n".join(f"- {r.content}" for r in recalled) or "No matching past incidents."
        )
        system_prompt = (
            "You help engineers debug third-party API/integration errors. "
            "These past incidents are semantically similar but NOT verified "
            "matches - use them only as leads, and say so explicitly if the "
            "team has not resolved this exact issue before.\n\n"
            f"Past incidents (unverified leads):\n{memory_context}"
        )

    reply = llm.reply(req.error, system_prompt)

    response = DebugResponse(
        reply=reply,
        recalled_incidents=[r.content for r in recalled],
        matches=matches,
        matched_incident=matched_incident,
        memory_tier=memory_tier,
        match_status=match_status,
        use_memory_active=req.use_memory,
    )

    # Remember this run so the demo can revisit earlier queries.
    store.add_history(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": req.error,
            "use_memory": req.use_memory,
            "memory_tier": memory_tier,
            "matched_api": matched_incident.api_name if matched_incident else None,
            "matched_incident_id": matched_incident.incident_id if matched_incident else None,
            "confidence": matched_incident.confidence if matched_incident else None,
            "reply": reply,
        }
    )

    return response


@app.get("/stats")
def stats():
    """Demo stats: incident counts, query volume, recall hit rate."""
    incidents = store.all()
    apis = {}
    for inc in incidents:
        apis[inc.api_name] = apis.get(inc.api_name, 0) + 1
    top_api = max(apis, key=apis.get) if apis else None

    history = store.history(limit=10_000)  # all of it
    mem_on = [h for h in history if h.get("use_memory")]
    hits = [h for h in mem_on if h.get("matched_incident_id")]
    hit_rate = (len(hits) / len(mem_on)) if mem_on else 0.0

    return {
        "total_incidents": len(incidents),
        "distinct_apis": len(apis),
        "top_api": top_api,
        "top_api_count": apis.get(top_api, 0) if top_api else 0,
        "total_queries": len(history),
        "memory_on_queries": len(mem_on),
        "recall_hits": len(hits),
        "hit_rate": round(hit_rate, 3),
        "time_saved_minutes": len(hits) * 45,  # rough estimate: 45 min per avoided re-debug
    }


@app.get("/graph")
def graph():
    """Nodes = incidents, edges = shared API or shared root-cause category."""
    incidents = store.all()
    by_api, by_cat = {}, {}
    nodes = []
    for inc in incidents:
        nodes.append({
            "id": inc.id,
            "api_name": inc.api_name,
            "category": inc.category,
            "error": inc.error_signature,
            "root_cause": inc.root_cause,
            "fix": inc.fix,
            "date": inc.date,
            "resolved_by": inc.resolved_by,
        })
        by_api.setdefault(inc.api_name, []).append(inc.id)
        by_cat.setdefault(inc.category, []).append(inc.id)

    edges, seen = [], set()
    def add_edges(ids):
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                key = tuple(sorted((ids[i], ids[j])))
                if key not in seen:
                    seen.add(key)
                    edges.append({"source": key[0], "target": key[1]})

    for ids in list(by_api.values()) + list(by_cat.values()):
        add_edges(ids)
    return {"nodes": nodes, "edges": edges}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "incidents": len(store.all()),
        "hindsight": bool(_lazy_hindsight()),
        "llm": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


@app.get("/history", response_model=List[HistoryEntry])
def get_history(limit: int = 50):
    """Recent /debug runs, newest first - the demo's saved history."""
    return [HistoryEntry(**e) for e in store.history(limit)]


@app.delete("/history")
def delete_history():
    store.clear_history()
    return {"status": "cleared"}


# Static files for the frontend last, so API routes win.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
