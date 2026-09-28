# Prompt to run inside this repo (Claude Code / Codex)

Paste this once you `cd` into this project folder and open your coding agent:

---

I'm building an "API/Integration Debugging Memory Agent" for a hackathon.
The idea: engineering teams re-debug the same undocumented third-party API
quirks over and over (webhook signature formats, token-refresh edge cases,
undocumented rate limits) because the fix only ever lives in a Slack
thread. This agent gives the team shared, persistent memory: every
resolved integration bug gets retained, and the next time anyone hits a
similar error, the agent recalls the exact past fix instead of giving
generic advice.

The repo already has a working skeleton:
- `app.py` - FastAPI backend using Hindsight (retain/recall) and Claude for
  reasoning. Has `/seed`, `/incidents` (GET+POST), and `/debug` (with a
  `use_memory` toggle for the before/after demo).
- `incidents_seed.json` - 6 synthetic past incidents (Razorpay, Auth0,
  Twilio, AWS S3, Slack, Stripe) to seed the demo.
- `requirements.txt`

Please, working incrementally and showing me the diff after each step:

1. Add a small web frontend (single HTML file is fine) with:
   - a text box to paste an error
   - a "memory on/off" toggle
   - a button to seed the demo data
   - a panel showing which past incident (if any) was recalled
2. Add a `POST /incidents/{id}` or similar so incidents can be edited/
   removed later (teams will want to correct bad memories).
3. Tighten the recall logic in `/debug` so the agent only claims a match
   when the API name AND the error are genuinely the same issue - not just
   semantically similar text. Show me how you'd test that with a couple of
   near-miss examples (e.g. a Stripe signature error vs a Razorpay one).
4. Add a couple of basic tests (pytest + FastAPI TestClient) covering:
   - seeding then debugging with use_memory=True recalls the right incident
   - use_memory=False never calls hindsight.recall
5. Write a one-paragraph "how it works" section I can drop into my
   hackathon submission, referencing the actual code you just wrote (not
   generic marketing copy).

Ask me before making any change that would require a new external service
or API key beyond OpenAI/Anthropic/Hindsight.
