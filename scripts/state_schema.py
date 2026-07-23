#!/usr/bin/env python3
"""Validate a workflow state file before any resumed action trusts it.

Usage::

    python3 "$LOADED_SKILL_DIR/scripts/state_schema.py" <state-file>

Always invoke through the loaded skill package directory (the directory
containing the active SKILL.md), never a repository-local ``scripts/`` path —
a repository could otherwise shadow the trusted helper.

The helper reads the file, parses the YAML frontmatter with a deliberately
RESTRICTED parser, applies phase-aware schema requirements, and prints one
JSON object::

    {"version": 1, "state": "valid" | "suspect",
     "errors": [...],
     "tainted": [{"path": ..., "digest": ..., "kind": "key"|"value"|"body"}, ...],
     "phase_requirements": "<tier>"}

Exit codes: 0 = valid, 1 = suspect, 2 = usage/internal error (callers treat
2 as suspect — fail closed).

Restricted grammar (canonical v1 serialization)
-----------------------------------------------
Block mappings with 2-space indentation; keys are plain identifiers or
JSON-quoted strings; scalar values are ``null``, booleans, integers, or
strings (plain or JSON-quoted); inline collections are restricted to the
empty ``{}``/``[]`` and single-line lists of JSON-compatible scalars; block
lists use ``- `` items (scalars or records). Everything the schema never
emits is REJECTED as structural error inside the frontmatter fence: tabs in
indentation, tags (``!``), anchors/aliases (``&``/``*``), merge keys
(``<<``), duplicate keys, ``...`` document-end markers, non-string
(unquoted numeric) keys, multiline flow collections, and block scalars
(``|``/``>``). The optional markdown body after the closing fence is
OPAQUE: it is never parsed as data (later ``---`` lines are plain text such
as markdown horizontal rules), carries no machine-read values, and is
taint-scanned only, with findings reported as ``body:<line>``.

Cross-field invariants (source of truth for the reference text)
----------------------------------------------------------------
(i)   ``current_phase`` agrees with ``phases.*``: the named phase is
      non-pending; ``aborted_at_<X>`` requires ``phases.<X>: "blocked"``
      when X has a phases member.
(ii)  Successful-predecessor chain — ``plan_review`` non-pending requires
      ``plan: complete``; ``implementation`` requires ``plan_review:
      complete``; ``self_review`` requires ``implementation: complete``;
      ``runtime_verification`` requires ``self_review: complete``; ``pr``
      requires ``runtime_verification: complete|waived``; ``monitor``
      requires ``pr: complete``. A blocked predecessor never authorizes a
      successor (Entry B bootstrap marks skipped phases complete first).
      ``pr: complete`` additionally requires a non-null top-level
      ``pr_number``.
(iii) Per-handoff derived status, every tier: result keys are a SUBSET of
      planned operation IDs (never orphans); ``idle`` iff operations and
      results are both empty; ``pending`` iff operations exist and any
      planned result is missing/pending/retryable; ``complete``/``failed``
      require result keys to exactly equal planned IDs (``complete`` = all
      complete; ``failed`` = all terminal, at least one failed); operation
      IDs valid and unique. Terminal monitor (complete|paused|blocked)
      additionally prohibits missing/pending/retryable results.
(iv)  Evidence consistency per ``defect_evidence_mode``:
      ``runtime_bug_fix`` requires ``change_type: bug_fix``;
      ``skill_helper_defect`` requires ``change_type: skill_only``. Once
      ``phases.pr`` is non-pending: mode != none requires regression
      ``complete|exempt`` AND variants ``complete``; mode none requires
      regression ``not_applicable`` AND variants ``skipped``.
(v)   Status-dependent evidence completeness: ``root_cause`` is required
      for ``red_verified``, ``complete``, AND ``exempt``; ``red_verified``
      requires a complete red record and non-empty ``test_paths``;
      ``complete`` requires a complete green record, non-empty
      ``test_paths``, red record or ``red_exemption_reason``, and
      ``evaluated_head_sha`` == green ``tested_head_sha``; ``exempt``
      requires ``exemption_reason`` and ``evaluated_head_sha``;
      ``not_applicable`` rejects execution evidence; variant ``skipped``
      requires ``skipped_reason``.
(vi)  Freshness fields when evidence is terminal: ``evaluated_head_sha``
      (regression complete|exempt) and ``analyzed_head_sha`` (variants
      complete) are full-length hex object IDs.

Persisted evidence ``argv`` is AUDIT-ONLY and is never an execution source;
``test_paths`` entries are shape-checked here (repository-relative, no
leading dash, no control characters, no traversal); tracked-blob and
symlink-containment verification happens at use time through git itself.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
import re
import sys
from typing import Any

SCHEMA_VERSION = 1

VALID = "valid"
SUSPECT = "suspect"

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FULL_HEX = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
_ISO_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
_PLAIN_SCALAR_FORBIDDEN = re.compile(r"[:#{}\[\],&*!|>'\"%@`]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

PHASE_NAMES = (
    "plan",
    "plan_review",
    "implementation",
    "self_review",
    "runtime_verification",
    "pr",
    "monitor",
)
ENTRY_PHASES = ("entry", "takeover")
LEGAL_CURRENT_PHASES = frozenset(
    ENTRY_PHASES
    + PHASE_NAMES
    + tuple(f"aborted_at_{name}" for name in ENTRY_PHASES + PHASE_NAMES)
)

SIMPLE_PHASE_ENUM = frozenset(("pending", "in_progress", "complete", "blocked"))
RUNTIME_VERIFICATION_ENUM = frozenset(
    ("pending", "in_progress", "complete", "blocked", "waived")
)
MONITOR_ENUM = frozenset(("pending", "in_progress", "paused", "complete", "blocked"))
REGRESSION_ENUM = frozenset(
    ("pending", "not_applicable", "red_verified", "complete", "exempt")
)
VARIANT_ENUM = frozenset(("pending", "complete", "skipped"))
DEFECT_MODE_ENUM = frozenset(("runtime_bug_fix", "skill_helper_defect", "none"))
CHANGE_TYPE_ENUM = frozenset(("bug_fix", "feature", "refactor", "skill_only"))
HANDOFF_STATUS_ENUM = frozenset(("idle", "pending", "complete", "failed"))
OPERATION_STATUS_ENUM = frozenset(("pending", "retryable", "complete", "failed"))
LAST_CHECK_ENUM = frozenset(("passing", "failing", "pending"))
LEDGER_STATUS_ENUM = frozenset(
    ("open", "fixed", "false_positive", "escalated", "auto_closed")
)
TERMINAL_MONITOR = frozenset(("complete", "paused", "blocked"))

# Full top-level key inventory of the documented v1 schema.  Presence beyond
# the tier's required set is fine as long as the key is known.
KNOWN_TOP_LEVEL_KEYS = frozenset(
    (
        "state_schema_version",
        "workflow_id",
        "description",
        "branch",
        "base_branch",
        "pre_takeover_branch",
        "current_phase",
        "pr_number",
        "stash_ref",
        "resolved_conventions",
        "validated_ticket",
        "regression_evidence",
        "variant_analysis",
        "last_processed_comments",
        "last_processed_reviews",
        "last_processed_threads",
        "authenticated_actor",
        "thread_reply_timestamps",
        "acknowledged_top_level_comments",
        "acknowledged_top_level_reviews",
        "acknowledged_human_top_level_comments",
        "acknowledged_human_top_level_reviews",
        "exhausted_feedback",
        "manual_unknown_feedback",
        "manual_branch_protection_blockers",
        "human_roundtrip",
        "handoffs",
        "last_check_status",
        "monitor_iterations",
        "monitor_poll_ticks",
        "monitor_self_review_call_count",
        "post_push_until",
        "last_observed_head_sha",
        "clean_poll_timestamps",
        "attempt_log",
        "gstack_integration",
        "finding_ledger",
        "phases",
        "decision_audit_trail",
    )
)

MINIMAL_REQUIRED = ("state_schema_version", "workflow_id", "description", "current_phase")
TAKEOVER_REQUIRED = MINIMAL_REQUIRED + ("pr_number", "base_branch")
FULL_REQUIRED = tuple(sorted(KNOWN_TOP_LEVEL_KEYS))

# Conservative instruction-pattern heuristics.  Advisory: a tainted string is
# surfaced (path + truncated digest), never echoed and never obeyed; taint
# alone does not flip the structural verdict.
TAINT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore (all |any )?(previous|prior|above) (instructions|context)",
        r"disregard [^\n]{0,60}instructions",
        r"curl[^|\n]{0,200}\|\s*(ba|z)?sh",
        r"wget[^|\n]{0,200}\|\s*(ba|z)?sh",
        r"rm\s+-rf\s+[~/]",
        r"you (must|should) now (run|execute)",
        r"sudo\s+rm\s",
    )
)

_SAFE_PATH_KEY = re.compile(r"^[A-Za-z0-9_.:@ -]{1,64}$")


class StructuralError(Exception):
    """Raised by the restricted parser; message never contains scalar values."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:24]


def _is_tainted(text: str) -> bool:
    return any(pattern.search(text) for pattern in TAINT_PATTERNS)


def _safe_key(key: str) -> str:
    """Render a dynamic map key for diagnostics without reproducing it.

    Masks BOTH charset-unsafe keys and instruction-like (tainted) keys — a
    tainted key can be plain letters and spaces, so the charset check alone
    is not sufficient. Every diagnostic surface (validator errors and taint
    paths) routes through this function.
    """
    if _is_tainted(key) or not _SAFE_PATH_KEY.match(key):
        return f"key<{_digest(key)}>"
    return key


# ---------------------------------------------------------------------------
# Restricted parser
# ---------------------------------------------------------------------------


class _Line:
    __slots__ = ("number", "indent", "content")

    def __init__(self, number: int, indent: int, content: str) -> None:
        self.number = number
        self.indent = indent
        self.content = content


def _strip_comment(text: str, line_number: int) -> str:
    """Remove a trailing comment, honoring JSON-quoted string contents."""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    if in_string:
        raise StructuralError(f"line {line_number}: unterminated quoted string")
    return "".join(out).rstrip()


def _parse_quoted(text: str, line_number: int) -> tuple[str, str]:
    """Parse a leading JSON-quoted string; return (value, remainder)."""
    try:
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(text)
    except ValueError as error:
        raise StructuralError(f"line {line_number}: invalid quoted string") from error
    if not isinstance(value, str):
        raise StructuralError(f"line {line_number}: expected a quoted string")
    return value, text[end:]


def _parse_scalar(token: str, line_number: int) -> Any:
    token = token.strip()
    if token == "" or token == "null" or token == "~":
        return None
    if token in ("true", "false"):
        return token == "true"
    if token.startswith('"'):
        value, rest = _parse_quoted(token, line_number)
        if rest.strip():
            raise StructuralError(f"line {line_number}: trailing content after string")
        return value
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        raise StructuralError(f"line {line_number}: floats are not part of the schema")
    if token.startswith(("&", "*", "!", "|", ">")) or token == "<<":
        raise StructuralError(
            f"line {line_number}: anchors, aliases, tags, and block scalars are rejected"
        )
    if token.startswith(("{", "[")):
        return _parse_inline(token, line_number)
    if _PLAIN_SCALAR_FORBIDDEN.search(token):
        raise StructuralError(
            f"line {line_number}: plain scalar contains characters that require quoting"
        )
    if _CONTROL_CHARS.search(token):
        raise StructuralError(f"line {line_number}: control characters are rejected")
    return token


def _parse_inline(token: str, line_number: int) -> Any:
    """Inline collections: empty {} / [] and single-line JSON-scalar lists."""
    if token == "{}":
        return {}
    if token == "[]":
        return []
    if token.startswith("{"):
        raise StructuralError(
            f"line {line_number}: non-empty inline mappings are rejected; use block form"
        )
    try:
        value = json.loads(token)
    except ValueError as error:
        raise StructuralError(
            f"line {line_number}: inline lists must be single-line JSON-compatible"
        ) from error
    if not isinstance(value, list) or any(
        not isinstance(item, (str, int, bool)) and item is not None for item in value
    ):
        raise StructuralError(
            f"line {line_number}: inline lists may contain only scalars"
        )
    return value


def _parse_key(text: str, line_number: int) -> tuple[str, str]:
    """Parse a mapping key; return (key, remainder-after-colon)."""
    if text.startswith('"'):
        key, rest = _parse_quoted(text, line_number)
    else:
        match = re.match(r"^([^\s:]+)", text)
        if not match:
            raise StructuralError(f"line {line_number}: expected a mapping key")
        key = match.group(1)
        rest = text[match.end() :]
        if not _PLAIN_KEY.match(key):
            raise StructuralError(
                f"line {line_number}: non-identifier keys must be JSON-quoted"
            )
    if key == "<<":
        raise StructuralError(f"line {line_number}: merge keys are rejected")
    rest = rest.lstrip()
    if not rest.startswith(":"):
        raise StructuralError(f"line {line_number}: expected ':' after mapping key")
    remainder = rest[1:]
    if remainder and not remainder.startswith(" "):
        raise StructuralError(f"line {line_number}: expected space after ':'")
    return key, remainder.strip()


def _collect_lines(fence_lines: list[tuple[int, str]]) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in fence_lines:
        if "\t" in raw:
            raise StructuralError(f"line {number}: tabs are rejected")
        stripped = _strip_comment(raw, number)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent % 2 != 0:
            raise StructuralError(f"line {number}: indentation must be 2-space aligned")
        lines.append(_Line(number, indent, stripped.strip()))
    return lines


def _parse_block(lines: list[_Line], index: int, indent: int) -> tuple[Any, int]:
    """Parse a block mapping or list at the given indent level."""
    if index >= len(lines) or lines[index].indent != indent:
        raise StructuralError(
            f"line {lines[min(index, len(lines) - 1)].number}: malformed block structure"
        )
    if lines[index].content.startswith("- "):
        return _parse_block_list(lines, index, indent)
    return _parse_block_map(lines, index, indent)


def _parse_block_map(lines: list[_Line], index: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        if line.content.startswith("- "):
            raise StructuralError(f"line {line.number}: unexpected list item in mapping")
        key, value_text = _parse_key(line.content, line.number)
        if key in result:
            raise StructuralError(f"line {line.number}: duplicate key {_safe_key(key)!r}")
        if value_text:
            result[key] = _parse_scalar(value_text, line.number)
            index += 1
        else:
            index += 1
            if index < len(lines) and lines[index].indent > indent:
                value, index = _parse_block(lines, index, lines[index].indent)
                result[key] = value
            else:
                result[key] = None
        if index < len(lines) and lines[index].indent > indent:
            raise StructuralError(
                f"line {lines[index].number}: unexpected deeper indentation"
            )
    return result, index


def _parse_block_list(lines: list[_Line], index: int, indent: int) -> tuple[list, int]:
    result: list[Any] = []
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        if not line.content.startswith("- "):
            break
        item_text = line.content[2:].strip()
        if not item_text:
            raise StructuralError(f"line {line.number}: empty list item")
        if ":" in item_text and not item_text.startswith(('"', "[", "{")):
            key, value_text = _parse_key(item_text, line.number)
            record: dict[str, Any] = {}
            record[key] = _parse_scalar(value_text, line.number) if value_text else None
            index += 1
            if index < len(lines) and lines[index].indent == indent + 2 and not lines[
                index
            ].content.startswith("- "):
                extra, index = _parse_block_map(lines, index, indent + 2)
                for extra_key, extra_value in extra.items():
                    if extra_key in record:
                        raise StructuralError(
                            f"line {line.number}: duplicate key in list record"
                        )
                    record[extra_key] = extra_value
            result.append(record)
        elif item_text.startswith('"') and item_text.rstrip().endswith(('":',)):
            raise StructuralError(f"line {line.number}: malformed quoted record key")
        else:
            result.append(_parse_scalar(item_text, line.number))
            index += 1
    return result, index


def parse_state_text(text: str) -> tuple[dict, list[str]]:
    """Parse the full state file; return (frontmatter mapping, body lines)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise StructuralError("line 1: state file must begin with a '---' fence")
    fence_lines: list[tuple[int, str]] = []
    close_index: int | None = None
    for offset, raw in enumerate(lines[1:], start=2):
        if raw.strip() == "---":
            close_index = offset
            break
        if raw.strip() == "...":
            raise StructuralError(f"line {offset}: document end markers are rejected")
        fence_lines.append((offset, raw))
    if close_index is None:
        raise StructuralError("state file frontmatter fence is never closed")
    # close_index is the 1-based line number of the closing fence, which is
    # lines[close_index - 1] zero-based; the body starts right after it.  The
    # body is OPAQUE: it is never parsed as data (so later "---" lines are
    # plain text, e.g. markdown horizontal rules) — it is only taint-scanned.
    body_lines = lines[close_index:]
    parsed_lines = _collect_lines(fence_lines)
    if not parsed_lines:
        raise StructuralError("state frontmatter is empty")
    if parsed_lines[0].indent != 0:
        raise StructuralError(
            f"line {parsed_lines[0].number}: top level must start at column 0"
        )
    mapping, index = _parse_block_map(parsed_lines, 0, 0)
    if index != len(parsed_lines):
        raise StructuralError(
            f"line {parsed_lines[index].number}: unparsed trailing content in frontmatter"
        )
    return mapping, body_lines


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _type_name(value: Any) -> str:
    return type(value).__name__


def _is_iso_timestamp(value: Any) -> bool:
    """Shape AND calendar validity, timezone required (not just regex shape)."""
    if not isinstance(value, str) or not _ISO_TS.match(value):
        return False
    normalized = value.replace("Z", "+00:00")
    # Normalize fractional seconds to exactly 6 digits so the verdict is
    # identical on every interpreter (pre-3.11 fromisoformat only accepts
    # 3- or 6-digit fractions; 3.11+ accepts any length).
    normalized = re.sub(
        r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), normalized, count=1
    )
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_full_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_FULL_HEX.match(value))


def _check_test_path(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return "must be a non-empty string"
    if _CONTROL_CHARS.search(path_value):
        return "contains control characters"
    if path_value.startswith(("/", "-")) or re.match(r"^[A-Za-z]:[\\/]", path_value):
        return "must be repository-relative and must not start with '-'"
    segments = path_value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return "must be normalized without traversal segments"
    return None


class _Validator:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    # -- field helpers ----------------------------------------------------

    def require_string(self, mapping: dict, key: str, path: str, *, nullable: bool = False) -> None:
        value = mapping.get(key)
        if value is None:
            if not nullable:
                self.error(f"{path}.{key}: required string is missing or null")
            return
        if not isinstance(value, str) or not value:
            self.error(f"{path}.{key}: expected a non-empty string, got {_type_name(value)}")

    def check_enum(self, value: Any, allowed: frozenset, path: str) -> bool:
        if not isinstance(value, str) or value not in allowed:
            self.error(f"{path}: illegal value")
            return False
        return True

    # -- tiers -------------------------------------------------------------

    def tier(self) -> tuple[str, tuple[str, ...]]:
        phase = self.state.get("current_phase")
        if phase in ("entry", "aborted_at_entry"):
            return "minimal_entry", MINIMAL_REQUIRED
        if phase in ("takeover", "aborted_at_takeover"):
            return "takeover", TAKEOVER_REQUIRED
        return "full", FULL_REQUIRED

    def validate(self) -> str:
        state = self.state
        version = state.get("state_schema_version")
        if version is None:
            self.error(
                "state_schema_version: missing (pre-versioning state); re-derive from "
                "remote truth; migrate only by manual review adding the field"
            )
        elif not isinstance(version, int) or isinstance(version, bool):
            self.error("state_schema_version: must be an integer")
        elif version > SCHEMA_VERSION:
            self.error(f"state_schema_version: unsupported future version {version}")
        elif version < 1:
            self.error("state_schema_version: must be >= 1")

        phase = state.get("current_phase")
        if not isinstance(phase, str) or phase not in LEGAL_CURRENT_PHASES:
            self.error("current_phase: illegal value")
            return "unknown"

        tier_name, required = self.tier()
        for key in required:
            if key not in state:
                self.error(f"top-level: required key {key!r} is missing for tier {tier_name}")
        for key in state:
            if key not in KNOWN_TOP_LEVEL_KEYS:
                self.error(f"top-level: unknown key {_safe_key(str(key))!r}")

        self.require_string(state, "workflow_id", "top-level")
        self.require_string(state, "description", "top-level")
        if "base_branch" in state and (
            tier_name != "minimal_entry" or state.get("base_branch") is not None
        ):
            self.require_string(state, "base_branch", "top-level", nullable=(tier_name == "minimal_entry"))
        if tier_name == "takeover" and "pr_number" in state and state.get("pr_number") is None:
            # Presence alone is not enough: a takeover without a PR number is
            # meaningless, so the takeover tier requires a non-null value.
            # (Absence is reported once by the required-key loop above.)
            self.error("pr_number: takeover requires a non-null PR number")
        if "pr_number" in state and state.get("pr_number") is not None:
            pr_number = state.get("pr_number")
            if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
                self.error("pr_number: must be a positive integer")
        for sha_key in ("last_observed_head_sha", "stash_ref"):
            value = state.get(sha_key)
            if value is not None and sha_key in state and not _is_full_hex(value):
                self.error(f"{sha_key}: must be a full-length hex object ID")
        if "post_push_until" in state and state.get("post_push_until") is not None:
            if not _is_iso_timestamp(state.get("post_push_until")):
                self.error("post_push_until: must be an ISO 8601 timestamp with timezone")
        for counter in (
            "monitor_iterations",
            "monitor_poll_ticks",
            "monitor_self_review_call_count",
        ):
            if counter in state:
                value = state.get(counter)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    self.error(f"{counter}: must be a non-negative integer")
        if "last_check_status" in state:
            self.check_enum(state.get("last_check_status"), LAST_CHECK_ENUM, "last_check_status")

        phases = state.get("phases")
        if tier_name == "full" or "phases" in state:
            self.validate_phases(phases, phase)
        self.validate_evidence(tier_name)
        if "attempt_log" in state:
            self.validate_attempt_log(state.get("attempt_log"))
        for ts_map_key in (
            "thread_reply_timestamps",
            "last_processed_comments",
            "last_processed_reviews",
            "last_processed_threads",
        ):
            if ts_map_key in state:
                self.validate_timestamp_map(state.get(ts_map_key), ts_map_key)
        for ack_key in (
            "acknowledged_top_level_comments",
            "acknowledged_top_level_reviews",
            "acknowledged_human_top_level_comments",
            "acknowledged_human_top_level_reviews",
        ):
            if ack_key in state:
                self.validate_ack_map(state.get(ack_key), ack_key)
        if "handoffs" in state:
            self.validate_handoffs(state.get("handoffs"), phases)
        if "human_roundtrip" in state:
            self.validate_human_roundtrip(state.get("human_roundtrip"))
        if "finding_ledger" in state:
            self.validate_finding_ledger(state.get("finding_ledger"))
        if "gstack_integration" in state:
            self.validate_gstack(state.get("gstack_integration"), tier_name)
        if "clean_poll_timestamps" in state:
            self.validate_clean_polls(state.get("clean_poll_timestamps"))
        # human_roundtrip's mapping check lives in validate_human_roundtrip;
        # listing it here would duplicate the diagnostic.
        for structured_key in (
            "resolved_conventions",
            "validated_ticket",
        ):
            if structured_key in state and not isinstance(state.get(structured_key), dict):
                self.error(f"{structured_key}: must be a mapping")
        conventions = state.get("resolved_conventions")
        if isinstance(conventions, dict):
            self.validate_conventions(conventions)
        if "decision_audit_trail" in state:
            trail = state.get("decision_audit_trail")
            if not isinstance(trail, list) or any(
                not isinstance(item, str) or not item for item in trail
            ):
                self.error(
                    "decision_audit_trail: must be a list of non-empty strings"
                )
        return tier_name

    # -- sections ----------------------------------------------------------

    def validate_phases(self, phases: Any, current_phase: str) -> None:
        if not isinstance(phases, dict):
            self.error("phases: must be a mapping")
            return
        for name in PHASE_NAMES:
            if name not in phases:
                self.error(f"phases.{name}: missing")
        for name in phases:
            if name not in PHASE_NAMES:
                self.error(f"phases: unknown key {_safe_key(str(name))!r}")

        def status_of(name: str) -> str | None:
            value = phases.get(name)
            if name == "runtime_verification":
                if not isinstance(value, dict):
                    self.error("phases.runtime_verification: must be a mapping with a status")
                    return None
                status = value.get("status")
                if not self.check_enum(status, RUNTIME_VERIFICATION_ENUM, "phases.runtime_verification.status"):
                    return None
                if status == "waived" and not value.get("reason"):
                    self.error(
                        "phases.runtime_verification: waived requires a non-empty reason"
                    )
                return status
            enum = MONITOR_ENUM if name == "monitor" else SIMPLE_PHASE_ENUM
            if not self.check_enum(value, enum, f"phases.{name}"):
                return None
            return value

        statuses = {name: status_of(name) for name in PHASE_NAMES if name in phases}

        # (i) current_phase / phase-status agreement
        if current_phase in PHASE_NAMES:
            status = statuses.get(current_phase)
            if status == "pending":
                self.error(
                    f"invariant(i): current_phase {current_phase!r} disagrees with a pending phase status"
                )
        elif current_phase.startswith("aborted_at_"):
            aborted = current_phase.removeprefix("aborted_at_")
            if aborted in PHASE_NAMES and statuses.get(aborted) != "blocked":
                self.error(
                    f"invariant(i): {current_phase!r} requires phases.{aborted} to be blocked"
                )

        # (ii) successful-predecessor chain
        chain = (
            ("plan_review", "plan", ("complete",)),
            ("implementation", "plan_review", ("complete",)),
            ("self_review", "implementation", ("complete",)),
            ("runtime_verification", "self_review", ("complete",)),
            ("pr", "runtime_verification", ("complete", "waived")),
            ("monitor", "pr", ("complete",)),
        )
        for successor, predecessor, allowed in chain:
            successor_status = statuses.get(successor)
            predecessor_status = statuses.get(predecessor)
            if successor_status and successor_status != "pending":
                if predecessor_status not in allowed:
                    self.error(
                        f"invariant(ii): phases.{successor} is non-pending but "
                        f"phases.{predecessor} is not in {'|'.join(allowed)}"
                    )
        # A complete pr phase (and via the chain, every monitor state) proves a
        # PR exists — pr_number may no longer be null. in_progress/blocked stay
        # exempt: they legitimately precede `gh pr create`.
        if statuses.get("pr") == "complete" and self.state.get("pr_number") is None:
            self.error(
                "invariant(ii): phases.pr complete requires a non-null pr_number"
            )

    def validate_evidence(self, tier_name: str) -> None:
        state = self.state
        regression = state.get("regression_evidence")
        variants = state.get("variant_analysis")
        if tier_name == "full":
            if not isinstance(regression, dict):
                self.error("regression_evidence: must be a mapping")
                regression = None
            if not isinstance(variants, dict):
                self.error("variant_analysis: must be a mapping")
                variants = None
        else:
            if regression is not None and not isinstance(regression, dict):
                self.error("regression_evidence: must be a mapping when present")
                regression = None
            if variants is not None and not isinstance(variants, dict):
                self.error("variant_analysis: must be a mapping when present")
                variants = None

        regression_status = None
        if isinstance(regression, dict):
            regression_status = regression.get("status")
            if self.check_enum(regression_status, REGRESSION_ENUM, "regression_evidence.status"):
                self.validate_regression_records(regression, regression_status)
            else:
                regression_status = None

        variant_status = None
        if isinstance(variants, dict):
            variant_status = variants.get("status")
            if not self.check_enum(variant_status, VARIANT_ENUM, "variant_analysis.status"):
                variant_status = None
            else:
                if variant_status == "complete" and not _is_full_hex(
                    variants.get("analyzed_head_sha")
                ):
                    self.error(
                        "invariant(vi): variant_analysis.complete requires a full-hex analyzed_head_sha"
                    )
                if variant_status == "skipped" and not variants.get("skipped_reason"):
                    self.error("invariant(v): skipped requires skipped_reason")
                for list_key in ("search_patterns", "variants_fixed", "variants_reported"):
                    value = variants.get(list_key)
                    if value is not None and not isinstance(value, list):
                        self.error(f"variant_analysis.{list_key}: must be a list")
                inspected = variants.get("matches_inspected")
                if inspected is not None and (
                    not isinstance(inspected, int) or isinstance(inspected, bool) or inspected < 0
                ):
                    self.error("variant_analysis.matches_inspected: must be a non-negative integer")

        # (iv) defect_evidence_mode consistency
        gstack = state.get("gstack_integration")
        mode = gstack.get("defect_evidence_mode") if isinstance(gstack, dict) else None
        change_type = gstack.get("change_type") if isinstance(gstack, dict) else None
        phases = state.get("phases") if isinstance(state.get("phases"), dict) else {}
        pr_status = phases.get("pr")
        if mode is not None and isinstance(mode, str) and mode in DEFECT_MODE_ENUM:
            if mode == "runtime_bug_fix" and change_type != "bug_fix":
                self.error(
                    "invariant(iv): defect_evidence_mode runtime_bug_fix requires change_type bug_fix"
                )
            if mode == "skill_helper_defect" and change_type != "skill_only":
                self.error(
                    "invariant(iv): defect_evidence_mode skill_helper_defect requires change_type skill_only"
                )
            if isinstance(pr_status, str) and pr_status != "pending":
                if mode == "none":
                    if regression_status not in (None, "not_applicable"):
                        self.error(
                            "invariant(iv): mode none requires regression_evidence not_applicable once pr is non-pending"
                        )
                    if variant_status not in (None, "skipped"):
                        self.error(
                            "invariant(iv): mode none requires variant_analysis skipped once pr is non-pending"
                        )
                else:
                    if regression_status not in ("complete", "exempt"):
                        self.error(
                            "invariant(iv): defect mode requires regression complete|exempt once pr is non-pending"
                        )
                    if variant_status != "complete":
                        self.error(
                            "invariant(iv): defect mode requires variant_analysis complete once pr is non-pending"
                        )

    def validate_regression_records(self, regression: dict, status: Any) -> None:
        def check_record(record: Any, path: str, expected_exit: int | None) -> bool:
            if record is None:
                return False
            if not isinstance(record, dict):
                self.error(f"{path}: must be a mapping")
                return False
            complete = True
            argv = record.get("argv")
            if not isinstance(argv, list) or not argv or any(
                not isinstance(item, str) for item in argv
            ):
                self.error(f"{path}.argv: must be a non-empty list of strings (audit-only)")
                complete = False
            exit_code = record.get("exit_code")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                self.error(f"{path}.exit_code: must be an integer")
                complete = False
            elif expected_exit == 0 and exit_code != 0:
                self.error(f"{path}.exit_code: green evidence must record exit code 0")
                complete = False
            elif expected_exit == 1 and exit_code == 0:
                self.error(f"{path}.exit_code: red evidence must record a failing exit code")
                complete = False
            if not _is_iso_timestamp(record.get("observed_at")):
                self.error(f"{path}.observed_at: must be a timezone-aware ISO 8601 timestamp")
                complete = False
            if not _is_full_hex(record.get("tested_head_sha")):
                self.error(f"{path}.tested_head_sha: must be a full-length hex object ID")
                complete = False
            if not isinstance(record.get("output_digest"), str) or not record.get("output_digest"):
                self.error(f"{path}.output_digest: must be a non-empty string")
                complete = False
            return complete

        red = regression.get("red_evidence")
        green = regression.get("green_evidence")
        red_ok = check_record(red, "regression_evidence.red_evidence", 1) if red is not None else False
        green_ok = (
            check_record(green, "regression_evidence.green_evidence", 0)
            if green is not None
            else False
        )

        test_paths = regression.get("test_paths")
        if test_paths is not None:
            if not isinstance(test_paths, list):
                self.error("regression_evidence.test_paths: must be a list")
            else:
                for position, item in enumerate(test_paths):
                    problem = _check_test_path(item)
                    if problem:
                        self.error(f"regression_evidence.test_paths[{position}]: {problem}")

        evaluated = regression.get("evaluated_head_sha")
        if status in ("red_verified", "complete", "exempt") and not regression.get(
            "root_cause"
        ):
            self.error(f"invariant(v): {status} requires root_cause")
        if status in ("red_verified", "complete") and not test_paths:
            self.error(f"invariant(v): {status} requires non-empty test_paths")
        if status == "red_verified":
            if red is None or not red_ok:
                self.error("invariant(v): red_verified requires a complete red_evidence record")
        elif status == "complete":
            if green is None or not green_ok:
                self.error("invariant(v): complete requires a complete green_evidence record")
            if red is None and not regression.get("red_exemption_reason"):
                self.error(
                    "invariant(v): complete requires red_evidence or red_exemption_reason"
                )
            if not _is_full_hex(evaluated):
                self.error("invariant(v): complete requires a full-hex evaluated_head_sha")
            elif isinstance(green, dict) and green.get("tested_head_sha") != evaluated:
                self.error(
                    "invariant(v): evaluated_head_sha must equal green_evidence.tested_head_sha"
                )
        elif status == "exempt":
            if not regression.get("exemption_reason"):
                self.error("invariant(v): exempt requires exemption_reason")
            if not _is_full_hex(evaluated):
                self.error("invariant(v): exempt requires a full-hex evaluated_head_sha")
        elif status == "not_applicable":
            if red is not None or green is not None:
                self.error("invariant(v): not_applicable rejects execution evidence")

    def validate_attempt_log(self, attempt_log: Any) -> None:
        if not isinstance(attempt_log, dict):
            self.error("attempt_log: must be a mapping")
            return
        for key, value in attempt_log.items():
            if not isinstance(key, str) or not key:
                self.error("attempt_log: keys must be non-empty strings")
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                self.error(
                    f"attempt_log.{_safe_key(key)}: must be a non-negative integer"
                )

    def validate_timestamp_map(self, mapping: Any, path: str) -> None:
        if not isinstance(mapping, dict):
            self.error(f"{path}: must be a mapping")
            return
        for key, value in mapping.items():
            if not _is_iso_timestamp(value):
                self.error(f"{path}.{_safe_key(str(key))}: must be an ISO 8601 timestamp")

    def validate_ack_map(self, mapping: Any, path: str) -> None:
        if not isinstance(mapping, dict):
            self.error(f"{path}: must be a mapping")
            return
        for key, value in mapping.items():
            safe = _safe_key(str(key))
            if _is_iso_timestamp(value):
                continue
            if isinstance(value, dict):
                if not value:
                    self.error(f"{path}.{safe}: acknowledgment record must not be empty")
                continue
            self.error(f"{path}.{safe}: must be a timestamp or an acknowledgment record")

    def validate_handoffs(self, handoffs: Any, phases: Any) -> None:
        if not isinstance(handoffs, dict):
            self.error("handoffs: must be a mapping")
            return
        monitor_status = None
        if isinstance(phases, dict):
            monitor_status = phases.get("monitor")
        for kind, handoff in handoffs.items():
            safe_kind = _safe_key(str(kind))
            if not isinstance(handoff, dict):
                self.error(f"handoffs.{safe_kind}: must be a mapping")
                continue
            status = handoff.get("status")
            if not self.check_enum(status, HANDOFF_STATUS_ENUM, f"handoffs.{safe_kind}.status"):
                continue
            operations = handoff.get("operations")
            results = handoff.get("operation_results")
            if operations is None:
                operations = []
            if results is None:
                results = {}
            if not isinstance(operations, list) or any(
                not isinstance(op, str) or not op for op in operations
            ):
                self.error(f"handoffs.{safe_kind}.operations: must be a list of non-empty string IDs")
                continue
            if len(set(operations)) != len(operations):
                self.error(f"handoffs.{safe_kind}.operations: operation IDs must be unique")
                continue
            if not isinstance(results, dict):
                self.error(f"handoffs.{safe_kind}.operation_results: must be a mapping")
                continue

            planned = set(operations)
            result_statuses: dict[str, str] = {}
            for op_id, record in results.items():
                safe_op = _safe_key(str(op_id))
                if op_id not in planned:
                    self.error(
                        f"invariant(iii): handoffs.{safe_kind}.operation_results.{safe_op} is an orphan result"
                    )
                    continue
                if not isinstance(record, dict):
                    self.error(f"handoffs.{safe_kind}.operation_results.{safe_op}: must be a mapping")
                    continue
                op_status = record.get("status")
                if not self.check_enum(
                    op_status,
                    OPERATION_STATUS_ENUM,
                    f"handoffs.{safe_kind}.operation_results.{safe_op}.status",
                ):
                    continue
                result_statuses[op_id] = op_status
                op_path = f"handoffs.{safe_kind}.operation_results.{safe_op}"
                for ts_field in ("started_at", "verified_at"):
                    ts_value = record.get(ts_field)
                    if ts_value is not None and not _is_iso_timestamp(ts_value):
                        self.error(f"{op_path}.{ts_field}: must be an ISO 8601 timestamp")
                if op_status == "pending" and not record.get("started_at"):
                    self.error(f"{op_path}: pending requires started_at")
                if op_status == "complete":
                    if not record.get("verified_at"):
                        self.error(f"{op_path}: complete requires verified_at")
                    if not record.get("evidence"):
                        self.error(f"{op_path}: complete requires non-empty evidence")
                if op_status in ("failed", "retryable"):
                    if not record.get("verified_at"):
                        self.error(f"{op_path}: {op_status} requires verified_at")
                    if not record.get("error"):
                        self.error(f"{op_path}: {op_status} requires a non-empty error")

            missing = [op for op in operations if op not in result_statuses]
            nonterminal = [
                op for op, op_status in result_statuses.items()
                if op_status in ("pending", "retryable")
            ]
            all_terminal = not missing and not nonterminal
            derived: str
            if not operations and not results:
                derived = "idle"
            elif missing or nonterminal:
                derived = "pending"
            elif all_terminal and all(
                result_statuses[op] == "complete" for op in operations
            ):
                derived = "complete"
            else:
                derived = "failed"
            if status != derived:
                self.error(
                    f"invariant(iii): handoffs.{safe_kind}.status {status!r} does not match derived {derived!r}"
                )
            if isinstance(monitor_status, str) and monitor_status in TERMINAL_MONITOR:
                if missing or nonterminal:
                    self.error(
                        f"invariant(iii): terminal monitor forbids missing/pending/retryable results in handoffs.{safe_kind}"
                    )

    def validate_human_roundtrip(self, roundtrip: Any) -> None:
        if not isinstance(roundtrip, dict):
            self.error("human_roundtrip: must be a mapping")
            return
        reviewers = roundtrip.get("reviewers")
        if reviewers is None:
            return
        if not isinstance(reviewers, dict):
            self.error("human_roundtrip.reviewers: must be a mapping")
            return
        for login, record in reviewers.items():
            safe = _safe_key(str(login))
            if not isinstance(record, dict):
                self.error(f"human_roundtrip.reviewers.{safe}: must be a mapping")
                continue
            if "assignable" in record and not isinstance(record.get("assignable"), bool):
                self.error(f"human_roundtrip.reviewers.{safe}.assignable: must be a boolean")
            for list_key in ("current_review_body_ids", "current_inline_root_ids", "fix_shas", "pushed_fix_shas"):
                if list_key in record and not isinstance(record.get(list_key), list):
                    self.error(f"human_roundtrip.reviewers.{safe}.{list_key}: must be a list")
            for map_key in ("review_bodies", "inline_roots"):
                if map_key in record and not isinstance(record.get(map_key), dict):
                    self.error(f"human_roundtrip.reviewers.{safe}.{map_key}: must be a mapping")

    def validate_finding_ledger(self, ledger: Any) -> None:
        if not isinstance(ledger, dict):
            self.error("finding_ledger: must be a mapping")
            return
        next_seq = ledger.get("next_seq_id")
        if next_seq is not None and (
            not isinstance(next_seq, int) or isinstance(next_seq, bool) or next_seq < 1
        ):
            self.error("finding_ledger.next_seq_id: must be an integer >= 1")
        entries = ledger.get("entries")
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            self.error("finding_ledger.entries: must be a list")
            return
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                self.error(f"finding_ledger.entries[{position}]: must be a mapping")
                continue
            seq_id = entry.get("seq_id")
            if not isinstance(seq_id, int) or isinstance(seq_id, bool) or seq_id < 1:
                self.error(f"finding_ledger.entries[{position}].seq_id: must be an integer >= 1")
            for str_key in ("fingerprint", "session_id"):
                if not isinstance(entry.get(str_key), str) or not entry.get(str_key):
                    self.error(
                        f"finding_ledger.entries[{position}].{str_key}: must be a non-empty string"
                    )
            if not self.check_enum(
                entry.get("status"), LEDGER_STATUS_ENUM, f"finding_ledger.entries[{position}].status"
            ):
                continue
            pass_number = entry.get("pass_number")
            if pass_number is not None and (
                not isinstance(pass_number, int) or isinstance(pass_number, bool) or pass_number < 1
            ):
                self.error(
                    f"finding_ledger.entries[{position}].pass_number: must be an integer >= 1"
                )
        seq_ids = [
            entry.get("seq_id")
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("seq_id"), int)
            and not isinstance(entry.get("seq_id"), bool)
        ]
        if len(set(seq_ids)) != len(seq_ids):
            self.error("finding_ledger.entries: seq_id values must be unique")
        if seq_ids:
            if not isinstance(next_seq, int) or isinstance(next_seq, bool):
                self.error(
                    "finding_ledger.next_seq_id: required when entries exist"
                )
            elif next_seq != max(seq_ids) + 1:
                self.error(
                    "finding_ledger.next_seq_id: must equal the highest seq_id + 1"
                )
        convergence = ledger.get("convergence")
        if convergence is not None and not isinstance(convergence, dict):
            self.error("finding_ledger.convergence: must be a mapping")

    def validate_gstack(self, gstack: Any, tier_name: str) -> None:
        if not isinstance(gstack, dict):
            self.error("gstack_integration: must be a mapping")
            return
        change_type = gstack.get("change_type")
        if change_type is not None:
            self.check_enum(change_type, CHANGE_TYPE_ENUM, "gstack_integration.change_type")
        mode = gstack.get("defect_evidence_mode")
        if mode is None:
            if tier_name == "full":
                self.error("gstack_integration.defect_evidence_mode: required from Phase 1 onward")
        else:
            self.check_enum(mode, DEFECT_MODE_ENUM, "gstack_integration.defect_evidence_mode")
        review = gstack.get("review")
        if isinstance(review, dict):
            notes = review.get("notes")
            if notes is not None and not isinstance(notes, list):
                self.error(
                    "gstack_integration.review.notes: must be an append-only list of records"
                )
            elif isinstance(notes, list):
                for position, record in enumerate(notes):
                    if not isinstance(record, dict):
                        self.error(
                            f"gstack_integration.review.notes[{position}]: must be a record"
                        )
                        continue
                    if "session_id" in record and not isinstance(record.get("session_id"), str):
                        self.error(
                            f"gstack_integration.review.notes[{position}].session_id: must be a string"
                        )
                    triggers = record.get("focus_triggers")
                    if triggers is not None and (
                        not isinstance(triggers, list)
                        or any(not isinstance(item, str) for item in triggers)
                    ):
                        self.error(
                            f"gstack_integration.review.notes[{position}].focus_triggers: must be a list of strings"
                        )

    def validate_conventions(self, conventions: dict) -> None:
        steps = conventions.get("quality_check_steps")
        if steps is not None:
            if not isinstance(steps, list):
                self.error("resolved_conventions.quality_check_steps: must be a list")
            else:
                for position, step in enumerate(steps):
                    # Executable cache — argv arrays of non-empty strings only.
                    if (
                        not isinstance(step, list)
                        or not step
                        or any(not isinstance(part, str) or not part for part in step)
                    ):
                        self.error(
                            f"resolved_conventions.quality_check_steps[{position}]: "
                            "must be a non-empty argv list of strings"
                        )
        branches = conventions.get("protected_branches")
        if branches is not None and (
            not isinstance(branches, list)
            or any(not isinstance(item, str) or not item for item in branches)
        ):
            self.error(
                "resolved_conventions.protected_branches: must be a list of non-empty strings"
            )
        environment = conventions.get("session_environment")
        if environment is not None:
            self.check_enum(
                environment,
                frozenset(("managed", "local")),
                "resolved_conventions.session_environment",
            )
        tracker = conventions.get("issue_tracker")
        if tracker is not None and not isinstance(tracker, dict):
            self.error("resolved_conventions.issue_tracker: must be a mapping")
        elif isinstance(tracker, dict):
            write_path = tracker.get("write_path")
            if write_path is not None:
                self.check_enum(
                    write_path,
                    frozenset(("environment_tool", "local_api", "none")),
                    "resolved_conventions.issue_tracker.write_path",
                )

    def validate_clean_polls(self, polls: Any) -> None:
        if not isinstance(polls, list):
            self.error("clean_poll_timestamps: must be a list")
            return
        for position, record in enumerate(polls):
            if not isinstance(record, dict):
                self.error(f"clean_poll_timestamps[{position}]: must be a record")
                continue
            if not _is_full_hex(record.get("head_sha")):
                self.error(f"clean_poll_timestamps[{position}].head_sha: must be full-length hex")
            if not _is_iso_timestamp(record.get("observed_at")):
                self.error(
                    f"clean_poll_timestamps[{position}].observed_at: must be an ISO 8601 timestamp"
                )


# ---------------------------------------------------------------------------
# Taint scan
# ---------------------------------------------------------------------------


def _scan_value(value: Any, path: str, findings: list[dict[str, str]]) -> None:
    if isinstance(value, str):
        if _is_tainted(value):
            findings.append({"path": path, "digest": _digest(value), "kind": "value"})
    elif isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            # Open-keyed maps make KEYS an injection surface too. _safe_key
            # digest-masks tainted keys (in every path they appear in), and a
            # tainted key is its own finding, distinguished from value
            # findings by kind so identical paths cannot collapse together.
            child_path = f"{path}.{_safe_key(key_text)}"
            if _is_tainted(key_text):
                findings.append(
                    {"path": child_path, "digest": _digest(key_text), "kind": "key"}
                )
            _scan_value(child, child_path, findings)
    elif isinstance(value, list):
        for position, child in enumerate(value):
            _scan_value(child, f"{path}[{position}]", findings)


def taint_scan(state: dict, body_lines: list[str]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for key, value in state.items():
        key_text = str(key)
        root_path = _safe_key(key_text)
        # Depth-0 keys are the same injection surface as nested map keys.
        if _is_tainted(key_text):
            findings.append(
                {"path": root_path, "digest": _digest(key_text), "kind": "key"}
            )
        _scan_value(value, root_path, findings)
    for offset, line in enumerate(body_lines, start=1):
        if _is_tainted(line):
            findings.append(
                {"path": f"body:{offset}", "digest": _digest(line), "kind": "body"}
            )
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate_state_text(text: str) -> dict[str, Any]:
    try:
        state, body_lines = parse_state_text(text)
    except StructuralError as error:
        return {
            "version": SCHEMA_VERSION,
            "state": SUSPECT,
            "errors": [f"structure: {error}"],
            "tainted": [],
            "phase_requirements": "unparsed",
        }
    validator = _Validator(state)
    tier_name = validator.validate()
    tainted = taint_scan(state, body_lines)
    return {
        "version": SCHEMA_VERSION,
        "state": SUSPECT if validator.errors else VALID,
        "errors": validator.errors,
        "tainted": tainted,
        "phase_requirements": tier_name,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "state": SUSPECT,
                    "errors": ["usage: state_schema.py <state-file>"],
                    "tainted": [],
                    "phase_requirements": "unparsed",
                }
            )
        )
        return 2
    try:
        with open(argv[1], encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        print(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "state": SUSPECT,
                    "errors": ["state file could not be read or decoded"],
                    "tainted": [],
                    "phase_requirements": "unparsed",
                }
            )
        )
        return 2
    result = evaluate_state_text(text)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == VALID else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
