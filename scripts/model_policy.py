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
            "supported_reasoning_levels": [{"effort": "xhigh"}]
          }]
        },
        "first_real_invocation": {"status": "success", "attempts": 1}
      },
      "claude": {
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
      },
      "claude_third_voice": {
        "installed": true,
        "version": "2.1.170 (Claude Code)",
        "fable_access": "available",
        "zero_data_retention": "compatible"
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
timeout or transport failure may retry once with the exact same model and
effort; every other Codex failure blocks, and no path proposes a downgrade.

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

Three legs are evaluated.  ``codex`` and ``claude`` are gating: a failure on
either blocks the workflow.  ``claude_third_voice`` is a supplement — the
escalation opinion called in on hard problems — so its failures report
``unavailable`` and the run continues with the degradation recorded.  Making
it blocking would misrepresent a supplement as a mandatory gate.

Model selection is floor-based, not pinned.  From the observed facts the
helper selects the newest eligible model at or above each floor: for Codex,
live-catalog models named ``gpt-<version>[-variant]`` that support the
required effort, excluding down-tier variants such as ``-mini``; for the
primary Claude leg, ``opus``-family entries in the optional
``claude.observed_models`` list; for the third voice, ``fable``/``mythos``
entries in ``claude_third_voice.observed_models``.  Upgrades are automatic and
reported under each decision's ``selection`` key; anything below a floor still
blocks, and no path proposes a downgrade.

A context-window variant suffix (``claude-opus-5[1m]``) denotes the same model
version as its bare slug.  It is accepted wherever the bare slug is, and is
never treated as either a downgrade or an upgrade.

``xhigh`` is the depth setting for this workflow's single-problem voices;
``ultra`` is the breadth mode, reserved for tasks that genuinely decompose
into independent parts, and is deliberately not part of this gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import selectors
import sys
import time
from typing import Any, Callable, IO, Iterable


SCHEMA_VERSION = 2

CODEX_MODEL = "gpt-5.6-sol"  # floor: newest eligible catalog model >= this wins
CODEX_FLOOR_VERSION = (5, 6)
CODEX_EFFORT = "xhigh"
MIN_CODEX_VERSION = (0, 144, 0)
CODEX_MAX_ATTEMPTS = 2
# Variant tokens that mark down-tier siblings, never auto-forward targets.
CODEX_EXCLUDED_VARIANT_TOKENS = ("mini", "nano", "lite", "chat")

# Primary Claude leg: explorers, the always-runs structured review, and every
# Claude fallback.  Gating — a failure here blocks.
CLAUDE_MODEL = "claude-opus-5"  # floor: newest observed opus >= this wins
CLAUDE_FLOOR_VERSION = (5,)
CLAUDE_MODEL_ALIAS = "opus"
CLAUDE_EFFORT = "max"

# Third voice: the escalation opinion, called in on hard problems alongside the
# Codex verdict.  Supplementary — a failure here degrades, it does not block.
THIRD_VOICE_MODEL = "claude-fable-5"  # floor: newest observed fable/mythos wins
THIRD_VOICE_FLOOR_VERSION = (5,)
THIRD_VOICE_MODEL_ALIAS = "fable"
THIRD_VOICE_EFFORT = "max"

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
_CLAUDE_UPGRADE_SLUG = re.compile(
    r"claude-opus-(?P<version>\d+(?:-\d+)*)(?P<variant>\[[0-9a-z]+\])?"
)

_THIRD_VOICE_UPGRADE_SLUG = re.compile(
    r"claude-(?P<family>fable|mythos)-(?P<version>\d+(?:-\d+)*)"
)

_CODEX_BLOCKING_FAILURES = {
    "entitlement_denied": (
        "entitlement_denied",
        "GPT-5.6 Sol entitlement was denied by the real invocation",
        "request_access",
    ),
    "quota_exhausted": (
        "quota_exhausted",
        "GPT-5.6 Sol usage quota is exhausted",
        "wait_for_quota_reset_or_change_access",
    ),
    "model_unavailable": (
        "model_unavailable",
        "GPT-5.6 Sol was unavailable to the real invocation",
        "request_access",
    ),
    "authentication_error": (
        "authentication_error",
        "Codex authentication failed during the real invocation; auth failures are non-retryable",
        "repair_authentication",
    ),
    "error": (
        "invocation_error",
        "The real GPT-5.6 Sol invocation failed",
        "inspect_error_and_block",
    ),
}

_CODEX_RETRYABLE_FAILURES = {"timeout", "transport_error"}

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

# Default supervision deadline.  A child that neither writes nor exits (hung
# TLS connect, stalled provider) must not park the supervisor forever: the
# fail-fast guarantee belongs in this module, not in caller prose.
DEFAULT_SUPERVISE_TIMEOUT_SECONDS = 60.0

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
    return " ".join(parts)


def _has_auth_signature(text: str) -> bool:
    lowered = text.lower()
    if any(signature in lowered for signature in _AUTH_SIGNATURES):
        return True
    return bool(re.search(r"(?<!\d)401(?!\d)", lowered))


def bounded_excerpt(existing: str, addition: str) -> str:
    """Append *addition* to *existing*, URL-stripped and byte-capped.

    Only recognised transport/error events and diagnostic stderr are ever
    passed here; assistant/tool/unknown payloads (which can embed repository
    source) never reach persisted evidence.

    This strips URL-embedded credentials ONLY.  A bare ``Authorization:
    Bearer ...`` or ``OPENAI_API_KEY=...`` in stderr survives, so the caller
    must still apply the package's format-anchored Secret/Token Redaction
    before persisting the excerpt anywhere.
    """

    combined = f"{existing}\n{strip_url_secrets(addition)}" if existing else strip_url_secrets(addition)
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


def supervise_stream(
    stdout_pipe: IO[bytes] | None,
    stderr_pipe: IO[bytes] | None,
    kill_callback: Callable[[], None],
    *,
    read_size: int = 65536,
    timeout_seconds: float | None = DEFAULT_SUPERVISE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Supervise a running Codex process's real pipes; kill it on auth failure.

    Takes the ACTUAL pipe handles so each line's provenance is established by
    construction -- there is no caller-supplied tag to forge.  Both pipes are
    drained concurrently through a selector: reading one to exhaustion while the
    other fills its kernel buffer would deadlock, and a deadlocked supervisor
    cannot terminate promptly, which is the whole point of this function.

    Returns ``{"outcome", "exit_code", "excerpts", "auth_line_source"}`` where
    outcome is ``clean``/``auth_error``/``internal_failure``/``timeout``.
    Every failure outcome kills the process group; the caller then BLOCKs.
    ``timeout_seconds`` bounds a silent child (pass ``None`` to wait forever,
    which only a caller with its own deadline should do).  This function
    never spawns a process and never inspects credentials.
    """

    excerpts: dict[str, str] = {SOURCE_STDOUT_JSON: "", SOURCE_STDERR: ""}
    readers = {
        SOURCE_STDOUT_JSON: _IncrementalLineReader(),
        SOURCE_STDERR: _IncrementalLineReader(),
    }
    outcome = "clean"
    auth_line_source: str | None = None

    selector = selectors.DefaultSelector()
    registered = 0
    for source, pipe in ((SOURCE_STDOUT_JSON, stdout_pipe), (SOURCE_STDERR, stderr_pipe)):
        if pipe is not None:
            selector.register(pipe, selectors.EVENT_READ, source)
            registered += 1

    deadline = (
        None if timeout_seconds is None else time.monotonic() + timeout_seconds
    )
    try:
        while registered and outcome == "clean":
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                outcome = "timeout"
                break
            ready = selector.select(remaining)
            if not ready:
                # No fd readable before the deadline: the child is silent.
                outcome = "timeout"
                break
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
        kill_callback()

    exit_code = {
        "clean": CLASSIFY_EXIT_CLEAN,
        "auth_error": CLASSIFY_EXIT_AUTH_ERROR,
        "internal_failure": CLASSIFY_EXIT_INTERNAL_FAILURE,
        "timeout": CLASSIFY_EXIT_TIMEOUT,
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


def classify_tagged_lines(lines: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Diagnostic/test-only helper behind ``--classify-stream``.

    The live path is :func:`supervise_stream`, which derives provenance from
    real file descriptors.  This helper accepts caller-supplied tags and exists
    for offline inspection and tests only.
    """

    verdicts: list[dict[str, str]] = []
    outcome = "clean"
    for source, line in lines:
        verdict = classify_stream_event(source, line)
        verdicts.append({"source": source, "verdict": verdict})
        if verdict != "benign" and outcome == "clean":
            outcome = verdict
            break
    exit_code = {
        "clean": CLASSIFY_EXIT_CLEAN,
        "auth_error": CLASSIFY_EXIT_AUTH_ERROR,
        "internal_failure": CLASSIFY_EXIT_INTERNAL_FAILURE,
    }[outcome]
    return {"outcome": outcome, "exit_code": exit_code, "verdicts": verdicts}


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

    if not _codex_model_is_eligible(frozen_model, live_catalog):
        return {
            "state": "blocked",
            "reason_code": "frozen_model_ineligible",
            "reason": (
                f"The frozen model {frozen_model} is no longer present in the live "
                "catalog with xhigh reasoning"
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


_CLAUDE_ACCESS_FAILURES = _access_failures("opus", "Claude Opus 5")
_CLAUDE_ZDR_FAILURES = _zdr_failures("Claude Opus 5")

_THIRD_VOICE_ACCESS_FAILURES = _access_failures("fable", "Claude Fable 5")
_THIRD_VOICE_ZDR_FAILURES = _zdr_failures("Claude Fable 5")

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
        "arguments": [
            "-m",
            CODEX_MODEL,
            "-c",
            'model_reasoning_effort="xhigh"',
        ],
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


def _codex_model_is_eligible(slug: str, catalog: Any) -> bool:
    """True when *slug* is present in *catalog* with the required effort.

    Used by :func:`verify_frozen_selection` to re-check an already-frozen model
    without re-running selection.
    """

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
        match = _GPT_SLUG.fullmatch(slug)
        if match is None:
            continue
        version = (int(match.group("major")), int(match.group("minor") or 0))
        variant = match.group("variant") or ""
        if version < CODEX_FLOOR_VERSION:
            continue
        if version == CODEX_FLOOR_VERSION and slug != CODEX_MODEL:
            continue
        if any(
            token in variant.split("-")
            for token in CODEX_EXCLUDED_VARIANT_TOKENS
        ):
            continue
        if not _supports_required_effort(model):
            continue
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
            "Codex CLI must be at least 0.144.0",
            "upgrade_codex_cli",
        )

    selected_model = _select_codex_model(config.get("live_catalog"))
    if selected_model is None:
        return _block_codex(
            decision,
            "live_catalog_missing_capability",
            "The live Codex catalog lacks an eligible model at or above "
            "GPT-5.6 Sol with xhigh reasoning",
            "request_access_or_refresh_live_catalog",
        )
    decision["live_catalog_verified"] = True
    decision["model"] = selected_model
    decision["arguments"] = [
        "-m",
        selected_model,
        "-c",
        'model_reasoning_effort="xhigh"',
    ]
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
        or attempts > CODEX_MAX_ATTEMPTS
        or (status == "not_run" and attempts != 0)
        or (status != "not_run" and attempts < 1)
    ):
        return _block_codex(
            decision,
            "invalid_invocation_attempts",
            "Invocation attempts must be zero before the probe and between one and two afterward",
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
                "reason": "The real GPT-5.6 Sol invocation succeeded",
                "next_action": "continue",
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
                        "GPT-5.6 Sol/xhigh configuration"
                    ),
                    "next_action": "retry_same_invocation_once",
                }
            )
            return decision
        return _block_codex(
            decision,
            f"{status}_retry_exhausted",
            "The one permitted Codex retry also failed",
            "block_and_report_failure",
        )

    blocking = _CODEX_BLOCKING_FAILURES.get(status)
    if blocking is not None:
        return _block_codex(decision, *blocking)
    return _block_codex(
        decision,
        "unknown_invocation_status",
        f"Unknown Codex invocation status: {status!r}",
        "correct_observation_input",
    )


def _explicit_cli_arguments(model: str, effort: str = CLAUDE_EFFORT) -> list[str]:
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


def _claude_base(version: Any) -> dict[str, Any]:
    return {
        "state": "blocked",
        "reason_code": None,
        "reason": None,
        "model": CLAUDE_MODEL,
        "effort": CLAUDE_EFFORT,
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
        "selection": None,
    }


def _claude_floor_variant(slug: Any) -> bool:
    """True when ``slug`` is the floor model, or a context-window variant of it.

    ``claude-opus-5[1m]`` is the same model version as ``claude-opus-5`` with a
    larger context window, so it is accepted anywhere the bare slug is.
    """

    if not isinstance(slug, str):
        return False
    match = _CLAUDE_UPGRADE_SLUG.fullmatch(slug)
    if match is None:
        return False
    version = tuple(int(part) for part in match.group("version").split("-"))
    return version == CLAUDE_FLOOR_VERSION


def _select_claude_model(observed_models: Any) -> tuple[str, str]:
    """Return the newest observed opus model at or above the primary floor.

    Falls back to the floor when nothing newer is observed.  A ``[1m]``-style
    context-window suffix marks the same version, so it never outranks the bare
    slug; ties prefer the bare slug (the standard-cost default), then
    lexicographic order — deterministic by construction.
    """

    if not isinstance(observed_models, list):
        return CLAUDE_MODEL, "floor_model"

    best: tuple[tuple[Any, ...], str] | None = None
    for item in observed_models:
        if not isinstance(item, str):
            continue
        match = _CLAUDE_UPGRADE_SLUG.fullmatch(item)
        if match is None:
            continue
        version = tuple(int(part) for part in match.group("version").split("-"))
        if version < CLAUDE_FLOOR_VERSION:
            continue
        variant_rank = 0 if match.group("variant") else 1
        key = (version, variant_rank, item)
        if best is None or key > best[0]:
            best = (key, item)

    if best is None or best[1] == CLAUDE_MODEL:
        return CLAUDE_MODEL, "floor_model"
    if _claude_floor_variant(best[1]):
        # Same version as the floor, larger context window — not an upgrade.
        return best[1], "floor_model_variant"
    return best[1], "newer_model_auto_selected"


def _select_third_voice_model(observed_models: Any) -> tuple[str, str]:
    """Return the newest observed fable/mythos model at or above the floor.

    Falls back to the floor when nothing newer is observed.  Ties on version
    prefer the ``fable`` family (generally available), then lexicographic
    order — deterministic by construction.
    """

    if not isinstance(observed_models, list):
        return THIRD_VOICE_MODEL, "floor_model"

    best: tuple[tuple[Any, ...], str] | None = None
    for item in observed_models:
        if not isinstance(item, str):
            continue
        match = _THIRD_VOICE_UPGRADE_SLUG.fullmatch(item)
        if match is None:
            continue
        version = tuple(int(part) for part in match.group("version").split("-"))
        if version < THIRD_VOICE_FLOOR_VERSION:
            continue
        family_rank = 1 if match.group("family") == "fable" else 0
        key = (version, family_rank, item)
        if best is None or key > best[0]:
            best = (key, item)

    if best is None or best[1] == THIRD_VOICE_MODEL:
        return THIRD_VOICE_MODEL, "floor_model"
    return best[1], "newer_model_auto_selected"


def _at_or_above_third_voice_floor(slug: Any) -> bool:
    """True when ``slug`` is a fable/mythos model at or above the third-voice floor.

    A waiver may authorize a different lineage; it may not authorize a version
    below a floor.  Nothing in this module proposes a downgrade, and an explicit
    human waiver is not an exception to that.
    """

    if not isinstance(slug, str):
        return False
    match = _THIRD_VOICE_UPGRADE_SLUG.fullmatch(slug)
    if match is None:
        return False
    version = tuple(int(part) for part in match.group("version").split("-"))
    return version >= THIRD_VOICE_FLOOR_VERSION


def _waive_or_block_claude(
    decision: dict[str, Any],
    config: dict[str, Any],
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
                "An explicit waiver requires an observed named Opus fallback",
            )
        fallback_model = fallback.get("model")
        observed_models = config.get("observed_models")
        if (
            fallback.get("available") is not True
            or fallback.get("explicitly_authorized") is not True
            or not isinstance(fallback_model, str)
            or not _at_or_above_third_voice_floor(fallback_model)
            or not isinstance(observed_models, list)
            or not all(isinstance(model, str) for model in observed_models)
            or fallback_model not in observed_models
            or fallback.get("effort") != CLAUDE_EFFORT
            or fallback.get("execution_path") != "explicit_cli"
            or config.get("installed") is not True
        ):
            return _block_claude_input(
                decision,
                "invalid_named_fallback",
                "The waived fallback must be an available, explicitly authorized Claude Fable or Mythos model at or above the Fable 5 floor, at max effort",
            )
        decision.update(
            {
                "state": "waived",
                "reason_code": reason_code,
                "reason": reason,
                "model": fallback_model,
                "effort": CLAUDE_EFFORT,
                "selection": {
                    "floor_model": CLAUDE_MODEL,
                    "selected_model": fallback_model,
                    "reason": "explicit_waiver_fallback",
                },
                "execution_path": "explicit_cli",
                "arguments": _explicit_cli_arguments(fallback_model),
                "environment_unset": list(CLAUDE_READ_ONLY_ENV_UNSET),
                "next_action": "invoke_explicit_named_fallback",
                "waiver_granted": True,
                "downgrade_allowed": True,
                "fallback_model": fallback_model,
            }
        )
        return decision
    decision.update(
        {
            "state": "blocked",
            "reason_code": reason_code,
            "reason": reason,
            "next_action": "request_explicit_waiver_or_restore_opus_access",
            "waiver_required": True,
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


def evaluate_claude(raw: Any) -> dict[str, Any]:
    """Evaluate the primary Opus/max gate and choose Agent or explicit CLI.

    This leg is gating: any failure blocks unless an explicit waiver names an
    observed Fable/Mythos-family model at max effort.
    """

    config = raw if isinstance(raw, dict) else {}
    version = config.get("version")
    decision = _claude_base(version)

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
            "version_unparseable",
            "Claude Code version could not be parsed as semantic versioning",
        )
    if not _version_at_least(version, MIN_CLAUDE_VERSION):
        return _waive_or_block_claude(
            decision,
            config,
            "cli_too_old",
            "Claude Code must be at least 2.1.170",
        )

    access = config.get("opus_access", "unknown")
    if access is not True and access != "available":
        if access is False:
            code, reason = _CLAUDE_ACCESS_FAILURES[False]
        elif isinstance(access, str) and access in _CLAUDE_ACCESS_FAILURES:
            code, reason = _CLAUDE_ACCESS_FAILURES[access]
        else:
            return _block_claude_input(
                decision,
                "invalid_opus_access",
                f"opus_access must be {_ACCESS_STATUS_VALUES}",
            )
        return _waive_or_block_claude(decision, config, code, reason)

    zdr = config.get("zero_data_retention", "unknown")
    if zdr is not True and zdr != "compatible":
        if zdr is False:
            code, reason = _CLAUDE_ZDR_FAILURES[False]
        elif isinstance(zdr, str) and zdr in _CLAUDE_ZDR_FAILURES:
            code, reason = _CLAUDE_ZDR_FAILURES[zdr]
        else:
            return _block_claude_input(
                decision,
                "invalid_zdr_status",
                f"zero_data_retention must be {_ZDR_STATUS_VALUES}",
            )
        return _waive_or_block_claude(decision, config, code, reason)

    waiver = config.get("explicit_waiver", False)
    if not isinstance(waiver, bool):
        return _waive_or_block_claude(
            decision,
            config,
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
    selected_model, selection_reason = _select_claude_model(
        config.get("observed_models")
    )
    at_floor = selected_model == CLAUDE_MODEL
    at_floor_version = _claude_floor_variant(selected_model)
    model_flag = CLAUDE_MODEL_ALIAS if at_floor else selected_model
    decision["model"] = selected_model
    decision["selection"] = {
        "floor_model": CLAUDE_MODEL,
        "selected_model": selected_model,
        "reason": selection_reason,
    }

    exact_override = override if isinstance(override, str) else ""
    compatible_overrides = {"", selected_model}
    if at_floor_version:
        compatible_overrides |= {CLAUDE_MODEL_ALIAS, CLAUDE_MODEL}
    # A context-window variant of the floor names the same model version, so it
    # is not a conflicting override.
    model_conflict = exact_override not in compatible_overrides and not (
        at_floor_version and _claude_floor_variant(exact_override)
    )
    effort_conflict = effort_override not in {None, CLAUDE_EFFORT}
    agent_selection_verified = (
        host_capabilities.get("agent_model_selection") is True
        and host_capabilities.get("agent_effort_selection") is True
        and host_capabilities.get("agent_read_only_enforced") is True
    )
    conflict = model_conflict or effort_conflict or not agent_selection_verified

    if conflict:
        execution_path = "explicit_cli"
        arguments = _explicit_cli_arguments(model_flag)
        next_action = "invoke_explicit_claude_cli"
        environment_unset = list(CLAUDE_READ_ONLY_ENV_UNSET)
    else:
        execution_path = "agent_tool"
        arguments = [f"model={model_flag}", f"effort={CLAUDE_EFFORT}"]
        next_action = "invoke_opus_agent"
        environment_unset = []

    if at_floor:
        selection_note = ""
    elif at_floor_version:
        selection_note = " (context-window variant of the Opus 5 floor)"
    else:
        selection_note = " (auto-selected above the Opus 5 floor)"

    decision.update(
        {
            "state": "ready",
            "reason_code": ("explicit_cli_required" if conflict else "opus_ready"),
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


def _third_voice_base(version: Any) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "reason_code": None,
        "reason": None,
        "role": "supplementary",
        "blocking": False,
        "model": THIRD_VOICE_MODEL,
        "effort": THIRD_VOICE_EFFORT,
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
        "downgrade_allowed": False,
        "next_action": None,
        "selection": None,
    }


def _degrade_third_voice(
    decision: dict[str, Any],
    reason_code: str,
    reason: str,
    next_action: str = "continue_without_third_voice",
) -> dict[str, Any]:
    """Record the third voice as unavailable without blocking the workflow."""

    decision.update(
        {
            "state": "unavailable",
            "reason_code": reason_code,
            "reason": reason,
            "next_action": next_action,
        }
    )
    return decision


def evaluate_third_voice(raw: Any) -> dict[str, Any]:
    """Evaluate the supplementary Fable/max escalation voice.

    This leg never blocks.  It is the extra opinion called in on hard problems
    — a stalled plan review, a large diff, an adversarial escalation, or a
    primary-vs-Codex dispute — so when it cannot run, the caller records the
    degradation and continues.  Treating a supplement as a mandatory gate would
    misreport what the workflow actually enforces.
    """

    config = raw if isinstance(raw, dict) else {}
    version = config.get("version")
    decision = _third_voice_base(version)

    if not config:
        return _degrade_third_voice(
            decision,
            "not_observed",
            "No third-voice observation was supplied",
            "observe_third_voice_or_continue_without_it",
        )
    if config.get("installed") is not True:
        return _degrade_third_voice(
            decision, "cli_missing", "Claude Code is not installed"
        )
    if not isinstance(version, str) or _semver(version) is None:
        return _degrade_third_voice(
            decision,
            "version_unparseable",
            "Claude Code version could not be parsed as semantic versioning",
            "correct_observation_input",
        )
    if not _version_at_least(version, MIN_CLAUDE_VERSION):
        return _degrade_third_voice(
            decision, "cli_too_old", "Claude Code must be at least 2.1.170"
        )

    access = config.get("fable_access", "unknown")
    if access is not True and access != "available":
        if access is False:
            code, reason = _THIRD_VOICE_ACCESS_FAILURES[False]
        elif isinstance(access, str) and access in _THIRD_VOICE_ACCESS_FAILURES:
            code, reason = _THIRD_VOICE_ACCESS_FAILURES[access]
        else:
            return _degrade_third_voice(
                decision,
                "invalid_fable_access",
                f"fable_access must be {_ACCESS_STATUS_VALUES}",
                "correct_observation_input",
            )
        return _degrade_third_voice(decision, code, reason)

    zdr = config.get("zero_data_retention", "unknown")
    if zdr is not True and zdr != "compatible":
        if zdr is False:
            code, reason = _THIRD_VOICE_ZDR_FAILURES[False]
        elif isinstance(zdr, str) and zdr in _THIRD_VOICE_ZDR_FAILURES:
            code, reason = _THIRD_VOICE_ZDR_FAILURES[zdr]
        else:
            return _degrade_third_voice(
                decision,
                "invalid_zdr_status",
                f"zero_data_retention must be {_ZDR_STATUS_VALUES}",
                "correct_observation_input",
            )
        return _degrade_third_voice(decision, code, reason)

    environment = config.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    override = environment.get("CLAUDE_CODE_SUBAGENT_MODEL")
    effort_override = environment.get("CLAUDE_CODE_EFFORT_LEVEL")
    # Validate before comparing: an unhashable value here would raise out of the
    # whole gate, which is strictly worse than blocking — the caller would get a
    # traceback instead of a decision for any leg.
    if override is not None and not isinstance(override, str):
        return _degrade_third_voice(
            decision,
            "invalid_subagent_override",
            "CLAUDE_CODE_SUBAGENT_MODEL must be a string or null",
            "correct_observation_input",
        )
    if effort_override is not None and not isinstance(effort_override, str):
        return _degrade_third_voice(
            decision,
            "invalid_effort_override",
            "CLAUDE_CODE_EFFORT_LEVEL must be a string or null",
            "correct_observation_input",
        )
    host_capabilities = config.get("host_capabilities")
    if not isinstance(host_capabilities, dict):
        host_capabilities = {}

    selected_model, selection_reason = _select_third_voice_model(
        config.get("observed_models")
    )
    at_floor = selected_model == THIRD_VOICE_MODEL
    model_flag = THIRD_VOICE_MODEL_ALIAS if at_floor else selected_model
    decision["model"] = selected_model
    decision["selection"] = {
        "floor_model": THIRD_VOICE_MODEL,
        "selected_model": selected_model,
        "reason": selection_reason,
    }

    exact_override = override if isinstance(override, str) else ""
    compatible_overrides = {"", selected_model}
    if at_floor:
        compatible_overrides |= {THIRD_VOICE_MODEL_ALIAS, THIRD_VOICE_MODEL}
    agent_selection_verified = (
        host_capabilities.get("agent_model_selection") is True
        and host_capabilities.get("agent_effort_selection") is True
        and host_capabilities.get("agent_read_only_enforced") is True
    )
    conflict = (
        exact_override not in compatible_overrides
        or effort_override not in {None, THIRD_VOICE_EFFORT}
        or not agent_selection_verified
    )

    if conflict:
        execution_path = "explicit_cli"
        arguments = _explicit_cli_arguments(model_flag, THIRD_VOICE_EFFORT)
        next_action = "invoke_explicit_third_voice_cli"
        environment_unset = list(CLAUDE_READ_ONLY_ENV_UNSET)
    else:
        execution_path = "agent_tool"
        arguments = [f"model={model_flag}", f"effort={THIRD_VOICE_EFFORT}"]
        next_action = "invoke_third_voice_agent"
        environment_unset = []

    decision.update(
        {
            "state": "ready",
            "reason_code": (
                "explicit_cli_required" if conflict else "third_voice_ready"
            ),
            "reason": (
                "Unverified model/effort/read-only agent selection or a conflicting override requires the explicit read-only third-voice CLI path"
                if conflict
                else (
                    f"{selected_model} at max effort is available"
                    + ("" if at_floor else " (auto-selected above the Fable 5 floor)")
                )
            ),
            "execution_path": execution_path,
            "arguments": arguments,
            "environment_unset": environment_unset,
            "subagent_model_override": override,
            "next_action": next_action,
        }
    )
    return decision


def evaluate_model_policy(request: Any) -> dict[str, Any]:
    """Return deterministic Codex, Claude, and third-voice decisions."""

    if not isinstance(request, dict):
        return {
            "version": SCHEMA_VERSION,
            "state": "blocked",
            "codex": None,
            "claude": None,
            "claude_third_voice": None,
            "errors": ["input must be a JSON object"],
        }

    codex = evaluate_codex(request.get("codex"))
    claude = evaluate_claude(request.get("claude"))
    third_voice = evaluate_third_voice(request.get("claude_third_voice"))
    # Only the gating legs decide the aggregate: the third voice is a
    # supplement, so an unavailable third voice never blocks the workflow.
    states = {codex["state"], claude["state"]}
    if "blocked" in states:
        state = "blocked"
    elif "retry" in states:
        state = "retry"
    elif "probe_required" in states:
        state = "probe_required"
    elif "waived" in states:
        state = "waived"
    else:
        state = "ready"

    return {
        "version": SCHEMA_VERSION,
        "state": state,
        "codex": codex,
        "claude": claude,
        "claude_third_voice": third_voice,
        "errors": [],
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        result = {
            "version": SCHEMA_VERSION,
            "state": "blocked",
            "codex": None,
            "claude": None,
            "claude_third_voice": None,
            "errors": [f"input must be valid JSON: {error}"],
        }
    else:
        result = evaluate_model_policy(request)

    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 2 if result["state"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
