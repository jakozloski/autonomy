#!/usr/bin/env python3
"""Deterministically validate the autonomy skill package.

The validator intentionally uses only the Python standard library so it can run
from Codex, Claude Code, CI, or a freshly cloned repository without installing
package-specific dependencies.
"""

from __future__ import annotations

import argparse
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
    "scripts/model_policy.py",
    "scripts/state_schema.py",
    "scripts/validate_package.py",
    "scripts/test_handoff_decision.py",
    "scripts/test_model_policy.py",
    "scripts/test_model_policy_supervision.py",
    "scripts/test_state_schema.py",
    "scripts/test_validate_package.py",
    "scripts/test_cli_fail_closed.py",
    "scripts/monitor_runner.py",
    "scripts/monitor_child_wrapper.py",
    "scripts/test_monitor_runner.py",
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
        # real return code.
        "supervise_stream(stdout_pipe, stderr_pipe, kill_callback, child_wait",
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
    ),
    "references/state-and-safety.md": (
        "Resume trust model",
        # R2 liveness contracts — dropping any of these regressions F2/F3/B:
        # the ceiling parameter, the schema-legal wait timestamp and its
        # reset-path clear, and the stale-reset clamp.
        "max_runtime_seconds="
        + str(model_policy.PER_ATTEMPT_CEILING_SECONDS),
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
    "references/monitor-ci-feedback.md": (
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
    "scripts/handoff_decision.py": (
        # R3 F5 — the attempt cap is REBOUND from state_schema, never a second
        # literal (interning makes equality tests vacuous; the source line is
        # the pin).
        "MAX_OPERATION_ATTEMPTS = state_schema.MAX_OPERATION_ATTEMPTS",
    ),
    "scripts/model_policy.py": (
        # R3 F2/r6 — the quota-wait ceiling is REBOUND from state_schema so
        # the helper clamp and the validator's resume bound cannot drift.
        "MAX_QUOTA_WAIT_SECONDS = state_schema.MAX_QUOTA_WAIT_SECONDS",
    ),
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
}

_LIST_MARKER_PREFIXES = ("- ", "* ", "+ ")

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
    # stated design rule. Labeled passwords stay out of scope by that
    # same rule (too many false positives), as the doc already records.
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
        # algo#1216 R2 finding 3779532276: the \b-anchored form missed
        # DB_PASSWORD/MYSQL_PASSWORD-style labels (underscore is a word
        # character, so no boundary exists before "password").
        r"""(?i)[\w-]*password["']?\s*[:=]\s*["']?[^\s"']{8,}""",
        (
            "password=" + "SuperSecret99",
            "PASSWORD: " + "hunter2hunter2",
            "DB_PASSWORD=" + "prodsecret99",
            "MYSQL_PASSWORD: " + "hunter2hunter2",
        ),
    ),
    "cookie_header_value": (
        r"""(?i)\b(Set-)?Cookie:\s*[^\s;=]+=[^\s;]{8,}""",
        ("Cookie: " + "session=abcdef12345678", "Set-Cookie: " + "sid=0123456789abcdef"),
    ),
    "anthropic_key": (
        r"sk-ant-[A-Za-z0-9_-]{40,}",
        ("sk-" + "ant-abcdefghijklmnopqrstuvwxyz_1234567890-ABCDE",),
    ),
    "jwt_base64url": (
        r"eyJ[A-Za-z0-9_\-=]{10,}\.eyJ[A-Za-z0-9_\-=]{10,}\.[A-Za-z0-9_\-=]+",
        ("eyJ" + "abcde-fghijk.eyJlmno_pqrstuv.signature-part",),
    ),
}
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
        "### Claude reviewer: Opus 5 at max",
        "### Codex voices: GPT-5.6 Sol at max",
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
        if scalar and scalar[0] in "'\"":
            if len(scalar) < 2 or scalar[-1] != scalar[0]:
                errors.append(
                    f"SKILL.md:{line_number}: frontmatter key {key!r} has an "
                    "unterminated quoted scalar"
                )
                continue
            scalar = scalar[1:-1]
        elif re.search(r":(?:[ \t]|$)", scalar):
            # An unquoted plain scalar containing ':' before whitespace or at
            # end-of-line is invalid YAML; a lenient regex accepting it here
            # would pass validation while the real skill loader fails to parse
            # the frontmatter.
            errors.append(
                f"SKILL.md:{line_number}: frontmatter key {key!r} must quote "
                "scalars containing ': '"
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
    pattern = re.compile(rf"^  {re.escape(key)}\s*:\s*(.*)$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
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
    """Reject direct interface scalars that start a quote but never close it."""

    errors: list[str] = []
    for key in keys:
        pattern = re.compile(rf"^  {re.escape(key)}\s*:\s*(.*)$")
        for line in text.splitlines():
            match = pattern.match(line)
            if match is None:
                continue
            value = match.group(1).strip()
            if (
                value
                and value[0] in "'\""
                and (len(value) < 2 or value[-1] != value[0])
            ):
                errors.append(
                    f"agents/openai.yaml interface.{key} has an unterminated "
                    "quoted scalar"
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
            compiled = re.compile(pattern)
            for sample in samples:
                if compiled.fullmatch(sample) is None:
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
            for substring in required:
                if substring not in matches[0]:
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
    errors.extend(_validate_policy_text(package_dir))
    errors.extend(_validate_gate_markers(package_dir))
    errors.extend(_validate_anchored_markers(package_dir))
    errors.extend(_validate_openai_yaml(package_dir))
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
    except ValueError as error:
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
