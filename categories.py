"""Keyword-based root-cause category tagger.

Deliberately dumb and transparent: a handful of keyword buckets checked in
priority order. Categories power the Memory Graph edges, so a wrong tag is
harmless (just a missing edge), and the rule list is easy to extend.
"""

# Priority order matters: check the most specific signals first.
_CATEGORIES = [
    ("signature/hmac", ["signature", "hmac", "signing", "presigned", "signed url"]),
    ("rate-limit", ["429", "rate limit", "too many requests", "throttl", "quota", "retry-after"]),
    ("auth/token", ["token", "auth", "401", "403", "unauthorized", "forbidden",
                    "permission", "scope", "credential", "api key", "invalid_grant"]),
    ("body-parsing", ["body", "json", "payload", "parse", "re-serializ", "middleware", "content-type"]),
    ("config", ["config", "setting", "env", "endpoint", "region", "dns", "cors", "origin"]),
]


def categorize(api_name: str, error_signature: str, root_cause: str, fix: str) -> str:
    """Return the first matching category, or 'other' if nothing matches."""
    text = f"{error_signature} {root_cause} {fix}".lower()
    for name, keywords in _CATEGORIES:
        if any(k in text for k in keywords):
            return name
    return "other"
