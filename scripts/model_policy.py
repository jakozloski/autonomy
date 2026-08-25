#!/usr/bin/env python3
"""Evaluate the mandatory Conductor model policy without making remote calls.

The command reads one JSON object from stdin and writes one JSON object to
stdout.  Every field in the input is observed by the caller; this module never
looks at the process environment, executes a vendor CLI, or uses the network.

Expected input shape::

    {
      "codex": {
        "installed": true,
        "version": "codex-cli 0.144.0",
        "live_catalog": {
          "models": [{
            "slug": "gpt-5.6-sol",
            "supported_reasoning_levels": [{"effort": "max"}]
          }]
        },
        "first_real_invocation": {"status": "success", "attempts": 1,
                                 "quota_reset_at": "2026-08-03T21:00:00Z",
                                 "observed_at": "2026-08-03T20:30:00Z"},
        "post_invocation": [{"status": "timeout",
                             "observed_at": "2026-08-03T20:00:00Z"}]
      },
      "claude": {
        "installed": true,
        "version": "2.1.170 (Claude Code)",
        "fable_access": "available",
        "zero_data_retention": "compatible",
        "environment": {
          "CLAUDE_CODE_SUBAGENT_MODEL": null,
          "CLAUDE_CODE_EFFORT_LEVEL": null
        },
        "host_capabilities": {
          "agent_model_selection": false,
          "agent_effort_selection": false,
          "agent_read_only_enforced": false
        },
        "explicit_waiver": false
      },
      "claude_reviewer": {
        "installed": true,
        "version": "2.1.170 (Claude Code)",
        "opus_access": "available",
        "zero_data_retention": "compatible",
        "environment": {
          "CLAUDE_CODE_SUBAGENT_MODEL": null,
          "CLAUDE_CODE_EFFORT_LEVEL": null
        },
        "host_capabilities": {
          "agent_model_selection": false,
          "agent_effort_selection": false,
          "agent_read_only_enforced": false
        },
        "explicit_waiver": false
      }
    }

The live catalog is the capability/model-selection gate: it proves an eligible
model exists, and never proves authentication, entitlement, or quota.  Access
is proven by a real invocation, and the workflow runs that invocation as an
entry smoke test BEFORE planning spend, so a dead credential fails fast instead
of surfacing after Phase 1.  ``first_real_invocation`` therefore carries the
MOST RECENT real-invocation observation (the entry smoke first, then each
Phase 2 review round); its ``attempts`` are scoped to that one invocation's
retry sequence and reset every round, never accumulated across stages.  A
timeout, transport, or runaway-ceiling failure is LIVENESS-CLASS: one
immediate retry with the exact same model and effort, then unbounded
wait-and-retry on the escalating backoff ladder — a slow or briefly-unavailable
route needs patience, not a human, so liveness-class failures never produce a
terminal block.  A ``quota_exhausted`` observation may carry the
provider-reported reset as ``quota_reset_at`` (timezone-aware ISO 8601): with
it, the observation MUST also carry ``observed_at`` and the codex-level
``post_invocation`` history list (canonical record fields ``status``,
``quota_reset_at``, ``observed_at``; empty on the first observation — absence
of either blocks as a malformed observation), and the verdict is a BOUNDED
wait to ``quota.wait_until`` = min(max(reset, observed_at + first ladder
rung), observed_at + ``MAX_QUOTA_WAIT_SECONDS``) — re-observed at wake, with
the helper itself taking the terminal no-usable-reset block on a second
consecutive elapsed raw reset (liveness-noise records skipped).  Without a
reported reset, quota blocks.
Every deterministic Codex failure (auth, entitlement, catalog, CLI) still
blocks immediately, and no path proposes a downgrade.

Once the entry smoke succeeds, its selection is FROZEN for the workflow.
``verify_frozen_selection`` re-checks a frozen model against a fresh catalog
and a frozen routing descriptor; it never re-selects, so a newer model that
appears mid-run is not silently adopted un-smoked.  Auto-forward happens at
the next workflow's entry.

Stream supervision lives here too, so the authentication boundary is tested
code rather than prose: ``classify_stream_event`` decides whether one
source-tagged line is an auth failure, and ``supervise_stream`` reads the real
stdout/stderr pipe handles concurrently and kills the process group on the
first ``auth_error``.  This module still makes no vendor calls and never spawns
Codex; the caller supplies the process handles and the kill callback.

Three legs are evaluated.  The base and Codex legs are gating: a failure on
either blocks the workflow.  ``claude`` is the base leg — the working side:
the implementing lineage, explorers, delegated work, and the fresh-context
escalation voice.  ``claude_reviewer`` is the reviewer leg — the always-runs
structured review and every Claude review fallback: the Claude reviewer next
to the mandatory Phase 2 Codex verdict (Phase 4 Codex participation is
tiered — Small and skill-only passes are Claude-only by design).  The
separation is the point: the model that writes the code is not a model that
approves it — under the nominal configuration; a reviewer degradation or an
explicit same-lineage waiver collapses the Claude legs onto one lineage, and
Codex then remains the independent cross-model verdict.

The reviewer leg degrades instead of gating.  An availability-class reviewer
failure (CLI missing/too old, access, entitlement, provider policy, ZDR) with
no explicit waiver does not block: when the base leg is ``ready``, the
aggregate rewrites the reviewer decision to state ``degraded`` — every Claude
review voice falls back to the selected base model in a fresh read-only
context, and the run continues with the degradation recorded (Claude review
is no longer cross-lineage from the base for that run).  Malformed reviewer
observations still block — garbage input is corrected, never degraded around
— an unverified observation ("unknown" access/ZDR) blocks until probed, and
an explicit reviewer waiver still preempts auto-degradation.

Model selection is floor-based, not pinned.  From the observed facts the
helper selects the newest eligible model at or above each floor: for Codex,
live-catalog models named ``gpt-<version>[-variant]`` that support the
required effort, excluding down-tier variants such as ``-mini``; for the base
leg, ``fable``/``mythos``-family entries in the optional
``claude.observed_models`` list; for the reviewer leg, ``opus``-family entries
in ``claude_reviewer.observed_models``.  Upgrades are automatic and reported
under each decision's ``selection`` key; anything below a floor still blocks,
and no path proposes a downgrade.  Each Claude leg forwards only along its own
lineage: a newer Opus never advances the base, and a newer Fable never
advances the reviewer.

A context-window variant suffix (``claude-opus-5[1m]``) denotes the same model
version as its bare slug.  It is accepted wherever the bare slug is, and is
never treated as either a downgrade or an upgrade.

``max`` is the pin for this workflow's single-problem voices: the deepest
non-delegating reasoning tier the floor model exposes (the depth axis runs
``high -> xhigh -> max``).  ``ultra`` is not a deeper rung on that axis — it
combines maximum reasoning with automatic delegation to parallel subagents
(the breadth axis), buys nothing on an indivisible review, and is
deliberately not part of this gate.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import selectors
import shlex
import signal
import sys
import time
from datetime import timedelta
from collections.abc import Callable, Mapping
from typing import IO, Any, NamedTuple

import state_schema
from state_schema import normalize_iso_timestamp


# Version 6: quota-with-reset observations REQUIRE observed_at and the
# post_invocation history list; the wait verdict payload is
# {reset_at, wait_until, clamped, reset_elapsed} (wait_until_reset removed);
# the no-usable-reset streak is decided here from the fed records.
SCHEMA_VERSION = 6

CODEX_MODEL = "gpt-5.6-sol"  # floor: newest eligible catalog model >= this wins
CODEX_FLOOR_VERSION = (5, 6)
CODEX_EFFORT = "max"  # deepest non-delegating tier on the floor model; never ultra
MIN_CODEX_VERSION = (0, 144, 0)
CODEX_MAX_ATTEMPTS = 2  # immediate same-config retries before backoff pacing kicks in
# Escalating wait-and-retry ladder for liveness-class failures (timeout /
# transport) once the immediate-retry budget is spent.  The last rung repeats
# forever: the gate waits instead of blocking, because waiting — spaced, with
# progress updates — is what "autonomous until done" means for a route that is
# merely slow or briefly down.  Deterministic failures never reach this ladder.
LIVENESS_BACKOFF_LADDER_SECONDS = (60, 300, 900, 1800)
# Single source of truth for the quota-wait ceiling is state_schema (the
# validator bounds persisted next_retry_at with the same value); this module
# REBINDS the name rather than re-declaring the literal.
MAX_QUOTA_WAIT_SECONDS = state_schema.MAX_QUOTA_WAIT_SECONDS
# Every quota wait is floored at the first backoff rung, elapsed resets
# included — derived, never a second literal.
QUOTA_WAIT_FLOOR_SECONDS = LIVENESS_BACKOFF_LADDER_SECONDS[0]
# Runaway backstop: the TOTAL-runtime ceiling per attempt (Timeout Heuristics
# PER_ATTEMPT_CEILING).  A byte-emitting child resets the idle clock forever,
# so only this bound can stop it; a kill here is liveness-class, not terminal.
PER_ATTEMPT_CEILING_SECONDS = 2700
# Variant tokens that mark down-tier siblings, never auto-forward targets.
CODEX_EXCLUDED_VARIANT_TOKENS = ("mini", "nano", "lite", "chat")

# Base Claude leg: the working side — the implementing lineage, explorers,
# delegated work, and the fresh-context escalation voice.  Gating — a failure
# here blocks.
BASE_MODEL = "claude-fable-5"  # floor: newest observed fable/mythos wins
BASE_FLOOR_VERSION = (5,)
BASE_MODEL_ALIAS = "fable"
BASE_EFFORT = "max"

# Reviewer Claude leg: the always-runs structured review and every Claude
# review fallback — one of the two reviewers, next to the Codex verdict.
# Gating — a failure here blocks.
REVIEWER_MODEL = "claude-opus-5"  # floor: newest observed opus >= this wins
REVIEWER_FLOOR_VERSION = (5,)
REVIEWER_MODEL_ALIAS = "opus"
REVIEWER_EFFORT = "max"

MIN_CLAUDE_VERSION = (2, 1, 170)
CLAUDE_READ_ONLY_ALLOWED_TOOLS = ("Read", "Glob", "Grep")
CLAUDE_READ_ONLY_DENIED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash",
    "WebFetch",
    "WebSearch",
    "Agent",
    "Task",
)
CLAUDE_READ_ONLY_ENV_UNSET = (
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_PERMISSION_MODE",
)

_SEMVER = re.compile(
    r"(?<!\d)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
)

_GPT_SLUG = re.compile(
    r"gpt-(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:-(?P<variant>[a-z0-9-]+))?"
)

# A trailing ``[1m]``-style suffix selects a context-window variant of the same
# model version.  It is captured so the variant can be recognised as equal, not
# ranked above or below the bare slug.
_OPUS_SLUG = re.compile(
    r"claude-opus-(?P<version>\d+(?:-\d+)*)(?P<variant>\[[0-9a-z]+\])?"
)

_FABLE_SLUG = re.compile(
    r"claude-(?P<family>fable|mythos)-(?P<version>\d+(?:-\d+)*)"
)

# Reasons template on {model}: these rows fire AFTER auto-forward selection,
# so the message must name the model actually invoked, not the floor literal.
_CODEX_BLOCKING_FAILURES = {
    "entitlement_denied": (
        "entitlement_denied",
        "{model} entitlement was denied by the real invocation",
        "request_access",
    ),
    "quota_exhausted": (
        "quota_exhausted",
        "{model} usage quota is exhausted and the provider reported no usable reset time",
        "wait_for_quota_reset_or_change_access",
    ),
    "model_unavailable": (
        "model_unavailable",
        "{model} was unavailable to the real invocation",
        "request_access",
    ),
    "authentication_error": (
        "authentication_error",
        "Codex authentication failed during the real invocation; auth failures are non-retryable",
        "repair_authentication",
    ),
    "error": (
        "invocation_error",
        "The real {model} invocation failed",
        "inspect_error_and_block",
    ),
}

_CODEX_RETRYABLE_FAILURES = {"timeout", "transport_error", "runaway"}

# ---------------------------------------------------------------------------
# Stream supervision (authentication boundary)
# ---------------------------------------------------------------------------

# Source constants are assigned by supervise_stream from the actual pipe handle
# a line arrived on.  They are never read from event content: an event may
# contain an attacker- or repository-supplied "source" field, and honouring it
# would let assistant text masquerade as a transport error (or hide one).
SOURCE_STDOUT_JSON = "stdout_json"
SOURCE_STDERR = "stderr"

# Only these event types can carry an authentication verdict.  Assistant
# messages, tool output, reasoning, and unknown well-formed events are always
# benign no matter what text they contain -- a plan or review that merely
# discusses "HTTP 401" must never kill a healthy invocation.
_AUTH_BEARING_EVENT_TYPES = frozenset(
    {"error", "transport_error", "stream_error", "response.failed"}
)

# Deterministic authentication signatures. Matched only inside auth-bearing
# structured events or CLI diagnostic stderr.
_AUTH_SIGNATURES = (
    "invalid_refresh_token",
    "invalid_api_key",
    "invalid_grant",
    "unauthorized",
    "authentication_error",
    "token has expired",
    "token expired",
    "credentials have been revoked",
    "incorrect api key",
)

# Word-bounded so identifier-embedded fragments ("unauthorized_count=0") never
# match: "_" is a word character, so \b refuses the boundary inside it.
# IGNORECASE (pass-3 opus #9 / codex #7): matching the ORIGINAL text keeps
# every reported offset exact — offsets derived from ``text.lower()`` drift
# when Unicode lowercasing changes string length (e.g. "İ" -> "i̇"), which
# could push a marker-anchored excerpt past the marker it preserved.
_AUTH_SIGNATURE_RE = re.compile(
    "|".join(rf"\b{re.escape(signature)}\b" for signature in _AUTH_SIGNATURES),
    re.IGNORECASE,
)

# Context-anchored HTTP 401 forms for _has_auth_signature.  "401 unauthorized"
# is already caught by the "unauthorized" signature above; these cover bare
# status renderings ("HTTP/1.1 401", "http 401", "status=401", "status code:
# 401", "error 401") without matching incidental numbers like "401ms".
# r14 F13: (?!\d) alone is not a token boundary — "status=401ms" and
# "error: 401_foo" both classified as auth failures. The status token
# must END at a delimiter or end-of-string: whitespace, common
# punctuation, or nothing.
_401_END = r"(?=$|[\s.,;:!?)\]}\"'/\\-])"
_HTTP_401_CONTEXT = re.compile(
    r"\bhttps?/[0-9.]+\s+401" + _401_END
    + r"|\bhttp\s+401" + _401_END
    + r"|\bstatus(?:[ _]code)?\s*[=:]?\s*401" + _401_END
    + r"|\berror\s*[=:]?\s*401" + _401_END
    + r"|\b401\s+unauthorized\b",
    re.IGNORECASE,
)

# A raw record larger than this means framing is broken (or hostile): bound the
# parser instead of accumulating an unbounded buffer.
MAX_RAW_RECORD_BYTES = 1 << 20  # 1 MiB

# Persisted diagnostic evidence is capped per channel.
MAX_EXCERPT_BYTES = 4096

CLASSIFY_EXIT_CLEAN = 0
CLASSIFY_EXIT_AUTH_ERROR = 3
CLASSIFY_EXIT_INTERNAL_FAILURE = 4
CLASSIFY_EXIT_TIMEOUT = 5
CLASSIFY_EXIT_RUNAWAY = 6

_URL_TAIL = re.compile(r"(?P<url>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"]*)")
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@")


def strip_url_secrets(text: str) -> str:
    """Remove userinfo, query strings, and fragments from any URL in *text*.

    Credential-named-field exclusion alone is not enough: a base URL such as
    ``https://user:token@host/path?key=SECRET#frag`` carries secrets in its
    value.  This runs BEFORE fingerprinting and before any excerpt is
    persisted.
    """

    def _clean(match: re.Match[str]) -> str:
        url = match.group("url")
        url = _URL_USERINFO.sub(r"\g<scheme>", url)
        for separator in ("?", "#"):
            head, sep, _ = url.partition(separator)
            if sep:
                url = head + sep + "[STRIPPED]"
        return url

    return _URL_TAIL.sub(_clean, text)


def classify_stream_event(source: str, line: str) -> str:
    """Classify one source-tagged line as ``auth_error``/``benign``/``internal_failure``.

    ``source`` must be one of the module's source constants; ``supervise_stream``
    derives it from the real file descriptor.  Only auth-bearing structured
    events on the JSON channel and CLI diagnostic stderr can yield
    ``auth_error``.  Unparseable JSON on the JSON channel means supervision
    itself failed, which is ``internal_failure`` -- not something to shrug off,
    because an unmonitored invocation is exactly what this boundary prevents.
    """

    if len(line.encode("utf-8", "surrogatepass")) > MAX_RAW_RECORD_BYTES:
        return "internal_failure"

    if source == SOURCE_STDERR:
        return "auth_error" if _has_auth_signature(line) else "benign"

    if source != SOURCE_STDOUT_JSON:
        return "internal_failure"

    stripped = line.strip()
    if not stripped:
        return "benign"

    try:
        event = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return "internal_failure"

    if not isinstance(event, dict):
        return "benign"

    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in _AUTH_BEARING_EVENT_TYPES:
        return "benign"

    return "auth_error" if _has_auth_signature(_auth_scope_text(event)) else "benign"


def _auth_scope_text(event: dict[str, Any]) -> str:
    """Collect only the error-bearing fields of an auth-bearing event."""

    parts: list[str] = []
    status = event.get("status") or event.get("status_code")
    if isinstance(status, (int, str)) and not isinstance(status, bool):
        parts.append(f"status={status}")
    for key in ("code", "error_code", "message", "error", "reason", "detail"):
        value = event.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for nested_key in ("code", "message", "type", "reason"):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    parts.append(nested)
                nested_status = value.get("status")
                if isinstance(nested_status, (int, str)) and not isinstance(
                    nested_status, bool
                ):
                    parts.append(f"status={nested_status}")
    # response.failed events nest their payload under "response": walk its
    # status and error fields too, or a nested invalid-key error reads benign.
    response = event.get("response")
    if isinstance(response, dict):
        for status_key in ("status", "status_code"):
            response_status = response.get(status_key)
            if isinstance(response_status, (int, str)) and not isinstance(
                response_status, bool
            ):
                parts.append(f"status={response_status}")
        response_error = response.get("error")
        if isinstance(response_error, str):
            parts.append(response_error)
        elif isinstance(response_error, dict):
            for nested_key in ("code", "message", "type", "reason"):
                nested = response_error.get(nested_key)
                if isinstance(nested, str):
                    parts.append(nested)
    return " ".join(parts)


def auth_signature_offset(text: str) -> int | None:
    """Earliest offset of a deterministic auth-failure marker, or ``None``.

    Single source for both the boolean predicate below and the
    marker-preserving stderr excerpt in ``monitor_runner`` (R7 codex #12):
    that excerpt must retain whatever detection actually fired on, so it
    anchors on this offset rather than re-deriving a span from the private
    regexes and drifting.  Offsets index the ORIGINAL string: the patterns
    carry ``re.IGNORECASE`` instead of searching ``text.lower()``, whose
    length can differ under Unicode lowercasing and shift the anchor
    (pass-3 opus #9 / codex #7).
    """

    match = _AUTH_SIGNATURE_RE.search(text)
    if match is not None:
        return match.start()
    # A bare 401 is NOT enough: transport messages legitimately contain
    # incidental numbers ("read timeout after 401ms", "backoff 401ms"), and
    # misclassifying one as auth converts a retryable transient failure into a
    # non-retryable kill.  Require an HTTP/status/error context or the literal
    # "401 unauthorized" phrase.
    context = _HTTP_401_CONTEXT.search(text)
    return context.start() if context is not None else None


def _has_auth_signature(text: str) -> bool:
    """True when *text* carries a deterministic authentication failure marker.

    Callers must pass only auth-bearing structured-event fields or diagnostic
    stderr — never assistant/tool content, which may discuss "401" innocently.
    """

    return auth_signature_offset(text) is not None


# admin#1495/algo#1216 finding 3806595004: the canonical redaction
# pattern list lives IN CODE so bounded_excerpt applies it at runtime;
# validate_package derives its doc byte-match from this same tuple, and
# state-and-safety.md's inventory is validated against it — one source.
REDACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ('aws_access_or_session_key', r"(AKIA|ASIA)[0-9A-Z]{16}"),
    ('authorization_bearer_header', r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
    ('slack_token', r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ('aws_secret_access_key', r"""(?i)AWS_SECRET_ACCESS_KEY["']?\s*[:=]\s*["']?[A-Za-z0-9/+=]{40}["']?"""),
    ('aws_session_token', r"""(?i)AWS_SESSION_TOKEN["']?\s*[:=]\s*["']?[A-Za-z0-9/+=]{16,4096}["']?"""),
    ('github_user_or_oauth_token', r"gh[pour]_[A-Za-z0-9]{20,255}"),
    ('github_server_token', r"ghs_([A-Za-z0-9]{20,255}|[A-Za-z0-9]+_[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),
    ('github_fine_grained_pat', r"github_pat_[A-Za-z0-9_]{20,255}"),
    ('linear_api_key', r"lin_api_[A-Za-z0-9_]{40,}"),
    ('openai_key', r"sk-((proj|svcacct)-)?[A-Za-z0-9_-]{20,}"),
    # Pass-4 series codex F1 (PR #3551): quoted branches are escape-aware -
    # a backslash-escaped quote inside the value must not terminate the
    # match early and leak the remainder.
    # r13 F12: EVERY non-empty anchored value redacts — the old
    # {4,}/{8,} minimums left short passwords publishable. The named
    # `keep` group preserves the label so operators still see WHICH
    # assignment was redacted; sanitize_for_publication re-emits it.
    ('password_assignment', r"""(?i)(?P<keep>[\w-]*password["']?\s*[:=]\s*)("(?:\\.|[^"\\\r\n])+"|'(?:\\.|[^'\\\r\n])+'|[^\s"']+)"""),
    ('cookie_header_value', r"""(?i)(?P<keep>\b(?:Set-)?Cookie:)[^\r\n]+"""),
    ('stripe_live_key', r"(sk|rk)_live_[A-Za-z0-9]{16,}"),
    ('gcp_api_key', r"AIza[0-9A-Za-z_-]{35}"),
    ('gcp_oauth_access_token', r"ya29\.[A-Za-z0-9_-]{20,}"),
    ('pem_private_key_block', r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ('anthropic_key', r"sk-ant-[A-Za-z0-9_-]{40,}"),
    ('jwt_base64url', r"eyJ[A-Za-z0-9_\-=]{10,}\.eyJ[A-Za-z0-9_\-=]{10,}\.[A-Za-z0-9_\-=]+"),
    # admin#1495 finding 3807823274: customer-PII detection was entirely
    # manual judgment; the targeted probe showed emails and phone numbers
    # surviving the sanitizer verbatim. Email and phone HAVE reliable
    # format anchors, so they join the executable list (phone: E.164, or
    # US forms anchored by parens/separators so bare numeric IDs never
    # match). Postal addresses and free-text identity stay judgment-scoped
    # — no anchor exists that does not swallow ordinary prose — and
    # generic `secret=` labels stay excluded per the documented
    # false-positive rule in state-and-safety.md.
    ('email_address', r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ('phone_number', r"\+[1-9]\d{7,14}|\(\d{3}\)[-.\s]?\d{3}[-.\s]\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
    ('stripe_webhook_secret', r"whsec_[A-Za-z0-9]{16,}"),
)


def sanitize_for_publication(text: str) -> str:
    """The ONE executable sanitizer for anything persisted or published.

    admin#1495 finding 3813789220: the format-anchored redactor was
    private to ``bounded_excerpt`` (diagnostic stderr only), so Prompt
    Ledger appends and PR-body trail syncs relied on manual judgment
    alone — a targeted probe published an uncommon secret assignment
    verbatim. Every ledger/PR/archive write now routes through this
    function (CLI: ``model_policy.py --sanitize``, stdin → stdout) before
    persisting. Scope is deliberate and stated: URL-embedded credentials
    plus the canonical format-anchored ``REDACTION_PATTERNS`` — this
    function does NOT claim free-text name/address detection; that
    remains the documented manual-judgment obligation on top of, never
    instead of, this pass.
    """

    for kind, pattern in REDACTION_PATTERNS:

        def _swap(match: "re.Match[str]", _kind: str = kind) -> str:
            # r13 F12: a pattern may name a `keep` group (the label part);
            # it is re-emitted so redaction removes the VALUE only.
            keep = match.groupdict().get("keep") or ""
            return f"{keep}[REDACTED: {_kind}]"

        text = re.sub(pattern, _swap, text)
    return text


def bounded_excerpt(existing: str, addition: str) -> str:
    """Append *addition* to *existing*, URL-stripped and byte-capped.

    Only recognised transport/error events and diagnostic stderr are ever
    passed here; assistant/tool/unknown payloads (which can embed repository
    source) never reach persisted evidence.

    Finding 3806595004: the format-anchored Secret/Token Redaction runs
    HERE, in code — prose deferral let a bare ``Authorization: Bearer ...``
    survive verbatim into persisted evidence. URL-embedded credentials are
    stripped first, then every canonical pattern is applied via the shared
    ``sanitize_for_publication`` chokepoint.
    """

    cleaned = sanitize_for_publication(strip_url_secrets(addition))
    combined = f"{existing}\n{cleaned}" if existing else cleaned
    encoded = combined.encode("utf-8", "surrogatepass")
    if len(encoded) <= MAX_EXCERPT_BYTES:
        return combined
    return encoded[:MAX_EXCERPT_BYTES].decode("utf-8", "ignore")


class _IncrementalLineReader:
    """UTF-8-safe, size-bounded line splitter for one pipe.

    Decoding happens incrementally so a multi-byte character split across two
    reads is not corrupted, and a record that grows past
    ``MAX_RAW_RECORD_BYTES`` without a newline is reported as overflow rather
    than buffered without bound.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self.overflowed = False

    def feed(self, chunk: bytes) -> list[str]:
        self._buffer += chunk
        lines: list[str] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline == -1:
                break
            raw, self._buffer = self._buffer[:newline], self._buffer[newline + 1 :]
            lines.append(raw.decode("utf-8", "replace").rstrip("\r"))
        if len(self._buffer) > MAX_RAW_RECORD_BYTES:
            self.overflowed = True
            self._buffer = b""
        return lines

    def flush(self) -> list[str]:
        """Return a final record that arrived without a trailing newline."""

        if not self._buffer:
            return []
        raw, self._buffer = self._buffer, b""
        return [raw.decode("utf-8", "replace").rstrip("\r")]


def _read_available(pipe: Any, size: int) -> bytes:
    """Read whatever is currently buffered, without waiting for a full *size*.

    ``BufferedReader.read(n)`` blocks until it has n bytes or hits EOF, so a
    40-byte auth event would sit unread until the child exited -- defeating
    prompt termination entirely.  ``read1`` returns after one underlying read.
    """

    reader = getattr(pipe, "read1", None)
    if callable(reader):
        return reader(size)
    return pipe.read(size)


def _accepts_timeout_kw(callable_obj: Callable[..., Any]) -> bool:
    """True when the callable's SIGNATURE accepts a ``timeout`` keyword.

    Probed via ``inspect.signature`` (never a trial call — R7 codex #9: a
    try/except ``TypeError`` probe both swallowed internal ``TypeError``s
    and silently fell back to an unbounded bare call). An unresolvable
    signature reads as not-capable, which fails closed under a ceiling.
    """

    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == parameter.VAR_KEYWORD:
            return True
        if parameter.name == "timeout" and parameter.kind in (
            parameter.POSITIONAL_OR_KEYWORD,
            parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _call_child_wait(
    child_wait: Callable[..., int | None],
    timeout_seconds: float,
    *,
    allow_unbounded: bool = False,
) -> tuple[str, int | None]:
    """Invoke the caller's wait with a bounded timeout when it supports one.

    Returns ``(status, returncode)``: ``"done"`` (returncode meaningful),
    ``"timeout"`` (the bounded window expired with the child still alive),
    ``"incapable"`` (no timeout support and unbounded calls are not allowed
    here — the caller must fail structurally rather than hang), or
    ``"error"`` (the wait itself failed).  ``subprocess.TimeoutExpired`` is
    matched BY NAME because this module never imports subprocess (the
    scanner's structural rule for files whose call names carry an eval
    substring).  A bare zero-argument callable is invoked directly ONLY when
    ``allow_unbounded`` is set (no ceiling was requested, so the caller owns
    boundedness); under a ceiling it lands ``"incapable"`` instead of
    reintroducing the unbounded post-EOF hang.
    """

    if _accepts_timeout_kw(child_wait):
        try:
            return ("done", child_wait(timeout=timeout_seconds))
        except Exception as error:
            if type(error).__name__ == "TimeoutExpired":
                return ("timeout", None)
            return ("error", None)
    if not allow_unbounded:
        return ("incapable", None)
    try:
        return ("done", child_wait())
    except Exception:
        return ("error", None)


def supervise_stream(
    stdout_pipe: IO[bytes] | None,
    stderr_pipe: IO[bytes] | None,
    kill_callback: Callable[[], None],
    child_wait: Callable[[], int | None] | None = None,
    *,
    child_pgid: int | None = None,
    read_size: int = 65536,
    idle_timeout_seconds: float | None = None,
    max_runtime_seconds: float | None = None,
) -> dict[str, Any]:
    """Supervise a running Codex process's real pipes; kill it on auth failure.

    Takes the ACTUAL pipe handles so each line's provenance is established by
    construction -- there is no caller-supplied tag to forge.  Both pipes are
    drained concurrently through a selector: reading one to exhaustion while the
    other fills its kernel buffer would deadlock, and a deadlocked supervisor
    cannot terminate promptly, which is the whole point of this function.

    Returns ``{"outcome", "exit_code", "excerpts", "auth_line_source"}`` where
    outcome is ``clean``/``auth_error``/``internal_failure``/``timeout``/
    ``runaway``.  Every failure outcome kills the process group; what happens
    next is outcome-dependent — ``auth_error`` and ``internal_failure`` follow
    the blocking failure matrix, while ``timeout`` and ``runaway`` are
    liveness-class (immediate retry, then the backoff ladder — never terminal).
    ``child_wait`` is the supervised process's own wait: when supplied and the
    streams close clean, a nonzero child status lands as ``internal_failure``
    instead of a false clean — EOF alone proves only that the pipes closed,
    not that the invocation succeeded.  That post-EOF wait honors the
    REMAINING total deadline in bounded chunks (R6-F4, mirroring the runner's
    ``_drain_child``): ``child_wait`` is called with a bounded ``timeout``
    keyword when it accepts one (``subprocess.Popen.wait``'s signature — the
    canonical value; a bare zero-argument callable is NEVER invoked while a
    deadline is active — it lands ``"incapable"`` and fails closed to the
    kill/reap path, pass-3 codex #13), and a
    child that closed its pipes but lives past the ceiling is killed as
    ``runaway`` — never waited on unboundedly.  The kill itself is guarded:
    ``ProcessLookupError`` means the child already died (the kill's goal
    state), and any other cleanup failure returns as the structured
    ``internal_failure`` this contract promises rather than raising.  Every
    kill — failure-path and post-EOF ceiling alike — is followed by a
    bounded reap through ``child_wait`` when one was supplied, so a killed
    gate child is collected instead of leaking a zombie into the long-lived
    session; a child that cannot be reaped within the bound lands as
    ``internal_failure`` (three same-signature strikes reach a human).
    ``idle_timeout_seconds`` bounds SILENCE, not total runtime: the clock
    resets on every byte received, so a slow-but-alive stream is never killed
    for being slow — a max-effort review legitimately runs for many minutes.
    ``child_pgid`` (admin#1495 r13 F9) is the process group captured by
    the caller AT SPAWN — before any wait can reap the leader (a reaped
    pid makes ``os.getpgid`` raise, which turned lazy group kills into
    silent no-ops while same-group descendants survived).  When supplied,
    EVERY non-clean result — the failure outcomes, the runaway/incapable
    waits, a raising wait, and clean streams followed by a nonzero exit —
    routes through guarded GROUP termination plus the bounded reap.  On
    supervisor-initiated kills the leader is still unreaped, which pins
    the id against reuse; on the one path where the wait reaped first, an
    existence probe skips a dead group, and the residual (a full PID-space
    wrap re-allocating this exact id between probe and kill) is
    documented, not defended.
    ``max_runtime_seconds`` is the orthogonal runaway backstop (Timeout
    Heuristics ``PER_ATTEMPT_CEILING``, canonically
    ``PER_ATTEMPT_CEILING_SECONDS``): it bounds TOTAL runtime, because a
    byte-emitting runaway holds the idle clock at zero forever; when both
    deadlines have expired the ceiling wins the tie.  A non-positive ceiling is
    already expired: the first supervision pass reports ``runaway``.  Both
    default to ``None`` (no internal bound) because the caller owns both
    deadlines.  This function
    never spawns a process and never inspects credentials.
    """

    excerpts: dict[str, str] = {SOURCE_STDOUT_JSON: "", SOURCE_STDERR: ""}
    readers = {
        SOURCE_STDOUT_JSON: _IncrementalLineReader(),
        SOURCE_STDERR: _IncrementalLineReader(),
    }
    outcome = "clean"
    auth_line_source: str | None = None

    def _guarded_kill() -> bool:
        """Kill the child; True unless the kill itself failed structurally."""
        try:
            kill_callback()
        except ProcessLookupError:
            pass  # already dead — the kill's goal state (reap still collects)
        except Exception:
            return False
        return True

    def _guarded_group_kill() -> bool:
        """admin#1495 r13 F9: guarded GROUP termination for every
        non-clean result. The pgid was captured at spawn; a zombie
        (unreaped) leader pins the id against reuse on supervisor-
        initiated paths, and the existence probe covers the post-reap
        path. The leader-targeted ``kill_callback`` still runs for
        callers that supplied no pgid."""
        ok = True
        if child_pgid is not None:
            try:
                os.killpg(child_pgid, 0)
            except ProcessLookupError:
                pass  # no members left — nothing to kill
            except Exception:
                ok = False
            else:
                try:
                    os.killpg(child_pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception:
                    ok = False
        if not _guarded_kill():
            ok = False
        return ok

    def _reap_after_kill(deadline_seconds: float = 30.0) -> bool:
        """R6-F4: bounded reap after every kill — without it each killed gate
        child leaks a zombie into the long-lived session. True when the child
        was observed to exit (or no wait handle was supplied — the caller
        owns the process object then)."""
        if child_wait is None:
            return True
        end = time.monotonic() + deadline_seconds
        # Bounded like every loop in this package (scanner rule): the
        # deadline is the real bound; the range is an unreachable backstop.
        for _reap_round in range(100_000):
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            status, _code = _call_child_wait(child_wait, min(5.0, remaining))
            if status == "done":
                return True
            if status in ("error", "incapable"):
                return False
            # "timeout": chunk expired with the child still alive — loop.
        return False

    selector = selectors.DefaultSelector()
    registered = 0
    for source, pipe in ((SOURCE_STDOUT_JSON, stdout_pipe), (SOURCE_STDERR, stderr_pipe)):
        if pipe is not None:
            selector.register(pipe, selectors.EVENT_READ, source)
            registered += 1
    if registered == 0:
        # Zero observable channels means zero supervision: fail closed instead
        # of reporting a clean (never-supervised) invocation.
        outcome = "internal_failure"

    def _next_deadline() -> float | None:
        if idle_timeout_seconds is None:
            return None
        return time.monotonic() + idle_timeout_seconds

    deadline = _next_deadline()
    total_deadline = (
        None
        if max_runtime_seconds is None
        else time.monotonic() + max_runtime_seconds
    )
    try:
        while registered and outcome == "clean":
            now = time.monotonic()
            # Ceiling before idle: on a tie the runaway backstop wins, because
            # a byte-emitting child can hold the idle clock at zero forever.
            if total_deadline is not None and now >= total_deadline:
                outcome = "runaway"
                break
            idle_remaining = None if deadline is None else deadline - now
            if idle_remaining is not None and idle_remaining <= 0:
                outcome = "timeout"
                break
            total_remaining = (
                None if total_deadline is None else total_deadline - now
            )
            candidates = [
                r for r in (idle_remaining, total_remaining) if r is not None
            ]
            remaining = min(candidates) if candidates else None
            ready = selector.select(remaining)
            if not ready:
                # Nothing readable for the whole window; attribute the expiry.
                if (
                    total_deadline is not None
                    and time.monotonic() >= total_deadline
                ):
                    outcome = "runaway"
                else:
                    outcome = "timeout"
                break
            # Activity: restart the idle clock, so a long-but-alive stream
            # (a max-effort review emitting over several minutes) is never killed.
            deadline = _next_deadline()
            for key, _ in ready:
                source = key.data
                pipe = key.fileobj
                try:
                    chunk = _read_available(pipe, read_size)
                except OSError:
                    outcome = "internal_failure"
                    break
                if not chunk:
                    selector.unregister(pipe)
                    registered -= 1
                    for line in readers[source].flush():
                        verdict = classify_stream_event(source, line)
                        outcome, auth_line_source, excerpts = _apply_verdict(
                            verdict, source, line, outcome, auth_line_source, excerpts
                        )
                        if outcome != "clean":
                            break
                    continue

                for line in readers[source].feed(chunk):
                    verdict = classify_stream_event(source, line)
                    outcome, auth_line_source, excerpts = _apply_verdict(
                        verdict, source, line, outcome, auth_line_source, excerpts
                    )
                    if outcome != "clean":
                        break
                if outcome == "clean" and readers[source].overflowed:
                    outcome = "internal_failure"
                if outcome != "clean":
                    break
    except Exception:  # pragma: no cover - defensive: supervision must fail closed
        outcome = "internal_failure"
    finally:
        selector.close()

    if outcome != "clean":
        # R2 round-2 finding 3737466443: the kill ran outside every guard,
        # so a raising callback escaped with a raw traceback instead of the
        # structured result the docstring promises — in exactly the failure
        # states this function exists to report. ProcessLookupError means
        # the child already died (a CLI that prints its error and exits
        # races the kill decision; on Darwin even an un-reaped zombie
        # raises it) — that is the kill's goal state, not a failure. Any
        # other cleanup failure becomes the structured internal_failure.
        # R6-F4: the kill is then followed by a bounded reap — a SIGKILLed
        # child left unwaited is a zombie in the long-lived session.
        # admin#1495 r13 F9: the kill is the GROUP kill — the leader may
        # already be dead while same-group descendants hold credentials.
        if not _guarded_group_kill():
            outcome = "internal_failure"
        if not _reap_after_kill():
            outcome = "internal_failure"
    elif child_wait is not None:
        # R2 round-2 finding 3737466493: EOF only proves the streams
        # closed. A child that emits benign output and exits nonzero (a
        # config error, a post-stream crash) previously reported
        # outcome "clean"/exit 0 and could pass a mandatory gate. When the
        # caller supplies the process's wait, a nonzero child status after
        # clean streams is an internal_failure (blocking failure matrix) —
        # the streams gave no classifiable reason, so the tooling itself
        # is broken from the gate's point of view.
        # R6-F4: this wait honors the REMAINING total deadline in bounded
        # chunks (mirroring the runner's _drain_child). A child that closed
        # its pipes and lives past the ceiling is killed as runaway and
        # boundedly reaped — the ceiling advertised to the caller bounds
        # the WHOLE invocation, not just the streaming phase. A failing
        # wait also kills and reaps: the child may still be alive, and
        # skipping the kill here was the second half of the finding.
        child_returncode = None
        # Bounded like every loop in this package (scanner rule): the
        # ceiling is the real bound; the range is an unreachable backstop.
        for _wait_round in range(1_000_000):
            remaining = (
                None
                if total_deadline is None
                else total_deadline - time.monotonic()
            )
            if remaining is not None and remaining <= 0:
                outcome = "runaway"
                if not _guarded_group_kill():
                    outcome = "internal_failure"
                if not _reap_after_kill():
                    outcome = "internal_failure"
                break
            chunk = 5.0 if remaining is None else min(5.0, remaining)
            status, code = _call_child_wait(
                child_wait, chunk, allow_unbounded=remaining is None
            )
            if status == "done":
                child_returncode = code
                break
            if status in ("error", "incapable"):
                # "incapable" = a ceiling is active but the wait cannot be
                # bounded (R7 codex #9): fail structurally — kill and reap —
                # rather than hang past the advertised ceiling.
                outcome = "internal_failure"
                _guarded_group_kill()
                _reap_after_kill()
                break
            # "timeout": the bounded chunk expired with the child alive —
            # re-check the ceiling and wait again.
        else:
            outcome = "internal_failure"
            # R2 #1328 finding 3767068801: a wait that RAISES proves nothing
            # about the child's lifecycle — it may still be running with
            # credentials, and the clean-streams path skipped cleanup before
            # this branch. Route through the same process-group kill as every
            # non-clean outcome; ProcessLookupError is the kill's goal state,
            # and any other cleanup failure adds nothing beyond the
            # internal_failure already recorded.
            _guarded_group_kill()
            _reap_after_kill()
        if outcome == "clean" and child_returncode not in (0, None):
            # admin#1495 r13 F9: clean streams plus a nonzero exit is a
            # NON-CLEAN result, and the leader's own exit says nothing
            # about group descendants — which previously survived this
            # branch with zero kill calls. Route through the same guarded
            # group termination and bounded reap as every other failure
            # (the reap returns immediately: the wait above already
            # collected the leader).
            outcome = "internal_failure"
            _guarded_group_kill()
            _reap_after_kill()

    exit_code = {
        "clean": CLASSIFY_EXIT_CLEAN,
        "auth_error": CLASSIFY_EXIT_AUTH_ERROR,
        "internal_failure": CLASSIFY_EXIT_INTERNAL_FAILURE,
        "timeout": CLASSIFY_EXIT_TIMEOUT,
        "runaway": CLASSIFY_EXIT_RUNAWAY,
    }[outcome]
    return {
        "outcome": outcome,
        "exit_code": exit_code,
        "excerpts": excerpts,
        "auth_line_source": auth_line_source,
    }


def _apply_verdict(
    verdict: str,
    source: str,
    line: str,
    outcome: str,
    auth_line_source: str | None,
    excerpts: dict[str, str],
) -> tuple[str, str | None, dict[str, str]]:
    """Fold one line's verdict into the running supervision outcome.

    Retains diagnostic stderr as evidence but never JSON-channel benign
    payloads, which can embed repository source.
    """

    if verdict == "auth_error":
        excerpts[source] = bounded_excerpt(excerpts[source], line)
        return "auth_error", source, excerpts
    if verdict == "internal_failure":
        return "internal_failure", auth_line_source, excerpts
    if source == SOURCE_STDERR:
        # Diagnostic stderr is retainable evidence; JSON-channel benign events
        # are assistant/tool/unknown payloads and never persisted.
        excerpts[source] = bounded_excerpt(excerpts[source], line)
    return outcome, auth_line_source, excerpts


# ---------------------------------------------------------------------------
# Frozen routing descriptor
# ---------------------------------------------------------------------------

# Closed schema. Anything not listed is rejected rather than persisted: this
# object lives in the state file, so an open schema is a credential-leak path.
DESCRIPTOR_FIELDS = frozenset(
    {"provider", "model", "effort", "policy_overrides", "routing_fingerprint"}
)
ROUTING_FINGERPRINT_FIELDS = (
    "base_url",
    "wire_api",
    "profile",
    "codex_home",
    "routing_env",
)
ALLOWLISTED_POLICY_OVERRIDE_KEYS = frozenset({"model_reasoning_effort"})
# Credential-source category is observability only and deliberately NOT part of
# the frozen comparison: a normal `none -> oauth` re-login must be able to clear
# a human:codex-login block instead of permanently mismatching.
CREDENTIAL_SOURCE_CATEGORIES = frozenset({"oauth", "env_key", "none"})
_SECRET_SHAPED_KEY = re.compile(
    r"(?i)(key|token|secret|password|credential|authorization|cookie)"
)


def routing_fingerprint(routing: dict[str, Any]) -> str:
    """Digest sanitized, non-secret routing configuration.

    A provider NAME is not a route: the same name can point at a different base
    URL, profile, or CODEX_HOME after a resume.  Values are URL-stripped and
    secret-shaped keys are dropped BEFORE digesting, so no credential material
    reaches the digest input.
    """

    canonical: dict[str, Any] = {}
    for field in ROUTING_FINGERPRINT_FIELDS:
        value = routing.get(field)
        if value is None:
            continue
        if field == "routing_env":
            if not isinstance(value, dict):
                raise ValueError("routing_env must be an object")
            canonical[field] = {
                str(k): strip_url_secrets(str(v))
                for k, v in sorted(value.items())
                if not _SECRET_SHAPED_KEY.search(str(k))
            }
        else:
            canonical[field] = strip_url_secrets(str(value))
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_descriptor(
    provider: str,
    model: str,
    effort: str,
    routing: dict[str, Any],
    policy_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the closed, non-secret execution descriptor."""

    overrides = policy_overrides or {}
    unknown = set(overrides) - ALLOWLISTED_POLICY_OVERRIDE_KEYS
    if unknown:
        raise ValueError(f"policy override keys not allowlisted: {sorted(unknown)}")
    return {
        "provider": strip_url_secrets(str(provider)),
        "model": str(model),
        "effort": str(effort),
        "policy_overrides": {str(k): str(v) for k, v in sorted(overrides.items())},
        "routing_fingerprint": routing_fingerprint(routing),
    }


def validate_descriptor(descriptor: Any) -> list[str]:
    """Return schema errors for a persisted descriptor (empty list = valid)."""

    if not isinstance(descriptor, dict):
        return ["descriptor must be an object"]
    errors: list[str] = []
    unknown = set(descriptor) - DESCRIPTOR_FIELDS
    if unknown:
        errors.append(f"unknown descriptor fields: {sorted(unknown)}")
    for field in ("provider", "model", "effort", "routing_fingerprint"):
        if not isinstance(descriptor.get(field), str) or not descriptor.get(field):
            errors.append(f"descriptor.{field} must be a non-empty string")
    overrides = descriptor.get("policy_overrides", {})
    if not isinstance(overrides, dict):
        errors.append("descriptor.policy_overrides must be an object")
    else:
        not_allowed = set(overrides) - ALLOWLISTED_POLICY_OVERRIDE_KEYS
        if not_allowed:
            errors.append(f"policy override keys not allowlisted: {sorted(not_allowed)}")
    for key in descriptor:
        if _SECRET_SHAPED_KEY.search(str(key)):
            errors.append(f"secret-shaped descriptor field rejected: {key}")
    return errors


def verify_frozen_selection(
    frozen_model: str,
    frozen_descriptor: dict[str, Any],
    live_catalog: Any,
    observed_descriptor: dict[str, Any],
) -> dict[str, Any]:
    """Verify a frozen selection still holds. Never re-selects.

    Phase 2 re-probes cheaply and calls this: the frozen model must still be
    catalog-eligible and the routing descriptor must still match.  A newer
    eligible model appearing mid-run is deliberately NOT adopted -- it has not
    been smoke-tested on this route -- so auto-forward waits for the next
    workflow entry.
    """

    schema_errors = validate_descriptor(frozen_descriptor) + validate_descriptor(
        observed_descriptor
    )
    if schema_errors:
        return {
            "state": "blocked",
            "reason_code": "invalid_descriptor",
            "reason": "; ".join(schema_errors),
            "next_action": "correct_observation_input",
        }

    # The descriptor must pin the very model/effort being verified: matching
    # frozen/observed descriptors that both name a DIFFERENT model or a lower
    # effort would otherwise re-verify a route the smoke never proved.
    frozen_overrides = frozen_descriptor.get("policy_overrides")
    override_contradicts = (
        isinstance(frozen_overrides, dict)
        and "model_reasoning_effort" in frozen_overrides
        # An explicit null is as disqualifying as a wrong value: the override
        # key, when present, must pin the required effort.
        and frozen_overrides.get("model_reasoning_effort") != CODEX_EFFORT
    )
    if (
        frozen_descriptor.get("model") != frozen_model
        or frozen_descriptor.get("effort") != CODEX_EFFORT
        or override_contradicts
    ):
        return {
            "state": "blocked",
            "reason_code": "descriptor_model_mismatch",
            "reason": (
                f"The frozen descriptor must pin {frozen_model} at {CODEX_EFFORT} "
                "(including any model_reasoning_effort override); it names a "
                "different model or effort"
            ),
            "next_action": "start_new_workflow_entry_preflight",
        }

    if not _codex_model_is_eligible(frozen_model, live_catalog):
        return {
            "state": "blocked",
            "reason_code": "frozen_model_ineligible",
            "reason": (
                f"The frozen model {frozen_model} is no longer present in the live "
                f"catalog with {CODEX_EFFORT} reasoning"
            ),
            "next_action": "start_new_workflow_entry_preflight",
        }

    mismatched = sorted(
        field
        for field in DESCRIPTOR_FIELDS
        if frozen_descriptor.get(field) != observed_descriptor.get(field)
    )
    if mismatched:
        return {
            "state": "blocked",
            "reason_code": "descriptor_mismatch",
            "reason": f"Execution descriptor changed since the entry smoke: {mismatched}",
            "next_action": "start_new_workflow_entry_preflight",
        }

    return {
        "state": "ready",
        "reason_code": "frozen_selection_verified",
        "reason": (
            f"The frozen model {frozen_model} remains eligible and the routing "
            "descriptor is unchanged"
        ),
        "next_action": "reuse_frozen_selection",
        "selection": {"selected_model": frozen_model, "reason": "frozen_selection"},
    }


def apply_auth_recovery(
    frozen_descriptor: dict[str, Any],
    observed_descriptor: dict[str, Any],
    previous_category: str,
    observed_category: str,
    smoke_status: str,
) -> dict[str, Any]:
    """Allow a credential-category change ONLY through the login verifier.

    The ``human:codex-login`` postcondition is "the user re-authenticated and a
    fresh smoke succeeds on the same route".  Routing must be unchanged; the
    category may change (that is the recovery).  A category change observed
    anywhere else is an anomaly, not a recovery.
    """

    schema_errors = validate_descriptor(frozen_descriptor) + validate_descriptor(
        observed_descriptor
    )
    if schema_errors:
        return {
            "state": "blocked",
            "reason_code": "invalid_descriptor",
            "reason": "; ".join(schema_errors),
            "next_action": "correct_observation_input",
        }

    for category in (previous_category, observed_category):
        if category not in CREDENTIAL_SOURCE_CATEGORIES:
            return {
                "state": "blocked",
                "reason_code": "invalid_credential_category",
                "reason": f"Unknown credential-source category: {category}",
                "next_action": "correct_observation_input",
            }

    # Compare the WHOLE descriptor, exactly as verify_frozen_selection does: a
    # smoke run on a different model, effort, provider, or override proves
    # nothing about the frozen route.  Credential category is deliberately not
    # in DESCRIPTOR_FIELDS — changing it is the recovery.
    mismatched = sorted(
        field
        for field in DESCRIPTOR_FIELDS
        if frozen_descriptor.get(field) != observed_descriptor.get(field)
    )
    if mismatched:
        return {
            "state": "blocked",
            "reason_code": "descriptor_mismatch",
            "reason": (
                "Execution descriptor changed during authentication recovery: "
                f"{mismatched}"
            ),
            "next_action": "start_new_workflow_entry_preflight",
        }

    if smoke_status != "success":
        return {
            "state": "blocked",
            "reason_code": "authentication_error",
            "reason": "Authentication recovery smoke did not succeed",
            "next_action": "repair_authentication",
        }

    return {
        "state": "ready",
        "reason_code": "authentication_recovered",
        "reason": (
            f"Credential source recovered from {previous_category} to "
            f"{observed_category} on an unchanged route"
        ),
        "next_action": "clear_human_codex_login_block",
        "credential_source": observed_category,
    }


def _access_failures(prefix: str, label: str) -> dict[Any, tuple[str, str]]:
    """Access-failure table for one leg, so the two legs cannot drift apart."""

    unavailable = (f"{prefix}_unavailable", f"{label} is unavailable")
    return {
        False: unavailable,
        "unavailable": unavailable,
        "entitlement_denied": (
            f"{prefix}_entitlement_denied",
            f"{label} entitlement was denied",
        ),
        "provider_policy_denied": (
            f"{prefix}_provider_policy_denied",
            f"Provider policy does not permit {label}",
        ),
        "unknown": (
            f"{prefix}_access_unverified",
            f"{label} access has not been verified",
        ),
    }


def _zdr_failures(label: str) -> dict[Any, tuple[str, str]]:
    """Zero-data-retention failure table for one leg."""

    incompatible = (
        "zdr_incompatible",
        f"{label} does not satisfy the required zero-data-retention policy",
    )
    return {
        False: incompatible,
        "incompatible": incompatible,
        "denied": incompatible,
        "unknown": (
            "zdr_unverified",
            f"{label} zero-data-retention compatibility is unverified",
        ),
    }


_BASE_ACCESS_FAILURES = _access_failures("fable", "Claude Fable 5")
_BASE_ZDR_FAILURES = _zdr_failures("Claude Fable 5")

_REVIEWER_ACCESS_FAILURES = _access_failures("opus", "Claude Opus 5")
_REVIEWER_ZDR_FAILURES = _zdr_failures("Claude Opus 5")

# Observed-unavailability reason codes the reviewer leg may degrade around.
# The *_unverified codes are deliberately absent: an unprobed observation
# blocks until it is probed — auto-degrading on "unknown" would let a caller
# skip the reviewer without ever checking whether Opus was available.
_DEGRADABLE_OBSERVED_FAILURES = frozenset(
    (
        "cli_missing",
        "version_unparseable",
        "cli_too_old",
        "opus_unavailable",
        "opus_entitlement_denied",
        "opus_provider_policy_denied",
        "zdr_incompatible",
    )
)

_ACCESS_STATUS_VALUES = (
    "available, unavailable, entitlement_denied, provider_policy_denied, or unknown"
)
_ZDR_STATUS_VALUES = "compatible, incompatible, denied, or unknown"


def _semver(value: Any) -> tuple[tuple[int, int, int], bool] | None:
    """Return the numeric core and whether the observed version is prerelease."""

    if not isinstance(value, str):
        return None
    match = _SEMVER.search(value)
    if match is None:
        return None
    core = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    return core, match.group("prerelease") is not None


def _version_at_least(value: Any, minimum: tuple[int, int, int]) -> bool:
    parsed = _semver(value)
    if parsed is None:
        return False
    core, is_prerelease = parsed
    if core != minimum:
        return core > minimum
    return not is_prerelease


def _too_old_reason(tool: str, minimum: tuple[int, ...]) -> str:
    """Render a cli_too_old reason from the minimum-version constant itself.

    The version literal lives only in MIN_CODEX_VERSION / MIN_CLAUDE_VERSION, so
    raising a floor cannot leave a message quoting the superseded version.
    """

    return f"{tool} must be at least {'.'.join(str(part) for part in minimum)}"


def _codex_arguments(model: str) -> list[str]:
    """codex exec argv for one invocation.

    Defined once so the two emission sites cannot drift apart, and derived
    from ``CODEX_EFFORT`` so an effort repoint cannot leave a stale literal.
    """

    # R2 round-2 finding 3737466478: without the sandbox pin, an
    # invocation reconstructed from this argv inherits the operator's
    # ambient codex sandbox (workspace-write would let a review voice
    # modify the implementation it judges). Pinned here so every
    # exec-shaped consumer carries it by construction.
    return [
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{CODEX_EFFORT}"',
        "-s",
        "read-only",
    ]


def _codex_base(version: Any) -> dict[str, Any]:
    return {
        "state": "blocked",
        "reason_code": None,
        "reason": None,
        "model": CODEX_MODEL,
        "effort": CODEX_EFFORT,
        "observed_version": version if isinstance(version, str) else None,
        "live_catalog_verified": False,
        "execution_path": "codex_exec",
        "arguments": _codex_arguments(CODEX_MODEL),
        "next_action": None,
        "retry": {
            "attempts": 0,
            "max_attempts": CODEX_MAX_ATTEMPTS,
            "remaining": 0,
        },
        "downgrade_allowed": False,
        "fallback_model": None,
        "selection": None,
    }


def _block_codex(
    decision: dict[str, Any], reason_code: str, reason: str, next_action: str
) -> dict[str, Any]:
    retry = decision["retry"]
    retry["remaining"] = 0
    decision.update(
        {
            "state": "blocked",
            "reason_code": reason_code,
            "reason": reason,
            "next_action": next_action,
        }
    )
    return decision


def _supports_required_effort(model: dict[str, Any]) -> bool:
    levels = model.get("supported_reasoning_levels")
    if not isinstance(levels, list):
        return False
    return any(
        isinstance(level, dict) and level.get("effort") == CODEX_EFFORT
        for level in levels
    )


def _slug_meets_floor_policy(slug: str) -> bool:
    """GPT-family slug at/above the floor with no down-tier variant token.

    The single eligibility predicate shared by selection and frozen
    re-verification: at exactly the floor version only the known floor slug
    qualifies, and ``-mini``-style variants never do.  Without this shared
    check, a tampered frozen state naming a below-floor or excluded model
    would re-verify on catalog membership alone.
    """

    match = _GPT_SLUG.fullmatch(slug)
    if match is None:
        return False
    version = (int(match.group("major")), int(match.group("minor") or 0))
    variant = match.group("variant") or ""
    if version < CODEX_FLOOR_VERSION:
        return False
    if version == CODEX_FLOOR_VERSION and slug != CODEX_MODEL:
        return False
    return not any(
        token in variant.split("-") for token in CODEX_EXCLUDED_VARIANT_TOKENS
    )


def _codex_model_is_eligible(slug: str, catalog: Any) -> bool:
    """True when *slug* satisfies floor policy AND is in *catalog* with the required effort.

    Used by :func:`verify_frozen_selection` to re-check an already-frozen model
    without re-running selection.
    """

    if not isinstance(slug, str) or not _slug_meets_floor_policy(slug):
        return False
    if not isinstance(catalog, dict):
        return False
    models = catalog.get("models")
    if not isinstance(models, list):
        return False
    return any(
        isinstance(model, dict)
        and model.get("slug") == slug
        and _supports_required_effort(model)
        for model in models
    )


def _select_codex_model(catalog: Any) -> str | None:
    """Return the newest eligible catalog slug at or above the floor, or None.

    Eligibility: GPT-family slug, version >= the floor, required effort
    supported, and no down-tier variant token.  At exactly the floor version
    only the known floor slug qualifies (same-version siblings are not proven
    upgrades).  Ties at newer versions prefer the ``-sol`` lineage, then bare
    slugs, then lexicographic order — deterministic by construction.
    """

    if not isinstance(catalog, dict):
        return None
    models = catalog.get("models")
    if not isinstance(models, list):
        return None

    best: tuple[tuple[Any, ...], str] | None = None
    for model in models:
        if not isinstance(model, dict):
            continue
        slug = model.get("slug")
        if not isinstance(slug, str):
            continue
        if not _slug_meets_floor_policy(slug):
            continue
        if not _supports_required_effort(model):
            continue
        match = _GPT_SLUG.fullmatch(slug)
        version = (int(match.group("major")), int(match.group("minor") or 0))
        variant = match.group("variant") or ""
        variant_rank = 2 if variant == "sol" else 1 if variant == "" else 0
        key = (version, variant_rank, slug)
        if best is None or key > best[0]:
            best = (key, slug)
    return None if best is None else best[1]


def evaluate_codex(raw: Any) -> dict[str, Any]:
    """Evaluate Codex preflight and authoritative invocation observations."""

    config = raw if isinstance(raw, dict) else {}
    version = config.get("version")
    decision = _codex_base(version)

    if config.get("installed") is not True:
        return _block_codex(
            decision,
            "cli_missing",
            "Codex CLI is not installed",
            "install_codex_cli",
        )

    if _semver(version) is None:
        return _block_codex(
            decision,
            "version_unparseable",
            "Codex CLI version could not be parsed as semantic versioning",
            "inspect_codex_installation",
        )
    if not _version_at_least(version, MIN_CODEX_VERSION):
        return _block_codex(
            decision,
            "cli_too_old",
            _too_old_reason("Codex CLI", MIN_CODEX_VERSION),
            "upgrade_codex_cli",
        )

    selected_model = _select_codex_model(config.get("live_catalog"))
    if selected_model is None:
        return _block_codex(
            decision,
            "live_catalog_missing_capability",
            "The live Codex catalog lacks an eligible model at or above "
            f"GPT-5.6 Sol with {CODEX_EFFORT} reasoning",
            "request_access_or_refresh_live_catalog",
        )
    decision["live_catalog_verified"] = True
    decision["model"] = selected_model
    decision["arguments"] = _codex_arguments(selected_model)
    decision["selection"] = {
        "floor_model": CODEX_MODEL,
        "selected_model": selected_model,
        "reason": (
            "floor_model"
            if selected_model == CODEX_MODEL
            else "newer_model_auto_selected"
        ),
    }

    invocation = config.get("first_real_invocation", {})
    if not isinstance(invocation, dict):
        return _block_codex(
            decision,
            "invalid_invocation_observation",
            "first_real_invocation must be an object",
            "correct_observation_input",
        )
    status = invocation.get("status", "not_run")
    if not isinstance(status, str):
        return _block_codex(
            decision,
            "invalid_invocation_status",
            "Invocation status must be a string",
            "correct_observation_input",
        )
    default_attempts = 0 if status == "not_run" else 1
    attempts = invocation.get("attempts", default_attempts)
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
        or (status == "not_run" and attempts != 0)
        or (status != "not_run" and attempts < 1)
    ):
        return _block_codex(
            decision,
            "invalid_invocation_attempts",
            "Invocation attempts must be zero before the probe and at least one afterward",
            "correct_observation_input",
        )
    decision["retry"] = {
        "attempts": attempts,
        "max_attempts": CODEX_MAX_ATTEMPTS,
        "remaining": max(0, CODEX_MAX_ATTEMPTS - attempts),
    }

    if status == "not_run":
        decision.update(
            {
                "state": "probe_required",
                "reason_code": "first_real_invocation_required",
                "reason": (
                    "Run the entry smoke invocation as the authoritative access, "
                    "entitlement, and quota test before any planning spend"
                ),
                "next_action": "run_first_real_invocation",
            }
        )
        return decision

    if status == "success":
        decision.update(
            {
                "state": "ready",
                "reason_code": "authoritative_invocation_succeeded",
                "reason": f"The real {decision['model']} invocation succeeded",
                "next_action": "continue",
            }
        )
        return decision

    if status == "quota_exhausted":
        reset_at = invocation.get("quota_reset_at")
        if reset_at is not None:
            parsed_reset = normalize_iso_timestamp(reset_at)
            if parsed_reset is None:
                return _block_codex(
                    decision,
                    "invalid_quota_reset_at",
                    "quota_reset_at must be a timezone-aware ISO 8601 "
                    "timestamp when present",
                    "correct_observation_input",
                )
            observed_at = normalize_iso_timestamp(invocation.get("observed_at"))
            if observed_at is None:
                return _block_codex(
                    decision,
                    "invalid_quota_observation",
                    "quota_exhausted with a reported reset requires a "
                    "timezone-aware ISO 8601 observed_at on the invocation "
                    "observation — the wait bound is computed from it",
                    "correct_observation_input",
                )
            history = config.get("post_invocation")
            if not isinstance(history, list):
                # Absence and an empty list are DISTINCT: an omitted history
                # (or any non-list) blocks, because defaulting it to [] would
                # silently disable the no-usable-reset terminal for every
                # caller that forgot to feed the records.
                return _block_codex(
                    decision,
                    "invalid_quota_observation",
                    "quota_exhausted with a reported reset requires the "
                    "post_invocation history list (empty on the first "
                    "observation) so the consecutive-elapsed decision is "
                    "made here, not in prose",
                    "correct_observation_input",
                )
            prior_elapsed = False
            for record in reversed(history):
                if not isinstance(record, dict) or not isinstance(
                    record.get("status"), str
                ):
                    return _block_codex(
                        decision,
                        "invalid_quota_observation",
                        "post_invocation history entries must be mappings "
                        "with a string status",
                        "correct_observation_input",
                    )
                record_status = record["status"]
                if record_status in _CODEX_RETRYABLE_FAILURES:
                    # Liveness noise (timeout/transport_error/runaway) neither
                    # forms nor breaks the streak — Dawid's R3 path 2.
                    continue
                if record_status == "quota_exhausted":
                    record_reset = normalize_iso_timestamp(
                        record.get("quota_reset_at")
                    )
                    record_observed = normalize_iso_timestamp(
                        record.get("observed_at")
                    )
                    if record_reset is not None and record_observed is not None:
                        # Elapsed-ness is judged at the record's OWN
                        # observation time, never the current clock.
                        prior_elapsed = record_reset <= record_observed
                    # An unjudgeable prior (pre-fix record shape) breaks the
                    # streak conservatively: the terminal block requires
                    # clean evidence.
                break
            reset_elapsed = parsed_reset <= observed_at
            try:
                floor = observed_at + timedelta(seconds=QUOTA_WAIT_FLOOR_SECONDS)
                ceiling = observed_at + timedelta(seconds=MAX_QUOTA_WAIT_SECONDS)
            except OverflowError:
                # A parseable-but-absurd observed_at (year 9999) must produce
                # a fail-closed verdict, never a traceback.
                return _block_codex(
                    decision,
                    "invalid_quota_observation",
                    "observed_at is too far in the future to bound a wait",
                    "correct_observation_input",
                )
            if reset_elapsed and prior_elapsed:
                return _block_codex(
                    decision,
                    "quota_exhausted",
                    f"{decision['model']} usage quota reports repeated "
                    "already-elapsed resets — no usable reset time",
                    "wait_for_quota_reset_or_change_access",
                )
            wait_until = min(max(parsed_reset, floor), ceiling)
            clamped = ceiling < parsed_reset
            reason = (
                f"{decision['model']} usage quota is exhausted with a "
                f"provider-reported reset at {reset_at}; wait until "
                f"{wait_until.isoformat()} (chunked, with progress) and retry "
                "the exact same configuration — never block, never downgrade"
            )
            if clamped:
                reason += (
                    "; the reset exceeds one bounded sleep, so the wait is "
                    "clamped to the MAX_QUOTA_WAIT_SECONDS ceiling and the "
                    "route is re-observed at wake"
                )
            decision.update(
                {
                    "state": "retry",
                    "reason_code": "quota_wait_for_reset",
                    "reason": reason,
                    "next_action": "wait_for_quota_reset",
                    "quota": {
                        "reset_at": reset_at,
                        "wait_until": wait_until.isoformat(),
                        "clamped": clamped,
                        "reset_elapsed": reset_elapsed,
                    },
                }
            )
            return decision
        # No reported reset: fall through to the terminal quota block below.

    if status == "internal_failure":
        # r13 F10: internal_failure joins the FINITE signature-bound retry
        # policy the prose already promised — strikes 1 and 2 retry the
        # exact same configuration, the third consecutive same-signature
        # strike blocks for a human, and a CHANGED normalized signature
        # resets the streak (a different failure is a different problem,
        # not progress toward the same block). Liveness noise between
        # internal failures neither forms nor breaks the streak, matching
        # the quota-streak rule above.

        def _normalized_signature(value: Any) -> str:
            if isinstance(value, str) and value.strip():
                return " ".join(value.split()).casefold()
            return "internal_failure:unclassified"

        normalized = _normalized_signature(invocation.get("failure_signature"))
        history = config.get("post_invocation")
        # r14 F8: the history list is REQUIRED beyond the first
        # observation, mirroring the quota-streak contract above —
        # defaulting a missing/malformed history to "streak 1" restarted
        # identical failures at strike one forever and the terminal block
        # never fired. Empty is legal only for the first observation.
        if not isinstance(history, list):
            # r14 F8 re-eval: the attempts>1 guard was toothless because
            # `attempts` itself DEFAULTS to 1 for every non-`not_run`
            # invocation — a CLI-level caller omitting both fields
            # restarted identical failures at strike one forever. The
            # history list is therefore REQUIRED whenever `attempts` was
            # defaulted rather than explicitly supplied: an explicit
            # attempts=1 with no history is a legitimate first
            # observation; a defaulted one is an unauditable restart.
            if attempts > 1 or "attempts" not in invocation:
                return _block_codex(
                    decision,
                    "invalid_internal_failure_observation",
                    "internal_failure requires the post_invocation history"
                    " list (empty only on an explicit first observation"
                    " with attempts=1) so the signature streak is decided"
                    " here, not in prose",
                    "correct_observation_input",
                )
            history = []
        streak = 1
        for record in reversed(history):
            if not isinstance(record, dict) or not isinstance(
                record.get("status"), str
            ):
                return _block_codex(
                    decision,
                    "invalid_internal_failure_observation",
                    "post_invocation history entries must be mappings with"
                    " a string status",
                    "correct_observation_input",
                )
            record_status = record.get("status")
            if record_status in _CODEX_RETRYABLE_FAILURES:
                continue
            if record_status != "internal_failure":
                break
            if (
                _normalized_signature(record.get("failure_signature"))
                != normalized
            ):
                break
            streak += 1
        if streak >= 3:
            return _block_codex(
                decision,
                "internal_failure",
                f"{decision['model']} invocation failed internally with the"
                f" same normalized signature {normalized!r} three"
                " consecutive times — a human must inspect the installation"
                " or environment",
                "inspect_codex_installation",
            )
        decision.update(
            {
                "state": "retry",
                "reason_code": "internal_failure",
                "reason": (
                    f"Codex internal failure (strike {streak} of 3 for"
                    f" signature {normalized!r}); retry the exact same"
                    f" {decision['model']}/{CODEX_EFFORT} configuration"
                ),
                "next_action": "retry_same_invocation_once",
                "internal_failure": {
                    "signature": normalized,
                    "strike": streak,
                },
            }
        )
        return decision

    if status in _CODEX_RETRYABLE_FAILURES:
        if attempts < CODEX_MAX_ATTEMPTS:
            decision.update(
                {
                    "state": "retry",
                    "reason_code": status,
                    "reason": (
                        "Transient Codex failure; retry once with the exact same "
                        f"{decision['model']}/{CODEX_EFFORT} configuration"
                    ),
                    "next_action": "retry_same_invocation_once",
                }
            )
            return decision
        # Liveness-class wait-and-retry: the immediate-retry budget is spent,
        # so pace further attempts along the escalating backoff ladder instead
        # of blocking.  attempts counts every try so far; the first backoff
        # wait uses rung 0, and the ladder's last rung repeats forever.
        rung = min(
            attempts - CODEX_MAX_ATTEMPTS,
            len(LIVENESS_BACKOFF_LADDER_SECONDS) - 1,
        )
        decision.update(
            {
                "state": "retry",
                "reason_code": f"{status}_backoff",
                "reason": (
                    "Liveness-class Codex failure persists after the immediate "
                    f"retry; wait {LIVENESS_BACKOFF_LADDER_SECONDS[rung]}s "
                    "(chunked, with progress) and retry the exact same "
                    f"{decision['model']}/{CODEX_EFFORT} configuration — never "
                    "block, never downgrade"
                ),
                "next_action": "wait_and_retry_with_backoff",
                "backoff": {
                    "wait_seconds": LIVENESS_BACKOFF_LADDER_SECONDS[rung],
                    "ladder_seconds": list(LIVENESS_BACKOFF_LADDER_SECONDS),
                    "rung": rung,
                    "last_rung_repeats": True,
                },
            }
        )
        return decision

    blocking = _CODEX_BLOCKING_FAILURES.get(status)
    if blocking is not None:
        code, reason, action = blocking
        return _block_codex(
            decision, code, reason.format(model=decision["model"]), action
        )
    return _block_codex(
        decision,
        "unknown_invocation_status",
        f"Unknown Codex invocation status: {status!r}",
        "correct_observation_input",
    )


# Owner-pinned child execution (scripts/monitor_runner.py). The slice budget
# keeps every runner invocation strictly inside the parent's own per-attempt
# ceiling, with margin for verification and commit — the runner derives each
# child's ceiling as min(PER_ATTEMPT_CEILING_SECONDS, slice deadline − now −
# cleanup margin) and never launches below the minimum viable budget.
MONITOR_SLICE_BUDGET_SECONDS = 2400
MONITOR_SLICE_CLEANUP_MARGIN_SECONDS = 120
MONITOR_CHILD_MIN_VIABLE_SECONDS = 240
# Same silence bound as every supervised model-gate call (Timeout
# Heuristics: liveness_idle_kill_seconds) — a monitor child that goes fully
# silent this long is dead, not slow.
MONITOR_CHILD_IDLE_TIMEOUT_SECONDS = 180
# Re-export of the schema-owned 3-strike limit: the runner may not import
# state_schema directly (structural rule — subprocess files stay free of the
# evaluator entry-point names), so this module is its constant bridge.
MONITOR_CHILD_FAILURE_LIMIT = state_schema.MONITOR_CHILD_FAILURE_LIMIT


def monitor_child_arguments(
    model: str, effort: str = REVIEWER_EFFORT, resume_id: str | None = None
) -> list[str]:
    """Owner-pinned monitor-child argv tail — the WORKING sibling of
    ``_explicit_cli_arguments``, defined once so the runner cannot drift.

    Differences from the read-only voice tail are the contract: the child is
    a working orchestrator (it dispatches write-capable base workers), so no
    read-only clamp; its session PERSISTS (``--resume`` on later ticks is the
    owner cache lineage); its output streams as JSON so the runner reads
    session id and served model from protocol events, never from
    model-authored text.
    """

    arguments = []
    if resume_id is not None:
        arguments += ["--resume", resume_id]
    arguments += [
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--output-format",
        "stream-json",
        "--verbose",
        "--disable-slash-commands",
        "--no-chrome",
        # admin#1495 finding 3806647922: the takeover flow checks out the
        # TARGET PR before monitoring, so project-level settings under the
        # child's cwd are attacker-writable — a crafted PR could grant
        # itself hooks, MCP servers, and wider permissions. The child loads
        # USER-level settings only; deployments grant the monitor child's
        # write permissions there (Keeper VM bootstrap already provisions
        # user-level settings), never through the mutable checkout.
        "--setting-sources",
        "user",
    ]
    return arguments


def monitor_child_prompt(
    skill_dir: str,
    state_path: str,
    candidate_path: str,
    attempt_id: str,
    tick_ordinal: int,
) -> str:
    """The single source of the child-tick contract (marker-pinned).

    Every load-bearing clause lives here, not in per-call prose: the Loading
    Contract reads, the one-iteration bound, the candidate-only write rule,
    the runner-owned ``monitor_cli`` block, and the strict verdict schema.
    """

    # admin#1495 r12 F17: the digest command is a copy-runnable shell line —
    # raw path interpolation broke on spaces and let quotes/dollars/
    # semicolons/newlines inject shell syntax. shlex.join quotes each argv
    # element; the prose path mentions stay raw (they are read, not run).
    digest_command = shlex.join(
        [
            "python3",
            f"{skill_dir}/scripts/state_schema.py",
            "--monitor-digest",
            candidate_path,
        ]
    )
    return (
        f"You are the Phase 6 monitor orchestrator for the autonomy workflow"
        f" whose skill package is at {skill_dir}. First follow that package's"
        f" SKILL.md Loading Contract for Phase 6 (read state-and-safety.md,"
        f" monitor-ci-feedback.md, monitor-exit-handoffs.md completely)."
        f" Then execute EXACTLY ONE monitor iteration per those references"
        f" for the workflow state file at {state_path} — one pass, no second"
        f" iteration, no waiting loop (the supervising runner owns waits)."
        f" Persist the FULL updated state to {candidate_path} and NEVER"
        f" write {state_path} itself; carry the monitor_cli block over"
        f" value-identical (it is runner-owned). Compute the candidate"
        f" digest with: {digest_command} . Your final message must be"
        f" ONLY this JSON object and nothing else:"
        f' {{"schema_version": 1, "attempt_id": "{attempt_id}",'
        f' "tick_ordinal": {tick_ordinal},'
        f' "outcome": <exactly one of the strings continue, terminal, or'
        f" blocked — the single word your iteration's result selects, never"
        f" this description>,"
        f' "post_workflow_digest": "<the digest>"}}'
    )


def _explicit_cli_arguments(model: str, effort: str = BASE_EFFORT) -> list[str]:
    """Read-only explicit-CLI invocation tail, defined once so every emission
    site — both Claude legs — cannot drift apart."""
    return [
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--permission-mode",
        "plan",
        "--allowedTools",
        ",".join(CLAUDE_READ_ONLY_ALLOWED_TOOLS),
        "--disallowedTools",
        ",".join(CLAUDE_READ_ONLY_DENIED_TOOLS),
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
    ]


class _ClaudeLegSpec(NamedTuple):
    """Everything that differs between the two gating Claude legs.

    Both legs run the same evaluation flow; keeping the differences in one
    declarative record means a rule added to one leg cannot silently miss the
    other.
    """

    role: str
    floor_model: str
    alias: str
    effort: str
    access_key: str
    invalid_access_code: str
    access_failures: Mapping[Any, tuple[str, str]]
    zdr_failures: Mapping[Any, tuple[str, str]]
    select: Callable[[Any], tuple[str, str]]
    floor_variant: Callable[[Any], bool]
    variant_note: str
    above_floor_note: str
    named_fallback_reason: str
    fallback_requirement: str
    fallback_at_or_above_floor: Callable[[Any], bool]
    restore_action: str
    ready_code: str
    agent_action: str
    cli_action: str
    degrades_to_base: bool


def _claude_leg_base(version: Any, spec: _ClaudeLegSpec) -> dict[str, Any]:
    return {
        "state": "blocked",
        "reason_code": None,
        "reason": None,
        "role": spec.role,
        "blocking": True,
        "model": spec.floor_model,
        "effort": spec.effort,
        "observed_version": version if isinstance(version, str) else None,
        "execution_path": None,
        "arguments": [],
        "environment_unset": [],
        "read_only": {
            "required": True,
            "permission_mode": "plan",
            "allowed_tools": list(CLAUDE_READ_ONLY_ALLOWED_TOOLS),
            "denied_tools": list(CLAUDE_READ_ONLY_DENIED_TOOLS),
        },
        "subagent_model_override": None,
        "next_action": None,
        "waiver_required": False,
        "waiver_granted": False,
        "downgrade_allowed": False,
        "fallback_model": None,
        "degradable_to_base": False,
        "selection": None,
    }


def _reviewer_floor_variant(slug: Any) -> bool:
    """True when ``slug`` is the reviewer floor, or a context-window variant.

    ``claude-opus-5[1m]`` is the same model version as ``claude-opus-5`` with a
    larger context window, so it is accepted anywhere the bare slug is.
    """

    if not isinstance(slug, str):
        return False
    match = _OPUS_SLUG.fullmatch(slug)
    if match is None:
        return False
    version = tuple(int(part) for part in match.group("version").split("-"))
    return version == REVIEWER_FLOOR_VERSION


def _never_floor_variant(slug: Any) -> bool:
    """The fable lineage has no context-window variant slugs to equate."""

    return False


def _select_reviewer_model(observed_models: Any) -> tuple[str, str]:
    """Return the newest observed opus model at or above the reviewer floor.

    Falls back to the floor when nothing newer is observed.  A ``[1m]``-style
    context-window suffix marks the same version, so it never outranks the bare
    slug; ties prefer the bare slug (the standard-cost default), then
    lexicographic order — deterministic by construction.
    """

    if not isinstance(observed_models, list):
        return REVIEWER_MODEL, "floor_model"

    best: tuple[tuple[Any, ...], str] | None = None
    for item in observed_models:
        if not isinstance(item, str):
            continue
        match = _OPUS_SLUG.fullmatch(item)
        if match is None:
            continue
        version = tuple(int(part) for part in match.group("version").split("-"))
        if version < REVIEWER_FLOOR_VERSION:
            continue
        variant_rank = 0 if match.group("variant") else 1
        key = (version, variant_rank, item)
        if best is None or key > best[0]:
            best = (key, item)

    if best is None or best[1] == REVIEWER_MODEL:
        return REVIEWER_MODEL, "floor_model"
    if _reviewer_floor_variant(best[1]):
        # Same version as the floor, larger context window — not an upgrade.
        return best[1], "floor_model_variant"
    return best[1], "newer_model_auto_selected"


def _select_base_model(observed_models: Any) -> tuple[str, str]:
    """Return the newest observed fable/mythos model at or above the base floor.

    Falls back to the floor when nothing newer is observed.  Ties on version
    prefer the ``fable`` family (generally available), then lexicographic
    order — deterministic by construction.
    """

    if not isinstance(observed_models, list):
        return BASE_MODEL, "floor_model"

    best: tuple[tuple[Any, ...], str] | None = None
    for item in observed_models:
        if not isinstance(item, str):
            continue
        match = _FABLE_SLUG.fullmatch(item)
        if match is None:
            continue
        version = tuple(int(part) for part in match.group("version").split("-"))
        if version < BASE_FLOOR_VERSION:
            continue
        family_rank = 1 if match.group("family") == "fable" else 0
        key = (version, family_rank, item)
        if best is None or key > best[0]:
            best = (key, item)

    if best is None or best[1] == BASE_MODEL:
        return BASE_MODEL, "floor_model"
    return best[1], "newer_model_auto_selected"


def _at_or_above_base_floor(slug: Any) -> bool:
    """True when ``slug`` is a fable/mythos model at or above the base floor.

    A waiver may authorize a different lineage; it may not authorize a version
    below a floor.  Nothing in this module proposes a downgrade, and an explicit
    human waiver is not an exception to that.
    """

    if not isinstance(slug, str):
        return False
    match = _FABLE_SLUG.fullmatch(slug)
    if match is None:
        return False
    version = tuple(int(part) for part in match.group("version").split("-"))
    return version >= BASE_FLOOR_VERSION


def _at_or_above_reviewer_floor(slug: Any) -> bool:
    """True when ``slug`` is an opus model at or above the reviewer floor.

    The same downgrade rule applies in this direction: a waiver may cross to
    the opus lineage, never to a version below its floor.  A context-window
    variant of an eligible version is accepted like its bare slug.
    """

    if not isinstance(slug, str):
        return False
    match = _OPUS_SLUG.fullmatch(slug)
    if match is None:
        return False
    version = tuple(int(part) for part in match.group("version").split("-"))
    return version >= REVIEWER_FLOOR_VERSION


def _waive_or_block_claude(
    decision: dict[str, Any],
    config: dict[str, Any],
    spec: _ClaudeLegSpec,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    waiver = config.get("explicit_waiver", False)
    if not isinstance(waiver, bool):
        decision.update(
            {
                "reason_code": "invalid_waiver_value",
                "reason": "explicit_waiver must be a JSON boolean",
                "next_action": "correct_observation_input",
            }
        )
        return decision
    if waiver:
        fallback = config.get("waiver_fallback")
        if not isinstance(fallback, dict):
            return _block_claude_input(
                decision,
                "named_fallback_required",
                spec.named_fallback_reason,
            )
        fallback_model = fallback.get("model")
        observed_models = config.get("observed_models")
        if (
            fallback.get("available") is not True
            or fallback.get("explicitly_authorized") is not True
            or not isinstance(fallback_model, str)
            or not spec.fallback_at_or_above_floor(fallback_model)
            or not isinstance(observed_models, list)
            or not all(isinstance(model, str) for model in observed_models)
            or fallback_model not in observed_models
            or fallback.get("effort") != spec.effort
            or fallback.get("execution_path") != "explicit_cli"
            or config.get("installed") is not True
        ):
            return _block_claude_input(
                decision,
                "invalid_named_fallback",
                spec.fallback_requirement,
            )
        decision.update(
            {
                "state": "waived",
                "reason_code": reason_code,
                "reason": reason,
                "model": fallback_model,
                "effort": spec.effort,
                "selection": {
                    "floor_model": spec.floor_model,
                    "selected_model": fallback_model,
                    "reason": "explicit_waiver_fallback",
                },
                "execution_path": "explicit_cli",
                "arguments": _explicit_cli_arguments(fallback_model, spec.effort),
                "environment_unset": list(CLAUDE_READ_ONLY_ENV_UNSET),
                "next_action": "invoke_explicit_named_fallback",
                "waiver_granted": True,
                # A named waiver fallback is floor-enforced substitution, not a
                # downgrade: the flag stays False everywhere.
                "downgrade_allowed": False,
                "fallback_model": fallback_model,
            }
        )
        return decision
    decision.update(
        {
            "state": "blocked",
            "reason_code": reason_code,
            "reason": reason,
            "next_action": spec.restore_action,
            "waiver_required": True,
            # Observed availability failure with no waiver engaged: the
            # aggregate may rewrite this block into a recorded base-lineage
            # degradation — reviewer leg only, observed failures only
            # (unverified observations stay blocking until probed), and
            # only onto a ready base.
            "degradable_to_base": (
                spec.degrades_to_base
                and reason_code in _DEGRADABLE_OBSERVED_FAILURES
            ),
        }
    )
    return decision


def _block_claude_input(
    decision: dict[str, Any], reason_code: str, reason: str
) -> dict[str, Any]:
    """Block malformed observations; a waiver cannot legitimize invalid input."""

    decision.update(
        {
            "state": "blocked",
            "reason_code": reason_code,
            "reason": reason,
            "next_action": "correct_observation_input",
        }
    )
    return decision


def _evaluate_claude_leg(raw: Any, spec: _ClaudeLegSpec) -> dict[str, Any]:
    """Evaluate one gating Claude leg and choose Agent or explicit CLI.

    Both legs are gating: any failure blocks unless an explicit waiver names an
    observed model from the other leg's lineage at or above that lineage's
    floor, at max effort.
    """

    config = raw if isinstance(raw, dict) else {}
    version = config.get("version")
    decision = _claude_leg_base(version, spec)

    installed = config.get("installed")
    if not isinstance(installed, bool):
        return _block_claude_input(
            decision,
            "invalid_installed_status",
            "installed must be a JSON boolean",
        )
    if installed is not True:
        return _waive_or_block_claude(
            decision,
            config,
            spec,
            "cli_missing",
            "Claude Code is not installed",
        )
    if not isinstance(version, str):
        return _block_claude_input(
            decision,
            "invalid_version_value",
            "Claude Code version must be a string",
        )
    if _semver(version) is None:
        return _waive_or_block_claude(
            decision,
            config,
            spec,
            "version_unparseable",
            "Claude Code version could not be parsed as semantic versioning",
        )
    if not _version_at_least(version, MIN_CLAUDE_VERSION):
        return _waive_or_block_claude(
            decision,
            config,
            spec,
            "cli_too_old",
            _too_old_reason("Claude Code", MIN_CLAUDE_VERSION),
        )

    access = config.get(spec.access_key, "unknown")
    if access is not True and access != "available":
        if access is False:
            code, reason = spec.access_failures[False]
        elif isinstance(access, str) and access in spec.access_failures:
            code, reason = spec.access_failures[access]
        else:
            return _block_claude_input(
                decision,
                spec.invalid_access_code,
                f"{spec.access_key} must be {_ACCESS_STATUS_VALUES}",
            )
        return _waive_or_block_claude(decision, config, spec, code, reason)

    zdr = config.get("zero_data_retention", "unknown")
    if zdr is not True and zdr != "compatible":
        if zdr is False:
            code, reason = spec.zdr_failures[False]
        elif isinstance(zdr, str) and zdr in spec.zdr_failures:
            code, reason = spec.zdr_failures[zdr]
        else:
            return _block_claude_input(
                decision,
                "invalid_zdr_status",
                f"zero_data_retention must be {_ZDR_STATUS_VALUES}",
            )
        return _waive_or_block_claude(decision, config, spec, code, reason)

    waiver = config.get("explicit_waiver", False)
    if not isinstance(waiver, bool):
        return _waive_or_block_claude(
            decision,
            config,
            spec,
            "invalid_waiver_value",
            "explicit_waiver must be a JSON boolean",
        )

    environment = config.get("environment", {})
    if not isinstance(environment, dict):
        return _block_claude_input(
            decision,
            "invalid_environment",
            "environment must be an object",
        )
    override = environment.get("CLAUDE_CODE_SUBAGENT_MODEL")
    # Validate before comparing: an unhashable value here would raise out of
    # the whole gate, which is strictly worse than blocking — the caller would
    # get a traceback instead of a decision for any leg.
    if override is not None and not isinstance(override, str):
        return _block_claude_input(
            decision,
            "invalid_subagent_override",
            "CLAUDE_CODE_SUBAGENT_MODEL must be a string or null",
        )
    effort_override = environment.get("CLAUDE_CODE_EFFORT_LEVEL")
    if effort_override is not None and not isinstance(effort_override, str):
        return _block_claude_input(
            decision,
            "invalid_effort_override",
            "CLAUDE_CODE_EFFORT_LEVEL must be a string or null",
        )
    host_capabilities = config.get("host_capabilities", {})
    if not isinstance(host_capabilities, dict):
        return _block_claude_input(
            decision,
            "invalid_host_capabilities",
            "host_capabilities must be an object",
        )
    selected_model, selection_reason = spec.select(config.get("observed_models"))
    at_floor = selected_model == spec.floor_model
    floor_versioned = at_floor or spec.floor_variant(selected_model)
    model_flag = spec.alias if at_floor else selected_model
    decision["model"] = selected_model
    decision["selection"] = {
        "floor_model": spec.floor_model,
        "selected_model": selected_model,
        "reason": selection_reason,
    }

    exact_override = override if isinstance(override, str) else ""
    compatible_overrides = {"", selected_model}
    if floor_versioned:
        compatible_overrides |= {spec.alias, spec.floor_model}
    # A context-window variant of the floor names the same model version, so it
    # is not a conflicting override.
    model_conflict = exact_override not in compatible_overrides and not (
        floor_versioned and spec.floor_variant(exact_override)
    )
    effort_conflict = effort_override not in {None, spec.effort}
    agent_selection_verified = (
        host_capabilities.get("agent_model_selection") is True
        and host_capabilities.get("agent_effort_selection") is True
        and host_capabilities.get("agent_read_only_enforced") is True
    )
    conflict = model_conflict or effort_conflict or not agent_selection_verified

    if conflict:
        execution_path = "explicit_cli"
        arguments = _explicit_cli_arguments(model_flag, spec.effort)
        next_action = spec.cli_action
        environment_unset = list(CLAUDE_READ_ONLY_ENV_UNSET)
    else:
        execution_path = "agent_tool"
        arguments = [f"model={model_flag}", f"effort={spec.effort}"]
        next_action = spec.agent_action
        environment_unset = []

    if at_floor:
        selection_note = ""
    elif spec.floor_variant(selected_model):
        selection_note = spec.variant_note
    else:
        selection_note = spec.above_floor_note

    decision.update(
        {
            "state": "ready",
            "reason_code": ("explicit_cli_required" if conflict else spec.ready_code),
            "reason": (
                "Unverified model/effort/read-only agent selection or a conflicting override requires the explicit read-only Claude CLI path"
                if conflict
                else f"{selected_model} at max effort is available{selection_note}"
            ),
            "execution_path": execution_path,
            "arguments": arguments,
            "environment_unset": environment_unset,
            "subagent_model_override": override,
            "next_action": next_action,
        }
    )
    return decision


_BASE_LEG = _ClaudeLegSpec(
    role="base",
    floor_model=BASE_MODEL,
    alias=BASE_MODEL_ALIAS,
    effort=BASE_EFFORT,
    access_key="fable_access",
    invalid_access_code="invalid_fable_access",
    access_failures=_BASE_ACCESS_FAILURES,
    zdr_failures=_BASE_ZDR_FAILURES,
    select=_select_base_model,
    floor_variant=_never_floor_variant,
    variant_note="",
    above_floor_note=" (auto-selected above the Fable 5 floor)",
    named_fallback_reason=(
        "An explicit waiver requires an observed named Opus fallback"
    ),
    fallback_requirement=(
        "The waived fallback must be an available, explicitly authorized "
        "Claude Opus model at or above the Opus 5 floor, at max effort"
    ),
    fallback_at_or_above_floor=_at_or_above_reviewer_floor,
    restore_action="request_explicit_waiver_or_restore_fable_access",
    ready_code="base_ready",
    agent_action="invoke_base_agent",
    cli_action="invoke_explicit_base_cli",
    degrades_to_base=False,
)

_REVIEWER_LEG = _ClaudeLegSpec(
    role="reviewer",
    floor_model=REVIEWER_MODEL,
    alias=REVIEWER_MODEL_ALIAS,
    effort=REVIEWER_EFFORT,
    access_key="opus_access",
    invalid_access_code="invalid_opus_access",
    access_failures=_REVIEWER_ACCESS_FAILURES,
    zdr_failures=_REVIEWER_ZDR_FAILURES,
    select=_select_reviewer_model,
    floor_variant=_reviewer_floor_variant,
    variant_note=" (context-window variant of the Opus 5 floor)",
    above_floor_note=" (auto-selected above the Opus 5 floor)",
    named_fallback_reason=(
        "An explicit waiver requires an observed named Fable or Mythos fallback"
    ),
    fallback_requirement=(
        "The waived fallback must be an available, explicitly authorized "
        "Claude Fable or Mythos model at or above the Fable 5 floor, at max effort"
    ),
    fallback_at_or_above_floor=_at_or_above_base_floor,
    restore_action="request_explicit_waiver_or_restore_opus_access",
    ready_code="reviewer_ready",
    agent_action="invoke_reviewer_agent",
    cli_action="invoke_explicit_reviewer_cli",
    degrades_to_base=True,
)


def evaluate_claude(raw: Any) -> dict[str, Any]:
    """Evaluate the base Fable/max leg — the working side of the workflow."""

    return _evaluate_claude_leg(raw, _BASE_LEG)


def evaluate_claude_reviewer(raw: Any) -> dict[str, Any]:
    """Evaluate the reviewer Opus/max leg — the standing Claude review voice."""

    return _evaluate_claude_leg(raw, _REVIEWER_LEG)


def _degrade_reviewer_to_base(
    reviewer: dict[str, Any], base: dict[str, Any]
) -> dict[str, Any]:
    """Rewrite a blocked reviewer decision as a recorded base-lineage fallback.

    Only availability-class failures arrive here (the no-waiver branch of
    ``_waive_or_block_claude`` marks them ``degradable_to_base``), and only a
    ``ready`` base hosts the fallback: the degraded voice reuses exactly the
    execution decision the base leg already proved out, under the same
    read-only review boundary.  A ``waived`` base never hosts it — that would
    put one substitute model on both sides of every review discussion, which
    is the cross-model property this gate exists to protect.  Degradation is
    a recorded state, not a silent repair: the caller logs it in the Decision
    Audit Trail, because for this run Claude review is no longer
    cross-lineage from the base.
    """

    degraded = dict(reviewer)
    degraded.update(
        {
            "state": "degraded",
            "blocking": False,
            "reason_code": "reviewer_degraded_to_base",
            "reason": (
                f"{reviewer['reason']}; every Claude review voice falls back"
                f" to the selected base model ({base['model']}) in a fresh"
                " read-only context — Claude review is no longer"
                " cross-lineage from the base for this run"
            ),
            "degradation": {
                "reason_code": reviewer["reason_code"],
                "reason": reviewer["reason"],
                "fallback_leg": "claude",
                "floor_model": REVIEWER_MODEL,
            },
            "model": base["model"],
            "effort": REVIEWER_EFFORT,
            "selection": {
                "floor_model": REVIEWER_MODEL,
                "selected_model": base["model"],
                "reason": "reviewer_degraded_to_base",
            },
            "execution_path": base["execution_path"],
            "arguments": list(base["arguments"]),
            "environment_unset": list(base["environment_unset"]),
            "subagent_model_override": base["subagent_model_override"],
            "next_action": (
                "invoke_reviewer_agent"
                if base["execution_path"] == "agent_tool"
                else "invoke_explicit_reviewer_cli"
            ),
            "waiver_required": False,
            "degradable_to_base": False,
            "fallback_model": base["model"],
        }
    )
    return degraded


def monitor_orchestrator_binding(
    model_runtime: Any, session_model: Any = None
) -> dict[str, Any]:
    """Bind the Phase 6 monitor-session owner from the persisted gate record.

    Input is the documented persisted contract —
    ``resolved_conventions.model_runtime`` (each Claude leg carrying
    ``model`` and ``gate_status``; see references/state-and-safety.md) —
    because the binding runs at monitor entry from STATE, and state is
    untrusted input: floors are re-checked here even though the gate
    enforced them at selection time, so a hand-edited record can never
    bind a below-floor owner.

    A cost-shape REBIND, not a fourth selection: monitor orchestration
    (poll, classify, draft, dispatch — the capability boundary lives in
    references/monitor-exit-handoffs.md, Phase 6 Session Ownership) does
    not need the base tier, while the monitor session's prompt-cache
    lineage is the dominant long-run spend — so a landed-ready reviewer
    leg owns the monitor session, and every substantive work item still
    dispatches to the frozen BASE selection. Reviewer unavailability keeps
    the monitor on the base lineage exactly as before this role existed
    (reason-coded, recorded, never a new block).

    ``session_model`` is the model of the session performing the binding.
    Ownership converges at session boundaries only (never re-model a live
    session): when the live session's model is a recorded leg other than
    the nominal owner, the binding records THAT lineage truthfully with
    ``reason_code: "orchestrator_continuity"`` and carries the nominal
    owner in ``pending_owner`` for the next boundary. A session model
    matching no recorded leg is a policy violation and fails closed —
    ``invalid`` is a state problem, never a license to guess a model.
    """

    if not isinstance(model_runtime, dict):
        return {
            "state": "invalid",
            "errors": ["model_runtime must be a JSON object"],
        }

    def _selection_evidence(leg: dict, model: str) -> bool:
        # R2 round-2 finding 3737466436: cross-lineage tolerance without
        # evidence let a hand-edited swap (opus on the base leg, fable on
        # the reviewer leg) silently invert which lineage owns the
        # session. A cross-floor model is legitimate only when the leg's
        # own persisted policy_decision records it as the selection — the
        # shape every waiver/degradation writer produces.
        decision = leg.get("policy_decision")
        selection = (
            decision.get("selection") if isinstance(decision, dict) else None
        )
        selected = (
            selection.get("selected_model")
            if isinstance(selection, dict)
            else None
        )
        return selected == model

    def _landed_leg(leg: Any, own_floor: Any, other_floor: Any) -> str | None:
        """Return the leg's model when its gate landed ready above a floor.

        The leg's OWN floor needs no evidence; the other lineage's floor
        (a waived/degraded substitute) is accepted only with the leg's own
        recorded selection evidence. Anything below both floors is
        untrusted garbage regardless of evidence.
        """

        if not isinstance(leg, dict):
            return None
        model = leg.get("model")
        if not isinstance(model, str) or not model:
            return None
        if leg.get("gate_status") != "ready":
            return None
        if own_floor(model):
            return model
        if other_floor(model) and _selection_evidence(leg, model):
            return model
        return None

    base_model = _landed_leg(
        model_runtime.get("claude"),
        _at_or_above_base_floor,
        _at_or_above_reviewer_floor,
    )
    reviewer_model = _landed_leg(
        model_runtime.get("claude_reviewer"),
        _at_or_above_reviewer_floor,
        _at_or_above_base_floor,
    )
    base_leg = model_runtime.get("claude")
    # references/monitor-exit-handoffs.md makes a confirmed write-capable
    # base worker a PREREQUISITE of reviewer ownership (R2 round-2 finding
    # 3737466426: the veto lived in prose while this binder returned
    # reviewer ownership for the routine unverified-host shape). The
    # persisted flag is the host's per-agent enforcement verification;
    # false is its initialized default, so absence never grants ownership.
    base_write_verified = (
        isinstance(base_leg, dict)
        and base_leg.get("host_agent_selection_verified") is True
    )

    # admin#1495 r12 F1: the OpenAI entry's Phase 6 controller is a live
    # session on the CODEX leg's selection — a recorded, gate-ready leg,
    # not an unrecorded model. Recompute its landed-ready model the same
    # untrusted-state way as the Claude legs (floor recheck via the shared
    # slug predicate; a hand-edited below-floor or excluded-variant record
    # never continues).
    codex_leg = model_runtime.get("codex")
    codex_model: str | None = None
    if isinstance(codex_leg, dict) and codex_leg.get("gate_status") == "ready":
        codex_candidate = codex_leg.get("model")
        if (
            isinstance(codex_candidate, str)
            and codex_candidate
            and _slug_meets_floor_policy(codex_candidate)
        ):
            codex_model = codex_candidate

    def _bound(
        lineage: str, model: str, reason_code: str, reason: str, pending: str | None
    ) -> dict[str, Any]:
        if lineage == "reviewer":
            effort = REVIEWER_EFFORT
        elif lineage == "codex":
            effort = CODEX_EFFORT
        else:
            effort = BASE_EFFORT
        return {
            "state": "bound",
            "lineage": lineage,
            "model": model,
            "effort": effort,
            "reason_code": reason_code,
            "reason": reason,
            "pending_owner": pending,
        }

    if reviewer_model is not None and base_write_verified:
        nominal_lineage, nominal_model = "reviewer", reviewer_model
        nominal_code = "orchestrator_on_reviewer"
        nominal_reason = (
            "the Phase 6 monitor session is owned by the reviewer-leg"
            f" selection ({reviewer_model}); substantive work items dispatch"
            " to the frozen base selection"
        )
    elif reviewer_model is not None and base_model is not None:
        nominal_lineage, nominal_model = "base", base_model
        nominal_code = "orchestrator_on_base"
        nominal_reason = (
            "the base leg's write path is not host-verified"
            " (host_agent_selection_verified is not true), and reviewer"
            " ownership requires a confirmed write-capable base worker —"
            f" the monitor session stays on the base lineage ({base_model})"
        )
    elif reviewer_model is not None:
        return {
            "state": "invalid",
            "errors": [
                "reviewer ownership requires a confirmed write-capable base"
                " worker, and no landed-ready above-floor base leg exists to"
                " fall back to — re-run the model gate before entering"
                " Phase 6"
            ],
        }
    elif base_model is not None:
        reviewer_leg = model_runtime.get("claude_reviewer")
        reviewer_status = (
            reviewer_leg.get("gate_status") if isinstance(reviewer_leg, dict) else None
        )
        if not isinstance(reviewer_status, str) or not reviewer_status:
            reviewer_status = "missing"
        nominal_lineage, nominal_model = "base", base_model
        nominal_code = "orchestrator_on_base"
        nominal_reason = (
            "the reviewer leg has no landed-ready above-floor selection"
            f" ({reviewer_status}); the monitor session stays on the base"
            f" lineage ({base_model}) — ownership is a cost decision and"
            " never a new way to block"
        )
    else:
        return {
            "state": "invalid",
            "errors": [
                "no landed-ready above-floor Claude leg can own the monitor"
                " session — re-run the model gate before entering Phase 6"
            ],
        }

    if session_model is None or session_model == nominal_model:
        return _bound(
            nominal_lineage, nominal_model, nominal_code, nominal_reason, None
        )
    if session_model == base_model:
        live_lineage, live_model = "base", base_model
    elif session_model == reviewer_model and base_write_verified:
        live_lineage, live_model = "reviewer", reviewer_model
    elif session_model == reviewer_model:
        # CR 3760683975 (keeper-agents#1328): a live reviewer-model session
        # WITHOUT a host-verified write-capable base worker path must not
        # take reviewer ownership — that lineage's capability boundary
        # dispatches all fix work to base workers, which is exactly the path
        # that is unverified, leaving write work with no compliant actor.
        # The truthful record keeps the live model (never re-model a live
        # session) but binds under the base lineage's unrestricted inline
        # capability set, mirroring the nominal no-write-path demotion; the
        # nominal owner still takes over at the next session boundary.
        live_lineage, live_model = "base", reviewer_model
    elif codex_model is not None and session_model == codex_model:
        # admin#1495 r12 F1: the OpenAI entry's controller continues
        # monitoring on the codex lineage — orchestrate-only, with Claude
        # child ownership retained (the nominal Claude owner rides in
        # pending_owner, the exact target the runner's session_model-free
        # recompute cross-checks, so every pinned child stays on the
        # frozen Claude selection). Like reviewer ownership it needs the
        # host-verified write-capable base worker path; unlike a live
        # Opus session, a Codex session cannot truthfully demote to the
        # base lineage's inline role, so without that path there is no
        # compliant write actor and the binding fails closed.
        if not base_write_verified:
            return {
                "state": "invalid",
                "errors": [
                    "a codex-leg controller dispatches every substantive"
                    " work item to base workers, and the base leg's write"
                    " path is not host-verified"
                    " (host_agent_selection_verified is not true) — no"
                    " compliant write actor exists; verify the base worker"
                    " path or run the monitor from a Claude-leg session"
                ],
            }
        live_lineage, live_model = "codex", codex_model
    else:
        return {
            "state": "invalid",
            "errors": [
                "session_model matches no landed leg of the persisted gate"
                " record — a session on an unrecorded model must not monitor;"
                " re-run the model gate"
            ],
        }
    return _bound(
        live_lineage,
        live_model,
        "orchestrator_continuity",
        (
            f"the live session ({live_model}) continues monitoring — a"
            " mid-run model swap would re-write the warm cache; the nominal"
            f" owner ({nominal_model}) takes over at the next session boundary"
        ),
        nominal_model,
    )


def evaluate_model_policy(request: Any) -> dict[str, Any]:
    """Return deterministic Codex, base-Claude, and reviewer-Claude decisions."""

    if not isinstance(request, dict):
        return {
            "version": SCHEMA_VERSION,
            "state": "blocked",
            "codex": None,
            "claude": None,
            "claude_reviewer": None,
            "errors": ["input must be a JSON object"],
        }

    codex = evaluate_codex(request.get("codex"))
    claude = evaluate_claude(request.get("claude"))
    reviewer = evaluate_claude_reviewer(request.get("claude_reviewer"))
    # The base and Codex legs gate: the base writes, and the cross-vendor
    # verdict must be able to judge what it wrote.  The reviewer leg
    # degrades instead of gating — an availability failure falls back onto
    # the ready base lineage as a recorded degradation.  Malformed
    # observations and engaged-but-invalid waivers keep their blocking
    # semantics (they are never marked degradable), and a blocked or
    # waived base never hosts the fallback.
    if (
        reviewer["state"] == "blocked"
        and reviewer.get("degradable_to_base") is True
        and claude["state"] == "ready"
    ):
        reviewer = _degrade_reviewer_to_base(reviewer, claude)
    states = {codex["state"], claude["state"], reviewer["state"]}
    if "blocked" in states:
        state = "blocked"
    elif "retry" in states:
        state = "retry"
    elif "probe_required" in states:
        state = "probe_required"
    elif "waived" in states:
        state = "waived"
    elif "degraded" in states:
        state = "degraded"
    else:
        state = "ready"

    return {
        "version": SCHEMA_VERSION,
        "state": state,
        "codex": codex,
        "claude": claude,
        "claude_reviewer": reviewer,
        "errors": [],
    }


def main() -> int:
    # finding 3813789220: the publication-sanitizer chokepoint, invokable
    # without JSON plumbing so every ledger append / PR trail sync can
    # shell through it: `model_policy.py --sanitize < raw > clean`.
    if "--sanitize" in sys.argv[1:]:
        sys.stdout.write(
            sanitize_for_publication(strip_url_secrets(sys.stdin.read()))
        )
        return 0
    try:
        request = json.load(sys.stdin)
    except (ValueError, OSError) as error:
        result = {
            "version": SCHEMA_VERSION,
            "state": "blocked",
            "codex": None,
            "claude": None,
            "claude_reviewer": None,
            "errors": [f"input must be valid JSON: {error}"],
        }
    else:
        result = evaluate_model_policy(request)

    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 2 if result["state"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
