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
      ``merge_readiness`` non-pending requires ``self_review: complete`` —
      a forward edge only: no later phase lists the optional gate as its
      predecessor, preserving pre-4b states and the Phase 5 run-the-gate-now
      recovery route.
      ``pr: complete`` additionally requires a non-null top-level
      ``pr_number``.
(iii) Per-handoff derived status, every tier: result keys are a SUBSET of
      planned operation IDs (never orphans); ``idle`` iff operations and
      results are both empty; ``pending`` iff operations exist and any
      planned result is missing/pending/retryable; ``complete``/``failed``
      require result keys to exactly equal planned IDs (``complete`` = all
      complete; ``failed`` = all terminal, at least one failed); operation
      IDs valid and unique. Terminal monitor (complete|paused|blocked)
      additionally prohibits missing/pending/retryable results. Each record
      satisfies the canonical operation-result contract
      (``validate_operation_result_record`` — the strict union shared with
      handoff_decision: started_at always; every supplied timestamp parses;
      verified_at >= started_at; non-empty mapping evidence on complete;
      non-empty string error on retryable/failed; no unknown fields;
      retryable below the attempt cap) and the collection satisfies
      ``validate_operation_collection`` (single in-flight; prefix with at
      most one in-flight tail).
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
(vii) Wait-key lifecycle: a terminal monitor (complete|paused|blocked)
      forbids a non-null ``next_retry_at``; a non-null ``next_retry_at``
      is bounded by now + MAX_QUOTA_WAIT_SECONDS + the 300s inclusive skew
      tolerance (beyond it the resume point is suspect); a non-null
      ``hold_started_at`` requires ``phases.monitor`` in_progress|blocked
      and must not be in the future (same tolerance).

Persisted evidence ``argv`` is AUDIT-ONLY and is never an execution source;
``test_paths`` entries are shape-checked here (repository-relative, no
leading dash, no control characters, no traversal); tracked-blob and
symlink-containment verification happens at use time through git itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
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
# Phase 4b (merge readiness) shipped after v1 state files existed in the wild.
# Its phase entry is OPTIONAL: a pre-4b state without it stays valid, and the
# workflow initializes it on resume and re-runs the gate before Phase 5 (see
# references/merge-readiness.md).  Listing it in PHASE_NAMES instead would make
# every old state suspect via the missing-key check.
OPTIONAL_PHASE_NAMES = ("merge_readiness",)
ENTRY_PHASES = ("entry", "takeover")
LEGAL_CURRENT_PHASES = frozenset(
    ENTRY_PHASES
    + PHASE_NAMES
    + OPTIONAL_PHASE_NAMES
    + tuple(
        f"aborted_at_{name}"
        for name in ENTRY_PHASES + PHASE_NAMES + OPTIONAL_PHASE_NAMES
    )
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
OPERATION_STATUS_ENUM = frozenset(
    ("pending", "retryable", "complete", "failed", "skipped_dependency")
)
# "precondition" (algo#1216 finding 3813491647): the observed pre-mutation
# remote state, persisted write-ahead with the pending record so resume's
# verify_before_retry can three-way compare instead of blindly replaying
# over a newer human action.
OPERATION_RESULT_ALLOWED_KEYS = frozenset(
    (
        "status",
        "attempts",
        "started_at",
        "verified_at",
        "response_id",
        "error",
        "evidence",
        "precondition",
    )
)

# --- Persisted handoff-kind vocabulary (single source of truth) -----------
# This schema is the dependency root, so the operation-ID grammar the planner
# MINTS and the grammar this validator ENFORCES must live here; handoff_decision
# REBINDS these names (see its rebind block) rather than re-declaring them, so a
# fabricated id can never pass one side while failing the other.
#
# Post-merge codex F3 + pass-3 codex F4: a prunable / persisted prior-generation
# id must match the COMPLETE grammar `family(:identity)?:g<12-hex>` - not merely
# end in the digest tail. A tail-only check let an extra-segment id like
# `qa.linear.assign_ticket:gBAD:g<hex>` launder as history while the real
# mutation re-queued. \Z, not $: a stray trailing newline would satisfy $ and
# launder a CURRENT completed id as history (replay).
GENERATION_SCOPED_ID = re.compile(
    r"(?P<family>[a-z_]+(?:\.[a-z_]+)+)"
    r"(?::(?P<identity>[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?))?"
    r":g[0-9a-f]{12}\Z"
)
# Maps family -> whether the id carries a per-reviewer identity segment.
# Pass-4 codex F1: identity is ARITY-CHECKED per family, not globally optional -
# `qa.github.replace_assignees:bogus:g<current>` parsed as a well-formed id under
# the optional grammar and pruned as history while the real operation re-queued.
QA_OPERATION_FAMILIES = {
    "qa.github.replace_assignees": False,
    "qa.github.verify_assignees": False,
    "qa.github.request_review": True,
    "qa.github.verify_review_request": True,
    "qa.linear.verify_ticket_binding": False,
    "qa.linear.assign_ticket": False,
    "qa.linear.verify_ticket_assignee": False,
    "qa.linear.set_ticket_state": False,
    "qa.linear.verify_ticket_state": False,
    "qa.linear.record_unavailable": False,
    "qa.linear.record_state_unavailable": False,
}
REVIEWER_REQUEST_FAMILIES = {
    "reviewer.github.request_review": True,
    "reviewer.github.verify_review_request": True,
    "reviewer.github.replace_assignees": False,
    "reviewer.github.verify_assignees": False,
}
ROUNDTRIP_FAMILIES = {
    "roundtrip.github.request_review": True,
    "roundtrip.github.verify_review_request": True,
    "roundtrip.github.replace_assignees": False,
    "roundtrip.github.verify_assignees": False,
}
# pr_artifacts is the DISTINCT artifact contract (references/state-and-safety.md):
# generic PR-anchored lifecycle mutations that are HEAD-bound, never generation-
# scoped, and never planned by handoff_decision.py. Its ids are
# algo#1216 r17 F5: the abbreviated-or-full Git object-ID grammar is ONE
# shared fact (Git accepts unambiguous abbreviations of at least 7 hex
# characters, up to a full SHA-256). Both consumers DERIVE from this
# fragment — PR_ARTIFACT_ID below and handoff_decision.GIT_OBJECT_ID —
# and validate_package pins each derivation as an operative source line,
# so the range can never drift between them. Full-match anchoring stays
# at the consumers (fullmatch / trailing \Z).
GIT_OBJECT_ID_HEX = r"[0-9a-fA-F]{7,64}"
GIT_OBJECT_ID = re.compile(GIT_OBJECT_ID_HEX)
# admin#1495 r13 F6: the closed set of runtime-verification kinds a
# repository may mandate.
RUNTIME_VERIFICATION_KINDS = frozenset(("ui", "api", "performance"))
# algo#1216 r17 F6: the closed runtime_verification record shape.
RUNTIME_VERIFICATION_KEYS = frozenset(
    (
        "status",
        "reason",
        "target_head_sha",
        "touched_diff_fingerprint",
        "started_at",
        "verified_at",
        "evidence",
    )
)
# `ci-evidence:<head_sha>` / `qa-rehearsal:<head_sha>` / `deferred-work:<head_sha>`
# (the last added by algo#1216 r16 F11 for the anchored deferred-work body
# record the terminal gate requires); the sha is the shared Git object-ID
# grammar above. The generation-family grammar would wrongly reject these,
# so this kind carries its own grammar - preserving that contract is an explicit
# requirement of algo#1216 r16 F6.
PR_ARTIFACT_ID = re.compile("(?:ci-evidence|qa-rehearsal|deferred-work):" + GIT_OBJECT_ID_HEX + r"\Z")


def parsed_generation_family(
    operation_id: str, vocabulary: dict[str, bool]
) -> str | None:
    """Family of a well-formed generation-scoped id, else ``None``.

    A record is a generation-scoped operation ONLY when this returns a family
    the vocabulary names AND the id's identity segment matches that family's
    arity (pass-4 codex F1): a surplus identity on an identity-free family, a
    missing identity on a per-reviewer family, a wrong digest shape, extra
    segments, uppercase identity, or a trailing newline all stay unknown-ID
    errors - pruned by neither planner sweep, accepted by neither this
    validator.
    """

    match = GENERATION_SCOPED_ID.fullmatch(operation_id)
    if match is None:
        return None
    family = match.group("family")
    requires_identity = vocabulary.get(family)
    if requires_identity is None:
        return None
    if (match.group("identity") is not None) != requires_identity:
        return None
    return family


# The persisted handoff kinds this schema accepts (algo#1216 r16 F6). Before
# this allowlist the validator accepted ARBITRARY kinds and operation IDs, so a
# mapped runner treated a fabricated terminal `bogus.qa.done` as valid. Each
# generation-scoped kind pins the family vocabulary its IDs must match; a kind
# absent from the union below is rejected outright.
HANDOFF_KIND_GENERATION_VOCABULARY = {
    "qa": QA_OPERATION_FAMILIES,
    "review_roundtrip": ROUNDTRIP_FAMILIES,
    "reviewer_request": REVIEWER_REQUEST_FAMILIES,
}
# pr_artifacts is allowed too, under PR_ARTIFACT_ID (head-bound) not a family
# vocabulary - the distinct artifact contract above.
ALLOWED_HANDOFF_KINDS = frozenset(
    (*HANDOFF_KIND_GENERATION_VOCABULARY, "pr_artifacts")
)
# "require applicable repository bindings" (F6): a qa handoff's operations act on
# a specific PR and the runner maps them by nameWithOwner, so a qa handoff that
# CARRIES operations must persist a non-empty repository_name_with_owner. The
# binding is applicable only where operations exist - an idle qa handoff
# (operations: []) keeps the template's null default. review_roundtrip persists
# the same binding for NEW ledgers (template + monitor-exit-handoffs Step 1)
# but stays OUT of this frozenset: its binding feeds the blocked-evidence
# recompute, which derives a pre-upgrade ledger's repo from
# monitor_cli.repository (algo#1216 r16 F1) - a hard requirement here would
# fail-closed every legacy ledger at resume instead of deriving. pr_artifacts
# is repo-agnostic.
# admin#1495 r13 F5: reviewer_request joined qa — an operation-bearing
# reviewer_request record without a binding slips the runner's cross-repo
# terminal compare (it skips absent bindings), so new records must
# persist it; the rejection message names the explicit legacy derivation.
HANDOFF_KINDS_REQUIRING_REPOSITORY = frozenset(("qa", "reviewer_request"))


def handoff_operation_id_valid(kind: str, operation_id: str) -> bool:
    """True when ``operation_id`` is well-formed for persisted handoff ``kind``.

    Generation-scoped kinds (qa/review_roundtrip/reviewer_request) require a
    ``family(:identity)?:g<12-hex>`` id whose family the kind mints, arity
    enforced. ``pr_artifacts`` requires a head-bound
    ``(ci-evidence|qa-rehearsal):<git-object-id>`` id. Callers gate on
    ALLOWED_HANDOFF_KINDS first, so an unlisted kind never reaches here; the
    closing ``False`` is a fail-closed backstop, not a reachable branch.
    """

    vocabulary = HANDOFF_KIND_GENERATION_VOCABULARY.get(kind)
    if vocabulary is not None:
        return parsed_generation_family(operation_id, vocabulary) is not None
    if kind == "pr_artifacts":
        return PR_ARTIFACT_ID.fullmatch(operation_id) is not None
    return False


# Canonical cross-helper constants — single source of truth. Consumers REBIND
# these names (handoff_decision.MAX_OPERATION_ATTEMPTS and
# model_policy.MAX_QUOTA_WAIT_SECONDS) instead of re-declaring literals;
# validate_package.py pins the exact rebind lines.
MAX_OPERATION_ATTEMPTS = 3
# Ceiling on ONE quota sleep (model_policy clamps wait_until to it; this
# validator bounds a persisted next_retry_at with it so a pre-fix or corrupted
# far-future resume point cannot re-open the unbounded wait through state).
MAX_QUOTA_WAIT_SECONDS = 3600
# Wall-clock skew tolerance (inclusive) for the time-dependent checks below.
# Time-dependent validation is deliberate: a future hold span-start or an
# over-ceiling retry instant is an error at write time, whatever the clock
# later says.
CLOCK_SKEW_TOLERANCE_SECONDS = 300
# Default bot-grace window — canonical here so the post_push_until resume
# ceiling and the monitor prose derive from one value; a per-project
# monitor_constants.bot_grace_window_seconds override (validated positive int
# within the sanity bound below) extends the ceiling with it, because the
# window the loop arms is the window the ceiling must honor.
BOT_GRACE_WINDOW_SECONDS = 900
# Overrides beyond one day are garbage, not configuration: an unbounded
# state-supplied window would both neuter the resume ceiling and overflow the
# timedelta arithmetic (the model_policy observed_at lesson) — a declared
# out-of-bound value is rejected as its own loud error (R5-F1; recovery: fix
# or remove the override), while the ceiling arithmetic still computes with
# the default so it stays provably safe with no dead exception handler.
MAX_GRACE_WINDOW_OVERRIDE_SECONDS = 86400
LAST_CHECK_ENUM = frozenset(("passing", "failing", "pending"))
LEDGER_STATUS_ENUM = frozenset(
    ("open", "fixed", "false_positive", "escalated", "auto_closed")
)
# Voices that may raise a ledger finding — the documented enum from
# references/state-and-safety.md, enforced so per-voice precision stays
# measurable (which reviewer found what, fixed vs false_positive).
LEDGER_REVIEWER_ENUM = frozenset(
    ("gstack_review", "octo_review", "code_reviewer", "adversarial", "escalation_voice")
)
TERMINAL_MONITOR = frozenset(("complete", "paused", "blocked"))
# Phase 6 session ownership (cheap orchestrator, pinned workers — see
# references/monitor-exit-handoffs.md). The block records which lineage owns
# the monitor session; when present, every core field is required (a
# versioned handoff fails closed on missing fields, never half-binds).
# pending_owner is the one optional field: a continuity binding carries the
# nominal owner it defers to the next session boundary, and MUST carry it.
# "codex" joined for admin#1495 r12 F1: the OpenAI entry's Phase 6
# controller continues on the recorded codex leg (orchestrator continuity
# only — pinned children stay on the nominal Claude owner via pending_owner).
MONITOR_OWNERSHIP_LINEAGE_ENUM = frozenset(("reviewer", "base", "codex"))
MONITOR_OWNERSHIP_REQUIRED_KEYS = frozenset(
    ("lineage", "model", "bound_at", "reason_code")
)
MONITOR_OWNERSHIP_KEYS = MONITOR_OWNERSHIP_REQUIRED_KEYS | frozenset(
    ("pending_owner",)
)
# Owner-pinned child execution (scripts/monitor_runner.py). The block is
# RUNNER-OWNED: the runner is the sole writer, a child candidate must carry
# it value-identical, and every field is required when the block is present
# (fail closed — a half-written control block is corruption, not progress).
# The immutable Phase 6 logical-work cap. Mirrors
# monitor_runner.MAX_WORK_ITERATIONS (the runner stays import-free of this
# module); validate_package's size/constant parity check compares the two
# assignment literals textually (admin#1495 r12 F8).
MAX_WORK_ITERATIONS = 50
# Mirrors monitor_runner.MAX_CANDIDATE_BYTES (language-boundary twin — the
# CLI must not import the runner); a canonical state past this is corruption.
STATE_READ_CEILING_BYTES = 8 * 1_048_576

MONITOR_CLI_SCHEMA_VERSION = 1
# "liveness" is OPTIONAL (migration tolerance for in-flight states written
# before it existed); so is "repository" (algo#1216 finding 3813491661:
# the runner's sticky origin binding for the required-handoff manifest,
# absent from pre-r18 states); every other key is required when the block
# exists.
MONITOR_CLI_OPTIONAL_KEYS = frozenset(("liveness", "repository"))
MONITOR_CLI_KEYS = frozenset(
    (
        "liveness",
        "repository",
        "schema_version",
        "child_session_id",
        "owner_model",
        "last_completed_attempt_id",
        "child_failures",
        "in_flight",
    )
)
MONITOR_CLI_IN_FLIGHT_KEYS = frozenset(
    (
        "attempt_id",
        "tick_ordinal",
        "started_at",
        "deadline_at",
        "child_pid",
        "child_pgid",
        "child_started_fingerprint",
        "base_workflow_digest",
    )
)
# r13 F8: the per-attempt containment mode is OPTIONAL (pre-upgrade states
# lack it). "cgroup:<path>" = the strict boundary; "degraded:<reason>" =
# the disclosed snapshot+group fallback on hosts without cgroup v2
# delegation — recorded so degradation is never silent.
MONITOR_CLI_IN_FLIGHT_OPTIONAL_KEYS = frozenset(("containment",))
MONITOR_CHILD_FAILURE_KEYS = frozenset(("signature", "at"))
# Same-signature child failures before the runner blocks (mirrors the
# workflow's 3-strike rule). The runner consumes it through model_policy's
# re-export — it never imports this module (structural rule).
MONITOR_CHILD_FAILURE_LIMIT = 3
# Phase 4b (merge readiness) value contracts — see references/merge-readiness.md.
AC_VERDICT_ENUM = frozenset(("pending", "met", "unmet", "deferred", "n_a"))
# A deferral's follow-up must be an immutable tracker reference: a
# TEAM-123-style ticket identifier or an issue/ticket URL (finding
# 3791925156 — arbitrary strings like "later" are not tracked follow-ups).
DEFERRAL_TICKET_PATTERN = re.compile(
    r"(\b[A-Z][A-Z0-9]+-[0-9]+\b|https?://\S+/(issues?|ticket|browse)/\S+)"
)
AC_ENTRY_KEYS = frozenset(("id", "text", "source", "verdict", "evidence"))
DEPLOY_ORDER_ENUM = frozenset(
    ("pending", "pass", "hazard_documented", "blocked", "n_a")
)
DEPENDENCIES_ENUM = frozenset(
    # hazard_documented (merged-but-not-live, ordering documented) and
    # unverified (external control plane / unreadable live state) are
    # completed-check outcomes, same as Check 1's — see merge-readiness.md.
    ("pending", "pass", "hazard_documented", "unverified", "blocked", "n_a")
)
HAZARD_DIRECTION_ENUM = frozenset(("additive", "destructive", "mixed"))
AC_CONFORMANCE_ENUM = frozenset(
    ("pending", "pass", "blocked", "n_a", "unavailable")
)
APPLIED_STATE_ENUM = frozenset(("applied", "pending", "unverified"))
MERGE_READINESS_KEYS = frozenset(
    (
        "deploy_order",
        "hazard_direction",
        "applied_state",
        "dependencies",
        "ac_conformance",
        "claims_audit",
        "backfill",
    )
)
# R2 #1328 finding 3767068795: merge-readiness Check 1 makes verified
# backfill completion a merge precondition when readers depend on populated
# rows, but the schema had no field to persist it — the deploy hold could
# release on schema-applied evidence while required rows stayed null.
# ``merge_readiness.backfill`` maps a backfill name to its requirement and
# verification state; a required backfill is hold-active until "complete",
# and completion requires evidence naming the verification.
BACKFILL_STATE_ENUM = frozenset(("pending", "complete", "n_a"))

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
        # admin#1495 finding 3813789199: the write-ahead stash record —
        # persisted BEFORE `git stash push` so a crash between the push
        # and the stash_ref persist leaves a durable nonce pointer for
        # resume to reconcile instead of stranding work in an unbound
        # stash.
        "stash_intent",
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
        "monitor_ownership",
        "monitor_cli",
        "post_push_until",
        "next_retry_at",
        "hold_started_at",
        "last_observed_head_sha",
        "clean_poll_timestamps",
        "attempt_log",
        "gstack_integration",
        "finding_ledger",
        "phases",
        "decision_audit_trail",
        "acceptance_criteria",
        "acceptance_criteria_capture",
        "merge_readiness",
    )
)

# Same migration rule as OPTIONAL_PHASE_NAMES: the Phase 4b blocks are known
# (so states carrying them validate) but never required (so pre-4b states stay
# valid).  New workflows initialize both at entry per project-and-entry.md.
# next_retry_at follows the same rule for pre-liveness states: it is the
# liveness-wait resume point (Timeout Heuristics), written only while a
# model-gate wait is pending, so most states legitimately omit it.
OPTIONAL_TOP_LEVEL_KEYS = frozenset(
    (
        "acceptance_criteria",
        "acceptance_criteria_capture",
        "merge_readiness",
        "monitor_ownership",
        "monitor_cli",
        "next_retry_at",
        "hold_started_at",
        # Migration tolerance (finding 3813789199): pre-r18 states never
        # wrote a stash intent; the write-ahead record is required by the
        # PROTOCOL when stashing, not by the tier.
        "stash_intent",
    )
)

MINIMAL_REQUIRED = ("state_schema_version", "workflow_id", "description", "current_phase")
TAKEOVER_REQUIRED = MINIMAL_REQUIRED + ("pr_number", "base_branch")
FULL_REQUIRED = tuple(sorted(KNOWN_TOP_LEVEL_KEYS - OPTIONAL_TOP_LEVEL_KEYS))

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
        # #3551 finding 3808151911: the probed credential-publication/push
        # paraphrases sailed past the seven families above. Four more
        # families cover that class: credential-disclosure verb+object,
        # credential-file exfiltration, push to an explicit foreign
        # URL/remote rewire, and force-push instructions. Still advisory —
        # surfaced, never obeyed — and still not the authorization
        # boundary; the architectural split (credential-free
        # interpretation, typed mutation executor) is tracked as the
        # standing host-contract thread on the same finding.
        r"(post|publish|paste|share|send|upload|copy|leak|expose|echo|print|dump|reveal)[^\n]{0,80}\b(token|secret|credential|password|api[ _-]?key|private[ _-]?key|ssh[ _-]?key)s?\b",
        r"(upload|send|post|copy|attach|exfiltrate)[^\n]{0,60}(id_rsa|\.pem\b|~/\.(ssh|aws|config)|\.env\b|keychain)",
        r"git\s+remote\s+(add|set-url)|push[^\n]{0,80}(https?://|git@)[^\s\n]{4,}",
        r"force[- ]?push|push\s+(-f\b|--force(?!-with-lease))",
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
            # YAML starts a comment only at line start or after whitespace.
            # Stripping a mid-scalar '#' would hide the remainder from THIS
            # parser while standard-YAML consumers still see it — a parser
            # differential that could smuggle content past the taint scan.
            # Keeping the '#' routes such scalars into the forbidden-character
            # rejection instead (fail closed, no differential).
            if not out or out[-1] in " \t":
                break
            out.append(ch)
            continue
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


def normalize_iso_timestamp(value: Any) -> datetime | None:
    """Parse a strict ISO-8601 timestamp, or None when the value fails.

    The single source of truth for shape, calendar validity, the
    timezone-required rule, AND the fractional-second normalization that makes
    the verdict identical on every interpreter (pre-3.11 fromisoformat only
    accepts 3- or 6-digit fractions; 3.11+ accepts any length).  Callers that
    order or compare timestamps (e.g. handoff_decision's eligibility gate)
    MUST use this instead of re-implementing the normalization, so both sides
    can never decide the same string differently.
    """

    if not isinstance(value, str) or not _ISO_TS.match(value):
        return None
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(
        r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), normalized, count=1
    )
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_iso_timestamp(value: Any) -> bool:
    """Shape AND calendar validity, timezone required (not just regex shape)."""
    return normalize_iso_timestamp(value) is not None


def _utcnow() -> datetime:
    """Validation clock seam — tests monkeypatch this to pin boundaries."""
    return datetime.now(timezone.utc)


def validate_operation_result_record(record: Any, *, label: str) -> tuple[str | None, list[str]]:
    """Canonical per-record operation-result contract (STRICT UNION).

    The single source of truth shared by this validator and
    ``handoff_decision``'s resume planner, so a state file can never validate
    clean and then be rejected on resume (or vice versa). The contract is the
    strict union of both sides' historic rules: every SUPPLIED timestamp must
    parse at every status (this module's rule), and the write-ahead lifecycle
    fields are required per status with ``verified_at >= started_at``, a
    non-empty mapping ``evidence`` on complete, a non-empty string ``error``
    on retryable/failed, no unknown fields, and ``retryable`` strictly below
    the attempt cap (the planner's rules). Message texts are the planner's
    historic phrasings, parameterized by ``label``.

    Returns ``(status, errors)`` — ``status`` is None when the record is too
    malformed to classify. Short-circuits at the first failed check per
    record, mirroring the planner's per-record precedence.
    """

    if not isinstance(record, dict):
        return None, [f"{label} must be an object"]
    unknown_keys = sorted(set(record) - OPERATION_RESULT_ALLOWED_KEYS)
    if unknown_keys:
        return None, [f"{label} has unknown field(s): " + ", ".join(str(k) for k in unknown_keys)]
    status = record.get("status")
    if not isinstance(status, str) or status not in OPERATION_STATUS_ENUM:
        return None, [
            f"{label}.status must be one of: complete, failed, pending,"
            " retryable, skipped_dependency"
        ]
    if status == "skipped_dependency":
        # A skipped record proves the OPPOSITE of an attempt: its declared
        # dependency terminally failed, so the planner never queued it
        # (R2 round-2 finding 3737466456 — before this status existed the
        # planner's terminal failed answer had no truthful persistable
        # form). Attempt-lifecycle fields on it would be fabricated
        # evidence, so they are forbidden rather than merely optional.
        skipped_attempts = record.get("attempts")
        if skipped_attempts != 0 or isinstance(skipped_attempts, bool):
            return status, [
                f"{label}.attempts must be 0 for skipped_dependency"
            ]
        skipped_error = record.get("error")
        if not isinstance(skipped_error, str) or not skipped_error.strip():
            return status, [
                f"{label} skipped_dependency requires a non-empty error"
                " naming the failed dependency"
            ]
        evidence_fields = sorted(
            key
            for key in (
                "started_at",
                "verified_at",
                "response_id",
                "evidence",
                # A precondition is observed at write-ahead time, i.e. by
                # an attempt — a never-queued record carrying one is
                # fabricated evidence (finding 3813491647).
                "precondition",
            )
            if record.get(key) is not None
        )
        if evidence_fields:
            return status, [
                f"{label} skipped_dependency forbids attempt evidence: "
                + ", ".join(evidence_fields)
            ]
        return status, []
    precondition = record.get("precondition")
    if precondition is not None and not isinstance(precondition, dict):
        return status, [
            f"{label}.precondition must be a mapping when present"
        ]
    attempts = record.get("attempts")
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 1  # a persisted record proves a started attempt
        or attempts > MAX_OPERATION_ATTEMPTS
    ):
        return status, [
            f"{label}.attempts must be between 1 and {MAX_OPERATION_ATTEMPTS}"
        ]
    raw_started = record.get("started_at")
    started_at = normalize_iso_timestamp(raw_started)
    if started_at is None:
        if raw_started is not None:
            return status, [f"{label}.started_at: must be an ISO 8601 timestamp"]
        return status, [f"{label} requires the write-ahead started_at timestamp"]
    raw_verified = record.get("verified_at")
    verified_at = normalize_iso_timestamp(raw_verified)
    if raw_verified is not None and verified_at is None:
        # Strict-union rule: a supplied-but-unparseable timestamp is invalid
        # at EVERY status, including pending.
        return status, [f"{label}.verified_at: must be an ISO 8601 timestamp"]
    if status in ("retryable", "complete", "failed") and verified_at is None:
        return status, [f"{label} {status} state requires verified_at"]
    if verified_at is not None and verified_at < started_at:
        return status, [f"{label}.verified_at cannot precede started_at"]
    if status in ("retryable", "failed") and (
        not isinstance(record.get("error"), str) or not record["error"]
    ):
        return status, [f"{label} {status} state requires error evidence"]
    if status == "complete" and (
        not isinstance(record.get("evidence"), dict) or not record["evidence"]
    ):
        return status, [f"{label} complete state requires verification evidence"]
    if status == "retryable" and attempts >= MAX_OPERATION_ATTEMPTS:
        return status, [f"{label} exhausted the three-attempt limit"]
    return status, []


MODEL_RUNTIME_LEGS = ("codex", "claude", "claude_reviewer")
MODEL_RUNTIME_LEG_KEYS = frozenset(
    (
        "model",
        "effort",
        "subagent_override",
        "effort_override",
        "host_agent_selection_verified",
        "gate_status",
        "policy_decision",
        "live_catalog_verified_at",
    )
)


def validate_model_runtime_shape(model_runtime: Any) -> list[str]:
    """Closed-shape validation of the persisted model-gate record.

    R2 round-2 finding 3737466436: this record was not shape-validated
    anywhere — unknown legs, unknown fields, and wrong types validated
    clean, leaving the binder's floor re-check as the only defense
    against hand-edited state. Shape per
    references/state-and-safety.md: the three legs plus the append-only
    escalation_invocations audit list. policy_decision stays free-form
    (it is the gate's own evidence record), and escalation entries may
    carry extra audit keys — required keys checked, growth allowed.
    """

    if model_runtime is None:
        return []
    if not isinstance(model_runtime, dict):
        return ["resolved_conventions.model_runtime: must be a mapping"]
    errors: list[str] = []
    allowed_top = set(MODEL_RUNTIME_LEGS) | {"escalation_invocations"}
    for key in sorted(set(model_runtime) - allowed_top):
        errors.append(
            f"resolved_conventions.model_runtime.{key}: unknown leg"
        )
    for leg_name in MODEL_RUNTIME_LEGS:
        leg = model_runtime.get(leg_name)
        if leg is None:
            continue
        prefix = f"resolved_conventions.model_runtime.{leg_name}"
        if not isinstance(leg, dict):
            errors.append(f"{prefix}: must be a mapping")
            continue
        for key in sorted(set(leg) - MODEL_RUNTIME_LEG_KEYS):
            errors.append(f"{prefix}.{key}: unknown field")
        model = leg.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            errors.append(f"{prefix}.model: must be a non-empty string")
        gate_status = leg.get("gate_status")
        if gate_status is not None and (
            not isinstance(gate_status, str) or not gate_status
        ):
            errors.append(
                f"{prefix}.gate_status: must be a non-empty string"
            )
        flag = leg.get("host_agent_selection_verified")
        if flag is not None and not isinstance(flag, bool):
            errors.append(
                f"{prefix}.host_agent_selection_verified: must be a boolean"
            )
        decision = leg.get("policy_decision")
        if decision is not None and not isinstance(decision, dict):
            errors.append(f"{prefix}.policy_decision: must be a mapping")
    invocations = model_runtime.get("escalation_invocations")
    if invocations is not None:
        if not isinstance(invocations, list):
            errors.append(
                "resolved_conventions.model_runtime.escalation_invocations:"
                " must be a list"
            )
        else:
            for position, entry in enumerate(invocations):
                if not isinstance(entry, dict) or not {
                    "trigger",
                    "voice",
                    "reason",
                } <= set(entry):
                    errors.append(
                        "resolved_conventions.model_runtime"
                        f".escalation_invocations[{position}]: must be a"
                        " mapping with trigger/voice/reason"
                    )
    return errors


# algo#1216 finding 3813491655: the ordered executor stops at the first
# terminal failure, so a persisted `failed` DESCENDANT (attempts >= 1 by
# the record contract) claims an attempt that cannot have run — accepting
# it let a terminal ledger conceal a misdirected external mutation behind
# a failed safety prerequisite. The one legitimate shape is a
# planner-rendered local automatic_failure (service "local": the planner
# knows the outcome without any remote call and callers persist it on
# round-trip), so those families are exempt BY NAME here. Kept in
# lockstep with handoff_decision's spec-side `automatic_failure`
# exemption by test_state_schema's planner-parity regression.
LOCAL_AUTOMATIC_FAILURE_FAMILIES = frozenset(
    {"qa.linear.record_unavailable", "qa.linear.record_state_unavailable"}
)


def validate_operation_collection(
    operations: list, result_statuses: dict, *, label: str
) -> list[str]:
    """Canonical collection contract shared with the resume planner.

    Two portable rules (the planner's scenario-specific checks stay with the
    planner): at most ONE in-flight (pending|retryable) result, and results
    form a PREFIX of the planned operations with at most one in-flight tail —
    the write-ahead protocol executes one operation at a time in order, so any
    other shape cannot have been produced by it.
    """

    errors: list[str] = []
    prefix = f"{label}: " if label else ""
    in_flight = {
        operation_id
        for operation_id, status in result_statuses.items()
        if status in ("pending", "retryable")
    }
    if len(in_flight) > 1:
        errors.append(f"{prefix}only one operation may be pending or retryable at a time")
    saw_unfinished = False
    for operation_id in operations:
        has_result = operation_id in result_statuses
        if saw_unfinished and has_result:
            errors.append(
                f"{prefix}operation results must form a prefix with at most one in-flight tail"
            )
            break
        if operation_id in in_flight or not has_result:
            saw_unfinished = True
    # algo#1216 finding 3792942228: dependency semantics existed only in the
    # planner — the schema and runner accepted a failed parent with a
    # completed dependent. The ordered-prefix protocol above already implies
    # the edge DIRECTION (each operation depends on its predecessors in the
    # planned order), so the portable check needs no persisted edge list: a
    # completed or pending result AFTER a failed/skipped predecessor in the
    # same plan cannot have been produced by the write-ahead executor, which
    # stops (or skips forward as skipped_dependency) at the first failure.
    blocking_parent: str | None = None
    for operation_id in operations:
        status = result_statuses.get(operation_id)
        # algo#1216 finding 3813491655: `failed` joins the rejected
        # descendant statuses — its record contract proves a started
        # attempt (attempts >= 1), which the stopped executor cannot have
        # made. Only the named local automatic-failure families above may
        # legitimately persist `failed` below a failed predecessor.
        impossible_descendant = status in (
            "complete",
            "pending",
            "retryable",
        ) or (
            status == "failed"
            and str(operation_id).split(":", 1)[0]
            not in LOCAL_AUTOMATIC_FAILURE_FAMILIES
        )
        if blocking_parent is not None and impossible_descendant:
            errors.append(
                f"{prefix}operation {operation_id!r} is"
                f" {status} after failed/skipped predecessor"
                f" {blocking_parent!r} — the ordered executor cannot"
                " produce this ledger"
            )
            break
        if status in ("failed", "skipped_dependency") and blocking_parent is None:
            blocking_parent = operation_id
    return errors


def _is_full_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_FULL_HEX.match(value))


def _check_test_path(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return "must be a non-empty string"
    if _CONTROL_CHARS.search(path_value):
        return "contains control characters"
    if "\\" in path_value:
        return "must use forward slashes only"
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
        # admin#1495 finding 3813789199: the write-ahead stash record.
        # Null when no stash is in flight; while pending it pins the nonce
        # (resume's reconciliation key into `git stash list`) and the
        # branch the work belongs to.
        intent = state.get("stash_intent")
        if "stash_intent" in state and intent is not None:
            if not isinstance(intent, dict):
                self.error("stash_intent: must be a mapping or null")
            else:
                for key in intent:
                    if key not in ("nonce", "original_branch", "status"):
                        self.error(
                            f"stash_intent: unknown key {_safe_key(str(key))!r}"
                        )
                nonce = intent.get("nonce")
                if not isinstance(nonce, str) or not nonce.strip():
                    self.error("stash_intent.nonce: must be a non-empty string")
                original_branch = intent.get("original_branch")
                if original_branch is not None and (
                    not isinstance(original_branch, str)
                    or not original_branch
                ):
                    self.error(
                        "stash_intent.original_branch: must be a non-empty"
                        " string or null"
                    )
                if intent.get("status") != "pending":
                    self.error(
                        "stash_intent.status: must be 'pending' — a bound or"
                        " abandoned intent is cleared to null in the same"
                        " write that records the outcome"
                    )
        # R5-F1: the per-project grace-window override is validated where it
        # is DECLARED, not only where it is consumed — a silently-applied
        # default would surface only as a confusing ceiling error at the
        # first push, whose re-arm advice re-reads the same bad override and
        # reproduces itself forever. Two keys are schema-consumed here
        # (this one and the immutable max_iterations below); the sibling
        # monitor_constants are prose-consumed and stay unvalidated by
        # design.
        conventions = state.get("resolved_conventions")
        constants = (
            conventions.get("monitor_constants")
            if isinstance(conventions, dict)
            else None
        )
        # admin#1495 r12 F8: max_iterations is IMMUTABLE. The 50-iteration
        # logical-work cap is enforced by the trusted runner
        # (MAX_WORK_ITERATIONS — parity-guarded by validate_package), and
        # it was never actually overridable: the template advertised a
        # knob the schema ignored and the runner overrode. Absent and null
        # mean the cap; the literal legacy 50 (states written from the
        # template) is tolerated; ANY other declaration is rejected rather
        # than silently ignored.
        declared_cap = (
            constants.get("max_iterations")
            if isinstance(constants, dict)
            else None
        )
        if declared_cap is not None and (
            not isinstance(declared_cap, int)
            or isinstance(declared_cap, bool)
            or declared_cap != MAX_WORK_ITERATIONS
        ):
            self.error(
                "resolved_conventions.monitor_constants.max_iterations:"
                f" immutable — the {MAX_WORK_ITERATIONS}-iteration work cap"
                " is not project-overridable; remove the key (absent, null,"
                f" and the literal {MAX_WORK_ITERATIONS} all mean the same"
                " enforced cap)"
            )
        declared = (
            constants.get("bot_grace_window_seconds")
            if isinstance(constants, dict)
            else None
        )
        declared_valid = (
            isinstance(declared, int)
            and not isinstance(declared, bool)
            and 0 < declared <= MAX_GRACE_WINDOW_OVERRIDE_SECONDS
        )
        # Explicit null is the package-wide "unset" idiom (every template key
        # initializes to null), not a declaration — it selects the default
        # exactly like an absent key and is deliberately not an error.
        if declared is not None and not declared_valid:
            self.error(
                "resolved_conventions.monitor_constants.bot_grace_window_seconds:"
                " override must be an integer in"
                f" (0, {MAX_GRACE_WINDOW_OVERRIDE_SECONDS}] — fix or remove the"
                f" declared override (the {BOT_GRACE_WINDOW_SECONDS}s default"
                " applies when absent)"
            )
        if "post_push_until" in state and state.get("post_push_until") is not None:
            parsed_push = normalize_iso_timestamp(state.get("post_push_until"))
            if parsed_push is None:
                self.error("post_push_until: must be an ISO 8601 timestamp with timezone")
            else:
                # Third deadline key, same resume-ceiling treatment as its
                # siblings: grace_elapsed(post_push_until) is a conjunct of
                # every monitor exit, and passive poll ticks never consume the
                # work cap — an unbounded far-future value would strand the
                # loop at in_progress forever. A VALID declared override
                # extends the ceiling; an invalid one already errored above,
                # and the arithmetic falls back to the default so it stays
                # overflow-safe.
                window = declared if declared_valid else BOT_GRACE_WINDOW_SECONDS
                ceiling = _utcnow() + timedelta(
                    seconds=window + CLOCK_SKEW_TOLERANCE_SECONDS
                )
                if parsed_push > ceiling:
                    self.error(
                        "post_push_until: exceeds the grace-window resume ceiling"
                        " (resolved window + skew) — re-arm post_push_until ="
                        " now + the resolved grace window on resume"
                    )
        wait_phases = state.get("phases")
        wait_monitor = wait_phases.get("monitor") if isinstance(wait_phases, dict) else None
        if "next_retry_at" in state and state.get("next_retry_at") is not None:
            parsed_retry = normalize_iso_timestamp(state.get("next_retry_at"))
            if parsed_retry is None:
                self.error("next_retry_at: must be an ISO 8601 timestamp with timezone")
            else:
                # Lifecycle: the wait clears before ready/blocked lands, so no
                # terminal monitor may still carry a pending model-gate wait.
                if isinstance(wait_monitor, str) and wait_monitor in TERMINAL_MONITOR:
                    self.error(
                        "next_retry_at: a terminal monitor forbids a pending model-gate wait"
                    )
                # R5-F2 wait-owner liveness: the model gate also runs before
                # the monitor exists (entry preflight, plan review), and the
                # documented lifecycle clears this key when the gate lands
                # ready/blocked — so outside a live owner the key is a stale
                # resume point that resume `continue` would sleep toward.
                # ENTRY phases are always live owners (no phases entry to
                # consult); the monitor's own case is the terminal-monitor
                # rule above; every other current_phase must be in_progress —
                # blocked, complete, aborted, and malformed owners all fail
                # closed. Recovery: resume `reset` clears the key; `continue`
                # must re-run the gate observation, never sleep toward it.
                if phase not in ENTRY_PHASES and phase != "monitor":
                    if phase.startswith("aborted_at_"):
                        self.error(
                            "next_retry_at: an aborted workflow cannot carry a"
                            " pending model-gate wait — resume reset clears it;"
                            " continue must re-run the gate observation instead"
                            " of sleeping toward it"
                        )
                    elif (
                        # CR 3760683996 (keeper-agents#1328):
                        # phases.runtime_verification is a MAPPING with a
                        # status field, not a bare string — read the status
                        # through it so a live runtime-verification owner is
                        # not misread as absent.
                        (lambda owner: owner.get("status") if isinstance(owner, dict) else owner)(
                            wait_phases.get(phase)
                            if isinstance(wait_phases, dict)
                            else None
                        )
                    ) != "in_progress":
                        self.error(
                            "next_retry_at: a pending model-gate wait needs a live"
                            f" owner — phases.{phase} must be in_progress (the wait"
                            " clears when the gate lands ready/blocked); resume"
                            " reset clears it, and continue must re-run the gate"
                            " observation instead of sleeping toward it"
                        )
                # A LANDED-blocked gate is stale everywhere, entry included:
                # the wait clears when the gate lands ready/blocked, so a
                # persisted blocked gate_status beside a live wait means the
                # clear never happened. Consulted only when the persisted
                # record exists and says "blocked" — absence or malformed
                # records add no error here (other checks own their shape).
                runtime = (
                    conventions.get("model_runtime")
                    if isinstance(conventions, dict)
                    else None
                )
                if isinstance(runtime, dict):
                    for leg_name in ("codex", "claude", "claude_reviewer"):
                        leg = runtime.get(leg_name)
                        if (
                            isinstance(leg, dict)
                            and leg.get("gate_status") == "blocked"
                        ):
                            self.error(
                                "next_retry_at: the"
                                f" {leg_name} gate landed blocked — a landed"
                                " gate clears its wait; resume reset clears"
                                " it, and continue must re-run the gate"
                                " observation instead of sleeping toward it"
                            )
                            break
                # Resume ceiling: a persisted retry instant beyond one bounded
                # sleep (+ skew) is a suspect resume point — re-derive by
                # re-running the gate observation, never sleep toward it.
                ceiling = _utcnow() + timedelta(
                    seconds=MAX_QUOTA_WAIT_SECONDS + CLOCK_SKEW_TOLERANCE_SECONDS
                )
                if parsed_retry > ceiling:
                    self.error(
                        "next_retry_at: exceeds the MAX_QUOTA_WAIT_SECONDS single-wait"
                        " ceiling (+ skew) — re-derive the wait, never sleep toward it"
                    )
        if "hold_started_at" in state and state.get("hold_started_at") is not None:
            parsed_hold = normalize_iso_timestamp(state.get("hold_started_at"))
            if parsed_hold is None:
                self.error("hold_started_at: must be an ISO 8601 timestamp with timezone")
            else:
                # A merge-readiness hold span exists only under a live monitor
                # (in_progress) or a hold-blocked exit (blocked); pending,
                # paused, complete, and phase-less tiers cannot carry one.
                if not (
                    isinstance(wait_monitor, str)
                    and wait_monitor in ("in_progress", "blocked")
                ):
                    self.error(
                        "hold_started_at: hold spans exist only under a live monitor"
                        " (in_progress or blocked)"
                    )
                # A future span-start would defer the BOT_GRACE_WINDOW hold
                # backstop indefinitely (tolerance inclusive).
                if parsed_hold > _utcnow() + timedelta(
                    seconds=CLOCK_SKEW_TOLERANCE_SECONDS
                ):
                    self.error("hold_started_at: must not be in the future")
        if "monitor_ownership" in state and state.get("monitor_ownership") is not None:
            ownership = state.get("monitor_ownership")
            if not isinstance(ownership, dict):
                self.error("monitor_ownership: must be a mapping")
            else:
                # A versioned handoff fails closed: when the block exists,
                # every field is required — a half-bound ownership record
                # cannot prove which lineage owns the session or when it was
                # bound, so nothing may act on it.
                for key in ownership:
                    if key not in MONITOR_OWNERSHIP_KEYS:
                        self.error(
                            f"monitor_ownership: unknown key {_safe_key(str(key))!r}"
                        )
                for key in sorted(MONITOR_OWNERSHIP_REQUIRED_KEYS - set(ownership)):
                    self.error(f"monitor_ownership: required key {key!r} is missing")
                pending_owner = ownership.get("pending_owner")
                if "pending_owner" in ownership and pending_owner is not None and (
                    not isinstance(pending_owner, str) or not pending_owner
                ):
                    self.error(
                        "monitor_ownership.pending_owner: must be a non-empty"
                        " string or null"
                    )
                if ownership.get("reason_code") == "orchestrator_continuity" and not (
                    isinstance(pending_owner, str) and pending_owner
                ):
                    self.error(
                        "monitor_ownership: a continuity binding must carry the"
                        " nominal owner in pending_owner"
                    )
                # R6-F12: the reverse direction — pending_owner is meaningful
                # ONLY on a continuity binding; a stray value elsewhere is
                # write-only metadata that can contradict the real owner.
                if (
                    isinstance(pending_owner, str)
                    and pending_owner
                    and ownership.get("reason_code") != "orchestrator_continuity"
                ):
                    self.error(
                        "monitor_ownership: pending_owner is only valid on an"
                        " orchestrator_continuity binding"
                    )
                lineage = ownership.get("lineage")
                if "lineage" in ownership and lineage not in MONITOR_OWNERSHIP_LINEAGE_ENUM:
                    self.error(
                        "monitor_ownership.lineage: must be one of"
                        " reviewer|base"
                    )
                for field in ("model", "reason_code"):
                    value = ownership.get(field)
                    if field in ownership and (
                        not isinstance(value, str) or not value
                    ):
                        self.error(
                            f"monitor_ownership.{field}: must be a non-empty string"
                        )
                if "bound_at" in ownership:
                    parsed_bound = normalize_iso_timestamp(ownership.get("bound_at"))
                    if parsed_bound is None:
                        self.error(
                            "monitor_ownership.bound_at: must be an ISO 8601"
                            " timestamp with timezone"
                        )
                    elif parsed_bound > _utcnow() + timedelta(
                        seconds=CLOCK_SKEW_TOLERANCE_SECONDS
                    ):
                        self.error("monitor_ownership.bound_at: must not be in the future")
        if "monitor_cli" in state and state.get("monitor_cli") is not None:
            cli = state.get("monitor_cli")
            if not isinstance(cli, dict):
                self.error("monitor_cli: must be a mapping")
            else:
                # Runner-owned control block: every field required when the
                # block exists (a half-written control block is corruption,
                # not progress), nullable only where the protocol says so.
                for key in cli:
                    if key not in MONITOR_CLI_KEYS:
                        self.error(f"monitor_cli: unknown key {_safe_key(str(key))!r}")
                for key in sorted(
                    MONITOR_CLI_KEYS - MONITOR_CLI_OPTIONAL_KEYS - set(cli)
                ):
                    self.error(f"monitor_cli: required key {key!r} is missing")
                # algo#1216 finding 3806594998 (+admin/mm twins): the
                # liveness ladder persists so a fresh slice resumes the
                # rung instead of restarting at 1.
                if "liveness" in cli and cli.get("liveness") is not None:
                    liveness = cli.get("liveness")
                    if not isinstance(liveness, dict):
                        self.error("monitor_cli.liveness: must be a mapping or null")
                    else:
                        for lkey in liveness:
                            if lkey not in ("rung", "next_retry_at"):
                                self.error(
                                    "monitor_cli.liveness: unknown key"
                                    f" {_safe_key(str(lkey))!r}"
                                )
                        rung = liveness.get("rung")
                        if not isinstance(rung, int) or isinstance(rung, bool) or rung < 1:
                            self.error(
                                "monitor_cli.liveness.rung: must be an integer >= 1"
                            )
                        nra = liveness.get("next_retry_at")
                        if nra is not None and normalize_iso_timestamp(nra) is None:
                            self.error(
                                "monitor_cli.liveness.next_retry_at: must be an"
                                " ISO 8601 timestamp or null"
                            )
                if "schema_version" in cli and cli.get("schema_version") != MONITOR_CLI_SCHEMA_VERSION:
                    self.error(
                        "monitor_cli.schema_version: must be"
                        f" {MONITOR_CLI_SCHEMA_VERSION}"
                    )
                for nullable in (
                    "child_session_id",
                    "last_completed_attempt_id",
                    "repository",
                ):
                    value = cli.get(nullable)
                    if nullable in cli and value is not None and (
                        not isinstance(value, str) or not value
                    ):
                        self.error(
                            f"monitor_cli.{nullable}: must be a non-empty string"
                            " or null"
                        )
                owner = cli.get("owner_model")
                if "owner_model" in cli and (
                    not isinstance(owner, str) or not owner
                ):
                    self.error("monitor_cli.owner_model: must be a non-empty string")
                failures = cli.get("child_failures")
                if "child_failures" in cli:
                    if not isinstance(failures, list):
                        self.error("monitor_cli.child_failures: must be a list")
                    else:
                        for index, record in enumerate(failures):
                            if not isinstance(record, dict) or set(record) != MONITOR_CHILD_FAILURE_KEYS:
                                self.error(
                                    f"monitor_cli.child_failures[{index}]: must be"
                                    " a {signature, at} record"
                                )
                                continue
                            if not isinstance(record.get("signature"), str) or not record.get("signature"):
                                self.error(
                                    f"monitor_cli.child_failures[{index}].signature:"
                                    " must be a non-empty string"
                                )
                            if normalize_iso_timestamp(record.get("at")) is None:
                                self.error(
                                    f"monitor_cli.child_failures[{index}].at: must"
                                    " be an ISO 8601 timestamp with timezone"
                                )
                in_flight = cli.get("in_flight")
                if "in_flight" in cli and in_flight is not None:
                    if not isinstance(in_flight, dict):
                        self.error("monitor_cli.in_flight: must be a mapping or null")
                    else:
                        for key in in_flight:
                            if (
                                key not in MONITOR_CLI_IN_FLIGHT_KEYS
                                and key
                                not in MONITOR_CLI_IN_FLIGHT_OPTIONAL_KEYS
                            ):
                                self.error(
                                    "monitor_cli.in_flight: unknown key"
                                    f" {_safe_key(str(key))!r}"
                                )
                        for key in sorted(MONITOR_CLI_IN_FLIGHT_KEYS - set(in_flight)):
                            self.error(
                                f"monitor_cli.in_flight: required key {key!r} is missing"
                            )
                        containment = in_flight.get("containment")
                        if "containment" in in_flight and (
                            not isinstance(containment, str)
                            or not (
                                containment.startswith("cgroup:")
                                or containment.startswith("degraded:")
                            )
                        ):
                            self.error(
                                "monitor_cli.in_flight.containment: must be"
                                " 'cgroup:<path>' or 'degraded:<reason>'"
                            )
                        for field in ("attempt_id", "child_started_fingerprint", "base_workflow_digest"):
                            value = in_flight.get(field)
                            if field in in_flight and (
                                not isinstance(value, str) or not value
                            ):
                                self.error(
                                    f"monitor_cli.in_flight.{field}: must be a"
                                    " non-empty string"
                                )
                        ordinal = in_flight.get("tick_ordinal")
                        if "tick_ordinal" in in_flight and (
                            not isinstance(ordinal, int)
                            or isinstance(ordinal, bool)
                            or ordinal < 1
                        ):
                            self.error(
                                "monitor_cli.in_flight.tick_ordinal: must be a"
                                " positive integer"
                            )
                        for pid_field in ("child_pid", "child_pgid"):
                            value = in_flight.get(pid_field)
                            if pid_field in in_flight and (
                                not isinstance(value, int)
                                or isinstance(value, bool)
                                or value <= 0
                            ):
                                self.error(
                                    f"monitor_cli.in_flight.{pid_field}: must be a"
                                    " positive integer"
                                )
                        pid_value = in_flight.get("child_pid")
                        pgid_value = in_flight.get("child_pgid")
                        if (
                            isinstance(pid_value, int)
                            and isinstance(pgid_value, int)
                            and not isinstance(pid_value, bool)
                            and not isinstance(pgid_value, bool)
                            and pid_value != pgid_value
                        ):
                            self.error(
                                "monitor_cli.in_flight: child_pid must equal"
                                " child_pgid — the runner spawns session"
                                " leaders (start_new_session), so an unequal"
                                " pair is a forged or corrupt record"
                            )
                        for ts_field in ("started_at", "deadline_at"):
                            if ts_field in in_flight and normalize_iso_timestamp(
                                in_flight.get(ts_field)
                            ) is None:
                                self.error(
                                    f"monitor_cli.in_flight.{ts_field}: must be an"
                                    " ISO 8601 timestamp with timezone"
                                )
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
        if "acceptance_criteria" in state:
            self.validate_acceptance_criteria(state.get("acceptance_criteria"))
        if "acceptance_criteria_capture" in state:
            self.validate_acceptance_criteria_capture(
                state.get("acceptance_criteria_capture"),
                state.get("acceptance_criteria"),
            )
        if "merge_readiness" in state:
            self.validate_merge_readiness(state.get("merge_readiness"))
        # (v) a complete merge-readiness phase cannot coexist with a blocked
        # check outcome or an unmet acceptance criterion — the workflow blocks
        # the phase in exactly those cases.  (The "unavailable"-with-waiver
        # nuance is CODE-enforced below (r13 F11 bound the typed waiver
        # to unavailable conformance; r14 F22 corrected this comment).)
        phases_for_gate = state.get("phases")
        if (
            isinstance(phases_for_gate, dict)
            and phases_for_gate.get("merge_readiness") == "complete"
        ):
            gate = state.get("merge_readiness")
            if not isinstance(gate, dict):
                self.error(
                    "invariant(v): phases.merge_readiness complete requires a "
                    "merge_readiness mapping with recorded check outcomes"
                )
            else:
                # Completion means every check RAN and landed non-blocked:
                # absent or still-pending outcomes are the empty-gate bypass.
                # ("unavailable" is schema-legal ONLY with the typed waiver —
                # bound below per r13 F11; the old claim that the schema
                # cannot read the waiver predates the capture block.)
                for check_key in ("deploy_order", "dependencies", "ac_conformance"):
                    check_value = gate.get(check_key)
                    if check_value in (None, "pending", "blocked"):
                        self.error(
                            "invariant(v): phases.merge_readiness complete requires "
                            f"a terminal non-blocked merge_readiness.{check_key}"
                        )
                audit = gate.get("claims_audit")
                if not isinstance(audit, dict) or not all(
                    isinstance(audit.get(count_key), int)
                    and not isinstance(audit.get(count_key), bool)
                    for count_key in ("audited", "rewritten")
                ):
                    self.error(
                        "invariant(v): phases.merge_readiness complete requires a "
                        "recorded claims_audit with audited/rewritten counts"
                    )
            criteria = state.get("acceptance_criteria")
            if "acceptance_criteria" not in state:
                # A complete gate proves AC Capture ran; only pre-4b states may
                # omit the key, and they cannot carry this phase at all.
                self.error(
                    "invariant(v): phases.merge_readiness complete requires "
                    "acceptance_criteria to be present"
                )
            # admin#1495 finding 3793025389: a complete gate without the
            # kickoff authorization snapshot accepted every repro — the
            # capture block is REQUIRED once the gate completes, and
            # "unavailable" criteria additionally need the typed waiver.
            capture = state.get("acceptance_criteria_capture")
            if not isinstance(capture, dict):
                self.error(
                    "invariant(v): phases.merge_readiness complete requires "
                    "the acceptance_criteria_capture kickoff snapshot"
                )
            elif criteria == "unavailable" and not (
                isinstance(capture.get("unavailable_waiver"), str)
                and capture.get("unavailable_waiver")
            ):
                self.error(
                    "invariant(v): unavailable acceptance criteria require "
                    "acceptance_criteria_capture.unavailable_waiver (the "
                    "typed user waiver)"
                )
            # r13 F11: the waiver binds to unavailable CONFORMANCE too — a
            # completed gate whose ac_conformance is "unavailable" needs
            # the typed waiver even when criteria were captured earlier
            # (the capture-then-outage case), and criteria that were never
            # captured cannot claim a passing conformance.
            conformance = (
                gate.get("ac_conformance") if isinstance(gate, dict) else None
            )
            if conformance == "unavailable" and not (
                isinstance(capture, dict)
                and isinstance(capture.get("unavailable_waiver"), str)
                and capture.get("unavailable_waiver")
            ):
                self.error(
                    "invariant(v): completed merge_readiness with "
                    "ac_conformance 'unavailable' requires "
                    "acceptance_criteria_capture.unavailable_waiver (the "
                    "typed user waiver covers the conformance outage)"
                )
            if criteria == "unavailable" and conformance == "pass":
                self.error(
                    "invariant(v): ac_conformance 'pass' is incoherent with "
                    "unavailable acceptance criteria — conformance cannot "
                    "pass against criteria that were never captured"
                )
            if isinstance(criteria, list) and any(
                isinstance(entry, dict)
                and entry.get("verdict") in ("unmet", "pending")
                for entry in criteria
            ):
                self.error(
                    "invariant(v): phases.merge_readiness complete forbids "
                    "pending or unmet acceptance criteria"
                )
        # A pre-Phase-1 tier claiming later-phase progress is contradictory:
        # every transition writes current_phase together with the status, so
        # entry/takeover states carry only pending phases (when initialized).
        if tier_name != "full" and isinstance(state.get("phases"), dict):
            for phase_name, phase_value in state["phases"].items():
                phase_status = (
                    phase_value.get("status")
                    if isinstance(phase_value, dict)
                    else phase_value
                )
                if phase_status not in (None, "pending"):
                    self.error(
                        f"invariant(i): tier {tier_name} forbids non-pending "
                        f"phases.{_safe_key(str(phase_name))}"
                    )
                    break
        # human_roundtrip's mapping check lives in validate_human_roundtrip;
        # listing it here would duplicate the diagnostic.
        for structured_key in (
            "resolved_conventions",
            "validated_ticket",
        ):
            if structured_key in state and not isinstance(state.get(structured_key), dict):
                self.error(f"{structured_key}: must be a mapping")
        ticket = state.get("validated_ticket")
        if isinstance(ticket, dict):
            # R2 round-2 finding 3737466471, the enforceable kernel: the
            # handoff planner deliberately trusts pre-validated ticket
            # state (it is a no-network pure function), so the validation
            # evidence must be coherent HERE. All-null is a legitimate
            # entry state; a provider_id without the rest of the evidence
            # is the tamper shape the planner would then act on.
            for field in ("tracker_type", "identifier", "provider_id", "source_fingerprint"):
                value = ticket.get(field)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    self.error(
                        f"validated_ticket.{field}: must be a non-empty string when set"
                    )
            validated_at = ticket.get("validated_at")
            if validated_at is not None and normalize_iso_timestamp(validated_at) is None:
                self.error(
                    "validated_ticket.validated_at: must be an ISO 8601 timestamp"
                )
            if ticket.get("provider_id") is not None:
                # Required set = the fields the documented persistence
                # procedures actually write (identifier + provider_id +
                # validation timestamp — phases-1-5.md / project-and-entry
                # ticket validation). tracker_type and source_fingerprint
                # are type-checked when present but not required here: the
                # fingerprint hashes PR title/body linkage, which cannot
                # exist before Phase 5 creates the PR, so requiring it
                # would fail-close every issue-first workflow at entry
                # (series self-review finding).
                missing = sorted(
                    field
                    for field in ("identifier", "validated_at")
                    if ticket.get(field) is None
                )
                if missing:
                    self.error(
                        "validated_ticket: provider_id requires the"
                        " documented validation evidence; missing: "
                        + ", ".join(missing)
                    )
        conventions = state.get("resolved_conventions")
        if isinstance(conventions, dict):
            self.validate_conventions(conventions)
        if "decision_audit_trail" in state:
            trail = self.state.get("decision_audit_trail")
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
            if name not in PHASE_NAMES and name not in OPTIONAL_PHASE_NAMES:
                self.error(f"phases: unknown key {_safe_key(str(name))!r}")

        def status_of(name: str) -> str | None:
            value = phases.get(name)
            if name == "runtime_verification":
                if not isinstance(value, dict):
                    self.error("phases.runtime_verification: must be a mapping with a status")
                    return None
                # algo#1216 r17 F6: the mapping is a closed shape — an
                # unknown key on a record Phase 5 trusts is exactly the
                # laundering surface the proof fields close.
                for key in value:
                    if key not in RUNTIME_VERIFICATION_KEYS:
                        self.error(
                            "phases.runtime_verification: unknown key "
                            f"{_safe_key(str(key))!r}"
                        )
                status = value.get("status")
                if not self.check_enum(status, RUNTIME_VERIFICATION_ENUM, "phases.runtime_verification.status"):
                    return None
                if status == "waived" and not value.get("reason"):
                    self.error(
                        "phases.runtime_verification: waived requires a non-empty reason"
                    )
                if status == "complete":
                    # algo#1216 r17 F6: a terminal record is trusted by
                    # Phase 5, so BEFORE Phase 5 has passed (phases.pr
                    # pending/in_progress/absent) `complete` must carry
                    # its proof: exact-head SHA, the touched-diff
                    # fingerprint, ordered timestamps, and nonempty
                    # evidence. A proofless complete under a COMPLETED pr
                    # phase is tolerated as pre-upgrade history — Phase 5
                    # already consumed it, and resetting it would break
                    # the successful-predecessor chain retroactively.
                    pr_status = phases.get("pr")
                    pre_phase5 = pr_status in (None, "pending", "in_progress")
                    if pre_phase5:
                        migration = (
                            " — legacy-v1 migration: set status to"
                            ' "in_progress" (and phases.pr back to'
                            ' "pending" if it was "in_progress"),'
                            " re-verify at the current head, fill the"
                            " proof fields, then restore complete"
                        )
                        if not _is_full_hex(value.get("target_head_sha")):
                            self.error(
                                "phases.runtime_verification: complete"
                                " requires a full-length hex"
                                " target_head_sha" + migration
                            )
                        fingerprint = value.get("touched_diff_fingerprint")
                        if not (
                            isinstance(fingerprint, str)
                            and len(fingerprint) == 64
                            and all(
                                c in "0123456789abcdefABCDEF"
                                for c in fingerprint
                            )
                        ):
                            self.error(
                                "phases.runtime_verification: complete"
                                " requires a 64-hex"
                                " touched_diff_fingerprint" + migration
                            )
                        started = normalize_iso_timestamp(
                            value.get("started_at")
                        )
                        verified = normalize_iso_timestamp(
                            value.get("verified_at")
                        )
                        if started is None or verified is None:
                            self.error(
                                "phases.runtime_verification: complete"
                                " requires ISO started_at and verified_at"
                                + migration
                            )
                        elif verified < started:
                            self.error(
                                "phases.runtime_verification: verified_at"
                                " must not precede started_at" + migration
                            )
                        evidence = value.get("evidence")
                        if not (isinstance(evidence, dict) and evidence):
                            self.error(
                                "phases.runtime_verification: complete"
                                " requires nonempty evidence" + migration
                            )
                return status
            enum = MONITOR_ENUM if name == "monitor" else SIMPLE_PHASE_ENUM
            if not self.check_enum(value, enum, f"phases.{name}"):
                return None
            return value

        all_phase_names = PHASE_NAMES + OPTIONAL_PHASE_NAMES
        statuses = {name: status_of(name) for name in all_phase_names if name in phases}

        # (i) current_phase / phase-status agreement
        if current_phase in all_phase_names:
            status = statuses.get(current_phase)
            if current_phase in OPTIONAL_PHASE_NAMES and current_phase not in phases:
                self.error(
                    f"invariant(i): current_phase {current_phase!r} requires a "
                    f"phases.{current_phase} entry"
                )
            if status == "pending":
                self.error(
                    f"invariant(i): current_phase {current_phase!r} disagrees with a pending phase status"
                )
        elif current_phase.startswith("aborted_at_"):
            aborted = current_phase.removeprefix("aborted_at_")
            if aborted in all_phase_names and statuses.get(aborted) != "blocked":
                self.error(
                    f"invariant(i): {current_phase!r} requires phases.{aborted} to be blocked"
                )

        # (ii) successful-predecessor chain.  merge_readiness carries only its
        # FORWARD edge (it needs a complete self_review); no later phase lists
        # it as a predecessor, because a pre-4b state legitimately has
        # runtime_verification/pr non-pending with no merge_readiness key at
        # all, and the documented Phase 5 recovery route ("go run Phase 4b
        # now") legitimately holds pr in_progress beside a pending gate.  The
        # gate itself is enforced by the workflow, not by schema shape: the
        # Phase 5 precondition (phases-1-5.md) and the resume router's
        # monitor bullet (project-and-entry.md), which sends a pre-4b
        # current_phase: monitor resume through Phase 4b before Phase 6.
        chain = (
            ("plan_review", "plan", ("complete",)),
            ("implementation", "plan_review", ("complete",)),
            ("self_review", "implementation", ("complete",)),
            ("merge_readiness", "self_review", ("complete",)),
            ("runtime_verification", "self_review", ("complete",)),
            ("pr", "runtime_verification", ("complete", "waived")),
            ("monitor", "pr", ("complete",)),
        )
        # A run that KNOWS about the gate (the optional phase key is present)
        # cannot finish around it: a completed pr phase or any non-pending
        # monitor state beside a still-pending/in-progress gate is the bypass.
        # pr in_progress/blocked stays legal — that is the documented Phase 5
        # "go run Phase 4b now" recovery route — and key ABSENCE stays legal
        # for pre-4b states.
        gate_status = statuses.get("merge_readiness")
        monitor_state = statuses.get("monitor")
        if gate_status in ("pending", "in_progress"):
            # algo#1216 finding 3806595010: the documented LEGACY re-entry
            # (a pre-4b state with completed pr / active monitor running
            # Phase 4b now) must be able to write in_progress without
            # invalidating its own state. The explicit migration marker in
            # the Decision Audit Trail records that transition; without it,
            # the combination stays the bypass this invariant rejects.
            trail = self.state.get("decision_audit_trail")
            legacy_reentry = isinstance(trail, list) and any(
                isinstance(entry, str)
                and entry.startswith("legacy-4b-reentry:")
                for entry in trail
            )
            if not legacy_reentry and (
                statuses.get("pr") == "complete"
                or (monitor_state and monitor_state != "pending")
            ):
                self.error(
                    "invariant(ii): pr completion or monitor progress requires the "
                    "present phases.merge_readiness gate to be terminal (a "
                    "documented legacy re-entry records legacy-4b-reentry:<ts> "
                    "in the Decision Audit Trail before writing in_progress)"
                )
        elif gate_status == "blocked" and monitor_state in ("paused", "complete"):
            # A blocked gate may legally coexist with a blocked monitor (the
            # monitor-loop world-state refresh blocks exactly as Phase 4b
            # would) and with a completed pr (the refresh runs post-creation),
            # but never with a CLEAN exit: paused/complete assert the world
            # is safe, which a blocked gate denies.
            self.error(
                "invariant(ii): a clean monitor exit (paused/complete) requires a "
                "non-blocked phases.merge_readiness gate"
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
            # The classifier is deterministic in BOTH directions: Scope
            # Analysis maps change_type bug_fix to runtime_bug_fix, so a
            # bug_fix carrying mode "none" would skip the red/green gate.
            if change_type == "bug_fix" and mode != "runtime_bug_fix":
                self.error(
                    "invariant(iv): change_type bug_fix requires defect_evidence_mode runtime_bug_fix"
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
            if kind not in ALLOWED_HANDOFF_KINDS:
                self.error(
                    f"handoffs.{safe_kind}: unknown handoff kind; allowed kinds are "
                    + ", ".join(sorted(ALLOWED_HANDOFF_KINDS))
                )
                continue
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
            malformed = [op for op in operations if not handoff_operation_id_valid(kind, op)]
            if malformed:
                self.error(
                    f"handoffs.{safe_kind}.operations: malformed operation ID(s) for kind "
                    f"{safe_kind}: " + ", ".join(_safe_key(op) for op in malformed)
                )
                continue
            if kind in HANDOFF_KINDS_REQUIRING_REPOSITORY and operations:
                binding = handoff.get("repository_name_with_owner")
                if not isinstance(binding, str) or not binding:
                    self.error(
                        f"handoffs.{safe_kind}.repository_name_with_owner: required "
                        "(non-empty) when the handoff carries operations — a"
                        " pre-upgrade record derives it from"
                        " monitor_cli.repository (the runner's"
                        " live-origin-agreed binding): persist that value"
                        " and resume"
                    )
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
                op_path = f"handoffs.{safe_kind}.operation_results.{safe_op}"
                op_status, record_errors = validate_operation_result_record(
                    record, label=op_path
                )
                for message in record_errors:
                    self.error(message)
                if op_status is None:
                    continue
                result_statuses[op_id] = op_status

            for message in validate_operation_collection(
                operations, result_statuses, label=f"handoffs.{safe_kind}"
            ):
                self.error(message)

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
            self.check_enum(
                entry.get("reviewer"),
                LEDGER_REVIEWER_ENUM,
                f"finding_ledger.entries[{position}].reviewer",
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
        for message in validate_model_runtime_shape(
            conventions.get("model_runtime")
        ):
            self.error(message)
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
        # algo#1216 r16 F14: dev-server commands are the same executable-
        # cache class as quality_check_steps and validate the same way -
        # argv vectors, never shell strings. A legacy plain string still
        # validates (pre-upgrade states must resume) but is BY CONTRACT a
        # cache miss: it can never exact-argv compare, so the runtime
        # re-resolves from repository sources and executes only the
        # re-resolved form (state-and-safety rule 4).
        for dev_key in ("dev_server_frontend", "dev_server_backend"):
            value = conventions.get(dev_key)
            if value is None or (isinstance(value, str) and value):
                continue
            if (
                not isinstance(value, list)
                or not value
                or any(
                    not isinstance(part, str) or not part for part in value
                )
            ):
                self.error(
                    f"resolved_conventions.{dev_key}: must be a non-empty"
                    " argv list of strings (null allowed; a legacy plain"
                    " string is tolerated as an always-cache-miss value)"
                )
        # admin#1495 r13 F6: mandatory_kinds is the closed ui|api|performance
        # set and each mandated kind needs its repository-rule evidence -
        # an arbitrary value silently disabled required runtime
        # verification (nothing downstream matched it), and an unsourced
        # mandate is unauditable.
        policy = conventions.get("runtime_verification_policy")
        if policy is not None and not isinstance(policy, dict):
            self.error(
                "resolved_conventions.runtime_verification_policy: must be"
                " a mapping"
            )
        elif isinstance(policy, dict):
            kinds = policy.get("mandatory_kinds")
            declared_kinds: list[str] = []
            if kinds is not None:
                if not isinstance(kinds, list):
                    self.error(
                        "resolved_conventions.runtime_verification_policy"
                        ".mandatory_kinds: must be a list"
                    )
                else:
                    for kind in kinds:
                        if (
                            not isinstance(kind, str)
                            or kind not in RUNTIME_VERIFICATION_KINDS
                        ):
                            self.error(
                                "resolved_conventions"
                                ".runtime_verification_policy"
                                ".mandatory_kinds: entries must be one of"
                                " ui, api, performance"
                            )
                        else:
                            declared_kinds.append(kind)
                    if len(set(declared_kinds)) != len(declared_kinds):
                        self.error(
                            "resolved_conventions"
                            ".runtime_verification_policy.mandatory_kinds:"
                            " entries must be unique"
                        )
            policy_evidence = policy.get("evidence")
            if policy_evidence is not None and not isinstance(
                policy_evidence, dict
            ):
                self.error(
                    "resolved_conventions.runtime_verification_policy"
                    ".evidence: must be a mapping"
                )
            evidence_map = (
                policy_evidence if isinstance(policy_evidence, dict) else {}
            )
            for key in evidence_map:
                if key not in RUNTIME_VERIFICATION_KINDS:
                    self.error(
                        "resolved_conventions.runtime_verification_policy"
                        f".evidence: unknown kind {_safe_key(str(key))!r}"
                    )
            for kind in sorted(set(declared_kinds)):
                value = evidence_map.get(kind)
                if not isinstance(value, str) or not value:
                    self.error(
                        "resolved_conventions.runtime_verification_policy"
                        f".evidence[{kind}]: a mandated kind requires its"
                        " exact repository rule/source"
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

    def validate_acceptance_criteria(self, value: Any) -> None:
        if value == "unavailable":
            return
        if not isinstance(value, list):
            self.error(
                'acceptance_criteria: must be a list or the string "unavailable"'
            )
            return
        for index, entry in enumerate(value):
            path = f"acceptance_criteria[{index}]"
            if not isinstance(entry, dict):
                self.error(f"{path}: must be a mapping")
                continue
            for key in entry:
                if key not in AC_ENTRY_KEYS:
                    self.error(f"{path}: unknown key {_safe_key(str(key))!r}")
            for key in ("id", "text", "source"):
                self.require_string(entry, key, path)
            self.check_enum(entry.get("verdict"), AC_VERDICT_ENUM, f"{path}.verdict")
            evidence = entry.get("evidence")
            if evidence is not None and (not isinstance(evidence, str) or not evidence):
                self.error(f"{path}.evidence: must be a non-empty string or null")
            # "explicitly deferred with a tracked ticket" (SKILL.md item 11,
            # merge-readiness.md) — a deferred AC with no evidence names no
            # follow-up, so the deferral is untracked (algo#1216 R2 finding
            # 3722493004: deferred + null evidence validated clean).
            if entry.get("verdict") == "deferred":
                # admin#1495 R2 finding 3791925156: "with a tracked ticket"
                # means a TICKET, not any prose — "later" validated clean.
                # Require an immutable tracker reference in the evidence: a
                # ticket identifier (TEAM-123 style) or a tracker/issue URL.
                if evidence is None or DEFERRAL_TICKET_PATTERN.search(
                    evidence
                ) is None:
                    self.error(
                        f"{path}.evidence: verdict 'deferred' requires a"
                        " tracked ticket reference (an identifier like"
                        " WEB-1234 or a tracker/issue URL) in evidence —"
                        " free-form prose does not track a follow-up"
                    )


    def validate_acceptance_criteria_capture(
        self, value: Any, criteria_for_digest: Any = None
    ) -> None:
        """admin#1495 R2 finding 3791925150: the kickoff authorization
        snapshot. Captured ACs come from MUTABLE ticket prose; without a
        recorded source revision and capture digest, a post-kickoff ticket
        edit silently expands what the run believes it was asked to do.
        Check 3's re-fetch compares against this snapshot and treats drift
        as a re-authorization gate (merge-readiness.md), so the block must
        be tamper-evident: fixed keys, ISO capture time, and a hex digest
        of the normalized captured list."""

        if not isinstance(value, dict):
            self.error("acceptance_criteria_capture: must be a mapping")
            return
        for key in value:
            if key not in (
                "captured_at",
                "requester",
                "source_revision",
                "digest",
                "unavailable_waiver",
            ):
                self.error(
                    "acceptance_criteria_capture: unknown key"
                    f" {_safe_key(str(key))!r}"
                )
        captured_at = value.get("captured_at")
        if normalize_iso_timestamp(captured_at) is None:
            self.error(
                "acceptance_criteria_capture.captured_at: must be an ISO 8601"
                " timestamp"
            )
        self.require_string(value, "requester", "acceptance_criteria_capture")
        source_revision = value.get("source_revision")
        if source_revision is not None and (
            not isinstance(source_revision, str) or not source_revision
        ):
            self.error(
                "acceptance_criteria_capture.source_revision: must be a"
                " non-empty string or null (null = entry-context capture"
                " with no ticket revision to bind)"
            )
        digest = value.get("digest")
        if not isinstance(digest, str) or re.fullmatch(
            r"[0-9a-f]{12,64}", digest
        ) is None:
            self.error(
                "acceptance_criteria_capture.digest: must be 12-64 lowercase"
                " hex (sha256 of the normalized captured AC list)"
            )
        elif isinstance(criteria_for_digest, list):
            # admin#1495 finding 3793025389: shape-checking the digest let an
            # arbitrary fixed value survive a criteria edit. Recompute it
            # from the normalized IMMUTABLE captured fields (id/text/source
            # — verdicts and evidence legitimately change later) and require
            # a prefix match at the recorded length.
            normalized = sorted(
                (
                    {
                        "id": entry.get("id"),
                        "text": entry.get("text"),
                        "source": entry.get("source"),
                    }
                    for entry in criteria_for_digest
                    if isinstance(entry, dict)
                ),
                key=lambda item: str(item.get("id")),
            )
            recomputed = hashlib.sha256(
                json.dumps(
                    normalized, sort_keys=True, separators=(",", ":"), default=str
                ).encode("utf-8")
            ).hexdigest()
            if recomputed[: len(digest)] != digest:
                self.error(
                    "acceptance_criteria_capture.digest: does not match the"
                    " digest recomputed from acceptance_criteria (id/text/"
                    "source) — the captured list drifted or the digest was"
                    " fabricated; re-authorize the capture"
                )
        waiver = value.get("unavailable_waiver")
        if waiver is not None and (not isinstance(waiver, str) or not waiver):
            self.error(
                "acceptance_criteria_capture.unavailable_waiver: must be a"
                " non-empty string when present"
            )

    def validate_merge_readiness(self, value: Any) -> None:
        if not isinstance(value, dict):
            self.error("merge_readiness: must be a mapping")
            return
        for key in value:
            if key not in MERGE_READINESS_KEYS:
                self.error(f"merge_readiness: unknown key {_safe_key(str(key))!r}")
        if "deploy_order" in value:
            self.check_enum(
                value.get("deploy_order"), DEPLOY_ORDER_ENUM, "merge_readiness.deploy_order"
            )
        # Direction gates the direction-aware holds: a documented hazard with a
        # missing/null direction would silently default those holds wrong, so
        # it is a validation error (forcing Check 1 reclassification on resume).
        direction = value.get("hazard_direction")
        if value.get("deploy_order") == "hazard_documented":
            if direction is None:
                self.error(
                    "merge_readiness.hazard_direction: required (non-null) when "
                    "deploy_order is 'hazard_documented' — re-run Check 1 step 3"
                )
            else:
                self.check_enum(
                    direction, HAZARD_DIRECTION_ENUM, "merge_readiness.hazard_direction"
                )
                # mm#3551 finding 3806719714: an additive/mixed documented
                # hazard with EMPTY applied_state validated clean and
                # derived no hold — the per-environment recording contract
                # requires at least one recorded environment.
                if direction in ("additive", "mixed"):
                    applied_record = value.get("applied_state")
                    if not isinstance(applied_record, dict) or not applied_record:
                        self.error(
                            "merge_readiness.applied_state: an additive/mixed"
                            " hazard_documented deploy order requires per-"
                            "environment applied-state records (empty means"
                            " the recheck never ran)"
                        )
        elif direction is not None:
            self.check_enum(
                direction, HAZARD_DIRECTION_ENUM, "merge_readiness.hazard_direction"
            )
        if "dependencies" in value:
            self.check_enum(
                value.get("dependencies"), DEPENDENCIES_ENUM, "merge_readiness.dependencies"
            )
        if "ac_conformance" in value:
            self.check_enum(
                value.get("ac_conformance"),
                AC_CONFORMANCE_ENUM,
                "merge_readiness.ac_conformance",
            )
        if "applied_state" in value:
            applied = value.get("applied_state")
            if not isinstance(applied, dict):
                self.error("merge_readiness.applied_state: must be a mapping")
            else:
                for env, migrations in applied.items():
                    env_path = f"merge_readiness.applied_state.{_safe_key(str(env))}"
                    # algo#1216 finding 3807740761: an EMPTY nested map
                    # ({prod: {}}) satisfied the outer non-empty rule while
                    # recording nothing — each environment entry must carry
                    # at least one migration status.
                    if isinstance(migrations, dict) and not migrations:
                        self.error(
                            f"{env_path}: must record at least one migration"
                            " status (an empty environment map means the"
                            " recheck never ran for it)"
                        )
                    if not isinstance(migrations, dict):
                        self.error(f"{env_path}: must be a mapping")
                        continue
                    for migration, status in migrations.items():
                        entry_path = f"{env_path}.{_safe_key(str(migration))}"
                        # admin#1495 finding 3813789228: a mixed change is
                        # additive migrations (pre-deploy, hold until
                        # applied) PLUS destructive ones (post-deploy —
                        # holding them forces running destructive DDL
                        # under the old code). One undifferentiated status
                        # cannot say which side a migration is, so each
                        # entry may carry the per-migration form
                        # {direction, status}; under a mixed hazard it
                        # MUST.
                        if isinstance(status, dict):
                            for key in status:
                                if key not in ("direction", "status"):
                                    self.error(
                                        f"{entry_path}: unknown key"
                                        f" {_safe_key(str(key))!r}"
                                    )
                            entry_direction = status.get("direction")
                            if entry_direction == "mixed":
                                self.error(
                                    f"{entry_path}.direction: a single"
                                    " migration cannot be 'mixed' — it has"
                                    " no compatible midpoint; split it into"
                                    " an additive and a destructive step"
                                )
                            else:
                                self.check_enum(
                                    entry_direction,
                                    frozenset(("additive", "destructive")),
                                    f"{entry_path}.direction",
                                )
                                if (
                                    direction in ("additive", "destructive")
                                    and entry_direction is not None
                                    and entry_direction != direction
                                ):
                                    self.error(
                                        f"{entry_path}.direction: conflicts"
                                        " with merge_readiness"
                                        f".hazard_direction {direction!r}"
                                    )
                            self.check_enum(
                                status.get("status"),
                                APPLIED_STATE_ENUM,
                                f"{entry_path}.status",
                            )
                        else:
                            if direction == "mixed":
                                self.error(
                                    f"{entry_path}: a mixed hazard requires"
                                    " the per-migration form {direction,"
                                    " status} — an undifferentiated scalar"
                                    " cannot say which side this migration"
                                    " is on"
                                )
                            self.check_enum(
                                status,
                                APPLIED_STATE_ENUM,
                                entry_path,
                            )
        if "backfill" in value:
            backfill = value.get("backfill")
            if not isinstance(backfill, dict):
                self.error("merge_readiness.backfill: must be a mapping")
            else:
                for name, record in backfill.items():
                    bf_path = f"merge_readiness.backfill.{_safe_key(str(name))}"
                    if not isinstance(record, dict):
                        self.error(f"{bf_path}: must be a mapping")
                        continue
                    for key in record:
                        if key not in ("required", "state", "evidence"):
                            self.error(
                                f"{bf_path}: unknown key {_safe_key(str(key))!r}"
                            )
                    required = record.get("required")
                    if not isinstance(required, bool):
                        self.error(f"{bf_path}.required: must be a boolean")
                    self.check_enum(
                        record.get("state"), BACKFILL_STATE_ENUM, f"{bf_path}.state"
                    )
                    evidence = record.get("evidence")
                    if evidence is not None and (
                        not isinstance(evidence, str) or not evidence
                    ):
                        self.error(
                            f"{bf_path}.evidence: must be a non-empty string or null"
                        )
                    if (
                        required is True
                        and record.get("state") == "complete"
                        and evidence is None
                    ):
                        self.error(
                            f"{bf_path}.evidence: a required backfill marked"
                            " complete must name its verification evidence"
                        )
                    # algo#1216 finding 3788363458: required + n_a is a
                    # contradiction that silently released the deploy hold.
                    if required is True and record.get("state") == "n_a":
                        self.error(
                            f"{bf_path}.state: a REQUIRED backfill cannot be"
                            " n_a — mark it pending until verified complete,"
                            " or set required: false with the rationale"
                        )
        if "claims_audit" in value:
            audit = value.get("claims_audit")
            if not isinstance(audit, dict):
                self.error("merge_readiness.claims_audit: must be a mapping")
            else:
                for key in audit:
                    if key not in ("audited", "rewritten"):
                        self.error(
                            f"merge_readiness.claims_audit: unknown key {_safe_key(str(key))!r}"
                        )
                for key in ("audited", "rewritten"):
                    if key in audit:
                        count = audit.get(key)
                        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                            self.error(
                                f"merge_readiness.claims_audit.{key}: must be a non-negative integer"
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


def _frontmatter_comments(text: str) -> list[str]:
    """Ordered raw comment remnants inside the frontmatter fence.

    R7 codex #7: ``_strip_comment`` removes trailing ``#`` comments while
    parsing frontmatter, and ``_collect_lines`` skips a comment-only line
    outright (never an error), so both ``taint_scan`` (which sees only the
    parsed mapping) and ``monitor_digest`` (which serializes only that mapping
    plus the body) were blind to them -- yet the monitored child is instructed
    to read the RAW state file, comments included. This collector is the
    single source both closures share: ``_frontmatter_comment_findings``
    taint-scans each remnant (injection channel) and ``monitor_digest`` folds
    the remnant sequence into the canonical digest (mutation channel).
    Body ``#`` is a Markdown heading and is deliberately never collected.
    """

    comments: list[str] = []
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return comments
    for offset, raw in enumerate(lines[1:], start=2):
        if raw.strip() == "---":
            break
        try:
            stripped = _strip_comment(raw, offset)
        except StructuralError:
            # An unterminated quote is rejected by parse_state_text upstream;
            # a line that never parses is not a comment channel to collect.
            continue
        if stripped != raw.rstrip():
            comments.append(raw.rstrip()[len(stripped) :].lstrip())
    return comments


def _frontmatter_comment_findings(text: str) -> list[dict[str, str]]:
    """Taint records for INSTRUCTION-LIKE comments inside the frontmatter fence.

    Pass-3 (opus #2, narrowing R7 codex #7): flagging comment PRESENCE bricked
    the package's own documented usage -- the state template in
    references/state-and-safety.md carries dozens of benign ``#`` annotations
    and merge-readiness.md tells agents to initialize from it, so a compliant
    template-derived state would block the runner on tick 1, recoverable only
    by a human ``--acknowledge-taint``. Comments therefore get exactly the
    trust bar every other frontmatter string gets: ``_is_tainted`` over the
    comment text. The mutation channel presence-flagging used to close is
    closed by ``monitor_digest`` instead, which folds the raw remnants into
    the canonical digest -- adding, removing, or rewriting ANY comment
    mid-loop moves the digest ``_require_unmutated_canonical`` pins, even
    though the parsed mapping is unchanged.

    Identity is content-keyed on purpose (stable path, digest of the raw
    remnant -- pass-3 opus #5/codex #10): a line-numbered path would let any
    unrelated state edit renumber the finding and silently revoke an operator
    acknowledgment, while a content digest keeps the acknowledgment bound to
    the exact instruction it was granted for.
    """

    findings: list[dict[str, str]] = []
    for remnant in _frontmatter_comments(text):
        content = remnant.lstrip("#").strip()
        if content and _is_tainted(content):
            findings.append(
                {
                    "path": "frontmatter-comment",
                    "digest": _digest(remnant),
                    "kind": "comment",
                }
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
    # R7 codex #7 (narrowed by pass-3 opus #2): comments are stripped before
    # taint_scan runs, so an instruction-bearing frontmatter comment would
    # reach the raw-reading child unflagged. Scan the remnants with the same
    # instruction heuristic as every other frontmatter string; benign
    # template annotations pass, and comment MUTATION is caught separately
    # by monitor_digest folding the remnants into the canonical digest.
    tainted.extend(_frontmatter_comment_findings(text))
    return {
        "version": SCHEMA_VERSION,
        "state": SUSPECT if validator.errors else VALID,
        "errors": validator.errors,
        "tainted": tainted,
        "phase_requirements": tier_name,
    }


def monitor_digest(text: str) -> str | None:
    """Workflow digest EXCLUDING the runner-owned ``monitor_cli`` block.

    The digest binds a child's verdict to the exact candidate it produced,
    while the runner's single-write finalization (session id, in_flight
    clear) mutates only ``monitor_cli`` — excluding that block means the
    finalization cannot invalidate the digest the verdict pinned.  Both
    sides obtain the value from THIS helper (the child via the
    ``--monitor-digest`` CLI mode), never by hand-rolling serialization.
    """

    try:
        state, body_lines = parse_state_text(text)
    except StructuralError:
        return None
    if not isinstance(state, dict):
        return None
    trimmed = {key: value for key, value in state.items() if key != "monitor_cli"}
    payload = json.dumps(trimmed, sort_keys=True, ensure_ascii=False)
    # Pass-3 (opus #2 follow-through on R7 codex #7): comments are stripped
    # from the parsed mapping, so fold the raw frontmatter comment remnants
    # into the digest -- a comment added, removed, or rewritten mid-loop must
    # move the canonical digest even though the parsed state is unchanged.
    # JSON-encoded so a body line can never collide with the comment block.
    payload += "\n" + json.dumps(_frontmatter_comments(text), ensure_ascii=False)
    payload += "\n" + "\n".join(line.rstrip("\n") for line in body_lines)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


# Durable condition-(c) blocker representations (references/
# monitor-exit-handoffs.md "If stuck"): presence-fired attempt_log families,
# three-strike counter families, the three feedback blocker maps, and the
# per-reviewer human-feedback evidence. The ephemeral triggers
# (CHANGES_REQUESTED, unresolved human threads) surface durably through
# ``human_roundtrip`` ``blocker_remaining: true`` records, which Step 2
# populates whenever human feedback is processed.
_BLOCKED_EVIDENCE_MAPS = (
    "exhausted_feedback",
    "manual_unknown_feedback",
    "manual_branch_protection_blockers",
)
_BLOCKED_THREE_STRIKE_PREFIXES = ("ci:", "conflict:", "branch:", "ready:")
# monitor-ci-feedback.md Step 3 conflict-complexity key: conflict:complex_<F>f_<H>h
# (F = file count, H = hunk count). fullmatch()ed at the call site so a malformed
# or below-threshold key is never mistaken for immediate block evidence. Pass-6
# codex F3/F4: [0-9] not \d (\d admits Unicode digits int() would still parse, so
# "complex_٤f_٦h" must NOT be read as 4f/6h) and a {1,9} bound not + (an
# overlong digit run must never reach int(), which raises above Python 3.11+'s
# decimal-digit ceiling -> the validator would emit no JSON and strand the
# runner). A non-ASCII-digit, >9-digit, or trailing-newline key simply fails the
# grammar and falls through to the generic conflict: three-strike path. Pass-7
# opus N4: the pattern self-anchors (\A...\Z) so it stays an EXACT-match grammar
# even if a future caller uses re.search instead of the fullmatch below - a bare
# pattern would let re.search accept a "ci:conflict:complex_9f_9h" substring.
_CONFLICT_COMPLEX_KEY = re.compile(
    r"\Aconflict:complex_([0-9]{1,9})f_([0-9]{1,9})h\Z"
)


def roundtrip_generation(
    raw_reviewers: Any,
    targets: Any,
    name_with_owner: Any = None,
    pull_request_number: Any = None,
) -> str:
    """Digest of the feedback evidence a roundtrip plan answers.

    Canonical single source (pass-3 codex #2): ``handoff_decision`` embeds
    this digest in every roundtrip operation ID (``...:g<12hex>``) so a
    completed earlier round's ledger can never satisfy a later round, and
    ``monitor_blocked_evidence_present`` recomputes it from the persisted
    reviewer evidence so only the CURRENT generation's ledger counts as
    durable blocked evidence -- a prior-generation terminal record is that
    round's history (the planner ignores it with a warning), never fresh
    evidence for a new blocked transition. ``raw_reviewers`` is a list of
    reviewer-evidence entries each carrying its ``login`` (the planner's
    request shape); the predicate flattens state's login-keyed
    ``human_roundtrip.reviewers`` map into that same shape before calling.
    """

    wanted = {
        login.casefold() for login in (targets or []) if isinstance(login, str)
    }
    payload: list[dict[str, Any]] = []
    for entry in raw_reviewers if isinstance(raw_reviewers, list) else []:
        if not isinstance(entry, dict):
            continue
        login = entry.get("login")
        if not isinstance(login, str) or login.casefold() not in wanted:
            continue
        bodies = entry.get("review_bodies")
        roots = entry.get("inline_roots")
        pushed = entry.get("pushed_fix_shas")
        payload.append(
            {
                "login": login.casefold(),
                "review_bodies": {
                    str(key): (
                        value.get("updated_at") if isinstance(value, dict) else None
                    )
                    for key, value in bodies.items()
                }
                if isinstance(bodies, dict)
                else None,
                "inline_roots": {
                    str(key): (
                        value.get("updated_at") if isinstance(value, dict) else None
                    )
                    for key, value in roots.items()
                }
                if isinstance(roots, dict)
                else None,
                "pushed_through_sha": entry.get("pushed_through_sha"),
                "pushed_fix_shas": sorted(
                    sha for sha in pushed if isinstance(sha, str)
                )
                if isinstance(pushed, list)
                else None,
            }
        )
    payload.sort(key=lambda item: item["login"])
    # admin#1495 finding 3793025386: the digest binds the PLAN TARGET too —
    # replanning a completed ledger from one PR onto another produced the
    # same generation and returned complete with zero calls. Repo + PR join
    # the payload; a missing value hashes as null, which fails CLOSED on
    # comparison — so the blocked-evidence recompute must resolve the repo
    # (the ledger's persisted binding, else monitor_cli.repository; algo#1216
    # r16 F1) rather than count on the omission being symmetric.
    payload = {
        "repository": name_with_owner if isinstance(name_with_owner, str) else None,
        "pull_request": pull_request_number
        if isinstance(pull_request_number, int)
        else None,
        "reviewers": payload,
    }
    # CR 3761135391: same serializer settings as qa_generation — a direct
    # caller's non-JSON pushed_through_sha (unchecked when fix_shas is
    # empty) must hash per the malformed-segments contract, not raise.
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def monitor_blocked_evidence_present(state: Any) -> bool:
    """Schema-owned blocker predicate: does this state carry ANY documented
    durable condition-(c) representation?

    One definition consumed by ``monitor_extract`` and, through the CLI, by
    the runner's blocked-outcome validation — the runner must accept every
    blocked exit the references document (``human:*`` keys fire on presence,
    ``prompt-trail:stale`` fires on presence, ``ci:``/``conflict:``/
    ``branch:``/``ready:`` fire at the three-strike limit), not only the
    three feedback maps. The conflict-resolution IMMEDIATE-block keys
    (``conflict:enumeration_failed`` and ``conflict:complex_<F>f_<H>h``) are
    the exception to the three-strike rule for the ``conflict:`` family:
    monitor-ci-feedback.md Step 3 PERSISTS them then BLOCKs on the FIRST
    REAL occurrence - a non-bool integer count >= 1 (the "count 1" the doc's
    "first occurrence" implies), NOT bare key presence; the complex key
    additionally requires its exact <F>f_<H>h grammar and the >3-file/>5-hunk
    threshold (pass-4 codex F3; both count guards hoisted round-7.7). Within
    ``attempt_log`` only ``human:*`` and ``prompt-trail:stale`` stay
    presence-fired; the feedback maps, a ``human_roundtrip`` blocker, and the
    review-roundtrip ledger below are separate non-count blocker sources.
    """

    if not isinstance(state, dict):
        return False
    for map_key in _BLOCKED_EVIDENCE_MAPS:
        value = state.get(map_key)
        if isinstance(value, dict) and value:
            return True
    attempt_log = state.get("attempt_log")
    if isinstance(attempt_log, dict):
        for key, count in attempt_log.items():
            if not isinstance(key, str):
                continue
            if key.startswith("human:") or key == "prompt-trail:stale":
                return True
            # pass-4 codex F3: the conflict-resolution complexity guard and
            # the enumeration-failure paths (monitor-ci-feedback.md Step 3)
            # PERSIST then BLOCK on the FIRST occurrence - an immediate,
            # count-1 block, not a three-strike retry (a deterministically
            # too-complex merge does not become resolvable by re-attempting
            # it twice more). Recognize them on presence so the runner accepts
            # the documented block instead of discarding the candidate and
            # misattributing the strand to a generic transition_rejected
            # 3-strike. Pass-6 codex F2 / pass-7 codex+opus: BOTH immediate keys
            # additionally require the same non-bool int occurrence count >= 1
            # that the doc's "first occurrence at count 1" implies (matching the
            # generic branch's guard below) - so an injected count 0 or a bool
            # True can never mint an immediate block from a key that has NOT
            # actually occurred. The guard is hoisted here to cover BOTH
            # enumeration_failed and the complex key; the earlier revision
            # guarded only the complex key, leaving enumeration_failed forgeable
            # at count 0 (validate_attempt_log permits count 0). human:* and
            # prompt-trail:stale above stay presence-fired BY DESIGN - cleared
            # by deletion, not by a count.
            occurred = (
                isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 1
            )
            if occurred and key == "conflict:enumeration_failed":
                return True
            # pass-5 codex F2: the complexity key is trusted on presence ONLY
            # when it matches the exact conflict:complex_<F>f_<H>h grammar AND
            # clears the documented threshold (> 3 files OR > 5 hunks). The old
            # startswith() accepted a below-threshold or malformed key
            # (conflict:complex_1f_1h, conflict:complex_x) as an immediate
            # block, letting a worker mint a spurious human handoff from a
            # trivial or spoofed conflict; a non-qualifying key falls through
            # to the generic conflict: three-strike path below (never a clean
            # merge - the fail-safe direction is preserved).
            complex_match = _CONFLICT_COMPLEX_KEY.fullmatch(key)
            if (
                occurred
                and complex_match
                and (
                    int(complex_match.group(1)) > 3
                    or int(complex_match.group(2)) > 5
                )
            ):
                return True
            if (
                key.startswith(_BLOCKED_THREE_STRIKE_PREFIXES)
                and isinstance(count, int)
                and not isinstance(count, bool)
                # The workflow's shared 3-strike doctrine (same limit the
                # operation ledger enforces).
                and count >= MAX_OPERATION_ATTEMPTS
            ):
                return True
    roundtrip = state.get("human_roundtrip")
    reviewers = roundtrip.get("reviewers") if isinstance(roundtrip, dict) else None
    if isinstance(reviewers, dict):
        for record in reviewers.values():
            if isinstance(record, dict) and record.get("blocker_remaining") is True:
                return True
    # R7 codex #3: the SUCCESSFUL review-roundtrip blocked exit ("roundtrip
    # complete, awaiting re-review") clears blocker_remaining to False by
    # eligibility, so its durable evidence is the roundtrip handoff ledger
    # itself — planned operations exist and the aggregate left idle.
    handoffs = state.get("handoffs")
    rt = handoffs.get("review_roundtrip") if isinstance(handoffs, dict) else None
    if isinstance(rt, dict):
        operations = rt.get("operations")
        status = rt.get("status")
        if (
            isinstance(operations, list)
            and operations
            and isinstance(status, str)
            and status != "idle"
        ):
            # Pass-3 codex #2, narrowing the branch above: operation IDs embed
            # the feedback generation (":g<12hex>", a digest of the eligible
            # reviewers' evidence), and the generation contract makes an
            # earlier round's completed ledger HISTORY, never evidence for a
            # fresh blocked transition. Recompute the digest from the
            # persisted reviewer evidence and accept only a matching ledger;
            # missing targets or drifted evidence mismatch and fail closed.
            targets = rt.get("targets")
            target_logins = (
                targets.get("reviewers") if isinstance(targets, dict) else None
            )
            entries: list[dict[str, Any]] = []
            if isinstance(reviewers, dict):
                for login, record in reviewers.items():
                    if isinstance(login, str) and isinstance(record, dict):
                        entries.append({**record, "login": login})
            # algo#1216 r16 F1: the planner mints these IDs with the
            # request's real nameWithOwner, but no pre-F1 ledger ever
            # persisted the binding — recomputing with the missing value
            # hashed null and PERMANENTLY mismatched every real ledger, so
            # a successful handback could never prove durable evidence
            # post-reassignment. Prefer the ledger's own persisted binding
            # (new ledgers; the template carries it); a pre-upgrade ledger
            # derives it from monitor_cli.repository, which the runner has
            # already live-origin-agreed (it fails closed on disagreement
            # before this predicate runs). Neither present still hashes
            # null and fails closed.
            bound_repo = rt.get("repository_name_with_owner")
            if not (isinstance(bound_repo, str) and bound_repo):
                cli = state.get("monitor_cli")
                bound_repo = (
                    cli.get("repository") if isinstance(cli, dict) else None
                )
            suffix = ":g" + roundtrip_generation(
                entries,
                target_logins if isinstance(target_logins, list) else [],
                bound_repo,
                state.get("pr_number"),
            )
            if any(
                isinstance(op, str) and op.endswith(suffix) for op in operations
            ):
                return True
    return False


def monitor_extract(text: str) -> dict[str, Any]:
    """Runner-facing extract: the validation verdict plus every field
    ``scripts/monitor_runner.py`` needs, from ONE parse.

    The runner shells out to this CLI instead of importing the evaluator —
    the package's structural rule keeps subprocess-using files free of the
    evaluator call names (see test_cli_fail_closed.py's module docstring).
    """

    result = evaluate_state_text(text)
    extract: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "state": result["state"],
        "errors": result["errors"],
        # R6-F5 + admin-portal#1495 R2 finding 3776596739: taint is part of
        # the runner-facing contract — path+digest records only, never the
        # flagged text. A structurally valid state carrying instruction-like
        # content must not reach a write-capable child, so the records are
        # surfaced here and the runner fails closed on them before every
        # launch. Hard index, not .get(): a missing key must crash the
        # extract rather than degrade to an empty (fail-open) taint list.
        "tainted": result["tainted"],
        "digest": monitor_digest(text),
        "monitor_cli": None,
        "monitor_ownership": None,
        "model_runtime": None,
        "counters": {},
        "current_phase": None,
        "monitor_status": None,
        "handoff_statuses": [],
        "handoff_status_by_kind": {},
        "handoff_bindings": {},
        "handoff_operations": {},
        "handoff_results": {},
        "handoff_result_digests": {},
        "handoff_result_attempts": {},
        "merge_readiness_backfill": {},
        "phases_merge_readiness": None,
        "merge_readiness_hold": False,
        "merge_readiness_post_deploy": [],
        "last_observed_head_sha": None,
        "deferred_work_evidence": {},
        "blocked_evidence_present": False,
    }
    try:
        state, _ = parse_state_text(text)
    except StructuralError:
        return extract
    if not isinstance(state, dict):
        return extract
    extract["current_phase"] = state.get("current_phase")
    if isinstance(state.get("monitor_cli"), dict):
        extract["monitor_cli"] = state["monitor_cli"]
    if isinstance(state.get("monitor_ownership"), dict):
        extract["monitor_ownership"] = state["monitor_ownership"]
    conventions = state.get("resolved_conventions")
    runtime = (
        conventions.get("model_runtime") if isinstance(conventions, dict) else None
    )
    if isinstance(runtime, dict):
        extract["model_runtime"] = runtime
    for counter in ("monitor_iterations", "monitor_poll_ticks"):
        value = state.get(counter)
        if isinstance(value, int) and not isinstance(value, bool):
            extract["counters"][counter] = value
    phases = state.get("phases")
    monitor_status = phases.get("monitor") if isinstance(phases, dict) else None
    if isinstance(monitor_status, str):
        extract["monitor_status"] = monitor_status
    handoffs = state.get("handoffs")
    statuses: list[str] = []
    # algo#1216 finding 3813491661: the runner's required-handoff manifest
    # needs statuses BY KIND — the unnamed list cannot say whether the qa
    # handoff specifically was ever planned.
    status_by_kind: dict[str, str] = {}
    if isinstance(handoffs, dict):
        for kind, record in handoffs.items():
            status = record.get("status") if isinstance(record, dict) else None
            normalized = status if isinstance(status, str) else "malformed"
            statuses.append(normalized)
            status_by_kind[str(kind)] = normalized
    extract["handoff_statuses"] = statuses
    extract["handoff_status_by_kind"] = status_by_kind
    # admin#1495 r11 finding 3825265263: the runner's terminal gate must
    # compare every handoff's persisted TARGET repository against the
    # runner's own binding — a candidate carrying another repository's
    # handoff must never commit. Operation payloads are not persisted in
    # state, so the schema-persisted repository_name_with_owner is the
    # per-handoff binding surface.
    bindings: dict[str, Any] = {}
    if isinstance(handoffs, dict):
        for kind, record in handoffs.items():
            if not isinstance(record, dict):
                continue
            repo_value = record.get("repository_name_with_owner")
            bindings[str(kind)] = (
                repo_value if isinstance(repo_value, str) else None
            )
    extract["handoff_bindings"] = bindings
    # algo#1216 R2 findings 3787189747/3787189752/3787189757: the runner's
    # terminal/launch decisions need schema-owned views of (a) per-operation
    # handoff results for monotonicity, (b) the direction-aware deploy/
    # backfill hold, and (c) the merge-readiness PHASE for the pre-4b gate.
    operations_map: dict[str, list[str]] = {}
    results_map: dict[str, dict[str, str]] = {}
    digests_map: dict[str, dict[str, str]] = {}
    attempts_map: dict[str, dict[str, int]] = {}
    if isinstance(handoffs, dict):
        for kind, record in handoffs.items():
            if not isinstance(record, dict):
                continue
            ops = record.get("operations")
            operations_map[str(kind)] = [
                op for op in ops if isinstance(op, str)
            ] if isinstance(ops, list) else []
            results: dict[str, str] = {}
            digests: dict[str, str] = {}
            attempts: dict[str, int] = {}
            raw = record.get("operation_results")
            if isinstance(raw, dict):
                for op_id, res in raw.items():
                    status = res.get("status") if isinstance(res, dict) else None
                    if isinstance(op_id, str) and isinstance(status, str):
                        results[op_id] = status
                        attempts_value = res.get("attempts")
                        if isinstance(attempts_value, int) and not isinstance(
                            attempts_value, bool
                        ):
                            attempts[op_id] = attempts_value
                        # admin#1495 R2 follow-up 3793041749: sidecar
                        # compaction must compare the ENTIRE record, not the
                        # status — attempts/evidence/timestamps that differ
                        # are history a human must reconcile, never
                        # "redundant". One canonical-JSON digest per record.
                        digests[op_id] = hashlib.sha256(
                            json.dumps(
                                res,
                                sort_keys=True,
                                separators=(",", ":"),
                                default=str,
                            ).encode("utf-8")
                        ).hexdigest()
            results_map[str(kind)] = results
            digests_map[str(kind)] = digests
            attempts_map[str(kind)] = attempts
    extract["handoff_operations"] = operations_map
    extract["handoff_results"] = results_map
    extract["handoff_result_digests"] = digests_map
    # algo#1216 r16 F11: the terminal deferred-work contract compares the
    # ledgered evidence PAYLOAD, not only statuses - expose each COMPLETE
    # deferred-work artifact record's list keyed by its head sha, plus the
    # state's own observed head, so the runner can require the exact
    # deferred list at the current head before committing terminal.
    head_value = state.get("last_observed_head_sha")
    extract["last_observed_head_sha"] = (
        head_value if isinstance(head_value, str) and head_value else None
    )
    artifacts = (
        handoffs.get("pr_artifacts") if isinstance(handoffs, dict) else None
    )
    artifact_results = (
        artifacts.get("operation_results")
        if isinstance(artifacts, dict)
        else None
    )
    deferred_evidence: dict[str, Any] = {}
    if isinstance(artifact_results, dict):
        for op_id, record in artifact_results.items():
            if not isinstance(op_id, str) or not op_id.startswith(
                "deferred-work:"
            ):
                continue
            if (
                not isinstance(record, dict)
                or record.get("status") != "complete"
            ):
                continue
            evidence = record.get("evidence")
            if isinstance(evidence, dict):
                deferred_evidence[op_id.split(":", 1)[1]] = evidence.get(
                    "deferred"
                )
    extract["deferred_work_evidence"] = deferred_evidence
    extract["handoff_result_attempts"] = attempts_map
    phases_map = state.get("phases")
    if isinstance(phases_map, dict):
        mr_phase = phases_map.get("merge_readiness")
        if isinstance(mr_phase, str):
            extract["phases_merge_readiness"] = mr_phase
    gate = state.get("merge_readiness")
    hold = False
    if isinstance(gate, dict):
        # admin#1495 finding 3793025414: the runner compares candidate
        # backfill records against launch state — expose them.
        raw_backfill = gate.get("backfill")
        if isinstance(raw_backfill, dict):
            extract["merge_readiness_backfill"] = {
                str(name): {
                    "required": record.get("required"),
                    "state": record.get("state"),
                }
                for name, record in raw_backfill.items()
                if isinstance(record, dict)
            }
        if gate.get("deploy_order") == "hazard_documented" and gate.get(
            "hazard_direction"
        ) in ("additive", "mixed"):
            applied = gate.get("applied_state")
            if not isinstance(applied, dict) or not applied:
                # Finding 3806719714: no recorded environments = the recheck
                # never ran — the hold stays live, never silently released.
                hold = True
            else:
                for env_map in applied.values():
                    if not isinstance(env_map, dict) or not env_map:
                        # Finding 3807740761: an empty environment map is an
                        # unran recheck for that environment — hold.
                        hold = True
                        continue
                    for status in env_map.values():
                        # admin#1495 finding 3813789228: only PRE-deploy
                        # requirements hold. A per-migration destructive
                        # entry pending is the DOCUMENTED post-deploy step
                        # of a mixed sequence — holding on it forced the
                        # destructive DDL to run under the old code.
                        if isinstance(status, dict):
                            if (
                                status.get("direction") == "additive"
                                and status.get("status") == "pending"
                            ):
                                hold = True
                        elif status == "pending":
                            hold = True
        # Post-deploy destructive work is a separate required state, never
        # silently dropped: destructive entries still pending are exposed
        # for the terminal gate / PR-body deferred-work contract.
        post_deploy: list[str] = []
        applied_any = gate.get("applied_state")
        top_direction = gate.get("hazard_direction")
        if isinstance(applied_any, dict):
            for env, env_map in applied_any.items():
                if not isinstance(env_map, dict):
                    continue
                for migration, status in env_map.items():
                    entry_destructive_pending = (
                        isinstance(status, dict)
                        and status.get("direction") == "destructive"
                        and status.get("status") == "pending"
                    ) or (
                        top_direction == "destructive" and status == "pending"
                    )
                    if entry_destructive_pending:
                        post_deploy.append(f"{env}:{migration}")
        extract["merge_readiness_post_deploy"] = sorted(post_deploy)
        backfill = gate.get("backfill")
        if isinstance(backfill, dict):
            for record in backfill.values():
                if (
                    isinstance(record, dict)
                    and record.get("required") is True
                    and record.get("state") != "complete"
                ):
                    # Defensive derivation (3788363458): anything required
                    # and not verified complete holds — including the
                    # contradictory n_a shape validation now rejects.
                    hold = True
        # algo#1216 R2 finding 3787662319: a documented merged-but-not-live
        # dependency holds the clean exits unconditionally (not direction-
        # gated) until it verifies live — monitor-exit-handoffs (a)/(d).
        if gate.get("dependencies") == "hazard_documented":
            hold = True
    extract["merge_readiness_hold"] = hold
    # R2 #1328 finding 3767068764 is satisfied by the shared predicate below:
    # blocker evidence is more than the three feedback maps - `human:*` and
    # `prompt-trail:stale` attempt_log keys fire on presence, the three-strike
    # ci:/conflict:/branch:/ready: families fire at the limit, and a human-
    # review block persists as the review_roundtrip ledger. Recomputing it from
    # the helper keeps monitor_extract and the runner's blocked-outcome CLI
    # validation on one definition (and adds the pass-3 codex #2 generation
    # recheck that rejects an earlier round's stale ledger).
    extract["blocked_evidence_present"] = monitor_blocked_evidence_present(state)
    return extract


_CLI_USAGE = (
    "usage: state_schema.py <state-file> | state_schema.py"
    " --monitor-extract|--monitor-digest <state-file> | state_schema.py"
    " --append-attempt <state-file> <human:key>"
)


def append_attempt_key(text: str, key: str) -> str | None:
    """Surgical textual append/increment of one ``attempt_log`` key.

    algo#1216 r16 F13: the stash-restore failure arms must persist the
    durable ``human:stash-restore`` blocked record with ONE concrete
    atomic call — a printed warning evaporates with the session. This is
    deliberately a TEXT operation (never a reserialization): the state
    file is agent-maintained, and rewriting unrelated lines would destroy
    content the schema does not model. Returns the new text, or ``None``
    when no top-level ``attempt_log`` line exists to anchor the surgery.
    """

    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line == "attempt_log: {}":
            lines[index] = "attempt_log:"
            lines.insert(index + 1, f'  "{key}": 1')
            return "\n".join(lines)
        if line == "attempt_log:":
            prefix = f'  "{key}": '
            scan = index + 1
            while scan < len(lines) and lines[scan].startswith("  "):
                existing = lines[scan]
                if existing.startswith(prefix):
                    count_text = existing[len(prefix):].strip()
                    try:
                        count = int(count_text)
                    except ValueError:
                        return None
                    if isinstance(count, bool) or count < 0:
                        return None
                    lines[scan] = f"{prefix}{count + 1}"
                    return "\n".join(lines)
                scan += 1
            lines.insert(index + 1, f'  "{key}": 1')
            return "\n".join(lines)
    return None


def _append_attempt_cli(path: str, key: str) -> int:
    """CLI glue for ``--append-attempt``: lock-probe, validate, atomic
    replace. Restricted to ``human:*`` keys — those are the presence-fired
    durable blocker records this persist call exists for; anything wider
    would turn a narrow persist helper into a general state mutator."""

    def _refuse(errors: list[str]) -> int:
        print(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "state": SUSPECT,
                    "errors": errors,
                    "tainted": [],
                    "phase_requirements": "unparsed",
                }
            )
        )
        return 1

    if not key.startswith("human:") or len(key) <= len("human:"):
        return _refuse(["--append-attempt accepts only human:* keys"])
    import fcntl as _fcntl
    import stat as _stat
    import uuid as _uuid

    # algo#1216 r17 F1 / admin#1495 r13 F2: the lock and temp paths are
    # attacker-predictable siblings of a child-writable state file, so
    # every open here is no-follow, the lock must be a REGULAR file whose
    # PATH still names the locked inode after acquisition (a swap between
    # open and flock re-acquires; persistent churn refuses), and the temp
    # below is an unpredictable same-directory O_EXCL 0600 file written
    # with a complete-write loop and fsynced before the atomic replace,
    # with the parent directory fsynced after it. Cleanup unlinks only
    # the temp THIS call created - an O_EXCL collision is someone else's
    # file and stays untouched.
    lock_path = path + ".monitor.lock"
    lock_fd = None
    for _ in range(5):
        try:
            candidate_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError:
            return _refuse(
                ["append-attempt could not open the runner lock as a"
                 " no-follow regular file"]
            )
        fd_stat = os.fstat(candidate_fd)
        if not _stat.S_ISREG(fd_stat.st_mode):
            os.close(candidate_fd)
            return _refuse(["runner lock path is not a regular file"])
        try:
            _fcntl.flock(candidate_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            os.close(candidate_fd)
            return _refuse(
                ["a monitor runner is active — persist the record after"
                 " it exits (lock held)"]
            )
        try:
            path_stat = os.stat(lock_path, follow_symlinks=False)
        except OSError:
            os.close(candidate_fd)
            continue
        if (path_stat.st_ino, path_stat.st_dev) == (
            fd_stat.st_ino,
            fd_stat.st_dev,
        ):
            lock_fd = candidate_fd
            break
        os.close(candidate_fd)
    if lock_fd is None:
        return _refuse(
            ["the runner lock kept changing identity under acquisition —"
             " refusing to trust it"]
        )
    try:
        try:
            text = _read_state_file(path)
        except (OSError, UnicodeDecodeError):
            return _refuse(["state file could not be read or decoded"])
        updated = append_attempt_key(text, key)
        if updated is None:
            return _refuse(
                ["no top-level attempt_log line to anchor the append"]
            )
        result = evaluate_state_text(updated)
        if result["state"] != VALID:
            return _refuse(
                ["append would leave the state invalid; file untouched:"]
                + list(result["errors"])
            )
        tmp_path = f"{path}.append-attempt.{_uuid.uuid4().hex}.tmp"
        try:
            tmp_fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            # O_EXCL collision or a planted symlink: not our file — refuse
            # without unlinking anything (ownership-safe cleanup).
            return _refuse(
                ["temp creation failed (collision or symlink refused);"
                 " original untouched, nothing cleaned"]
            )
        replaced = False
        try:
            payload = memoryview(updated.encode("utf-8"))
            try:
                while payload:
                    payload = payload[os.write(tmp_fd, payload):]
                os.fsync(tmp_fd)
            finally:
                os.close(tmp_fd)
            os.replace(tmp_path, path)
            replaced = True
            dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            if replaced:
                return _refuse(
                    ["state WAS replaced but the parent-directory fsync"
                     " failed — durability is unproven; re-verify the"
                     " record after the filesystem syncs"]
                )
            return _refuse(["atomic replace failed; original untouched"])
        print(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "state": VALID,
                    "appended": key,
                }
            )
        )
        return 0
    finally:
        os.close(lock_fd)


def _read_state_file(path: str) -> str:
    # mm#3551 finding 3806719734: the CLI's target is child-writable —
    # refuse special files without blocking and bound the read (the
    # ceiling mirrors the runner's MAX_CANDIDATE_BYTES; a state past it
    # is corruption, not progress). O_NONBLOCK is harmless on regular
    # files; S_ISREG is proven on the OPEN descriptor.
    import stat as _stat

    _fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not _stat.S_ISREG(os.fstat(_fd).st_mode):
            raise OSError("target is not a regular file")
        _chunks = []
        _budget = STATE_READ_CEILING_BYTES + 1
        while _budget > 0:
            _chunk = os.read(_fd, min(1_048_576, _budget))
            if not _chunk:
                break
            _chunks.append(_chunk)
            _budget -= len(_chunk)
    finally:
        os.close(_fd)
    _raw = b"".join(_chunks)
    if len(_raw) > STATE_READ_CEILING_BYTES:
        raise OSError("target exceeds the state read ceiling")
    return _raw.decode("utf-8")


def main(argv: list[str]) -> int:
    mode = "validate"
    args = argv[1:]
    if args and args[0] in (
        "--monitor-extract",
        "--monitor-digest",
        "--append-attempt",
    ):
        mode = args[0]
        args = args[1:]
    if mode == "--append-attempt":
        if len(args) != 2:
            print(
                json.dumps(
                    {
                        "version": SCHEMA_VERSION,
                        "state": SUSPECT,
                        "errors": [_CLI_USAGE],
                        "tainted": [],
                        "phase_requirements": "unparsed",
                    }
                )
            )
            return 2
        return _append_attempt_cli(args[0], args[1])
    if len(args) != 1:
        print(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "state": SUSPECT,
                    "errors": [_CLI_USAGE],
                    "tainted": [],
                    "phase_requirements": "unparsed",
                }
            )
        )
        return 2
    try:
        text = _read_state_file(args[0])
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
    if mode == "--monitor-digest":
        digest = monitor_digest(text)
        print(json.dumps({"version": SCHEMA_VERSION, "digest": digest}))
        return 0 if digest is not None else 1
    if mode == "--monitor-extract":
        extract = monitor_extract(text)
        print(json.dumps(extract, sort_keys=True))
        return 0 if extract["state"] == VALID else 1
    result = evaluate_state_text(text)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == VALID else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
