"""Secret redaction and privacy scrubbing.

Two layers, two different jobs:

- `redact()` strips credential-shaped strings. Applied at INGEST (adapters), so
  the distilling LLM never sees a live key, and again at the WRITE boundary.
- `scrub_home()` replaces absolute home directories with `~`. Not a secret —
  a privacy + portability leak: a card citing `/home/alice/...` names its author
  and is meaningless on any other machine.

`sanitize()` = both, and it is what `cards.save()` calls. That is the real trust
boundary: the artifact that leaves this machine is the CARD, not the transcript.
Cards are written by an LLM and by agents via `save_insight`/`afterwit queue`, neither
of which passes through an adapter, and the wiki is `git push`ed. Redacting only
at ingest defended the wrong door.

Both functions are idempotent: a redaction marker never re-matches a pattern.
"""

from __future__ import annotations

import re
from typing import Match

# Ordered: specific vendor formats before the generic keyword rule, so a token
# is labelled with what it actually is. A pattern that fires first wins.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Whole PEM block when the END marker is present...
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # ...and the header plus whatever base64 follows when it is not. A truncated
    # key pasted into a transcript is the common case and still leaks material.
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\s*[A-Za-z0-9+/=]{16,})*\s*",
        ),
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj|svcacct|admin)?-?[A-Za-z0-9_-]{20,}")),
    ("stripe_key", re.compile(r"\b[srp]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/\S+")),
    ("google_key", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    # AWS's own docs and every SDK example use a bare space, not `=`, so the
    # generic keyword rule below (which requires `:` or `=`) never sees it.
    (
        "aws_secret",
        re.compile(r"(?i)\baws_secret_access_key\b\s*[:=]?\s*['\"]?[A-Za-z0-9/+=]{30,}"),
    ),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        # `(?!\[REDACTED)` so a value already replaced by a prior pass is not
        # re-matched — keeps the marker from technically satisfying the pattern
        # again (audit claim 2; the sub was already a no-op, this makes it clean).
        "url_password",
        re.compile(r"(?P<prefix>[a-z][a-z0-9+.-]*://[^/\s:@]+:)(?!\[REDACTED)[^@\s/]+@", re.I),
    ),
    (
        # Any snake/kebab identifier ending in a secret-ish word catches the
        # forms the bare-keyword rule missed because `\btoken\b` will not match
        # inside `aws_session_token` (audit claim 1): AWS session/refresh/id
        # tokens, `x_api_key`, `access_key_id`. Value must follow a : or =.
        "generic_secret",
        re.compile(
            r"(?i)\b(?:[a-z0-9]+[_-])*"
            r"(api[_-]?(?:key|token)|access[_-]?(?:token|key[_-]?id)|auth[_-]?token|"
            r"session[_-]?token|refresh[_-]?token|id[_-]?token|token|password|"
            r"passwd|pwd|secret|client[_-]?secret)\b\s*[:=]\s*['\"]?[^'\"\s]+"
        ),
    ),
]

# A redaction marker must never look like a secret to the next pass.
_MARKER = re.compile(r"\[REDACTED:[a-z_]+\]")

# `foo@bar.py` and `git@github.com` are not email addresses. The first is a file
# reference, the second is every SSH clone URL in every transcript — redacting it
# would corrupt `repo_url` provenance, which is the cross-device key (ADR-020).
_CODE_TLDS = frozenset(
    "py ts js jsx tsx mjs cjs md json toml yml yaml sh rs go java rb php c h cpp "
    "hpp cs kt swift sql txt lock cfg ini env log csv html css scss vue svelte".split()
)
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+)\.([A-Za-z]{2,24})\b")

# Any user's home, not just this machine's — the point is that a card is portable.
_HOME_POSIX = re.compile(r"(?:/home|/Users)/[^/\s'\"<>:;,)\]}]+")
_HOME_WIN = re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s'\"<>:;,)\]}]+")


def _redact_assignment(match: Match[str]) -> str:
    head = re.split(r"[:=]", match.group(0), maxsplit=1)[0].rstrip()
    sep = ":" if ":" in match.group(0) and "=" not in match.group(0).split(":", 1)[0] else "="
    return f"{head}{sep}[REDACTED:generic_secret]"


def _redact_email(match: Match[str]) -> str:
    local, _, tld = match.groups()
    if local == "git" or tld.lower() in _CODE_TLDS:
        return match.group(0)
    return "[REDACTED:email]"


def redact(text: str) -> str:
    """Replace credential-shaped strings with typed markers. Idempotent."""
    out = text
    for secret_type, pattern in _PATTERNS:
        if secret_type == "url_password":
            out = pattern.sub(
                lambda m: f"{m.group('prefix')}[REDACTED:{secret_type}]@", out
            )
        elif secret_type == "generic_secret":
            out = pattern.sub(_redact_assignment, out)
        else:
            out = pattern.sub(f"[REDACTED:{secret_type}]", out)
    return _EMAIL.sub(_redact_email, out)


def scrub_home(text: str) -> str:
    """`/home/alice/x` -> `~/x`. Idempotent (a `~` has no home prefix to strip)."""
    out = _HOME_POSIX.sub("~", text)
    return _HOME_WIN.sub("~", out)


def sanitize(text: str) -> str:
    """Everything applied at the card-write boundary. See module docstring."""
    return scrub_home(redact(text))


def has_secret(text: str) -> bool:
    """True if `sanitize` would have removed a credential from `text`.

    Used by auto-review as a hard pre-LLM reject: a card body that still carries
    a marker means a secret reached the distiller, and no model gets a vote on
    whether that is publishable.
    """
    return bool(_MARKER.search(text))


def contains_raw_secret(text: str) -> bool:
    """True when a fresh credential pattern remains in text."""
    return redact(text) != text
