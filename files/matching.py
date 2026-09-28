"""Strict incident matching: never claim a match on text similarity alone.

A past incident is only claimed when the provider is genuinely the same AND
the error is structurally the same issue:

  1. Provider gate - if the new error names a known provider and the incident
     belongs to a different known provider ("Stripe" vs "Razorpay"), it can
     never match, no matter how similar the wording is. If no provider is
     named, the gate abstains.
  2. Exact signal  - a shared error code (invalid_grant, SignatureDoesNotMatch,
     card_declined) or a shared HTTP status (429) is a strict match (score 1.0).
  3. Structure     - otherwise, after normalizing away volatile parts (numbers,
     ids, timestamps) and stemming, the two errors must share enough
     distinguishing vocabulary (overlap coefficient) AND a real fraction of
     their total language (jaccard) to count as the same issue (score 0.8).

match() returns (incident, score, reason). incident is None when nothing
clears the bar, so /debug falls back to semantic recall instead of claiming.
"""

import re
from typing import Dict, Optional, Set, Tuple

from models import Incident

# Providers that share 90% of their behavior but are NOT the same API. Every
# pair here has bitten someone who assumed "it's all payment gateways".
_CONFUSABLES = [
    {"stripe", "razorpay", "braintree", "square", "paypal", "checkout.com", "payu"},
    {"auth0", "okta", "cognito", "keycloak", "firebase auth", "supabase auth"},
    {"twilio", "messagebird", "vonage", "plivo", "sns"},
    {"sendgrid", "mailgun", "ses", "postmark"},
    {"slack", "discord", "teams"},
    {"s3", "gcs", "azure blob"},
    {"github", "gitlab", "bitbucket"},
    {"hubspot", "salesforce"},
    {"zendesk", "intercom", "freshdesk"},
]

_PROVIDER_NAMES = set().union(*_CONFUSABLES)

_STOPWORDS = {
    "error", "the", "a", "an", "in", "on", "at", "from", "with", "for", "to", "of", "and", "or",
    "when", "while", "via", "api", "call", "calling", "request", "response", "failed", "failure",
    "failing", "got", "get", "getting", "but", "is", "was", "isnt", "wasnt", "not", "no", "yes",
    "despite", "even", "though", "using", "used", "try", "trying", "tried", "after", "before",
    "your", "my", "our", "their", "it", "its", "this", "that", "these", "those", "there", "here",
    "http", "https", "com", "www", "any", "some", "still", "keeps", "keep", "always", "never",
    "how", "why", "what", "fix", "please", "help", "does", "did", "doing", "just", "also",
    "very", "only", "than", "then", "because", "since", "been", "being", "into", "out", "up",
    "down", "over", "under", "again", "once",
}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._\-/+#]*")
_SCREAMING_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
_CAMEL_CODE_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_SNAKE_CODE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_STATUS_RE = re.compile(r"\b[1-5]\d{2}\b")

# Generic acronyms/methods that carry no identity and must not act as codes.
_CODE_EXCLUDE = {"HTTP", "HTTPS", "API", "URL", "JSON", "XML", "AWS", "SDK",
                 "GET", "POST", "PUT", "DELETE", "OAUTH", "JWT"}

_PLACEHOLDERS = {"<n>", "<id>", "<ts>", "<date>", "<oid>"}


def _norm_api(name: str) -> str:
    return re.sub(r"[^a-z0-9.+ ]", " ", name.lower().replace("api", " ")).strip()


def _same_api(detected: str, incident_api: str) -> bool:
    """True unless both sides name DIFFERENT known providers (then hard veto)."""
    a, b = _norm_api(detected), _norm_api(incident_api)
    if not a or not b:
        return True  # gate abstains when the provider is unspecified
    if a == b or a in b or b in a:  # "slack" vs "slack api", "s3" vs "aws s3"
        return True
    return any(a in group and b in group for group in _CONFUSABLES)


def _detect_api(text: str) -> str:
    """Which known provider (if any) is explicitly named in the text?"""
    t = text.lower()
    hits = [(len(name), name) for name in _PROVIDER_NAMES if re.search(rf"\b{re.escape(name)}\b", t)]
    return max(hits)[1] if hits else ""


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokens(text: str) -> Set[str]:
    """Lowercase, stemmed, meaningful tokens (stopwords and placeholders dropped)."""
    toks: Set[str] = set()
    for t in _WORD_RE.findall(text.lower()):
        if t in _STOPWORDS or t in _PLACEHOLDERS:
            continue
        toks.add(_singular(t))
    return toks


def _normalize_error(text: str) -> str:
    """Collapse the parts of an error string that vary between occurrences."""
    t = text.lower()
    t = re.sub(r"\b[0-9a-f]{8,}\b", " <id> ", t)  # hex ids / hashes
    t = re.sub(r"\b(19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", " <date> ", t)
    t = re.sub(r"\b\d{10,13}\b", " <ts> ", t)  # epoch seconds/millis
    t = re.sub(r"\d+", " <n> ", t)  # any remaining numbers
    t = re.sub(r"[^a-z0-9<>/_ .\-]", " ", t)
    return t


def _extract_codes(text: str) -> Set[str]:
    """SCREAMING_SNAKE, CamelCase and snake_case error-code-ish tokens."""
    codes: Set[str] = set(_SCREAMING_CODE_RE.findall(text))
    codes |= set(_CAMEL_CODE_RE.findall(text))
    codes |= {c.upper() for c in _SNAKE_CODE_RE.findall(text)}
    return codes - _CODE_EXCLUDE


def _extract_statuses(text: str) -> Set[str]:
    return set(_STATUS_RE.findall(text))


class StrictMatcher:
    """Rules-only matcher: same provider AND structurally same error, or nothing."""

    ERROR_TOKEN_THRESHOLD = 0.25  # min overlap coefficient of distinguishing tokens
    JACCARD_THRESHOLD = 0.15  # min fraction of shared total language
    STRICT_SCORE = 1.0
    STRUCTURE_SCORE = 0.8

    def __init__(self, incidents: Optional[Dict[str, Incident]] = None):
        self._by_id: Dict[str, Incident] = dict(incidents or {})

    def update(self, incident: Incident) -> None:
        self._by_id[incident.id] = incident

    def remove(self, incident_id: str) -> None:
        self._by_id.pop(incident_id, None)

    def match(self, error: str, api_name: str = "") -> Tuple[Optional[Incident], float, str]:
        """Return (best incident, score, reason) or (None, 0.0, '')."""
        detected = _detect_api(error) or (api_name or "").strip()
        best: Tuple[Optional[Incident], float, str] = (None, 0.0, "")
        for incident in self._by_id.values():
            score, reason = self._score(incident, error, detected)
            if score > best[1]:
                best = (incident, score, reason)
        return best

    def _score(self, incident: Incident, error: str, detected_api: str) -> Tuple[float, str]:
        if not _same_api(detected_api, incident.api_name):
            return 0.0, ""  # hard gate: different provider, never a match

        shared_codes = _extract_codes(error) & _extract_codes(incident.error_signature)
        if shared_codes:
            return self.STRICT_SCORE, f"shared error code {sorted(shared_codes)[0]}"

        shared_status = _extract_statuses(error) & _extract_statuses(incident.error_signature)
        if shared_status:
            return self.STRICT_SCORE, f"shared HTTP status {sorted(shared_status)[0]}"

        new_toks = _tokens(_normalize_error(error))
        old_toks = _tokens(_normalize_error(incident.error_signature))
        if not new_toks or not old_toks:
            return 0.0, ""
        overlap = len(new_toks & old_toks) / min(len(new_toks), len(old_toks))
        jaccard = len(new_toks & old_toks) / len(new_toks | old_toks)
        if overlap >= self.ERROR_TOKEN_THRESHOLD and jaccard >= self.JACCARD_THRESHOLD:
            return self.STRUCTURE_SCORE, "same provider with matching error structure"
        return 0.0, ""
