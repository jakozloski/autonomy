#!/usr/bin/env python3
"""Deterministically validate the autonomy skill package.

The validator intentionally uses only the Python standard library so it can run
from Codex, Claude Code, CI, or a freshly cloned repository without installing
package-specific dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_policy
import state_schema  # noqa: E402  (sibling module; path set immediately above)


ALLOWED_FRONTMATTER_KEYS = frozenset({"name", "description"})
REQUIRED_FRONTMATTER_KEYS = ALLOWED_FRONTMATTER_KEYS
MAX_SKILL_LINES_EXCLUSIVE = 500
MAX_REFERENCE_LINES_EXCLUSIVE = 500

# Model-policy facts have exactly one source: model_policy.py. The validator
# derives the strings it pins from those constants instead of restating them, so
# a floor or effort change cannot leave the validator asserting the old policy.
CODEX_FLOOR_MODEL = model_policy.CODEX_MODEL
# R2 round-2 finding 3737466478: the sandbox pin is part of the canonical
# exec flags — an unpinned reconstruction inherits the operator's ambient
# codex sandbox, and a review voice must never be able to write to the
# implementation it judges. (`codex review` exposes no sandbox flag; the
# pin applies to exec-shaped invocations.)
EXEC_MODEL_FLAGS = (
    f"-m <selected> -c 'model_reasoning_effort=\"{model_policy.CODEX_EFFORT}\"'"
    " -s read-only"
)
REVIEW_MODEL_FLAGS = (
    f"-c 'model=\"<selected>\"' -c 'model_reasoning_effort=\"{model_policy.CODEX_EFFORT}\"'"
)
# admin#1495 r12 F10: codex CLI 0.144.x accepts exec-level flags (including
# the -s sandbox pin) BEFORE the `resume` subcommand and none after it — a
# package documenting only trailing flags lets a delegated resume silently
# drop the sandbox pin. The exact ordered shape must appear in the text.
EXEC_RESUME_SHAPE = f"codex exec {EXEC_MODEL_FLAGS} resume"

# admin#1495 r15 F12: the pinned upstream source is MIT-licensed; every
# vendored copy must retain the notice. Checked as its own required file
# (content-checked below), deliberately separate from byte-parity rules.
REQUIRED_LICENSE_FILE = "LICENSE"

REQUIRED_REFERENCE_FILES = (
    "references/project-and-entry.md",
    "references/phases-1-5.md",
    "references/merge-readiness.md",
    "references/monitor-ci-feedback.md",
    "references/monitor-exit-handoffs.md",
    "references/state-and-safety.md",
)
REQUIRED_SCRIPT_FILES = (
    "scripts/handoff_decision.py",
    # admin#1495 r19 F7: the evaluator-free target/family leaf the planner
    # and runner import at boot - a copy missing it would pass validation
    # and fail only at import time, so it is inventoried like every other
    # load-bearing script.
    "scripts/handoff_targets.py",
    "scripts/model_policy.py",
    "scripts/state_schema.py",
    "scripts/validate_package.py",
    "scripts/test_handoff_decision.py",
    "scripts/test_model_policy.py",
    "scripts/test_model_policy_supervision.py",
    "scripts/test_state_schema.py",
    # admin#1495 r19 F4: the cross-process lock-race regression lives in
    # its own module because it spawns a real child while
    # test_state_schema.py calls the evaluate-named core API - the
    # security scan forbids pairing those in one file (r32-r33 lesson).
    "scripts/test_state_lock_races.py",
    "scripts/test_validate_package.py",
    "scripts/test_cli_fail_closed.py",
    "scripts/monitor_runner.py",
    "scripts/monitor_child_wrapper.py",
    "scripts/test_monitor_runner.py",
    "scripts/test_monitor_runner_unit.py",
    # algo#1216 r18 F1 / admin#1495 r14 F6: the package-wide
    # undefined-global (Ruff F821 class) gate.
    "scripts/test_static_globals.py",
)

# Evidence-gate and state-hardening content contracts.  Each marker is an
# exact substring the named file must contain; renaming the prose label in a
# reference must update this inventory in the same commit (same contract as
# the heading inventory).
REQUIRED_GATE_MARKERS = {
    "SKILL.md": (
        "state_schema.py",
        "monitor_runner.py",
        # R2 F1 + R3 F2 — the failure-matrix quota row: bounded helper wait,
        # constant-derived ceiling (a value bump in state_schema fails
        # validation until this prose catches up), helper-decided streak.
        "WAIT until the helper-computed `quota.wait_until`",
        "`MAX_QUOTA_WAIT_SECONDS` ("
        + str(model_policy.MAX_QUOTA_WAIT_SECONDS)
        + "s)",
        "decided by the helper from the fed `post_invocation` records",
        "red/green regression evidence",
        "evaluated_head_sha",
        "Prompt Ledger",
        "prompt-trail:stale",
        "merge-readiness.md",
        "phases.merge_readiness",
        "direction-aware merge-readiness hold",
        "Never trade a test for a green review",
        # Follow-through contracts: fail-fast gate, terminal turn, salvage.
        "Model-Gate Entry Preflight",
        "Deterministic authentication failure",
        "Terminal-exit turn contract",
        "branch-established:",
        "validation-before-push:",
        "Stranded work",
        # Phase 6 session ownership — the role must stay documented in the
        # core beside the legs it rebinds.
        "Monitor orchestrator",
        "monitor_orchestrator_binding",
        # Derived policy pins — the Claude floors and CLI minimums must appear
        # in the core, under the same single-source contract as the codex flag
        # strings (a floor bump in model_policy.py fails validation until the
        # prose catches up).
        model_policy.BASE_MODEL,
        model_policy.REVIEWER_MODEL,
        ">= " + ".".join(str(part) for part in model_policy.MIN_CLAUDE_VERSION),
        ">= " + ".".join(str(part) for part in model_policy.MIN_CODEX_VERSION),
        # Tier-doctrine derived pins (phase-4 review F12): the supplement's
        # starting tier, the routine tiers, and the breadth effort must appear
        # in the core exactly as the constants state them — a constant change
        # fails validation until the prose catches up.
        "starts at `" + model_policy.REVIEWER_SUPPLEMENT_STARTING_EFFORT + "`",
        "`" + "` / `".join(model_policy.ROUTINE_EFFORTS) + "`",
        '"' + model_policy.CODEX_BREADTH_EFFORT + '"',
    ),
    "references/phases-1-5.md": (
        "Red/green regression evidence (mandatory when",
        "Variant analysis (mandatory when",
        "Diff-triggered review focus lines",
        "regression_evidence.status",
        "prompt-trail:start",
        "Repository reviewer rubric",
        "Root cause & scope decision",
        "one sanitized checkbox per AC",
        "CI evidence: pending for head",
        "test-integrity tripwire",
        "Review-response fixes never tier Small",
        "Merge Readiness Gate",
        "re-run merge-readiness Check 3 against the just-created ticket",
        # Authentication detection must stay structured-channel only.
        "Authentication detection (every real Codex invocation",
        # R2 F1/F2 + R3 F2 — the quota-wait dispatch: helper-computed bounded
        # wait, helper-decided streak over the fed records, and the
        # concretely-passed runaway ceiling.
        "wait_for_quota_reset",
        "quota_reset_at",
        "clamped to `MAX_QUOTA_WAIT_SECONDS` per sleep",
        "the HELPER takes the terminal no-usable-reset block",
        "judged at its own `observed_at`",
        "max_runtime_seconds="
        + str(model_policy.PER_ATTEMPT_CEILING_SECONDS),
        # R2 round-2 finding 3737466493: without the child's wait, a smoke
        # child exiting nonzero after benign output read as clean/0 — the
        # canonical wiring must pass child_wait so the gate observes the
        # real return code. mm#3551 dawid-r8 F2: the pin now extends
        # through child_pgid - the prose templates ARE the production
        # callers, and an anchor stopping at child_wait let a template
        # drop the group kwarg (leader-only kill regression) unnoticed.
        "supervise_stream(stdout_pipe, stderr_pipe, kill_callback,"
        " child_wait, child_pgid=pgid",
        "verify_frozen_selection()",
        "attempts are per-invocation and reset each round",
    ),
    "references/merge-readiness.md": (
        "Deploy-Order Safety",
        "Dependency Merge-State",
        "AC Conformance",
        "Claims Audit",
        "test-integrity tripwire",
        "never tier Small",
        "merge_readiness.deploy_order",
        "Zero-caller implementations are NOT met",
        "Merged-but-not-live is the same ordering hazard as unmerged",
        # R2 round-2 finding 3737466485: the plain additive template is
        # wrong when readers need populated rows, not just the schema —
        # dropping this line reverts to deploying readers into the
        # null/default window.
        "readers depend on populated rows",
        # 28e13163ef direction-aware generation — a regeneration that drops
        # these loses the destructive-direction fix and the ready-PR signal.
        "merge_readiness.hazard_direction",
        'merge_readiness.dependencies: "hazard_documented"',
        "posts the `### Deploy order`",
        "return to the caller",
        # mm#3551 dawid-r9 F4: the fingerprint-refresh bullet is the sole
        # instruction keeping post-4b fix pushes from the runner's
        # reject-and-charge loop (dawid-r8 F3) — a prose-only fix in a
        # template file needs the same anti-regeneration pin as its code
        # siblings, or a regeneration deletes the bullet unnoticed.
        "**Classification fingerprint refreshed?** ALWAYS, on every fix"
        " push",
        "persist the fingerprint (with any changed selectors) BEFORE"
        " submitting a terminal candidate",
    ),
    "references/state-and-safety.md": (
        "Resume trust model",
        # R2 liveness contracts — dropping any of these regressions F2/F3/B:
        # the ceiling parameter, the schema-legal wait timestamp and its
        # reset-path clear, and the stale-reset clamp.
        "max_runtime_seconds="
        + str(model_policy.PER_ATTEMPT_CEILING_SECONDS),
        # mm#3551 dawid-r9 F2: the child_pgid group-kill kwarg is pinned in
        # EVERY file carrying a supervise_stream call template, not only
        # phases-1-5.md — the templates ARE the production callers, and an
        # unpinned file lets a regeneration drop the group kwarg here
        # (leader-only kill regression) while the pinned sibling stays green.
        "supervise_stream(..., child_pgid=pgid, idle_timeout_seconds="
        + str(model_policy.MONITOR_CHILD_IDLE_TIMEOUT_SECONDS)
        + ")",
        "supervise_stream(..., child_pgid=pgid, max_runtime_seconds="
        + str(model_policy.PER_ATTEMPT_CEILING_SECONDS)
        + ")",
        "persist `next_retry_at`",
        "hold_started_at",
        "`next_retry_at`, and phase-specific blocked status fields",
        # R4 F1 — the grace window is canonical in state_schema; the template
        # line must carry the derived value or a bump silently splits the
        # loop's armed window from the validator's resume ceiling.
        "bot_grace_window_seconds: "
        + str(state_schema.BOT_GRACE_WINDOW_SECONDS),
        # R5 F1 — the override bound is canonical in state_schema and the
        # prose must state it (a silently-applied cap was the finding); the
        # derived value keeps a constant bump from splitting prose and code.
        "must be an integer in (0, "
        + str(state_schema.MAX_GRACE_WINDOW_OVERRIDE_SECONDS)
        + "]",
        "never silently replaced",
        # R5 F2 — the wait-owner liveness tie is part of the documented
        # invariant list; dropping the mention decouples prose from the
        # validator's enforced lifecycle.
        "live-wait-owner",
        "takes the terminal quota block",
        "clamped to `MAX_QUOTA_WAIT_SECONDS` per sleep",
        "`MAX_QUOTA_WAIT_SECONDS` resume ceiling",
        "regression_evidence:",
        "variant_analysis:",
        "state_schema_version",
        "analyzed_head_sha",
        "audit-only",
        "defect_evidence_mode",
        "pr_artifacts",
        "acceptance_criteria:",
        "merge_readiness:",
        "hazard_direction",
        "Stranded work",
        "postcondition-bound",
    ),
    # admin#1495 r18 F3: this key appeared TWICE in this dict literal, and a
    # Python dict display silently keeps only the last duplicate - the tuple
    # carrying the fullDatabaseId projections was replaced wholesale, so the
    # effective mapping held zero fullDatabaseId markers while the per-marker
    # test derived its oracle from the same overwritten dict. One merged
    # entry now; the duplicate-key AST guard in test_validate_package.py
    # keeps the whole class from returning.
    "references/monitor-ci-feedback.md": (
        # algo#1216 r19 F10: the GraphQL joins are prose-executed contracts —
        # pin the fullDatabaseId projections so a revert to the deprecated
        # 32-bit field fails validation.
        "nodes { fullDatabaseId author { __typename login } updatedAt }",
        "id fullDatabaseId submittedAt updatedAt",
        "world-state refresh",
        # R4 F1 — the pseudocode constant derives from state_schema.
        "BOT_GRACE_WINDOW = " + str(state_schema.BOT_GRACE_WINDOW_SECONDS),
        "merge-readiness holds are clear",
        "reviewable files (code, config, tests, skills/docs",
        # R2 F4 — hold-waits are poll ticks with a bounded human-dependency
        # exit; dropping any line reverts holds to cap-burning hot polls or
        # an unbounded silent strand.
        "Live merge-readiness hold",
        "human:deploy-hold",
        "hold_started_at",
        # Takeover review round: pseudocode (d) mirrors the authoritative
        # grace conjunct (stable-poll section: independent conjunct of every
        # exit).
        "stable_poll_confirmed AND grace_elapsed(post_push_until)",
    ),
    "references/monitor-exit-handoffs.md": (
        "diff-triggered review focus lines",
        "CI-config self-verification",
        "QA rehearsal (advisory, non-blocking",
        "deploy-hold",
        "ready:dependency-hold",
        "exit:deploy-hold",
        'A `"destructive"`-direction hazard does NOT hold the flip',
        "never the Small tier",
        # Human-only dependency grammar: forms select fixed verifiers.
        "closed, package-authored grammar",
        "human:codex-login",
        "human:user-confirm",
        "fires on PRESENCE",
        "Degraded terminal path",
        # R2 F4 — the hold sites route to wait_repoll and carry the bounded
        # human-dependency backstop with its persisted span and its own
        # grammar rows (fixed applied/live-state verifiers).
        'set `loop_reason = "wait_repoll"`',
        "human:deploy-hold",
        "human:dependency-hold",
        # Row-unique verifier text: the key substrings above also occur in
        # hold prose, so deleting the grammar table rows alone would still
        # pass without these.
        "applied-state query for the held migration",
        "clears when the dependency verifies live",
        "BOT_GRACE_WINDOW` of continuous hold time",
        "hold_started_at",
        # R2 round-2 finding 3737466450: identity-only operation IDs let a
        # completed first-round ledger satisfy the second round's plan
        # (state "complete", empty call plan, nobody re-pinged). Dropping
        # this line loses the contract that fresh feedback mints fresh
        # operations.
        "operation IDs embed the feedback generation",
        # R2 round-2 finding 3737466456: without a persistable non-attempt
        # record, the planner's terminal failed answer violated the schema
        # in every monitor state (missing descendant records derive
        # pending). Dropping this loses the persistence instruction.
        "persists its rendered `skipped_dependency` record",
        # Bugbot 2026-08-06 (thread 3729764470): condition (d) must carry the
        # live-hold action itself — without it, a ready unapproved PR whose
        # only remaining blocker is a live hold matches no condition ((a)
        # needs APPROVED, (e) needs grace/stability unmet) and the pass falls
        # through to hot-poll the 50-iteration work cap. Presence markers
        # catch regeneration loss only; binding this text to (d)'s own line
        # is REQUIRED_ANCHORED_MARKERS' job — a file-wide substring cannot
        # see placement.
        "the signal the hold exists to prevent. A live hold here takes"
        " condition (a)'s own hold action, not a fall-through",
        # Takeover review round: (d) carries grace_elapsed explicitly — the
        # stable-poll section declares grace an independent conjunct of EVERY
        # exit; this adjacency exists only in (d)'s conjunct list.
        "stable_poll_confirmed` AND `grace_elapsed(post_push_until)`",
        # Phase 6 session ownership — the binding call, the recorded
        # fallback, the continuity contract, the capability boundary with
        # its write-capable worker precondition, the trail marker, and the
        # terminal audit; dropping any of these regresses the orchestrator
        # role to an unrecorded model swap or strands fix work without a
        # compliant actor.
        "monitor_orchestrator_binding",
        "orchestrator_on_base",
        "orchestrator_continuity",
        "pending_owner",
        "never writes code/config/tests",
        "write-capable base worker path",
        "monitor-ownership:<lineage>:<model>",
        "worker-dispatch:<trigger>",
        "ONE read-only base-lineage audit",
        # Owner-pinned runner contract — the execution locus, the commit
        # protocol, and the no-inline rule; dropping any of these regresses
        # automatic ownership to a manual model pick or a clobber-capable
        # writer.
        "scripts/monitor_runner.py",
        "sole canonical committer",
        "base_workflow_digest",
        "NO inline fallback under a continuity binding",
    ),
    # The state_schema re-export bindings (attempt cap, quota-wait ceiling,
    # 3-strike child-failure limit) are NOT here: a file-wide substring is
    # satisfied by the same text parked in a comment, which is exactly how a
    # rebind silently reverts to an independent literal (R7.2 codex #9). They
    # live in REQUIRED_PY_BINDINGS below, checked as operative source lines.
    "references/project-and-entry.md": (
        "red/green + variant evidence gate",
        "defect_evidence_mode",
        "Capture acceptance criteria",
        "Real access smoke test (authoritative)",
        "Path-inheritance invariant",
        "routing_fingerprint",
        "Package-root identity (evaluated BEFORE the empty-diff guard)",
        # R2 round-2 finding 3737466502: a resume landing at
        # current_phase: monitor bypassed the Phase 5 merge-readiness
        # self-heal — pre-4b states reached clean exits with the gate
        # never run. The resume router's monitor bullet must carry the
        # same precondition Phase 5 enforces.
        "then re-enter the monitor",
        # mm#3551 dawid-r9 F2: same child_pgid pin as phases-1-5.md and
        # state-and-safety.md — all three files carry production
        # supervise_stream call templates, and each needs its own anchor
        # (a marker in one file cannot see a kwarg dropped from another).
        "supervise_stream(stdout_pipe, stderr_pipe, kill_callback,"
        " child_wait, child_pgid=pgid, idle_timeout_seconds=60",
    ),
}

# Operative-line pins for the state_schema re-export bindings. Each value must
# appear as a real Python statement — a whole source line carrying only the
# binding (leading indentation and an optional trailing comment aside), NOT the
# same text hidden in a comment or embedded in other code. This is the single
# source of truth these constants forbid drift against: the actual runtime
# value cannot be pinned by identity because CPython interns the small ints, so
# reverting `X = state_schema.X` to an independent literal `X = 3` yields the
# same object — only the source form distinguishes them (R7.2 codex #9, the
# complete set of three: attempt cap, quota-wait ceiling, child-failure limit).
REQUIRED_PY_BINDINGS = {
    "scripts/handoff_decision.py": (
        "MAX_OPERATION_ATTEMPTS = state_schema.MAX_OPERATION_ATTEMPTS",
        "QA_OPERATION_FAMILIES = state_schema.QA_OPERATION_FAMILIES",
        "REVIEWER_REQUEST_FAMILIES = state_schema.REVIEWER_REQUEST_FAMILIES",
        "ROUNDTRIP_FAMILIES = state_schema.ROUNDTRIP_FAMILIES",
        "parsed_generation_family = state_schema.parsed_generation_family",
        # algo#1216 r17 F5: the Git object-ID grammar derives from the
        # schema-owned fragment on both sides.
        "GIT_OBJECT_ID = state_schema.GIT_OBJECT_ID",
    ),
    "scripts/state_schema.py": (
        'PR_ARTIFACT_ID = re.compile("(?:ci-evidence|qa-rehearsal|deferred-work):" + GIT_OBJECT_ID_HEX + r"\\Z")',
    ),
    "scripts/model_policy.py": (
        "MAX_QUOTA_WAIT_SECONDS = state_schema.MAX_QUOTA_WAIT_SECONDS",
        "MONITOR_CHILD_FAILURE_LIMIT = state_schema.MONITOR_CHILD_FAILURE_LIMIT",
    ),
}

# Placement-anchored markers: the presence markers above are file-wide
# substrings — right for regeneration-loss checking, blind to placement
# (text relocated wholesale still matches). Each entry binds required
# substrings to THE single operative line carrying its anchor core, in the
# Markdown context the operative line actually occupies (`fenced` False =
# ordinary prose: list-marker-tolerant, excluding fenced blocks and
# 4-space-indented code; `fenced` True = the line's operative form lives
# inside a fenced block, as the monitor pseudocode does). Exactly one such
# line may exist, so reverting condition (d) while parking the marker text
# elsewhere — in a fenced block of either family (tracking enforces
# CommonMark's delimiter line grammar via _iter_fence_state, shared with
# the heading scanner: family match, closing run at least opener-length
# with a whitespace-only tail, closer indent absolute (<= 3 columns) for
# openers at three or fewer and relative (opener + 3) for deeper
# list-nested openers, backtick openers rejected when their info string
# contains a backtick, unclosed fence at EOF an error), under a swapped
# bullet marker, as code indented four-plus columns (tabs expand to
# 4-column stops), or inside an HTML-comment block (pass-3/4/5/6/7
# takeover review rounds) — fails validation. Known divergence, stated
# as scope: openers are recognized at any absolute indentation because
# in-list fences measure indent relative to the item's content column
# (this package holds legitimate fences at four and five columns) and no
# list context is modeled here; an over-indented hand-authored opener
# can therefore classify visible prose as fenced (see the
# _iter_fence_state docstring). Boundary, stated honestly:
# this defends regeneration drift and text relocation through the Markdown
# constructs above; no text-level check can bind an adversary who edits
# the validator itself.
REQUIRED_ANCHORED_MARKERS = {
    "references/monitor-exit-handoffs.md": (
        (
            "**(d) If everything is clean AND",
            False,
            (
                "`grace_elapsed(post_push_until)`",
                "A live hold here takes condition (a)'s own hold action",
                'set `loop_reason = "wait_repoll"`',
            ),
        ),
    ),
    "references/monitor-ci-feedback.md": (
        (
            "d. If everything is clean AND",
            True,
            ("grace_elapsed(post_push_until)",),
        ),
    ),
    # algo#1216 r16 F12: the operative claims-audit instruction — Check 4's
    # verify step — is pinned like the exit conditions: a regeneration that
    # drops or relocates it (or parks a decoy inside a fenced block) fails
    # validation. Substrings avoid the apostrophe on purpose (a curly-quote
    # regeneration would false-fail an exact ASCII pin).
    "references/merge-readiness.md": (
        (
            "2. For each claim, verify against the actual code path",
            False,
            (
                "re-read the intention",
                "Three outcomes:",
            ),
        ),
    ),
}

_LIST_MARKER_PREFIXES = ("- ", "* ", "+ ")

# Patterns are canonical in model_policy.REDACTION_PATTERNS (runtime
# enforcement, finding 3806595004); this dict binds each to its
# fixtures. The parity check below fails if the two sets drift.
REQUIRED_REDACTION_PATTERNS = {
    "aws_access_or_session_key": (
        r"(AKIA|ASIA)[0-9A-Z]{16}",
        ("AKIA" + "1234567890ABCDEF", "ASIA" + "1234567890ABCDEF"),
    ),
    # R2 round-2 finding 3737466468: model_policy.py's excerpt handling
    # explicitly defers bare Authorization headers to "the package's
    # format-anchored Secret/Token Redaction" — but the pattern list had
    # no such pattern (a comment promising coverage that did not exist).
    # Slack tokens are equally format-anchored; both fit the doc's own
    # stated design rule.
    "authorization_bearer_header": (
        r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9._~+/-]{8,}=*",
        (
            "Authorization: Bearer " + "abc123def456ghi789",
            "authorization: bearer " + "eyJhbGciOiJIUzI1NiJ9.payload.sig",
        ),
    ),
    "slack_token": (
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        (
            "xox" + "b-1234567890-abcdefghij",
            "xox" + "p-9876543210-zyxwvutsrq",
        ),
    ),
    "aws_secret_access_key": (
        r"""(?i)AWS_SECRET_ACCESS_KEY["']?\s*[:=]\s*["']?[A-Za-z0-9/+=]{40}["']?""",
        (
            "AWS_SECRET_ACCESS_KEY=" + "A" * 40,
            'AWS_SECRET_ACCESS_KEY": "' + "B" * 40 + '"',
        ),
    ),
    "aws_session_token": (
        r"""(?i)AWS_SESSION_TOKEN["']?\s*[:=]\s*["']?[A-Za-z0-9/+=]{16,4096}["']?""",
        (
            "AWS_" + "SESSION_TOKEN=" + "C" * 32,
            "AWS_" + 'SESSION_TOKEN": "' + "D" * 32 + '"',
        ),
    ),
    "github_user_or_oauth_token": (
        r"gh[pour]_[A-Za-z0-9]{20,255}",
        (
            "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890",
            "gh" + "o_abcdefghijklmnopqrstuvwxyz1234567890",
            "gh" + "u_abcdefghijklmnopqrstuvwxyz1234567890",
            "gh" + "r_abcdefghijklmnopqrstuvwxyz1234567890",
        ),
    ),
    "github_server_token": (
        r"ghs_([A-Za-z0-9]{20,255}|[A-Za-z0-9]+_[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})",
        (
            "gh" + "s_abcdefghijklmnopqrstuvwxyz1234567890",
            "gh"
            + "s_12345_"
            + ".".join(
                (
                    "eyJhbGciOiJSUzI1NiJ9",
                    "eyJpc3MiOiIxMjM0NSJ9",
                    "c2lnbmF0dXJlX3BhcnQ",
                )
            ),
        ),
    ),
    "github_fine_grained_pat": (
        r"github_pat_[A-Za-z0-9_]{20,255}",
        ("github_" + "pat_abcdefghijklmnopqrstuvwxyz_1234567890",),
    ),
    "linear_api_key": (
        r"lin_api_[A-Za-z0-9_]{40,}",
        ("lin_" + "api_abcdefghijklmnopqrstuvwxyz_1234567890_ABCDE",),
    ),
    "openai_key": (
        r"sk-((proj|svcacct)-)?[A-Za-z0-9_-]{20,}",
        (
            "sk-" + "proj-abcDEF0123456789_-abcDEF",
            "sk-" + "svcacct-abcDEF0123456789_-abcDEF",
        ),
    ),
    # algo#1216 R2 round-3 delivery: password/Cookie redaction was
    # incomplete — both are label/format-anchored to avoid false positives.
    "password_assignment": (
        r'''(?i)(?P<keep>[\w-]*password["']?\s*[:=]\s*)("(?:\\.|[^"\\\r\n])+"|'(?:\\.|[^'\\\r\n])+'|[^\s"']+)''',
        (
            'password: x',
            'DB_PASSWORD="a"',
            "my-password: 'b'",
            'ADMIN_PASSWORD=supersecretvalue99',
        ),
    ),
    "cookie_header_value": (
        r'''(?i)(?P<keep>\b(?:Set-)?Cookie:)[^\r\n]+''',
        (
            'Cookie: s=1',
            'Set-Cookie: session=abc123def456; HttpOnly',
        ),
    ),
    # algo#1216 R2 round-7 finding 3788363460: Keeper's own credential
    # formats (Stripe live keys, GCP API keys / OAuth tokens, PEM private
    # keys) had no enforced pattern — synthetic samples survived
    # bounded_excerpt unchanged. All four are format-anchored per the
    # doc's stated design rule. Fixtures are split literals so the
    # fixtures themselves never trip push-protection secret scanners.
    "stripe_live_key": (
        r"(sk|rk)_live_[A-Za-z0-9]{16,}",
        (
            "sk" + "_live_" + "abcdefghijklmnop0123",
            "rk" + "_live_" + "ABCDEFGHIJKLMNOP0123",
        ),
    ),
    "gcp_api_key": (
        r"AIza[0-9A-Za-z_-]{35}",
        ("AIza" + "Sy" + "A" * 33,),
    ),
    "gcp_oauth_access_token": (
        r"ya29\.[A-Za-z0-9_-]{20,}",
        ("ya29" + ".a0AbCdEfGh0123456789_-x",),
    ),
    "pem_private_key_block": (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        (
            "-----BEGIN " + "PRIVATE KEY-----\nMIIEfakefakefakefake\n-----END " + "PRIVATE KEY-----",
            "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEfakefakefakefake\n-----END " + "RSA PRIVATE KEY-----",
        ),
    ),
    "anthropic_key": (
        r"sk-ant-[A-Za-z0-9_-]{40,}",
        ("sk-" + "ant-abcdefghijklmnopqrstuvwxyz_1234567890-ABCDE",),
    ),
    "jwt_base64url": (
        r"eyJ[A-Za-z0-9_\-=]{10,}\.eyJ[A-Za-z0-9_\-=]{10,}\.[A-Za-z0-9_\-=]+",
        ("eyJ" + "abcde-fghijk.eyJlmno_pqrstuv.signature-part",),
    ),
    # admin#1495 finding 3807823274: email/phone are the two customer-PII
    # classes with reliable format anchors; the probe showed both surviving
    # the sanitizer. Stripe webhook secrets were called out in the same
    # finding. Address/free-text PII stays judgment-scoped (documented in
    # state-and-safety.md's Secret/Token Redaction intro).
    "email_address": (
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        ("jane.doe+qa@example.com", "ops-alerts@mail.example.co"),
    ),
    "phone_number": (
        r"\+[1-9]\d{7,14}|\(\d{3}\)[-.\s]?\d{3}[-.\s]\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b",
        ("+14155550123", "(415) 555-0123", "415-555-0123"),
    ),
    "stripe_webhook_secret": (
        r"whsec_[A-Za-z0-9]{16,}",
        ("whsec_" + "abcdefghijklmnop1234",),
    ),
}
_POLICY_PATTERNS = dict(model_policy.REDACTION_PATTERNS)
if set(_POLICY_PATTERNS) != set(REQUIRED_REDACTION_PATTERNS) or any(
    _POLICY_PATTERNS[kind] != pattern
    for kind, (pattern, _samples) in REQUIRED_REDACTION_PATTERNS.items()
):
    raise ValueError(
        "REQUIRED_REDACTION_PATTERNS drifted from"
        " model_policy.REDACTION_PATTERNS — one canonical list"
    )

# This is the complete heading inventory moved out of the former monolithic
# SKILL.md. Exact headings are deliberate: renaming one is a navigation change
# and must update this inventory (or an explicitly supplied JSON manifest).
BUILTIN_EXPECTED_HEADINGS: Mapping[str, tuple[str, ...]] = {
    "SKILL.md": (
        "# Full Autonomy Workflow",
        "## Loading Contract",
        "## Non-Negotiable Invariants",
        "## Mandatory Model Policy",
        "### Claude base: Fable 5 at max",
        "### Claude reviewer: Fable 5.1",
        "### Codex voices: GPT-6 Astra at max/ultra",
        "## Authorization and Entry Routing",
        "## Project Profile and State",
        "## Phase State Machine",
        "## Feedback Identity and Human Roundtrips",
        "## Ownership Transfer Rules",
        "## Validation Before Push",
        "## Completion Semantics",
        "### Blocked-Exit Work Preservation",
        "## Final Rules",
    ),
    "references/project-and-entry.md": (
        "## Resolved Project Profile",
        "### Discovery Order",
        "### `BASE_BRANCH`",
        "### `QUALITY_CHECK_STEPS`",
        "### `DEV_SERVER_FRONTEND` / `DEV_SERVER_BACKEND`",
        "### `PROTECTED_BRANCHES`",
        "### `ISSUE_TRACKER`",
        "### Model-Gate Entry Preflight",
        "## Entry Points",
        "### Entry A: Solve an Issue",
        "### Entry B: Take Over a PR",
        "## Scope Analysis & Skill Selection",
        "### Step 1: Check gstack Availability",
        "### Step 2: Classify Scope from Diff",
        "### Step 3: Classify Change Type",
        "### Step 4: Select Skills via Capability-Gated Matrix",
        "### Step 5: Persist to State",
        "### Adapter Architecture",
        "### Security Model (Autonomous Mode)",
    ),
    "references/merge-readiness.md": (
        "## Phase 4b: Merge Readiness Gate (World-State Checks)",
        "### AC Capture (Entry A step 5 / Entry B step 5)",
        "### Check 1: Deploy-Order Safety (migrations & runtime schema)",
        "### Check 2: Dependency Merge-State (cross-repo & sibling PRs)",
        "### Check 3: AC Conformance",
        "### Check 4: Claims Audit (PR body, comments, docstrings)",
        "### Review-Fix Integrity (tripwire, tier floor, consumer widening)",
        "### Monitor-Loop World-State Refresh (Phase 6 Step 2, step 12 — canonical definition)",
        "### Phase 4b State",
    ),
    "references/phases-1-5.md": (
        "## Phase 1: Plan",
        "## Phase 2: Review the Plan",
        "## Escalation Voice Triggers",
        "## Phase 3: Implement",
        "## Phase 4: Self-Review",
        "## Phase 4a: Security Gate",
        "## Runtime Verification (Advisory — Human QA Downstream)",
        "### `skill_only` Exemption (auto-waived)",
        "### Opt-In Frontend Verification (when user asks)",
        "### Opt-In Backend Verification (when user asks)",
        "### Phase 6 Re-Verification",
        "## Phase 5: Create / Update PR",
        "### PR Body Template (MANDATORY)",
        "### Issue Tracker Enforcement (Conditional on `ISSUE_TRACKER.type`)",
        "### PR labels (Keeper-Dating org repos)",
        "### If no PR exists yet:",
        "### If PR already exists (takeover):",
    ),
    "references/monitor-ci-feedback.md": (
        "## Phase 6: Monitor Loop",
        "### Step 1: Check CI / Check Runs",
        "### Step 2: Check Review Feedback",
        "#### Detect unaddressed human inline threads:",
        "#### Compute unreplied inline comment sets (canonical):",
        "#### Check top-level bot comments:",
        "#### Check bot review summaries:",
        "### Step 3: Check Branch Status",
        "#### Merge Conflict Resolution (Step 3, conflicts branch)",
    ),
    "references/monitor-exit-handoffs.md": (
        "### Phase 6 Session Ownership (cheap orchestrator, pinned workers)",
        "### Step 4: Evaluate Loop Exit",
        "#### MANDATORY VERIFICATION GATE",
        "#### Exit conditions",
        "#### QA handoff (repo-conditional — conditions (a) and (d))",
        "#### Review-roundtrip handoff (condition (c), human feedback only)",
        "#### Draft-PR gate (flip draft → ready on the first clean pass after the grace window)",
        "#### Stable-poll gate (prevents exiting right as Bugbot posts a new comment)",
        "### PHASE_6_SELF_REVIEW (Diff-Scoped Post-Fix Review)",
    ),
    "references/state-and-safety.md": (
        "## State Tracking",
        "## Aborting Mid-Workflow",
        "## Timeout Heuristics",
        "## Secret/Token Redaction",
        "## Completion Signals",
        "## Rules",
    ),
}

# R7 codex #14 / R7.2 codex #4: a real YAML loader (the skill scanner, the
# runtime) rejects inputs that outer-quote matching alone accepts. If this
# validator passes a skill the loader cannot parse, the pinned scanner silently
# omits that package and still exits zero, so an unloadable, UNSCANNED skill
# ships green — verified empirically: a SKILL.md whose frontmatter PyYAML
# rejects drops the scanner's "Total Skills Scanned" count and leaves
# `scan-all --fail-on-findings` at exit 0. The scanner loads frontmatter via
# `python-frontmatter` → PyYAML 6.0.2, so a value it refuses is a silent skip.
# We cannot `import yaml` here (this must run from a bare clone with nothing
# installed), so mirror three PyYAML reject classes, each pinned against
# `frontmatter.loads` in test_validate_package.py:
#   * quoted-scalar escapes — an unknown escape (`"bad\q"`), a stray interior
#     quote (`"a"b"`), or a `\x`/`\u`/`\U` escape resolving to a surrogate or a
#     value past U+10FFFF (`_quoted_scalar_error`);
#   * plain-scalar indicators — an unquoted value opening a non-plain-scalar
#     node: a flow collection, alias, anchor, tag, directive, or block scalar
#     (`_plain_scalar_error`);
#   * raw control characters — any non-printable byte the reader rejects
#     (`_forbidden_control_char` / `_is_yaml_printable`).
# Escape set sourced from PyYAML 6.0.2 `Scanner.ESCAPE_REPLACEMENTS` /
# `Scanner.ESCAPE_CODES`; the printable set from `reader.Reader.NON_PRINTABLE`.
_YAML_DQ_ESCAPES = frozenset("0abtnvfre \"/\\N_LP\t")
_YAML_DQ_HEX = {"x": 2, "u": 4, "U": 8}
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


def _trailing_after_quote_error(rest: str, quote_word: str) -> str | None:
    """None if what follows a closing quote is valid YAML, else a phrase.

    PyYAML strips trailing whitespace and a `#` comment after a closing quote
    (`"x" # note`, `'x'# note`, `"x"\t# c` all load to `x`; verified against
    `frontmatter.loads` -- R7.2 opus #10). A `#` needs NO preceding space
    after a quote: the quote is the token boundary. Anything else after the
    quote (`"x"junk`, `"x" junk`) is a real parse error the loader rejects."""
    stripped = rest.lstrip(" \t")
    if stripped == "" or stripped.startswith("#"):
        return None
    return f"content after the closing {quote_word} quote"


def _quoted_scalar(scalar: str) -> tuple[str | None, str | None]:
    """Return `(inner, None)` for a valid single-line YAML quoted scalar, else
    `(None, phrase)`. `scalar` is already known to open with `'` or `"`. A
    closing quote may be followed by whitespace and/or a `#` comment, which
    PyYAML strips; `inner` is the RAW between-quotes text (escapes undecoded --
    the validator needs it only for presence/name checks, not decoding).
    Matches what PyYAML rejects so the validator gate agrees with the real
    loader; no PyYAML dependency."""
    quote = scalar[0]
    length = len(scalar)
    index = 1
    if quote == "'":
        # Single-quoted: the sole escape is `''` -> `'`; a lone `'` closes.
        while index < length:
            if scalar[index] == "'":
                if index + 1 < length and scalar[index + 1] == "'":
                    index += 2
                    continue
                trailing = _trailing_after_quote_error(scalar[index + 1 :], "single")
                if trailing is not None:
                    return None, trailing
                return scalar[1:index], None
            index += 1
        return None, "an unterminated single-quoted scalar"
    # Double-quoted: validate every backslash escape; the closing quote may be
    # followed only by whitespace and/or a comment.
    while index < length:
        char = scalar[index]
        if char == "\\":
            if index + 1 >= length:
                return None, "a trailing backslash escape"
            marker = scalar[index + 1]
            width = _YAML_DQ_HEX.get(marker)
            if width is not None:
                digits = scalar[index + 2 : index + 2 + width]
                if len(digits) != width or any(d not in _HEXDIGITS for d in digits):
                    return None, f"an invalid \\{marker} hex escape"
                code = int(digits, 16)
                if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
                    # Both refused for the same load-bearing reason: the scanner's
                    # parser (python-frontmatter aliases SafeLoader to libyaml's
                    # CSafeLoader when the C ext is present) raises on a lone
                    # surrogate (U+D800 to U+DFFF) AND on a value past U+10FFFF, so
                    # either skips the package UNSCANNED and the gate must reject
                    # to stay aligned. Trap for an auditor: bare yaml.safe_load
                    # (pure-Python SafeLoader) LOADS a lone surrogate, so probing
                    # with it instead of frontmatter.loads misreads this as
                    # over-rejection. Even without libyaml a lone surrogate has no
                    # UTF-8 encoding, so refusing it stays safe in either env.
                    return None, f"a \\{marker} escape outside the Unicode scalar range"
                index += 2 + width
                continue
            if marker not in _YAML_DQ_ESCAPES:
                return None, f"an unknown escape character \\{marker}"
            index += 2
            continue
        if char == '"':
            trailing = _trailing_after_quote_error(scalar[index + 1 :], "double")
            if trailing is not None:
                return None, trailing
            return scalar[1:index], None
        index += 1
    return None, "an unterminated double-quoted scalar"


def _quoted_scalar_error(scalar: str) -> str | None:
    """The error side of `_quoted_scalar` for callers that only gate (the
    openai.yaml interface check); returns None when the scalar is valid."""
    return _quoted_scalar(scalar)[1]


def _strip_plain_trailing_comment(scalar: str) -> str:
    """Drop a trailing YAML `#` comment from a plain (unquoted) scalar.

    A `#` opens a comment only when preceded by whitespace (`foo # note` ->
    `foo`); `foo#bar` has no comment and is returned unchanged. Verified
    against `frontmatter.loads` (R7.2 opus #10)."""
    match = re.search(r"[ \t]#", scalar)
    if match is None:
        return scalar
    return scalar[: match.start()].rstrip(" \t")


def _is_yaml_printable(code: int) -> bool:
    """True if a decoded character with this code point is in the YAML printable
    set (PyYAML `reader.Reader.NON_PRINTABLE`, negated). A character outside it
    raises `ReaderError` in the real loader, so the whole skill is skipped and
    left UNSCANNED. The two line-break forms (LF/CR) are printable in YAML but
    never survive `splitlines`, so they are intentionally omitted here."""
    return (
        code == 0x09
        or 0x20 <= code <= 0x7E
        or code == 0x85
        or 0xA0 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _forbidden_control_char(text: str) -> int | None:
    """Return the code point of the first non-printable character in `text`,
    else None. A raw control character anywhere in the frontmatter block
    (a key, value, or comment — all read by the YAML reader) is a `ReaderError`
    that skips the package; TAB is the sole allowed control character."""
    for char in text:
        if not _is_yaml_printable(ord(char)):
            return ord(char)
    return None


def _forbidden_yaml_whitespace(text: str) -> int | None:
    """Return the code point of the first Unicode-whitespace character in `text`
    that YAML does NOT accept as separation, else None.

    Pass-4 codex F5: YAML separation whitespace (s-white) is exactly SPACE
    (0x20) and TAB (0x09). Every other code point Python treats as whitespace
    (`str.strip()`/`str.split()` and the `re` module's `\\s` all match the
    Unicode set: NBSP, the Zs space separators, NEL, LS/PS, ...) is CONTENT
    to the YAML reader, never separation. When one lands in a structural
    position (after a `:`, after a closing quote, trailing a plain scalar) the
    real loader raises a Scanner/Parser error and the whole skill is skipped
    UNSCANNED, while this validator's Python-native strip/`\\s` silently
    absorbs it and ACCEPTS - the one-way invariant's under-rejection failure.
    Flag on presence (fail-closed, over-rejection-safe: the rare inside-a-
    quoted-scalar use the reader WOULD accept is also rejected, but no real
    skill frontmatter carries these code points). LF/CR are line breaks split
    away before this scan, so they are excluded here."""
    for char in text:
        if char.isspace() and char not in "\t \r\n":
            return ord(char)
    return None


# YAML indicator characters that cannot open a plain scalar: each introduces a
# non-plain-scalar node (flow collection, alias, anchor, tag, directive, or
# block scalar). Verbatim from the YAML 1.1 c-indicator set minus the three
# context-sensitive ones ('-', '?', ':'), which `_plain_scalar_error` handles
# by their following character.
_PLAIN_SCALAR_INDICATORS = {
    "@": "reserved indicator '@'",
    "`": "reserved indicator '`'",
    "%": "directive indicator '%'",
    ",": "flow indicator ','",
    "[": "flow-sequence indicator '['",
    "]": "flow indicator ']'",
    "{": "flow-mapping indicator '{'",
    "}": "flow indicator '}'",
    "*": "alias indicator '*'",
    "!": "tag indicator '!'",
    "&": "anchor indicator '&'",
    "|": "block-scalar indicator '|'",
    ">": "block-scalar indicator '>'",
}


def _plain_scalar_error(scalar: str) -> str | None:
    """Return a phrase if an unquoted (plain) frontmatter value is not a plain
    YAML string scalar, else None. `scalar` is non-empty and does not open with
    a quote.

    A skill's frontmatter values (`name`, `description`) are plain or quoted
    string scalars. This refuses every unquoted value that opens a
    non-plain-scalar node, and it does so for two reasons that resolve the same
    way — reject:
      * The forms a real loader ALSO rejects (`@bad`, `[bad`, `*a`, `!tag`,
        `| x`, `- x`, a bare `:`) make the scanner skip the package UNSCANNED,
        so they must be caught here (the whole point of R7.2 codex #4).
      * The forms a real loader would ACCEPT as a non-string (`[a, b]` list,
        `{a: b}` map, `!!str x` tagged, `&a v` anchored, a bare `|`/`>` empty
        block) are out of contract for a skill name/description and never
        appear in a real one — refusing them is correct, not over-rejection.
    Confirmed against `frontmatter.loads` (the scanner's own parser) in
    test_validate_package.py."""
    leader = _PLAIN_SCALAR_INDICATORS.get(scalar[0])
    if leader is not None:
        return f"an unquoted value opening with YAML {leader}"
    if scalar[0] in "-?:" and (len(scalar) == 1 or scalar[1] in " \t"):
        # '-', '?', ':' open a plain scalar only when a non-space "safe"
        # character follows; '- ', '? ', ': ', or a bare one is a block/mapping
        # indicator the loader rejects.
        return f"an unquoted value opening with the '{scalar[0]}' indicator"
    if re.search(r":(?:[ \t]|$)", scalar):
        # An interior ': ' (or a trailing ':') turns the remainder into a
        # nested mapping the loader rejects as a plain scalar.
        return "an unquoted ': ' that must be quoted"
    return None


def _parse_frontmatter(skill_text: str) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    lines = skill_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, ["SKILL.md must begin with a YAML frontmatter delimiter (---)"]

    try:
        end_index = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return {}, ["SKILL.md frontmatter is missing its closing --- delimiter"]

    # Pass-4 opus F1: str.splitlines() consumes U+000B/000C/001C/001D/001E as
    # line boundaries, so a per-splitlines-line scan can never see exactly the
    # non-printables the YAML reader raises ReaderError on (package skipped
    # UNSCANNED - the invariant failure this battery exists to prevent). Scan
    # \n-physical lines, which keep those bytes in-line, across the whole
    # block. When the closing delimiter is not \n-physical (itself possible
    # only via a boundary control char), scan to EOF - the offending char is
    # in range either way. This scan strictly subsumes a per-splitlines-line
    # check: splitlines boundaries are a superset of \n, so every splitlines
    # line is a substring of some physical line.
    physical_lines = skill_text.split("\n")
    physical_closing = next(
        (
            index
            for index, line in enumerate(physical_lines[1:], start=1)
            if line.strip() == "---"
        ),
        len(physical_lines),
    )
    for line_number, raw_line in enumerate(
        physical_lines[1:physical_closing], start=2
    ):
        control = _forbidden_control_char(raw_line)
        if control is not None:
            # A raw control character anywhere in the block (key, value, or
            # comment) is a ReaderError that skips the package.
            return {}, [
                f"SKILL.md:{line_number}: frontmatter has a non-printable "
                f"character (U+{control:04X}) the YAML reader rejects"
            ]
        whitespace = _forbidden_yaml_whitespace(raw_line)
        if whitespace is not None:
            # A Unicode whitespace char YAML does not treat as separation
            # (only SPACE/TAB are). In a structural position the real loader
            # raises and skips the package; this validator's strip/\s would
            # absorb it and accept. See _forbidden_yaml_whitespace.
            return {}, [
                f"SKILL.md:{line_number}: frontmatter uses a non-separation "
                f"Unicode whitespace character (U+{whitespace:04X}); YAML "
                "separation whitespace is SPACE or TAB only"
            ]

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines[1:end_index], start=2):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)\s*:\s*(.*)", raw_line)
        if not match:
            errors.append(
                f"SKILL.md:{line_number}: unsupported frontmatter syntax; "
                "use one scalar key per line"
            )
            continue
        key, value = match.groups()
        if key in values:
            errors.append(f"SKILL.md:{line_number}: duplicate frontmatter key {key!r}")
            continue
        scalar = value.strip()
        if scalar[:1] in ("'", '"'):
            inner, quote_error = _quoted_scalar(scalar)
            if quote_error is not None:
                errors.append(
                    f"SKILL.md:{line_number}: frontmatter key {key!r} has "
                    f"{quote_error}"
                )
                continue
            # inner is the comment-stripped between-quotes text (never None
            # once quote_error is None).
            scalar = inner or ""
        elif scalar:
            # An unquoted plain scalar the real skill loader cannot parse would
            # pass a lenient validator while making the scanner skip the
            # package UNSCANNED; refuse exactly what PyYAML refuses. Strip a
            # trailing `#` comment first (PyYAML does), so the check runs on the
            # real value and the stored value matches what the loader sees.
            scalar = _strip_plain_trailing_comment(scalar)
            plain_error = _plain_scalar_error(scalar)
            if plain_error is not None:
                errors.append(
                    f"SKILL.md:{line_number}: frontmatter key {key!r} has "
                    f"{plain_error}"
                )
                continue
        values[key] = scalar

    unknown = sorted(set(values) - ALLOWED_FRONTMATTER_KEYS)
    if unknown:
        errors.append(
            "SKILL.md frontmatter has non-portable key(s): " + ", ".join(unknown)
        )
    missing = sorted(REQUIRED_FRONTMATTER_KEYS - set(values))
    if missing:
        errors.append(
            "SKILL.md frontmatter is missing required key(s): " + ", ".join(missing)
        )
    for key in sorted(REQUIRED_FRONTMATTER_KEYS & set(values)):
        if not values[key]:
            errors.append(f"SKILL.md frontmatter key {key!r} must not be empty")
    if values.get("name") and values["name"] != "autonomy":
        errors.append("SKILL.md frontmatter name must be 'autonomy'")
    return values, errors


def _iter_fence_state(text: str):
    """Yield (line, state) tracking CommonMark's fence-delimiter grammar.

    ``state`` is None for a fence-delimiter line, True inside a fence, and
    False outside; the generator's return value is the EOF fence state
    (True = the text ends inside an unclosed fence). Delimiter rules
    enforced, per CommonMark's line grammar: a fence closes only on a run
    of its own family, at least as long as its opener, carrying nothing
    but whitespace after the run, and indented at most three columns
    (CommonMark's absolute closer rule) when its opener sits at three
    columns or fewer — for deeper, list-nested openers the bound is
    relative, opener indent plus three; a backtick opener whose info
    string contains a backtick is not a fence line at all (tilde info
    strings are unrestricted). Deliberate superset: openers are
    recognized at any absolute indentation, because in-list fences
    measure indent relative to the list item's content column (this
    package legitimately holds fences at four and five columns) and this
    validator does not model list context. Fail direction of that
    superset, disclosed: an over-indented opener that CommonMark would
    read as indented code opens a fence here, so visible prose after it
    classifies as fenced — a hand-authored construction, not one
    regeneration drift produces. Single source of truth for every
    Markdown-context scan in this validator (headings and anchored
    markers alike).
    """

    in_fence = False
    fence_marker = ""
    fence_len = 0
    fence_indent = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        indent_cols = len(line[: len(line) - len(stripped)].expandtabs(4))
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence_match:
            run = fence_match.group(1)
            rest = stripped[len(run) :]
            if not in_fence:
                if run[0] == "~" or "`" not in rest:
                    in_fence = True
                    fence_marker = run[0]
                    fence_len = len(run)
                    fence_indent = indent_cols
                    yield line, None
                    continue
            elif (
                run[0] == fence_marker
                and len(run) >= fence_len
                and not rest.strip()
                and indent_cols
                <= (3 if fence_indent <= 3 else fence_indent + 3)
            ):
                in_fence = False
                fence_marker = ""
                fence_len = 0
                fence_indent = 0
                yield line, None
                continue
        yield line, in_fence
    return in_fence


def _scan_fence_states(text: str):
    """Materialize ``_iter_fence_state`` rows plus the EOF fence state.

    Returns ``(rows, ends_open)``: the full (line, state) sequence and
    whether the text ends inside an unclosed fence — the signature of a
    truncated regeneration, which must fail validation rather than
    silently reclassify the tail of the file.
    """

    generator = _iter_fence_state(text)
    rows = []
    while True:
        try:
            rows.append(next(generator))
        except StopIteration as stop:
            return rows, bool(stop.value)


def _markdown_headings(text: str) -> set[str]:
    headings: set[str] = set()
    rows, _ends_open = _scan_fence_states(text)
    for line, state in rows:
        if state is False and re.fullmatch(r"#{1,6}\s+.+", line):
            headings.add(line.rstrip())
    return headings


def _package_policy_files(package_dir: Path) -> list[Path]:
    files = [package_dir / "SKILL.md"]
    files.extend(sorted((package_dir / "references").glob("**/*.md")))
    files.extend(sorted((package_dir / "agents").glob("**/*.yaml")))
    files.extend(sorted((package_dir / "agents").glob("**/*.yml")))
    return [path for path in files if path.is_file()]


def _extract_direct_interface_scalar(text: str, key: str) -> list[str]:
    """Extract only direct two-space children of the root interface mapping."""

    values: list[str] = []
    lines = text.splitlines()
    # Pattern string, not re.compile (admin#1495 r18 F4; see
    # _loader_collection_error's structural rule).
    pattern = rf"^  {re.escape(key)}\s*:\s*(.*)$"
    for index, line in enumerate(lines):
        match = re.match(pattern, line)
        if match is None:
            continue
        value = match.group(1).strip()
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block_lines: list[str] = []
            for continuation in lines[index + 1 :]:
                if not continuation.strip():
                    block_lines.append("")
                    continue
                indentation = len(continuation) - len(continuation.lstrip())
                if indentation <= 2:
                    break
                block_lines.append(continuation.strip())
            value = "\n".join(block_lines).strip()
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values.append(value)
    return values


def _direct_interface_quote_errors(text: str, keys: Iterable[str]) -> list[str]:
    """Reject direct interface scalars the real YAML loader would refuse:
    unterminated, an unknown escape, or a stray interior quote. Shares the
    SKILL.md frontmatter rule via `_quoted_scalar_error` so both quoted-scalar
    gates in this file agree with PyYAML rather than drifting (R7 codex #14)."""

    errors: list[str] = []
    for key in keys:
        # Pattern string, not re.compile (admin#1495 r18 F4; see
        # _loader_collection_error's structural rule).
        pattern = rf"^  {re.escape(key)}\s*:\s*(.*)$"
        for line in text.splitlines():
            match = re.match(pattern, line)
            if match is None:
                continue
            value = match.group(1).strip()
            if not value or value[0] not in "'\"":
                continue
            quote_error = _quoted_scalar_error(value)
            if quote_error is not None:
                errors.append(
                    f"agents/openai.yaml interface.{key} has {quote_error}"
                )
    return errors


def _validate_references(
    package_dir: Path,
    expected_headings: Mapping[str, Sequence[str]],
) -> list[str]:
    errors: list[str] = []
    package_root = package_dir.resolve()

    for required_file in REQUIRED_REFERENCE_FILES:
        required_path = package_dir / required_file
        if not required_path.is_file():
            errors.append(f"missing required reference file: {required_file}")
            continue
        line_count = len(required_path.read_text(encoding="utf-8").splitlines())
        if line_count >= MAX_REFERENCE_LINES_EXCLUSIVE:
            errors.append(
                f"{required_file} has {line_count} lines; required phase references "
                f"must stay below {MAX_REFERENCE_LINES_EXCLUSIVE}"
            )

    for relative_path, headings in sorted(expected_headings.items()):
        candidate = (package_dir / relative_path).resolve()
        try:
            candidate.relative_to(package_root)
        except ValueError:
            errors.append(
                f"heading inventory path escapes the skill package: {relative_path}"
            )
            continue
        if not candidate.is_file():
            if relative_path not in REQUIRED_REFERENCE_FILES:
                errors.append(f"missing heading target file: {relative_path}")
            continue
        heading_text = candidate.read_text(encoding="utf-8")
        # CR 3760684106: an unclosed fence corrupts heading classification
        # for everything after it — report it for EVERY heading-scanned
        # file, not only anchored-marker files.
        _heading_rows, heading_ends_open = _scan_fence_states(heading_text)
        if heading_ends_open:
            errors.append(f"{relative_path}: unclosed code fence at end of file")
        actual_headings = _markdown_headings(heading_text)
        expected_heading_set = set(headings)
        for heading in headings:
            if heading not in actual_headings:
                errors.append(f"{relative_path}: missing exact heading {heading!r}")
        for heading in sorted(actual_headings - expected_heading_set):
            errors.append(
                f"{relative_path}: unexpected heading {heading!r}; "
                "add it to BUILTIN_EXPECTED_HEADINGS in scripts/validate_package.py"
            )
    return errors


def _validate_gate_markers(package_dir: Path) -> list[str]:
    """Require every evidence-gate/state-hardening marker in its named file."""
    errors: list[str] = []
    for relative_path, markers in sorted(REQUIRED_GATE_MARKERS.items()):
        candidate = package_dir / relative_path
        if not candidate.is_file():
            # Missing reference/skill files are reported by their own checks;
            # do not duplicate that error here.
            continue
        text = candidate.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                errors.append(
                    f"{relative_path}: missing required gate marker {marker!r}"
                )
    return errors


def _validate_py_bindings(package_dir: Path) -> list[str]:
    """Require every REQUIRED_PY_BINDINGS statement as an OPERATIVE source line.

    A whole line must carry exactly the binding — leading indentation and a
    trailing ``# comment`` aside — so the same text parked in a comment or
    embedded in other code does NOT satisfy it. That closes the substring
    bypass a plain ``marker in text`` check leaves open (R7.2 codex #9)."""
    errors: list[str] = []
    for relative_path, statements in sorted(REQUIRED_PY_BINDINGS.items()):
        candidate = package_dir / relative_path
        if not candidate.is_file():
            # Missing script files are reported by their own checks.
            continue
        text = candidate.read_text(encoding="utf-8")
        for statement in statements:
            pattern = (
                r"^[ \t]*" + re.escape(statement) + r"[ \t]*(?:#.*)?$"
            )
            if re.search(pattern, text, re.MULTILINE) is None:
                errors.append(
                    f"{relative_path}: binding {statement!r} must appear as an "
                    "operative source line (not a comment or substring)"
                )
    return errors


def _validate_policy_text(package_dir: Path) -> list[str]:
    # Package-level contracts only — the current policy's exact flag strings and
    # floor model must appear somewhere in the package text. Per-line prose
    # linting was deliberately dropped in review: its exemption rules could not
    # be applied consistently, and the derived pins above are the real guard.
    errors: list[str] = []
    combined_parts: list[str] = []
    for path in _package_policy_files(package_dir):
        combined_parts.append(path.read_text(encoding="utf-8"))

    combined = "\n".join(combined_parts)
    if EXEC_MODEL_FLAGS not in combined:
        errors.append("missing exact codex exec flags: " + EXEC_MODEL_FLAGS)
    if REVIEW_MODEL_FLAGS not in combined:
        errors.append("missing exact codex review flags: " + REVIEW_MODEL_FLAGS)
    if EXEC_RESUME_SHAPE not in combined:
        errors.append(
            "missing exact codex exec-resume shape (flags BEFORE the resume"
            " subcommand): " + EXEC_RESUME_SHAPE
        )
    if CODEX_FLOOR_MODEL not in combined:
        errors.append("missing documented codex floor model: " + CODEX_FLOOR_MODEL)
    state_path = package_dir / "references" / "state-and-safety.md"
    if state_path.is_file():
        state_text = state_path.read_text(encoding="utf-8")
        for kind, (pattern, samples) in REQUIRED_REDACTION_PATTERNS.items():
            if f"`{pattern}`" not in state_text:
                errors.append(
                    f"missing current redaction pattern for {kind}: {pattern}"
                )
                continue
            for sample in samples:
                # Direct re.fullmatch, not re.compile (admin#1495 r18 F4;
                # see _loader_collection_error's structural rule).
                if re.fullmatch(pattern, sample) is None:
                    errors.append(
                        f"redaction pattern for {kind} does not match its fixture"
                    )
                    break
    return errors


def _validate_openai_yaml(package_dir: Path) -> list[str]:
    path = package_dir / "agents" / "openai.yaml"
    if not path.is_file():
        return ["missing required agents/openai.yaml"]

    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    lines = text.splitlines()
    root_entries = [
        (index, line.strip())
        for index, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace()
        and line.strip() not in {"---", "..."}
    ]
    interface_entries = [
        index for index, value in root_entries if value == "interface:"
    ]
    if len(interface_entries) != 1 or len(root_entries) != 1:
        errors.append(
            "agents/openai.yaml must contain exactly one root interface mapping"
        )

    errors.extend(
        _direct_interface_quote_errors(
            text, ("display_name", "short_description", "default_prompt")
        )
    )

    display_names = _extract_direct_interface_scalar(text, "display_name")
    short_descriptions = _extract_direct_interface_scalar(text, "short_description")
    default_prompts = _extract_direct_interface_scalar(text, "default_prompt")

    if len(display_names) != 1 or not display_names[0].strip():
        errors.append(
            "agents/openai.yaml must contain exactly one non-empty interface.display_name"
        )

    if len(short_descriptions) != 1 or not short_descriptions[0].strip():
        errors.append(
            "agents/openai.yaml must contain exactly one non-empty interface.short_description"
        )
    if len(default_prompts) != 1 or not default_prompts[0].strip():
        errors.append(
            "agents/openai.yaml must contain exactly one non-empty interface.default_prompt"
        )
    elif "$autonomy" not in default_prompts[0]:
        errors.append(
            "agents/openai.yaml default_prompt must mention $autonomy"
        )
    return errors


def _anchored_candidate(line: str, *, fenced: bool, in_fence: bool) -> str | None:
    """Return the anchor-comparable form of an operative line, else None.

    ``fenced`` False accepts ordinary prose only: a line inside a fenced
    block or indented four-plus columns (Markdown code — tabs advance to
    4-column stops) is display content, not the operative condition, and
    one leading list marker is stripped so a renderer-equivalent bullet
    swap cannot dodge the anchor. ``fenced`` True accepts fenced lines
    only — the pseudocode's operative form.
    """

    if fenced != in_fence:
        return None
    stripped = line.lstrip()
    if not fenced:
        indent = line[: len(line) - len(stripped)]
        if len(indent.expandtabs(4)) >= 4:
            return None
        for prefix in _LIST_MARKER_PREFIXES:
            if stripped.startswith(prefix):
                return stripped[len(prefix):]
    return stripped


# Pattern string, not a compiled object: no re.compile call may exist in
# this file (admin#1495 r18 F4; see _loader_collection_error's structural
# rule). The re module's own pattern cache keeps this cost-free.
_INLINE_HTML_COMMENT = r"<!--.*?-->"


def _strip_inline_html_comments(line: str) -> str:
    """Display text of a prose line: inline ``<!-- ... -->`` spans render as
    nothing, and a trailing unclosed opener hides the rest of the line.

    R6-F16: the required-substring check must run against this display text
    for prose anchors - a required clause hidden inside an inline comment on
    the anchor line passes a raw-line substring search while rendering as
    absent, the same decoy family the block-comment scan already rejects.
    """

    stripped = re.sub(_INLINE_HTML_COMMENT, "", line)
    open_index = stripped.rfind("<!--")
    if open_index != -1:
        stripped = stripped[:open_index]
    return stripped


def _anchored_matches(
    text: str,
    anchor: str,
    *,
    fenced: bool,
    rows: list[tuple[str, bool | None]] | None = None,
) -> list[str]:
    """Collect operative lines matching an anchor in its declared context.

    Fence state comes from ``_iter_fence_state`` (the heading scanner's
    own tracker, enforcing CommonMark's delimiter line grammar), so mixed
    ```/~~~ constructions, shorter runs, info-string-bearing or
    over-indented "closers", and backtick-info fakes cannot
    desynchronize classification. Outside fences, HTML-comment blocks are
    display content: a line inside an unclosed ``<!--`` never matches.
    """

    matches: list[str] = []
    in_comment = False
    if rows is None:
        # CR 3760684113: callers that already scanned pass their rows;
        # standalone callers keep the self-contained scan.
        rows, _ends_open = _scan_fence_states(text)
    for line, state in rows:
        if state is None:
            continue  # fence delimiter
        if state is False:
            if in_comment:
                if "-->" in line:
                    in_comment = False
                continue
            comparable = _anchored_candidate(line, fenced=fenced, in_fence=False)
            if comparable is not None and comparable.startswith(anchor):
                matches.append(line)
            open_index = line.rfind("<!--")
            if open_index != -1 and "-->" not in line[open_index:]:
                in_comment = True
            continue
        comparable = _anchored_candidate(line, fenced=fenced, in_fence=True)
        if comparable is not None and comparable.startswith(anchor):
            matches.append(line)
    return matches


def _validate_anchored_markers(package_dir: Path) -> list[str]:
    """Bind required substrings to the single operative anchored line.

    Presence markers are file-wide substrings; this check is what makes a
    placement claim true — reverting an anchored condition line while
    parking its marker text elsewhere fails here, including decoys parked
    in fenced blocks of either family (or behind a delimiter run that is
    shorter than its opener, carries an info string, or is over-indented
    relative to its opener — none of which close a fence), after a
    backtick opener whose info string contains a backtick (not a fence
    line at all), under swapped list markers, in code indented four-plus
    columns by spaces or tabs, or inside HTML-comment blocks; a file
    ending inside an open fence fails outright. Boundary: this
    defends regeneration drift and relocation through those Markdown
    constructs; it does not bind an adversary who edits the validator.
    """

    errors: list[str] = []
    for relative_path, anchor_specs in sorted(REQUIRED_ANCHORED_MARKERS.items()):
        candidate = package_dir / relative_path
        if not candidate.is_file():
            # Missing reference files are reported by their own checks.
            continue
        text = candidate.read_text(encoding="utf-8")
        scanned_rows, ends_open = _scan_fence_states(text)
        if ends_open:
            errors.append(
                f"{relative_path}: unclosed code fence at end of file"
            )
        for anchor, fenced, required in anchor_specs:
            matches = _anchored_matches(
                text, anchor, fenced=fenced, rows=scanned_rows
            )
            if len(matches) != 1:
                errors.append(
                    f"{relative_path}: expected exactly one operative line"
                    f" anchored by {anchor!r}, found {len(matches)}"
                )
                continue
            # R6-F16: prose anchors are checked against their DISPLAY text —
            # a clause hidden in an inline HTML comment renders as nothing
            # and must not satisfy the check. Fenced anchors keep the raw
            # line: inside a code fence, comment syntax is literal content.
            haystack = (
                matches[0] if fenced else _strip_inline_html_comments(matches[0])
            )
            for substring in required:
                if substring not in haystack:
                    errors.append(
                        f"{relative_path}: anchored line {anchor!r} is"
                        f" missing required text {substring!r}"
                    )
    return errors


def validate_package(package_dir: Path) -> list[str]:
    """Return every validation error for ``package_dir`` in stable order."""

    package_dir = package_dir.resolve()
    skill_path = package_dir / "SKILL.md"
    if not skill_path.is_file():
        return [f"missing SKILL.md in {package_dir}"]

    expected_headings = BUILTIN_EXPECTED_HEADINGS

    skill_text = skill_path.read_text(encoding="utf-8")
    errors: list[str] = []
    _, frontmatter_errors = _parse_frontmatter(skill_text)
    errors.extend(frontmatter_errors)

    line_count = len(skill_text.splitlines())
    if line_count >= MAX_SKILL_LINES_EXCLUSIVE:
        errors.append(
            f"SKILL.md has {line_count} lines; it must stay below "
            f"{MAX_SKILL_LINES_EXCLUSIVE}"
        )

    errors.extend(_validate_references(package_dir, expected_headings))
    for required_file in REQUIRED_SCRIPT_FILES:
        if not (package_dir / required_file).is_file():
            errors.append(f"missing required script file: {required_file}")
    errors.extend(_validate_test_collection(package_dir))
    license_path = package_dir / REQUIRED_LICENSE_FILE
    try:
        license_text = license_path.read_text(encoding="utf-8")
    except OSError:
        errors.append(
            "LICENSE: missing — the pinned upstream source is MIT-licensed"
            " and requires its notice in copies or substantial portions"
            " (admin#1495 r15 F12); vendor the upstream LICENSE file"
        )
    else:
        for marker in (
            "MIT License",
            "Permission is hereby granted, free of charge",
        ):
            if marker not in license_text:
                errors.append(
                    f"LICENSE: does not carry the MIT notice ({marker!r}"
                    " absent) — admin#1495 r15 F12"
                )
    errors.extend(_validate_policy_text(package_dir))
    errors.extend(_validate_gate_markers(package_dir))
    errors.extend(_validate_py_bindings(package_dir))
    errors.extend(_validate_anchored_markers(package_dir))
    errors.extend(_validate_openai_yaml(package_dir))
    errors.extend(_validate_entry_points(package_dir))
    errors.extend(_validate_size_boundary_parity(package_dir))
    return errors


def _validate_size_boundary_parity(package_dir: Path) -> list[str]:
    """algo#1216 r16 F5 + admin#1495 r12 F8: boundary constants defined
    independently in the runner and the schema CLI carry mirror comments
    but had no guard. Compare each pair's assignment literals TEXTUALLY:
    the isolated runner must stay import-free of the schema module, so
    the parity guard lives here."""

    pairs = (
        (
            "size-boundary parity",
            "MAX_CANDIDATE_BYTES",
            "STATE_READ_CEILING_BYTES",
            "the schema read ceiling must mirror the runner candidate cap"
            " exactly",
        ),
        (
            "work-cap parity",
            "MAX_WORK_ITERATIONS",
            "MAX_WORK_ITERATIONS",
            "the schema's immutable work cap must mirror the runner's"
            " enforced cap exactly",
        ),
    )
    errors: list[str] = []
    sources: dict[str, str] = {}
    for rel_path in ("scripts/monitor_runner.py", "scripts/state_schema.py"):
        path = package_dir / rel_path
        if not path.is_file():
            errors.append(f"constant parity: missing {rel_path}")
            continue
        sources[rel_path] = path.read_text(encoding="utf-8")
    for label, runner_name, schema_name, requirement in pairs:
        values: list[str] = []
        for rel_path, constant in (
            ("scripts/monitor_runner.py", runner_name),
            ("scripts/state_schema.py", schema_name),
        ):
            text = sources.get(rel_path)
            if text is None:
                continue
            match = re.search(
                rf"^{constant} = (.+)$", text, re.MULTILINE
            )
            if match is None:
                errors.append(
                    f"{label}: no {constant} assignment found in {rel_path}"
                )
                continue
            values.append(match.group(1).strip())
        if len(values) == 2 and values[0] != values[1]:
            errors.append(
                f"{label}: monitor_runner.{runner_name} ({values[0]}) !="
                f" state_schema.{schema_name} ({values[1]}) — {requirement}"
            )
    return errors


def _workflow_event_paths(text: str, event: str) -> list[str]:
    """Narrow std-lib structural read of a workflow's ``on.<event>.paths``
    entries (admin#1495 r12 F20, generalized to both events by r13 F13).
    Deliberately NARROW: a top-level ``on:`` mapping, a block-form
    ``<event>:`` child, a block-form ``paths:`` child, and ``- item``
    entries — comment lines and inline `` #`` comments stripped, quotes
    unwrapped. Flow-form or otherwise unrecognized shapes yield no
    entries and fail closed at the caller (use block form)."""

    def _operative(raw: str) -> str:
        if raw.lstrip().startswith("#"):
            return ""
        if " #" in raw:
            raw = raw.split(" #", 1)[0]
        return raw.rstrip()

    paths: list[str] = []
    in_on = False
    on_child_indent: int | None = None
    in_pull_request = False
    pr_child_indent: int | None = None
    in_paths = False
    for raw in text.splitlines():
        line = _operative(raw)
        if not line.strip():
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_on = stripped in ("on:", '"on":', "'on':")
            on_child_indent = None
            in_pull_request = False
            pr_child_indent = None
            in_paths = False
            continue
        if not in_on:
            continue
        if on_child_indent is None:
            on_child_indent = indent
        if indent == on_child_indent:
            in_pull_request = stripped == event + ":"
            pr_child_indent = None
            in_paths = False
            continue
        if not in_pull_request:
            continue
        if pr_child_indent is None:
            pr_child_indent = indent
        if indent == pr_child_indent:
            in_paths = stripped == "paths:"
            continue
        if in_paths and indent > pr_child_indent and stripped.startswith("- "):
            paths.append(stripped[2:].strip().strip("'\""))
    return paths


def _covers_path(entries: list[str], target: str) -> bool:
    """True when a structural paths list covers ``target`` — exactly, as
    ``target/**``, or through a ``prefix/**`` glob ancestor."""

    for entry in entries:
        if entry == target or entry == target + "/**":
            return True
        if entry.endswith("/**") and target.startswith(entry[:-3] + "/"):
            return True
    return False


def _covers_symlink_blob(entries: list[str], target: str) -> bool:
    """True when the filter matches ``target`` AS A FILE PATH — the shape
    a symlink change presents to GitHub's paths filter (admin#1495 r14
    F8). ``target/**`` deliberately does NOT count: it matches only
    descendants, never the bare blob."""

    for entry in entries:
        if entry == target:
            return True
        if entry.endswith("/**") and target.startswith(entry[:-3] + "/"):
            return True
    return False


def _strip_html_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` spans (inline and multi-line) so delegation
    checks see only operative Markdown (admin#1495 r12 F20)."""

    operative_lines: list[str] = []
    open_comment = False
    for raw_line in text.splitlines():
        piece = raw_line
        while True:
            if open_comment:
                end = piece.find("-->")
                if end == -1:
                    piece = ""
                    break
                piece = piece[end + 3:]
                open_comment = False
            start = piece.find("<!--")
            if start == -1:
                break
            end = piece.find("-->", start + 4)
            if end == -1:
                piece = piece[:start]
                open_comment = True
                break
            piece = piece[:start] + piece[end + 3:]
        operative_lines.append(piece)
    return "\n".join(operative_lines)


def _strip_fenced_code_blocks(text: str) -> str:
    """Remove fenced code blocks - delimiters and contents - so delegation
    checks see only operative Markdown (mm#3551 dawid-r8 F12): a decoy
    link inside a fence renders as literal text, never as a link, yet
    satisfied the r14 F10 link parse. Classification comes from
    _iter_fence_state (this validator's single fence tracker, both
    CommonMark families), and the pass runs BEFORE HTML-comment
    stripping for the R6-F16 reason: inside a fence, comment syntax is
    literal content, so fenced text must never reach the comment pass.
    Fail direction: this pass only ever WITHHOLDS text from the link
    scan (an unclosed fence withholds the tail; the tracker's one
    documented divergence classifies MORE as fenced), so a mis-parse
    can fail a real delegation loudly, never admit a fenced decoy."""

    rows, _ends_open = _scan_fence_states(text)
    return "\n".join(line for line, state in rows if state is False)


# Pattern string, not a compiled object: no re.compile call may exist in
# this file (admin#1495 r18 F4; see _loader_collection_error's structural
# rule). The re module's own pattern cache keeps this cost-free.
_MD_LINK_TARGET = r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?\s*\)"


def _delegates_to_autonomy(text: str, source: Path, package_dir: Path) -> bool:
    """True when ``text``'s operative Markdown (fenced code blocks,
    then HTML comments, stripped - mm#3551 dawid-r8 F12) contains a
    STRUCTURALLY PARSED link whose target resolves to the canonical
    package (admin#1495 r14 F10). The r13 predicate accepted
    any substring mention, so ``../evil-autonomy/SKILL.md`` (a suffix
    lookalike) and prose that NAMES the path while refusing to follow it
    ("Do not follow ../autonomy/SKILL.md; run the legacy workflow") both
    passed. Delegation is now source-aware: a markdown link target,
    resolved from the SOURCE file's real directory (symlinked roots
    resolve physically first, so a `.cursor` alias root reaches the same
    sibling as the real root), must equal the package directory or its
    SKILL.md. Both canonical link forms resolve — the sibling-relative
    ``../autonomy/SKILL.md`` a superseded root carries and the
    repo-rooted ``../../.agents/skills/autonomy/SKILL.md`` the command
    pointers carry. Bare-path mentions, prefix/suffix lookalikes,
    negated prose, commented links (still stripped, finding
    3813789192), and fence-parked decoy links (mm#3551 dawid-r8 F12)
    all fail."""

    operative = _strip_html_comments(_strip_fenced_code_blocks(text))
    try:
        source_dir = source.parent.resolve()
        package_real = package_dir.resolve()
    except OSError:
        return False
    for match in re.finditer(_MD_LINK_TARGET, operative):
        target = match.group(1)
        if "://" in target or target.startswith(("mailto:", "#")):
            continue
        try:
            resolved = (source_dir / target).resolve()
        except OSError:
            continue
        if resolved in (package_real, package_real / "SKILL.md"):
            return True
    return False


def _validate_entry_points(package_dir: Path) -> list[str]:
    """Reject non-delegating legacy autonomy entry points in the host repo.

    admin#1495 finding 3813789192: replacing the legacy skill with a
    delegating alias left `.cursor/commands/autonomous-*.md` still driving
    the OLD state machine (its own state file, direct `git push`/`gh pr
    create`) — a Cursor user invoking one bypassed every write-ahead,
    merge-readiness, review, and monitored-handoff gate while believing
    they ran the canonical workflow. Every discoverable autonomy entry
    point outside this package must either be gone or visibly delegate to
    it. The scan walks up from the package to the repository root (the
    directory whose `.cursor`/`.agents` could shadow this package); repos
    without such surfaces no-op.
    """

    errors: list[str] = []
    root = package_dir.resolve()
    for candidate in (root.parent, root.parent.parent, root.parent.parent.parent):
        if candidate == candidate.parent:
            break
        if (candidate / ".git").exists():
            commands_dir = candidate / ".cursor" / "commands"
            if commands_dir.is_dir():
                for entry in sorted(commands_dir.glob("autonomous-*.md")):
                    try:
                        content = entry.read_text(encoding="utf-8")
                    except OSError:
                        errors.append(
                            f"unreadable legacy entry point: {entry}"
                        )
                        continue
                    # admin#1495 r12 F20: delegation must be OPERATIVE text
                    # — a mention inside an HTML comment reads as compliant
                    # to a substring check while delegating nothing. r14
                    # F10: and it must be a structurally parsed link
                    # resolving to the exact package, source-aware.
                    if not _delegates_to_autonomy(content, entry, package_dir):
                        errors.append(
                            "legacy autonomy entry point does not delegate"
                            f" to the canonical package: {entry} — make it"
                            " a thin pointer at the autonomy skill or"
                            " remove it (finding 3813789192; HTML comments"
                            " do not count)"
                        )
            # mm#3551 dawid-r8 F6: `.claude/commands` carries the same
            # autonomy command aliases one root over - R7 F2's
            # partial-coverage class. Each existing alias must delegate
            # under exactly the `.cursor` rule above; absent aliases
            # stay legal.
            claude_commands_dir = candidate / ".claude" / "commands"
            claude_command_aliases = (
                sorted(claude_commands_dir.glob("autonomous-*.md"))
                if claude_commands_dir.is_dir()
                else []
            )
            for entry in claude_command_aliases:
                try:
                    content = entry.read_text(encoding="utf-8")
                except OSError:
                    errors.append(
                        f"unreadable legacy entry point: {entry}"
                    )
                    continue
                if not _delegates_to_autonomy(content, entry, package_dir):
                    errors.append(
                        "legacy autonomy entry point does not delegate"
                        f" to the canonical package: {entry} - make it"
                        " a thin pointer at the autonomy skill or"
                        " remove it (mm#3551 dawid-r8 F6, per finding"
                        " 3813789192; HTML comments and fenced code"
                        " blocks do not count)"
                    )
            # admin#1495 finding 3816225750 / algo r13 F5: the trigger
            # wiring is required UNCONDITIONALLY whenever the workflow
            # exists — gating it on .cursor/commands presence meant the
            # FIRST legacy-command addition in a repo without the
            # directory (algo) never started the run that would reject
            # it. The absent-directory → first-command transition is
            # exactly what the trigger must catch.
            workflow = (
                candidate
                / ".github"
                / "workflows"
                / "skill-package-checks.yml"
            )
            if workflow.is_file():
                try:
                    workflow_text = workflow.read_text(encoding="utf-8")
                except OSError:
                    workflow_text = ""
                event_paths = {
                    event: _workflow_event_paths(workflow_text, event)
                    for event in ("pull_request", "push")
                }
                # admin#1495 r13 F13: BOTH event filters carry the guarded
                # paths, and a workflow that CONSUMES .gitignore (the
                # check-ignore visibility pin) must also trigger on it —
                # a gitignore-only regression otherwise never runs the pin.
                required_paths = [".cursor/commands/autonomous-*.md"]
                if "check-ignore" in workflow_text:
                    required_paths.append(".gitignore")
                for event, paths in event_paths.items():
                    for needed in required_paths:
                        if not _covers_path(paths, needed) and needed not in paths:
                            errors.append(
                                f"skill-package-checks.yml {event} paths do"
                                f" not include {needed} under structural"
                                f" on.{event}.paths — a YAML comment, a"
                                " wrong key, or a wrong indent does not"
                                " count (findings 3816225750 / r13 F5;"
                                " admin#1495 r12 F20 / r13 F13)"
                            )
                # mm#3551 dawid-r8 F6: when `.claude/commands` autonomy
                # aliases exist, both event filters must cover their
                # glob too - an alias-only edit otherwise never reruns
                # the delegation scan above. Absent aliases stay legal
                # (the presence-gated shape of the r13 F10 legacy-root
                # demand below, not the unconditional `.cursor` demand),
                # so finding 3816225750's first-addition window applies
                # to this surface: a repo's very first alias lands
                # unflagged until the next validator run reports it.
                if claude_command_aliases:
                    claude_glob = ".claude/commands/autonomous-*.md"
                    for event, paths in event_paths.items():
                        if (
                            not _covers_path(paths, claude_glob)
                            and claude_glob not in paths
                        ):
                            errors.append(
                                f"skill-package-checks.yml {event} paths"
                                f" do not include {claude_glob} under"
                                f" structural on.{event}.paths - a YAML"
                                " comment, a wrong key, or a wrong"
                                " indent does not count (mm#3551"
                                " dawid-r8 F6)"
                            )
            else:
                workflow_text = ""
                event_paths = {"pull_request": [], "push": []}
            # admin#1495 r13 F10: the superseded autonomous-workflow skill
            # roots are entry points too — a non-delegating or retargeted
            # copy at either root validated cleanly. Each EXISTING root
            # must visibly delegate to the canonical autonomy package
            # (operative text, HTML comments stripped), and when any root
            # exists the CI filters must cover it on both events.
            legacy_workflow_roots = tuple(
                root
                for root in (
                    candidate / ".claude" / "skills" / "autonomous-workflow",
                    candidate / ".agents" / "skills" / "autonomous-workflow",
                    # admin#1495 r14 F8: the tracked Cursor root is a live
                    # discovery surface too — a root-only retarget or
                    # replacement there restored a nondelegating workflow
                    # without ever running these checks.
                    candidate / ".cursor" / "skills" / "autonomous-workflow",
                )
                if root.is_symlink() or root.exists()
            )
            for legacy_root in legacy_workflow_roots:
                legacy_skill = legacy_root / "SKILL.md"
                if not legacy_skill.is_file():
                    errors.append(
                        f"legacy autonomous-workflow root {legacy_root} has"
                        " no readable SKILL.md — a dangling or emptied"
                        " alias still discovers (admin#1495 r13 F10)"
                    )
                    continue
                try:
                    legacy_text = legacy_skill.read_text(encoding="utf-8")
                except OSError:
                    errors.append(
                        f"unreadable legacy skill root: {legacy_skill}"
                    )
                    continue
                if not _delegates_to_autonomy(
                    legacy_text, legacy_skill, package_dir
                ):
                    errors.append(
                        "legacy autonomous-workflow root does not delegate"
                        f" to the canonical package: {legacy_skill}"
                        " (admin#1495 r13 F10 / r14 F10: a structurally"
                        " parsed link resolving to the exact package; HTML"
                        " comments, bare mentions, and lookalike paths do"
                        " not count)"
                    )
            if legacy_workflow_roots and workflow.is_file():
                for event, paths in event_paths.items():
                    for legacy_root in legacy_workflow_roots:
                        rel_root = legacy_root.relative_to(candidate).as_posix()
                        if not _covers_path(paths, rel_root):
                            errors.append(
                                f"skill-package-checks.yml {event} paths do"
                                f" not cover {rel_root} — a root-only"
                                " change bypasses the delegation guard"
                                " (admin#1495 r13 F10)"
                            )
                        # admin#1495 r14 F8: a SYMLINK root changes as its
                        # own bare blob path, which "root/**" never
                        # matches in GitHub's filter — a retarget slides
                        # past CI unless the exact path (or a strictly
                        # shorter ancestor glob) is present.
                        if legacy_root.is_symlink() and not _covers_symlink_blob(
                            paths, rel_root
                        ):
                            errors.append(
                                f"skill-package-checks.yml {event} paths do"
                                f" not match the bare symlink path"
                                f" {rel_root} — \"{rel_root}/**\" never"
                                " matches the symlink blob itself, so a"
                                " retarget bypasses CI; add the exact path"
                                " or an ancestor glob (admin#1495 r14 F8)"
                            )
            # admin#1495 r14 F11 (alongside r14 F8): the RETIRED
            # autonomous-workflow interfaces must not return, each
            # rejected independently. Only the five retired workflow*
            # script keys and the one retired shell path are banned —
            # unrelated scripts (current or future) are untouched.
            retired_shell = (
                candidate
                / ".cursor"
                / "ralph-scripts"
                / "autonomous-workflow.sh"
            )
            if retired_shell.is_symlink() or retired_shell.exists():
                errors.append(
                    f"retired legacy shell reintroduced: {retired_shell} —"
                    " the ralph autonomous-workflow entry bypassed every"
                    " canonical gate and was removed; delete it"
                    " (admin#1495 r14 F11)"
                )
            package_json = candidate / "package.json"
            if package_json.is_file():
                retired_keys = {
                    "workflow",
                    "workflow:status",
                    "workflow:init",
                    "workflow:poll",
                    "workflow:clean",
                }
                try:
                    manifest = json.loads(
                        package_json.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    errors.append(
                        f"unreadable or unparseable {package_json} — the"
                        " retired-interface check cannot rule the five"
                        " workflow* script keys out (admin#1495 r14 F11)"
                    )
                else:
                    scripts_map = (
                        manifest.get("scripts")
                        if isinstance(manifest, dict)
                        else None
                    )
                    if isinstance(scripts_map, dict):
                        reintroduced = sorted(
                            retired_keys & set(scripts_map)
                        )
                        if reintroduced:
                            errors.append(
                                "retired workflow script keys reintroduced"
                                f" in {package_json}:"
                                f" {', '.join(reintroduced)} — these drove"
                                " the legacy shell around every canonical"
                                " gate (admin#1495 r14 F11)"
                            )
            # admin#1495 finding 3822586140: the SYMLINK load roots are
            # entry points too. Whichever of the known roots is the
            # real package directory, every OTHER root that exists must be
            # a link resolving exactly to it — a retargeted, dangling, or
            # regular-file alias silently changes (or breaks) the package
            # a client loads, and a symlink-only change must not escape
            # this guard or its CI triggers.
            package_real = package_dir.resolve()
            cursor_load_root = candidate / ".cursor" / "skills" / "autonomy"
            load_roots = (
                candidate / ".claude" / "skills" / "autonomy",
                candidate / ".agents" / "skills" / "autonomy",
                # mm#3551 dawid-r7 F2: the Cursor discovery surface loads
                # the package through this alias too (matchmaking is the
                # only repo that carries it). Absent stays legal - when
                # present it obeys the same resolution rule as the pair
                # above, PLUS the r14 F8 bare-blob CI-trigger rule below,
                # which is scoped to this alias-only root on purpose (see
                # the comment at that check).
                cursor_load_root,
            )
            workflow = (
                candidate / ".github" / "workflows" / "skill-package-checks.yml"
            )
            try:
                workflow_text = workflow.read_text(encoding="utf-8")
            except OSError:
                workflow_text = ""
            for root in load_roots:
                exists_at_all = root.is_symlink() or root.exists()
                if not exists_at_all:
                    continue
                if root.resolve() == package_real:
                    if root.is_symlink() and workflow_text:
                        rel = root.relative_to(candidate).as_posix()
                        # admin#1495 r13 F13: the trigger check is
                        # STRUCTURAL on both events — a raw substring
                        # accepted a comment-only mention.
                        for event in ("pull_request", "push"):
                            structural = _workflow_event_paths(
                                workflow_text, event
                            )
                            if not _covers_path(structural, rel):
                                errors.append(
                                    f"skill-package-checks.yml {event}"
                                    f" paths do not cover {rel} under"
                                    f" structural on.{event}.paths — a"
                                    " symlink-only change to the load path"
                                    " would bypass this guard, and a"
                                    " comment-only mention does not count"
                                    " (findings 3822586140 / r13 F13)"
                                )
                            # mm#3551 dawid-r7 F2, mirroring the admin#1495
                            # r14 F8 symlink-blob rule: a symlink load root
                            # changes as its own bare blob path, which
                            # "root/**" never matches in GitHub's filter,
                            # so a retarget would slide past CI. Scoped to
                            # the alias-only .cursor root: the
                            # .claude/.agents pair keeps the _covers_path
                            # contract it shipped with (finding 3822586140)
                            # because tightening those two would change the
                            # CI contract of every host repository, beyond
                            # this finding's scope.
                            elif root == cursor_load_root and not (
                                _covers_symlink_blob(structural, rel)
                            ):
                                errors.append(
                                    f"skill-package-checks.yml {event}"
                                    " paths do not match the bare symlink"
                                    f" path {rel} - \"{rel}/**\" never"
                                    " matches the symlink blob itself, so"
                                    " a retarget bypasses CI; add the"
                                    " exact path or an ancestor glob"
                                    " (mm#3551 dawid-r7 F2, per admin#1495"
                                    " r14 F8)"
                                )
                    continue
                errors.append(
                    f"autonomy load root {root} does not resolve to the"
                    " validated package"
                    f" ({package_real}) — a retargeted, dangling, or"
                    " replaced alias changes what a client loads"
                    " (finding 3822586140)"
                )
            break
    return errors


# admin#1495 r18 F4: bound for one loader child importing and counting one
# required test module. Package imports are stdlib-only and finish in well
# under a second; the bound only stops a hung import from stalling CI.
_TEST_COLLECTION_TIMEOUT_SECONDS = 60

# admin#1495 r18 F4: the child driver imports one module and prints how many
# cases the unittest loader collects - it never RUNS test bodies.
# ``loadTestsFromModule`` on an explicitly imported module, never
# ``loadTestsFromName``: the latter wraps an import failure into a synthetic
# _FailedTest that counts as 1, which would read as "collects".
_TEST_COLLECTION_DRIVER = (
    "import importlib, sys, unittest\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "module = importlib.import_module(sys.argv[2])\n"
    "suite = unittest.defaultTestLoader.loadTestsFromModule(module)\n"
    "sys.stdout.write(str(suite.countTestCases()))\n"
)

# admin#1495 r18 F4: memo for _loader_collection_error, keyed by module name
# plus a content digest of every ``*.py`` beside it. Collection depends only
# on the module, the sibling sources it can import (``-I`` shuts out cwd,
# PYTHONPATH, and user site), and the interpreter, which is constant within
# a process - so identical bytes give identical results. CI's single
# validate_package() call gains nothing here; the point is the package's own
# test suite, which calls validate_package() hundreds of times against
# byte-identical fixture packages and must not re-spawn nine children per
# call.
_COLLECTION_CACHE: dict[tuple[str, str], str | None] = {}


def _script_directory_fingerprint(scripts_dir: Path) -> str:
    """Content digest of every ``*.py`` in ``scripts_dir`` (memo key part)."""

    import hashlib

    digest = hashlib.sha256()
    for path in sorted(scripts_dir.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def _loader_collection_error(
    scripts_dir: Path, module_name: str, required_file: str
) -> str | None:
    """Real, isolated, timeout-bounded loader collection for one module.

    admin#1495 r18 F4: the AST predicate accepted any class-local ``test*``
    method even when the class is not a ``unittest.TestCase`` - the loader
    collected zero cases while aggregate discovery stayed green on other
    modules' tests. The gate is therefore the loader's own nonzero count,
    taken in a child interpreter so a broken module cannot crash or corrupt
    the validator process.

    Structural rule for this whole file: the vendored repositories' AI Skill
    Security Scan raises a gate-failing CRITICAL when a single file pairs
    subprocess with ANY call name containing eval/exec/compile/__import__
    (substring match over dotted call names - ``re.compile`` trips it;
    string literals do not). This subprocess call is why every regex in this
    file is used as a pattern string, never via re.compile, and
    test_validate_package.py pins the pairing ban.
    """

    import subprocess

    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        _TEST_COLLECTION_DRIVER,
        str(scripts_dir),
        module_name,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(scripts_dir),
            capture_output=True,
            timeout=_TEST_COLLECTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            f"{required_file}: loader collection timed out after"
            f" {_TEST_COLLECTION_TIMEOUT_SECONDS}s - the module hung at"
            " import (admin#1495 r18 F4)"
        )
    stderr_tail = " ".join(
        completed.stderr.decode("utf-8", "replace").split()
    )[-200:]
    if completed.returncode != 0:
        return (
            f"{required_file}: loader collection child exited"
            f" {completed.returncode} - the module failed to import"
            f" (admin#1495 r18 F4): {stderr_tail}"
        )
    stdout_text = completed.stdout.decode("utf-8", "replace").strip()
    try:
        count = int(stdout_text)
    except ValueError:
        return (
            f"{required_file}: loader collection child emitted no usable"
            f" count (stdout {stdout_text[:80]!r}) - refusing to assume the"
            " suite collects (admin#1495 r18 F4)"
        )
    if count == 0:
        return (
            f"{required_file}: unittest loader collected zero test cases -"
            " a test* method on a plain (non-TestCase) class satisfies the"
            " AST scan but never collects, and aggregate discovery hides"
            " the zero behind other modules (admin#1495 r18 F4)"
        )
    return None


def _validate_test_collection(package_dir: Path) -> list[str]:
    """CR 3761135481: ``unittest discover`` exits 0 after collecting zero
    tests, so file existence alone cannot prove the CI suite runs anything.
    The discover pattern ``test_*.py`` already matches the filenames by
    construction.

    Two layers (admin#1495 r18 F4). The AST scan stays as a fast pre-filter
    with precise messages for modules that cannot collect under this
    package's conventions: parse failure, or no class-local ``test*`` method
    (a ``load_tests``-only module is deliberately outside those conventions
    and fails here). The GATE is real loader collection: an isolated,
    timeout-bounded child interpreter imports each required module and must
    count a nonzero number of collected cases, because the AST predicate
    alone also accepts ``test*`` methods on plain non-TestCase classes that
    the loader collects zero cases from."""

    import ast

    errors: list[str] = []
    fingerprints: dict[Path, str] = {}
    for required_file in REQUIRED_SCRIPT_FILES:
        name = required_file.rsplit("/", 1)[-1]
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        path = package_dir / required_file
        if not path.is_file():
            continue  # the existence check reports this separately
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError):
            errors.append(f"{required_file}: test module failed to parse")
            continue
        has_test = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
            for candidate_class in ast.walk(tree)
            if isinstance(candidate_class, ast.ClassDef)
            for node in candidate_class.body
        )
        if not has_test:
            errors.append(
                f"{required_file}: defines no test* method in any class —"
                " discovery would collect zero tests and still exit 0"
            )
            continue
        scripts_dir = path.parent
        fingerprint = fingerprints.get(scripts_dir)
        if fingerprint is None:
            fingerprint = _script_directory_fingerprint(scripts_dir)
            fingerprints[scripts_dir] = fingerprint
        module_name = name[: -len(".py")]
        cache_key = (module_name, fingerprint)
        if cache_key not in _COLLECTION_CACHE:
            _COLLECTION_CACHE[cache_key] = _loader_collection_error(
                scripts_dir, module_name, required_file
            )
        collection_error = _COLLECTION_CACHE[cache_key]
        if collection_error is not None:
            errors.append(collection_error)
    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the autonomy skill package."
    )
    parser.add_argument(
        "package_dir",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="skill package directory (defaults to the parent of this script directory)",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        errors = validate_package(args.package_dir)
    except (ValueError, OSError) as error:
        # CR 3761135467: read_text on an unreadable or non-UTF-8 file must
        # report the same structured fail-closed way as the other CLIs
        # (UnicodeDecodeError is a ValueError subclass; OSError is not).
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if errors:
        print(f"autonomy package validation failed ({len(errors)} error(s)):")
        for error in errors:
            print(f"- {error}")
        return 1

    print("autonomy package validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
