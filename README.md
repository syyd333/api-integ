# API/Integration Debugging Memory Agent

A shared, persistent "team memory" for third-party API quirks. When an
engineer resolves a bug caused by an undocumented behavior in a payment
gateway, auth provider, etc., the fix gets retained. Next time anyone on the
team hits a similar error, the agent recalls it instantly instead of
re-discovering it from scratch.

## How it works

Every incident flows through two layers. **Writing**: when a fix is logged
(via the web form or `POST /incidents`), `IncidentStore` in `store.py`
assigns it an id, writes the authoritative record to `incidents_local.json`,
and mirrors the text into the team's shared Hindsight memory bank
(`app.py`, `_retain_incident`). **Reading**: a pasted error (`POST /debug`)
first passes through the strict matcher in `matching.py`, which claims a
match only when the provider is genuinely the same (`_same_api` vets a
confusables list of look-alike providers, so a Stripe error can never
resurrect a Razorpay fix) *and* the error is structurally the same issue —
a shared error code like `SignatureDoesNotMatch`, a shared HTTP status like
`429`, or ≥25% overlap of distinguishing tokens after volatile values are
normalized away (`_normalize_error`). Verified matches surface the exact
past fix (`memory_tier: "strict"`); otherwise semantic recall runs as
clearly-labeled leads. Because Hindsight itself is append-only, `PUT` /
`DELETE /incidents/{id}` re-retain corrected text and tombstone old text ids
(`_forget_text`), and every recall is filtered through
`IncidentStore.is_live` — so a deleted fix can never resurface as advice.
The `use_memory` toggle flips the whole pipeline off for the before/after
demo: with it off, `debug()` in `app.py` never touches recall and the model
answers cold.

## What's here

- `app.py` — FastAPI backend: `/debug` (strict match → labeled recall →
  reasoning), incident CRUD, seeding, serves the frontend at `/`.
- `models.py` — Pydantic shapes for incidents, debug requests/responses.
- `store.py` — the editable incident index (`incidents_local.json`),
  mirrored into Hindsight; tombstones make deletes stick.
- `matching.py` — rules-only strict matcher (provider gate + error-code /
  structure checks). No match is ever claimed on vibes.
- `static/index.html` — single-file frontend (D3 pinned from cdnjs):
  error box, memory toggle, **Compare mode**, seed/reset buttons,
  recalled-incident panel, editable + filterable incident log with
  **JSON/Markdown export**, **Memory Graph**, and a **History tab** with a
  queries-per-day chart.
- `categories.py` — transparent keyword tagger (signature/hmac, rate-limit,
  auth/token, body-parsing, config); powers the Memory Graph edges.

> **Note:** the seed incidents are **illustrative**: realistic error
> signatures paired with plausible root causes, written for the demo. They
> are not verified vendor documentation.
- `incidents_seed.json` — 6 synthetic past incidents across different APIs.
- `test_app.py` — 26 pytest tests: matcher near-misses, store CRUD +
  tombstones, history, reset, and the `/debug` memory-on/memory-off contract.
- `BUILD_PROMPT.md` — the original build brief.

## Running it

```bash
pip install -r requirements.txt

cp .env.example .env   # then fill in your keys; app.py loads .env automatically
#   ANTHROPIC_API_KEY - the agent's reasoning model
#   OPENAI_API_KEY    - used by Hindsight to extract/store memories

# separate terminal - starts Hindsight itself
docker run --rm -it --pull always -p 8888:8888 -p 9999:9999 \
  -e HINDSIGHT_API_LLM_API_KEY=$OPENAI_API_KEY \
  -v $HOME/.hindsight-docker:/home/hindsight/.pg0 \
  ghcr.io/vectorize-io/hindsight:latest

uvicorn app:app --reload --port 8000
# then open http://localhost:8000
```

The header status line shows what's live: `incidents · hindsight up/down · llm
up/down`. Seeding is safe to click any time — if Hindsight was down during an
earlier seed, the next seed **backfills** the missed retains into Hindsight.

Everything degrades gracefully: with no Hindsight container the strict
matcher still works off the local index, and with no `ANTHROPIC_API_KEY`
the reply shows the retained team memory verbatim instead of crashing.

## Running the tests

```bash
pip install pytest httpx
pytest test_app.py -v      # 26 tests
```

Notable contract tests:

- `test_seed_then_debug_with_memory_recalls_right_incident` — after seeding,
  a Razorpay-style error returns `memory_tier: "strict"` with the exact
  Razorpay incident.
- `test_memory_off_never_calls_recall` — with `use_memory=false`,
  `hindsight.recall` raising is fine because it is never called.
- `test_razorpay_error_does_not_match_stripe_incident` and friends — the
  near-miss cases proving similarity alone never claims a match.

## The demo script

This is the exact "before / after" moment to show judges (all buttons are in
the UI at http://localhost:8000, curl shown for clarity):

```bash
# 1. Load the synthetic incident history (idempotent - re-running is safe)
curl -X POST http://localhost:8000/seed

# 2. MEMORY OFF - paste a Razorpay-style error, get generic advice
curl -X POST http://localhost:8000/debug \
  -H "Content-Type: application/json" \
  -d '{"error": "Webhook signature verification failed: invalid signature", "use_memory": false}'

# 3. MEMORY ON - same error, agent verifies the match and cites the fix
curl -X POST http://localhost:8000/debug \
  -H "Content-Type: application/json" \
  -d '{"error": "Webhook signature verification failed: invalid signature", "use_memory": true}'
# -> matched_incident names Razorpay, memory_tier: "strict"

# 4. NEAR-MISS - a Stripe error must NOT claim the Razorpay fix
curl -X POST http://localhost:8000/debug \
  -H "Content-Type: application/json" \
  -d '{"error": "Stripe: PaymentIntent confirmation failed with card_declined", "use_memory": true}'
# -> matched_incident: null (provider gate vetoes it)
```

Or just click **Compare** in the UI: it runs the same error both ways and
shows the two replies side by side — the single most convincing screen in
the demo.

## Features tour (for the submission video)

- **Compare mode** — one click runs `/debug` twice on the same error and
  shows Memory OFF vs Memory ON side by side, with the verified-match badge
  on the ON side.
- **Memory Graph** (Memory Graph tab) — D3 force-directed map of the team's
  memory. Nodes are incidents, colored by API; edges connect incidents that
  share an API or a root-cause category (signature/hmac, rate-limit,
  auth/token, body-parsing, config). Drag nodes, hover for error+fix
  tooltip, click for the full incident. When `/debug` verifies a match, the
  matching node pulses while the rest dim — clear on the next query. Newly
  logged incidents animate in.
- **Stats cards** — total incidents, distinct APIs, debug queries, recall
  hit rate, and **est. time saved** (hits × 45 min, clearly labeled as an
  estimate).
- **History tab** — every query with timestamp, memory on/off, match and
  confidence; click a row to re-run that exact query. Includes a
  queries-per-day bar chart and CSV export.
- **Log filters & export** — search box plus API and resolver dropdowns on
  the incident log; export everything as JSON or Markdown.

### Screenshot checklist for the video

1. [ ] Header with status line: `incidents · hindsight up · llm up`
2. [ ] Stats cards row (after a few queries, so hit rate + time saved are non-zero)
3. [ ] **Compare mode** side-by-side (use the Razorpay example button)
4. [ ] Recalled-incident panel showing the green `verified · 80%` badge
5. [ ] **Memory Graph** tab: full view, then hover tooltip, then a node click
6. [ ] Memory Graph mid-query: one node pulsing, others dimmed
7. [ ] **History tab**: table + queries-per-day chart
8. [ ] Log filters in action (filter to one API, then search a keyword)
9. [ ] Export dialog: JSON/MD for incidents, CSV for history
10. [ ] "Log a resolved incident" dialog → new node animating into the graph
11. [ ] Near-miss demo: Stripe error → `no match` badge (provider gate)
12. [ ] Edit/Delete an incident → corrected text re-retained (tombstones)

Then log a brand-new incident live during the demo (the "+ Log fix" button)
to show it's not canned data, and use Edit/Delete on any incident to show
bad memories can be corrected — the UI refreshes immediately.

## Where to take this next

Positioning note for your pitch: be explicit that this is about
**third-party integration quirks**, not internal code review or production
incident response — that's what keeps it distinct from the more generic
"Code Review Agent" / "Incident Response Agent" ideas.

Ideas: Slack bot front-end ("paste error → get past fix" without leaving
Slack), auto-capture of resolved incidents from the ticketing system, and a
"stale memory" review queue that surfaces incidents nobody has confirmed
recently.


- [ ] One final submission per team, before **29 September**
