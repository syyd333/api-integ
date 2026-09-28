"""Tests for the strict matcher, the editable store, and the /debug contract."""
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
from matching import StrictMatcher
from models import Incident, UpdateIncident
from store import IncidentStore


# ---------------------------------------------------------------- fixtures


def make_incident(**overrides) -> Incident:
    base = dict(
        api_name="Stripe",
        error_signature="No signatures found matching the expected signature for payload",
        root_cause="A global JSON body parser re-serialized the raw webhook body.",
        fix="Read the raw request body for signature verification.",
        resolved_by="meera",
        date="2026-03-29",
        id="inc-test",
    )
    base.update(overrides)
    return Incident(**base)


class FakeHindsight:
    """Records retains; recall returns whatever was retained (like real Hindsight)."""

    def __init__(self):
        self.retained = []  # (text_id, content)
        self.recall_calls = 0

    def retain(self, bank_id, content):
        text_id = f"txt-{len(self.retained) + 1}"
        self.retained.append((text_id, content))
        return {"results": [{"id": text_id}]}

    def recall(self, bank_id, query):
        self.recall_calls += 1
        return {"results": [{"id": i, "content": c} for i, c in self.retained]}

    def delete(self, bank_id, text_id):
        self.deleted = getattr(self, "deleted", []) + [text_id]
        return {"status": "deleted"}


SEED_PATH = __import__("pathlib").Path(__file__).parent / "incidents_seed.json"


@pytest.fixture()
def hs():
    return FakeHindsight()


@pytest.fixture()
def store(hs, tmp_path):
    return IncidentStore(hs, bank_id="test-bank", path=tmp_path / "local.json", history_path=tmp_path / "history.json")


@pytest.fixture()
def client(hs, store, monkeypatch):
    """TestClient wired to the fake hindsight + fresh store, Claude stubbed."""
    monkeypatch.setattr(app_module, "hindsight", hs)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "_lazy_hindsight", lambda: hs)  # skip TCP probe in tests

    class FakeLLM:
        def reply(self, error, system_prompt):
            return f"LLM[{system_prompt[:40]}...]"

    monkeypatch.setattr(app_module, "llm", FakeLLM())
    with TestClient(app_module.app) as c:
        yield c


# ------------------------------------------------------- 1) strict matcher


class TestStrictMatcher:
    def test_stripe_seed_matches_stripe_error(self):
        m = StrictMatcher({"a": make_incident()})
        inc, score, why = m.match("stripe.error.SignatureVerificationError: No signatures found matching the expected signature for payload")
        assert inc is not None and inc.id == "inc-test"
        assert score >= 0.8

    def test_razorpay_seed_matches_its_own_error(self):
        """Seed data regression guard: the Razorpay entry must recall its own error."""
        seed_data = json.loads(SEED_PATH.read_text())
        inc = next(i for i in seed_data if i["api_name"] == "Razorpay")
        m = StrictMatcher({"r": Incident(**inc)})
        found, score, _ = m.match(inc["error_signature"])
        assert found is not None and score >= m.STRICT_SCORE - 0.21  # structure match is fine

    def test_razorpay_fix_does_not_mention_timestamp(self):
        """Accuracy guard: Razorpay verification is plain HMAC over the raw body."""
        seed_data = json.loads(SEED_PATH.read_text())
        inc = next(i for i in seed_data if i["api_name"] == "Razorpay")
        assert "timestamp" not in inc["fix"].lower()
        assert "timestamp" not in inc["root_cause"].lower()

    def test_razorpay_error_does_not_match_stripe_incident(self):
        m = StrictMatcher({"a": make_incident()})
        inc, score, _ = m.match(
            "razorpay webhook error: invalid signature received in x-razorpay-signature header"
        )
        assert inc is None, "Stripe incident must not be claimed for a Razorpay error"

    def test_auth0_error_does_not_match_stripe_incident(self):
        m = StrictMatcher({"a": make_incident()})
        inc, _, _ = m.match("invalid_grant: Unknown or invalid refresh token from auth0")
        assert inc is None

    def test_same_code_same_api_matches(self):
        m = StrictMatcher({"s3": make_incident(
            api_name="AWS S3",
            error_signature="SignatureDoesNotMatch when uploading via presigned URL from browser",
        )})
        inc, score, why = m.match("S3 put_object via presigned URL failed with SignatureDoesNotMatch")
        assert inc is not None and score == m.STRICT_SCORE and "SignatureDoesNotMatch" in why

    def test_same_api_similar_words_still_needs_structure(self):
        # Same provider, hand-wavy wording that shares only generic words -> no claim.
        m = StrictMatcher({"a": make_incident()})
        inc, _, _ = m.match("Stripe checkout failing intermittently, need help debugging payments")
        assert inc is None

    def test_volatile_values_normalized(self):
        m = StrictMatcher({"a": make_incident(
            api_name="Auth0",
            error_signature="invalid_grant: Unknown or invalid refresh token req_id=a1b2c3d4e5f67890",
        )})
        inc, score, _ = m.match("Auth0 said invalid_grant: Unknown or invalid refresh token req_id=99f ee88d1a22b3")
        assert inc is not None and score >= 0.8

    def test_status_code_match(self):
        m = StrictMatcher({"a": make_incident(
            api_name="Twilio",
            error_signature="HTTP 429: Too Many Requests on outbound SMS, no Retry-After header",
        )})
        inc, _, why = m.match("Twilio returned HTTP 429 while sending an SMS blast")
        assert inc is not None and "status" in why


# ------------------------------------------------------------ 2) the store


class TestStore:
    def test_add_retains_and_assigns_id(self, store, hs):
        stored = store.add(make_incident(id=None))
        assert stored.id and stored.text_id == "txt-1"
        assert len(hs.retained) == 1

    def test_update_re_retains_and_tombstones_old_text(self, store, hs):
        stored = store.add(make_incident(id=None))
        old_text_id = stored.text_id
        store.update(stored.id, UpdateIncident(fix="corrected fix"))
        assert len(hs.retained) == 2  # re-retain
        assert old_text_id in store._tombstones

    def test_delete_tombstones(self, store):
        stored = store.add(make_incident(id=None))
        assert store.delete(stored.id) is True
        assert store.get(stored.id) is None
        assert stored.text_id in store._tombstones
        assert store.is_live(stored.text_id) is False

    def test_seed_is_idempotent(self, store, hs):
        seed_data = json.loads(SEED_PATH.read_text())
        first = store.seed([Incident(**i) for i in seed_data])
        second = store.seed([Incident(**i) for i in seed_data])
        assert first["loaded"] == 6 and second["loaded"] == 0 and second["skipped"] == 6
        assert len(store.all()) == 6

    def test_seed_backfills_retains_missed_while_hindsight_down(self, store, hs):
        """If an earlier seed ran while Hindsight was unreachable (text_id ''),
        re-seeding must retain those texts now."""
        seed_data = json.loads(SEED_PATH.read_text())
        store.seed([Incident(**i) for i in seed_data])
        assert all(i.text_id for i in store.all())
        # Simulate the outage: pretend none of the retains made it.
        for inc in store.all():
            inc.text_id = ""
        calls_before = len(hs.retained)
        result = store.seed([Incident(**i) for i in seed_data])
        assert result["backfilled"] == 6
        assert len(hs.retained) == calls_before + 6
        assert all(i.text_id for i in store.all())

    def test_reset_to_seed(self, store, hs):
        seed_data = json.loads(SEED_PATH.read_text())
        store.seed([Incident(**i) for i in seed_data])
        store.add_history({"ts": "t", "error": "e", "use_memory": True, "memory_tier": "strict", "reply": "r"})
        old_ids = {i.id for i in store.all()}
        result = store.reset_to_seed([Incident(**i) for i in seed_data])
        assert result["loaded"] == 6
        assert {i.id for i in store.all()} != old_ids  # fresh ids
        assert store.history() == []  # history wiped
        assert len(hs.retained) == 12  # 6 original + 6 re-retained

    def test_history_roundtrip(self, store):
        store.add_history({"ts": "t1", "error": "e1", "use_memory": True, "memory_tier": "strict", "reply": "r1"})
        store.add_history({"ts": "t2", "error": "e2", "use_memory": False, "memory_tier": "none", "reply": "r2"})
        items = store.history()
        assert [i["error"] for i in items] == ["e2", "e1"]  # newest first
        store.clear_history()
        assert store.history() == []

    def test_state_survives_restart(self, store, hs, tmp_path):
        stored = store.add(make_incident(id=None))
        text_id = stored.text_id
        store.delete(stored.id)  # tombstone before restart
        reopened = IncidentStore(hs, bank_id="test-bank", path=store.path)
        assert reopened.get(stored.id) is None
        assert reopened.is_live(text_id) is False


# -------------------------------------------------- 3) /debug API contract


class TestDebugEndpoint:
    def test_seed_then_debug_with_memory_recalls_right_incident(self, client, store):
        seed_data = json.loads(SEED_PATH.read_text())
        store.seed([Incident(**i) for i in seed_data])
        resp = client.post("/debug", json={
            "error": "Webhook signature verification failed: invalid signature",
            "use_memory": True,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched_incident"]["api_name"] == "Razorpay"
        assert body["memory_tier"] == "strict"
        assert body["use_memory_active"] is True
        # the exact fix reached the model, not just similar text
        assert "raw" in body["recalled_incidents"][0] or "raw" in body["reply"]

    def test_memory_off_never_calls_recall(self, client, store, monkeypatch):
        called = {"n": 0}

        def boom(*a, **k):
            called["n"] += 1
            raise AssertionError("hindsight.recall must not run with memory off")

        monkeypatch.setattr(app_module.hindsight, "recall", boom)
        resp = client.post("/debug", json={"error": "anything at all", "use_memory": False})
        assert resp.status_code == 200
        assert called["n"] == 0
        assert resp.json()["recalled_incidents"] == []
        assert resp.json()["matched_incident"] is None
        assert resp.json()["use_memory_active"] is False

    def test_near_miss_is_flagged_not_claimed(self, client, store, hs):
        seed_data = json.loads(SEED_PATH.read_text())
        store.seed([Incident(**i) for i in seed_data])
        # Stripe wording but our index holds Razorpay/Stripe etc.; this Stripe error
        # shares the word "signature" with Razorpay's - the provider gate must veto it.
        resp = client.post("/debug", json={
            "error": "Stripe: PaymentIntent confirmation failed with card_declined",
            "use_memory": True,
        })
        body = resp.json()
        assert body["matched_incident"] is None
        assert body["memory_tier"] in ("recall", "none")

    def test_no_seed_no_false_claim(self, client, store):
        resp = client.post("/debug", json={
            "error": "Webhook signature verification failed: invalid signature",
            "use_memory": True,
        })
        body = resp.json()
        assert body["matched_incident"] is None

    def test_crud_flow(self, client, store):
        r = client.post("/incidents", json={
            "api_name": "SendGrid",
            "error_signature": "550 5.7.1 Sender identity not verified",
            "root_cause": "Sender not verified",
            "fix": "Verify the sender",
        })
        assert r.status_code == 200
        inc_id = r.json()["incident"]["id"]

        r = client.put(f"/incidents/{inc_id}", json={"fix": "Verify sender + DKIM"})
        assert r.status_code == 200 and r.json()["incident"]["fix"] == "Verify sender + DKIM"

        assert client.get("/incidents").json()["incidents"]

        r = client.delete(f"/incidents/{inc_id}")
        assert r.status_code == 200
        assert client.get("/incidents").json()["incidents"] == []
        assert client.delete(f"/incidents/{inc_id}").status_code == 404

    def test_debug_run_is_saved_to_history(self, client, store):
        store.seed([Incident(**i) for i in json.loads(SEED_PATH.read_text())])
        client.post("/debug", json={"error": "Webhook signature verification failed: invalid signature", "use_memory": True})
        client.post("/debug", json={"error": "something else entirely", "use_memory": False})
        hist = client.get("/history").json()
        assert len(hist) == 2
        assert hist[0]["error"] == "something else entirely"  # newest first
        assert hist[1]["matched_api"] == "Razorpay"
        assert client.delete("/history").status_code == 200
        assert client.get("/history").json() == []

    def test_reset_demo_endpoint(self, client, store):
        client.post("/seed")
        client.post("/debug", json={"error": "x", "use_memory": False})
        r = client.post("/reset-demo")
        assert r.status_code == 200
        assert r.json()["loaded"] == 6
        assert len(client.get("/incidents").json()["incidents"]) == 6
        assert client.get("/history").json() == []

    def test_health_reports_llm(self, client):
        body = client.get("/health").json()
        assert "llm" in body

    def test_frontend_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"Integration Debugging Memory Agent" in r.content
