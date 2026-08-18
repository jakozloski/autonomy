#!/usr/bin/env python3
"""Build deterministic, side-effect-free handoff operation plans.

The command reads one JSON object from stdin and writes one JSON object to
stdout.  It never calls GitHub, Linear, or any other remote service.  Before a
caller executes ``call_plan[0]``, it persists an ``operation_results`` record
with status ``pending`` and the incremented attempt count.  A resumed pending
operation produces ``verify_before_retry`` instead of replaying a mutation.
After verification, the caller records ``complete``, ``failed``, or
``retryable`` with timestamps and evidence, then invokes this helper again.

Roundtrip reviewer records are deliberately evidence-bearing.  Every review
body and inline root carries both its current edit timestamp and the timestamp
that was evaluated/replied to.  Fix SHAs are compared with the pushed SHA set.
An edit, missing reply, unpushed fix, or remaining blocker therefore invalidates
the handoff instead of silently requesting review on stale work.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import state_schema  # noqa: E402  (sibling module; path set immediately above)


SCHEMA_VERSION = 1

APPROVED_QA = "approved_qa"
CLEAN_UNAPPROVED = "clean_unapproved"
HUMAN_REVIEW_ROUNDTRIP = "human_review_roundtrip"
REVIEWER_REQUEST = "reviewer_request"
SCENARIOS = {
    APPROVED_QA,
    CLEAN_UNAPPROVED,
    HUMAN_REVIEW_ROUNDTRIP,
    REVIEWER_REQUEST,
}
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
# Post-merge codex F3 + pass-3 codex F4: a prunable prior-generation id
# must match the COMPLETE grammar `family(:identity)?:g<12-hex>` - not
# merely end in the digest tail. A tail-only check let an extra-segment id
# like `qa.linear.assign_ticket:gBAD:g<hex>` prune as history while the
# real mutation re-queued. \Z, not $: a stray trailing newline would
# satisfy $ and launder a CURRENT completed id as history (replay).
GENERATION_SCOPED_ID = re.compile(
    r"(?P<family>[a-z_]+(?:\.[a-z_]+)+)"
    r"(?::(?P<identity>[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?))?"
    r":g[0-9a-f]{12}\Z"
)


# Static per-scenario family vocabularies for the prior-generation sweeps.
# CR 3760683938 requires that prunable history name a family the scenario
# MINTS - deriving that set from the current plan's ids broke down once
# reviewer turnover could legitimately empty a family (a prior round's
# request_review records must still prune when the new round routes no
# reviewer), so the vocabulary is the scenario's static mint surface.
# Maps family -> whether the id carries a per-reviewer identity segment.
# Pass-4 codex F1: identity must be ARITY-CHECKED per family, not globally
# optional - `qa.github.replace_assignees:bogus:g<current>` parsed as a
# well-formed id under the optional grammar and pruned as history while
# the real operation re-queued.
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


def parsed_generation_family(
    operation_id: str, vocabulary: dict[str, bool]
) -> str | None:
    """Family of a well-formed generation-scoped id, else ``None``.

    The sweeps prune a record as prior-generation history ONLY when this
    returns a family the scenario mints AND the id's identity segment
    matches that family's arity (pass-4 codex F1): a surplus identity on
    an identity-free family, a missing identity on a per-reviewer family,
    a wrong digest shape, extra segments, uppercase identity, or a
    trailing newline all stay unknown-ID errors downstream.
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
# Git accepts unambiguous abbreviated object IDs. Require at least seven hex
# characters while allowing full SHA-1 and SHA-256 object IDs.
GIT_OBJECT_ID = re.compile(r"[0-9a-fA-F]{7,64}")
LINEAR_WRITE_PATHS = {"environment_tool", "local_api", "none"}
ISSUE_TRACKER_TYPES = {"linear", "jira", "github", "none"}
# Single source of truth: the attempt cap lives in state_schema (dependency
# root); this module REBINDS the name rather than re-declaring the literal.
MAX_OPERATION_ATTEMPTS = state_schema.MAX_OPERATION_ATTEMPTS

# Match nameWithOwner exactly.  Repository basename matching would incorrectly
# hand off forks such as another-owner/matchmaking.
# Identity binding (admin-portal#1495 R2 findings 3722356257 + 3776596721):
# ``linear_user_id`` is the STABLE binding — sourced from
# keeper-agents/scripts/users.json, the org's identity map — and the planner
# hard-fails when a resolved QA user does not match it. ``linear_email`` is
# the field Keeper's authorized managed broker (`linear_update_issue`)
# accepts for assignment (it resolves IDs internally); provider IDs remain
# the postcondition-verification key. ``linear_name`` is an ADVISORY display
# cross-check only: a mismatch warns (display names drift), never blocks.
QA_OWNER_BY_REPOSITORY = {
    "Keeper-Dating/admin-portal": {
        "github_login": "shafqatukhan",
        "linear_email": "shafqat@keeper.ai",
        "linear_user_id": "18fadb17-d9e6-495b-af66-c234f457ff20",
        "linear_name": "Shafqat",
    },
    "Keeper-Dating/calculator-api": {
        "github_login": "tjkeeper",
        "linear_email": "tj@keeper.ai",
        "linear_user_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
        "linear_name": "Timothy Jhon Pascual",
    },
    "Keeper-Dating/keeper-lead-generator": {
        "github_login": "tjkeeper",
        "linear_email": "tj@keeper.ai",
        "linear_user_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
        "linear_name": "Timothy Jhon Pascual",
    },
    "Keeper-Dating/matchmaking": {
        "github_login": "tjkeeper",
        "linear_email": "tj@keeper.ai",
        "linear_user_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
        "linear_name": "Timothy Jhon Pascual",
    },
}

# QA workflow state the validated ticket moves to during the first-clean-exit
# handoff, keyed by Linear team key (the ticket-identifier prefix).  The
# handoff transfers ownership AND stage: assign-only handoffs left tickets
# reading as in-progress after QA already owned them.  Workflow-state IDs are
# team-scoped, so callers resolve the ID by this exact name within the
# ticket's own team and pass it as ``issue_tracker.qa_state``.  Teams absent
# from this map get no state operation.
QA_STATE_NAME_BY_TEAM = {
    "ADM": "Dev - Ready for QA",
    "WEB": "Vercel Preview QA",
}


def _ticket_team_key(ticket_identifier: str) -> str:
    return ticket_identifier.split("-", 1)[0]


def _base_plan(scenario: str | None) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "scenario": scenario,
        "state": "blocked",
        "reason": None,
        "targets": {
            "assignees": [],
            "reviewers": [],
            "linear_assignee": None,
        },
        "operations": [],
        "call_plan": [],
        "warnings": [],
        "errors": [],
    }


def _blocked(scenario: str | None, *errors: str) -> dict[str, Any]:
    plan = _base_plan(scenario)
    plan["errors"] = list(errors)
    return plan


def _idle(scenario: str, reason: str) -> dict[str, Any]:
    plan = _base_plan(scenario)
    plan["state"] = "idle"
    plan["reason"] = reason
    return plan


def _iso_timestamp(value: Any) -> datetime | None:
    # Single source of truth: state_schema.normalize_iso_timestamp owns the
    # strict shape check AND the fractional-second normalization, so this
    # eligibility gate can never accept or reject a timestamp the state
    # validator decides the other way.
    return state_schema.normalize_iso_timestamp(value)


def _is_stripped_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _is_git_object_id(value: Any) -> bool:
    return isinstance(value, str) and GIT_OBJECT_ID.fullmatch(value) is not None


def _live_id_set(value: Any, field: str) -> tuple[set[str], list[str]]:
    if not isinstance(value, list):
        return set(), [f"{field} must be a list"]
    if not all(isinstance(item, str) and item for item in value):
        return set(), [f"{field} must contain only non-empty strings"]
    if len(value) != len(set(value)):
        return set(), [f"{field} must not contain duplicates"]
    return set(value), []


def _repository_and_pr(
    request: dict[str, Any], scenario: str
) -> tuple[str | None, int | None, list[str]]:
    errors: list[str] = []
    repository = request.get("repository")
    if not isinstance(repository, dict):
        errors.append("repository must be an object containing nameWithOwner")
        name_with_owner = None
    else:
        name_with_owner = repository.get("nameWithOwner")
        if not isinstance(name_with_owner, str) or not name_with_owner:
            errors.append("repository.nameWithOwner must be a non-empty string")
            name_with_owner = None

    pull_request_number = request.get("pull_request_number")
    if (
        not isinstance(pull_request_number, int)
        or isinstance(pull_request_number, bool)
        or pull_request_number <= 0
    ):
        errors.append("pull_request_number must be a positive integer")
        pull_request_number = None

    if scenario in (HUMAN_REVIEW_ROUNDTRIP, REVIEWER_REQUEST):
        actor = request.get("authenticated_actor")
        if not isinstance(actor, str) or GITHUB_LOGIN.fullmatch(actor) is None:
            errors.append("authenticated_actor must be a valid GitHub login")

    return name_with_owner, pull_request_number, errors


def _github_operation(
    operation_id: str,
    action: str,
    name_with_owner: str,
    pull_request_number: int,
    *,
    depends_on: list[str],
    **payload: Any,
) -> dict[str, Any]:
    return {
        "id": operation_id,
        "service": "github",
        "action": action,
        "depends_on": depends_on,
        "payload": {
            "nameWithOwner": name_with_owner,
            "pull_request_number": pull_request_number,
            **payload,
        },
    }



def _normalized_reviewer_logins(raw: Any) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    return sorted(
        {login.casefold() for login in raw if isinstance(login, str)}
    )


def qa_generation(request: dict[str, Any]) -> str:
    """Digest of the remote targets a QA-handoff plan mutates.

    Embedded in every ``qa.*`` operation ID so a completed ledger persisted
    for DIFFERENT targets — another PR, a re-keyed ticket, a changed owner
    map, a different QA user or workflow state — can never satisfy the
    current plan (algo#1216 R2 finding 3722492998: with fixed IDs, changing
    every PR/ticket/assignee/state payload still returned ``complete`` with
    zero calls).  Mirrors ``roundtrip_generation``: plan-level on purpose,
    so the plan re-mints as one atomic unit when any target changes.
    Malformed segments hash as ``None`` — the planner's own validation
    rejects them separately; this digest only has to CHANGE when a target
    changes.

    ``plan_version`` is a plan-SHAPE component (post-fix review F1 of R2
    round-3 finding 3774514905): inserting ``verify_ticket_binding`` into
    the chain changed the operation set without changing the targets, so a
    ledger persisted by the previous plan shape kept the same digest and
    hit the prefix rule as an opaque generic error. Bumping the version
    re-mints the IDs instead, so pre-upgrade ledgers take the DOCUMENTED
    prior-generation path: terminal records pruned with a warning, an
    in-flight record failing closed with the recovery named.
    """

    repository = request.get("repository")
    name_with_owner = (
        repository.get("nameWithOwner") if isinstance(repository, dict) else None
    )
    pull_request_number = request.get("pull_request_number")
    owner = (
        QA_OWNER_BY_REPOSITORY.get(name_with_owner)
        if isinstance(name_with_owner, str)
        else None
    )
    tracker = request.get("issue_tracker")
    tracker = tracker if isinstance(tracker, dict) else {}
    qa_assignee = tracker.get("qa_assignee")
    qa_assignee = qa_assignee if isinstance(qa_assignee, dict) else {}
    qa_state = tracker.get("qa_state")
    qa_state = qa_state if isinstance(qa_state, dict) else {}
    # Post-merge pass-3 codex F3 / opus F1: code_reviewers ALTER the minted
    # operation set (per-reviewer request/verify ids), so they are a plan
    # target and must move the digest - otherwise a reviewer change keeps
    # the old generation, the prior round's identity-bearing ids are
    # neither current nor prunable, and resume hard-blocks on unknown IDs.
    # Normalized exactly like the operation ids (casefolded, deduped,
    # sorted) so raw-case spelling differences never re-mint a plan.
    payload = {
        "plan_version": 2,
        "nameWithOwner": name_with_owner,
        "pull_request_number": pull_request_number,
        "github_login": owner["github_login"] if owner else None,
        "ticket_identifier": tracker.get("ticket_identifier"),
        "ticket_provider_id": tracker.get("ticket_provider_id"),
        "write_path": tracker.get("write_path"),
        "qa_assignee_provider_id": qa_assignee.get("provider_id"),
        "qa_state_provider_id": qa_state.get("provider_id"),
        # admin#1495 R2 finding 3791925153 (executed repro): the reviewer set
        # shapes operation IDs and dependencies, so it is a TARGET fact —
        # omitting it reused a generation across reviewer changes and
        # stranded resumes on unknown-operation errors. Normalized (casefold,
        # sorted, deduplicated) so ordering is never a spurious rollover.
        "code_reviewers": _normalized_reviewer_logins(
            request.get("code_reviewers", [])
        ),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _approved_qa_operations(
    request: dict[str, Any],
    name_with_owner: str,
    pull_request_number: int,
    owner: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str]]:
    github_login = owner["github_login"]
    linear_name = owner["linear_name"]
    generation = qa_generation(request)
    # R2 #3551 finding 3737466462: the post-flip reviewer request needed the
    # same write-ahead/replay coverage as the roundtrip scenario — a crash
    # between the ready flip and the reviewer request otherwise loses the
    # request with no ledger record for resume to replay. The caller passes
    # the routed code reviewers (judgment stays with the workflow's routing
    # rules); each mints request+verify operations ahead of the assignee
    # replacement, mirroring roundtrip's shapes.
    raw_code_reviewers = request.get("code_reviewers", [])
    code_reviewers: list[str] = []
    reviewer_errors: list[str] = []
    # Post-merge pass-3 opus F3: mirror _reviewer_request_operations' actor
    # guard - GitHub 422s a self-request into a permanent ledger failure.
    # The QA handoff must still transfer ownership, so the actor is
    # FILTERED (not blocked) and the drop is surfaced as a plan warning by
    # the caller-visible targets delta.
    actor = request.get("authenticated_actor")
    actor_identity = actor.casefold() if isinstance(actor, str) else None
    if not isinstance(raw_code_reviewers, list):
        reviewer_errors.append("code_reviewers must be a list of logins")
    else:
        for login in raw_code_reviewers:
            if not isinstance(login, str) or GITHUB_LOGIN.fullmatch(login) is None:
                reviewer_errors.append(
                    "code_reviewers entries must be valid GitHub logins"
                )
                break
            if login.casefold() == actor_identity:
                continue
            if login.casefold() not in {
                seen.casefold() for seen in code_reviewers
            }:
                code_reviewers.append(login)
    # admin#1495 finding 3806647929: the digest normalizes (casefold sort)
    # but operations preserved REQUEST order — reordering reviewers kept
    # the generation while the ledger prefix no longer matched. Operations
    # use the digest's own canonical order, so order is never load-bearing.
    code_reviewers = sorted(code_reviewers, key=str.casefold)
    targets = {
        "assignees": [github_login],
        "reviewers": list(code_reviewers),
        "linear_assignee": None,
    }
    operations = []
    reviewer_verification_ids: list[str] = []
    previous_operation_id: str | None = None
    for login in code_reviewers:
        identity = login.casefold()
        request_id = f"qa.github.request_review:{identity}:g{generation}"
        verify_id = (
            f"qa.github.verify_review_request:{identity}:g{generation}"
        )
        operations.append(
            _github_operation(
                request_id,
                "request_pull_request_review",
                name_with_owner,
                pull_request_number,
                depends_on=(
                    [previous_operation_id]
                    if previous_operation_id is not None
                    else []
                ),
                reviewer=login,
            )
        )
        operations.append(
            _github_operation(
                verify_id,
                "verify_pull_request_review_request",
                name_with_owner,
                pull_request_number,
                depends_on=[request_id],
                expected_reviewer=login,
            )
        )
        reviewer_verification_ids.append(verify_id)
        previous_operation_id = verify_id
    operations.append(
        _github_operation(
            f"qa.github.replace_assignees:g{generation}",
            "replace_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=list(reviewer_verification_ids),
            # This is the complete desired set, not an additive update.  Stale
            # assignees supplied by GitHub are intentionally absent.
            assignees=[github_login],
        )
    )
    operations.append(
        _github_operation(
            f"qa.github.verify_assignees:g{generation}",
            "verify_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=[f"qa.github.replace_assignees:g{generation}"],
            expected_assignees=[github_login],
        )
    )
    errors: list[str] = list(reviewer_errors)
    advisory_warnings: list[str] = []

    issue_tracker = request.get("issue_tracker", {})
    if not isinstance(issue_tracker, dict):
        return (
            targets,
            operations,
            ["issue_tracker must be an object"],
            advisory_warnings,
        )

    tracker_type = issue_tracker.get("type", "none")
    if not isinstance(tracker_type, str):
        errors.append("issue_tracker.type must be a string")
        return targets, operations, errors, advisory_warnings
    if tracker_type not in ISSUE_TRACKER_TYPES:
        errors.append("issue_tracker.type must be one of: github, jira, linear, none")
        return targets, operations, errors, advisory_warnings

    if tracker_type == "linear":
        ticket_required = issue_tracker.get("ticket_required", True)
        if not isinstance(ticket_required, bool):
            errors.append("issue_tracker.ticket_required must be a boolean")
            return targets, operations, errors, advisory_warnings
        ticket_validated = issue_tracker.get("ticket_validated") is True
        if not ticket_required:
            ticket_exemption_reason = issue_tracker.get("ticket_exemption_reason")
            if not _is_stripped_nonempty_string(ticket_exemption_reason):
                errors.append(
                    "issue_tracker.ticket_exemption_reason must be non-empty "
                    "when a Linear ticket is not required"
                )
                return targets, operations, errors, advisory_warnings
            if not ticket_validated:
                return targets, operations, errors, advisory_warnings
        if not ticket_validated:
            errors.append("a Linear QA handoff requires a currently validated ticket")
            return targets, operations, errors, advisory_warnings
        ticket_identifier = issue_tracker.get("ticket_identifier")
        if not _is_stripped_nonempty_string(ticket_identifier):
            errors.append(
                "issue_tracker.ticket_identifier must be stripped and non-empty "
                "when a Linear ticket is validated"
            )
            return targets, operations, errors, advisory_warnings
        ticket_provider_id = issue_tracker.get("ticket_provider_id")
        if not _is_stripped_nonempty_string(ticket_provider_id):
            errors.append(
                "issue_tracker.ticket_provider_id must be stripped and non-empty "
                "when a Linear ticket is validated"
            )
            return targets, operations, errors, advisory_warnings

        write_path = issue_tracker.get("write_path")
        if write_path not in LINEAR_WRITE_PATHS:
            errors.append(
                "issue_tracker.write_path must be one of: environment_tool, local_api, none"
            )
            return targets, operations, errors, advisory_warnings

        session_environment = request.get("session_environment")
        if write_path == "local_api" and session_environment != "local":
            errors.append(
                "issue_tracker.write_path local_api requires session_environment='local'"
            )
            return targets, operations, errors, advisory_warnings

        if write_path == "none":
            operations.append(
                {
                    "id": f"qa.linear.record_unavailable:g{generation}",
                    "service": "local",
                    "action": "record_unavailable",
                    "depends_on": [f"qa.github.verify_assignees:g{generation}"],
                    "payload": {
                        "ticket_identifier": ticket_identifier,
                        "ticket_provider_id": ticket_provider_id,
                        "expected_assignee_name": linear_name,
                        "expected_state_name": QA_STATE_NAME_BY_TEAM.get(
                            _ticket_team_key(ticket_identifier)
                        ),
                        "write_path": write_path,
                    },
                    # The planner knows this outcome without a remote call. It
                    # becomes terminal only after its dependency is terminal.
                    "automatic_failure": "No authorized Linear write path is available.",
                }
            )
            return targets, operations, errors, advisory_warnings

        qa_assignee = issue_tracker.get("qa_assignee")
        if not isinstance(qa_assignee, dict):
            errors.append(
                "issue_tracker.qa_assignee must contain the resolved Linear provider ID"
            )
            return targets, operations, errors, advisory_warnings
        linear_provider_id = qa_assignee.get("provider_id")
        resolved_name = qa_assignee.get("name")
        if not _is_stripped_nonempty_string(linear_provider_id):
            errors.append(
                "issue_tracker.qa_assignee.provider_id must be stripped and non-empty"
            )
            return targets, operations, errors, advisory_warnings
        if linear_provider_id != owner["linear_user_id"]:
            errors.append(
                "issue_tracker.qa_assignee.provider_id must be the mapped"
                f" Linear user id {owner['linear_user_id']!r} (stable binding"
                " from the org identity map); resolving a different user is"
                " a wrong-target handoff, not a drifted label"
            )
            return targets, operations, errors, advisory_warnings
        if resolved_name != linear_name:
            advisory_warnings.append(
                f"qa_assignee.name {resolved_name!r} differs from the mapped"
                f" display label {linear_name!r} — display names drift;"
                " binding is by linear_user_id and proceeds"
            )
        targets["linear_assignee"] = {
            "provider_id": linear_provider_id,
            "name": linear_name,
        }

        # R2 round-3 finding 3774514905: the planner is a pure function, so
        # the EXECUTION boundary owns ticket identity — before the first
        # tracker mutation, the executor RESOLVES the identifier through
        # the authorized path (the broker is identifier-keyed) and
        # confirms the broker-resolved ticket is the validated one: its
        # true provider id equals expected_ticket_provider_id and the
        # ticket links THIS PR, at fetch time. A mismatch fails this read-only operation, and the
        # dependency cascade renders every Linear mutation below it
        # skipped_dependency — a stale or re-keyed provider ID can never
        # reach an unrelated ticket.
        binding_id = f"qa.linear.verify_ticket_binding:g{generation}"
        operations.append(
            {
                "id": binding_id,
                "service": "linear",
                "action": "verify_ticket_binding",
                "depends_on": [f"qa.github.verify_assignees:g{generation}"],
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "expected_ticket_provider_id": ticket_provider_id,
                    "expected_repository": name_with_owner,
                    "expected_pull_request_number": pull_request_number,
                    "write_path": write_path,
                },
            }
        )
        operations.append(
            {
                "id": f"qa.linear.assign_ticket:g{generation}",
                "service": "linear",
                "action": "assign_ticket",
                "depends_on": [binding_id],
                # algo#1216 finding 3792942223: the managed broker resolves
                # the IDENTIFIER and mutates that ticket — it exposes no
                # expected-provider-id precondition, so a provider id in a
                # MUTATION payload is a decoy implying a binding nothing
                # enforces. Mutations are keyed by identifier alone; the
                # recorded provider id moves to the VERIFY ops (and to the
                # PRE-mutation verify_ticket_binding read above, which
                # fetches by identifier and compares the broker-resolved
                # id), where mismatch fails loudly.
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "assignee_id": linear_provider_id,
                    "assignee_email": owner["linear_email"],
                    "assignee_name": linear_name,
                    "write_path": write_path,
                },
            }
        )
        operations.append(
            {
                "id": f"qa.linear.verify_ticket_assignee:g{generation}",
                "service": "linear",
                "action": "verify_ticket_assignee",
                "depends_on": [f"qa.linear.assign_ticket:g{generation}"],
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "expected_ticket_provider_id": ticket_provider_id,
                    "expected_assignee_id": linear_provider_id,
                    "expected_assignee_name": linear_name,
                    "write_path": write_path,
                },
            }
        )

        team_key = _ticket_team_key(ticket_identifier)
        expected_state_name = QA_STATE_NAME_BY_TEAM.get(team_key)
        qa_state = issue_tracker.get("qa_state")
        if expected_state_name is None:
            if qa_state is not None:
                errors.append(
                    f"issue_tracker.qa_state must be omitted for team {team_key!r}, "
                    "which has no mapped QA workflow state"
                )
            return targets, operations, errors, advisory_warnings
        if qa_state is None:
            unresolved_reason = issue_tracker.get("qa_state_unresolved_reason")
            if not _is_stripped_nonempty_string(unresolved_reason):
                errors.append(
                    "issue_tracker.qa_state must contain the resolved "
                    f"{expected_state_name!r} workflow state for team {team_key!r}; "
                    "pass qa_state_unresolved_reason to record a manual state move"
                )
                return targets, operations, errors, advisory_warnings
            operations.append(
                {
                    "id": f"qa.linear.record_state_unavailable:g{generation}",
                    "service": "local",
                    "action": "record_unavailable",
                    "depends_on": [f"qa.linear.verify_ticket_assignee:g{generation}"],
                    "payload": {
                        "ticket_identifier": ticket_identifier,
                        "ticket_provider_id": ticket_provider_id,
                        "expected_state_name": expected_state_name,
                        "reason": unresolved_reason,
                    },
                    # Like qa.linear.record_unavailable: a known-local outcome
                    # that becomes terminal only after its dependency does.
                    "automatic_failure": unresolved_reason,
                }
            )
            return targets, operations, errors, advisory_warnings
        if not isinstance(qa_state, dict):
            errors.append(
                "issue_tracker.qa_state must contain the resolved Linear "
                "workflow-state provider ID"
            )
            return targets, operations, errors, advisory_warnings
        state_provider_id = qa_state.get("provider_id")
        if not _is_stripped_nonempty_string(state_provider_id):
            errors.append(
                "issue_tracker.qa_state.provider_id must be stripped and non-empty"
            )
            return targets, operations, errors, advisory_warnings
        if qa_state.get("name") != expected_state_name:
            errors.append(
                "issue_tracker.qa_state.name must resolve exactly to "
                f"{expected_state_name!r} for team {team_key!r}"
            )
            return targets, operations, errors, advisory_warnings

        operations.append(
            {
                "id": f"qa.linear.set_ticket_state:g{generation}",
                "service": "linear",
                "action": "set_ticket_state",
                "depends_on": [f"qa.linear.verify_ticket_assignee:g{generation}"],
                # Finding 3792942223: mutation keyed by identifier only —
                # see qa.linear.assign_ticket's comment.
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "state_id": state_provider_id,
                    "state_name": expected_state_name,
                    "write_path": write_path,
                },
            }
        )
        operations.append(
            {
                "id": f"qa.linear.verify_ticket_state:g{generation}",
                "service": "linear",
                "action": "verify_ticket_state",
                "depends_on": [f"qa.linear.set_ticket_state:g{generation}"],
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "expected_ticket_provider_id": ticket_provider_id,
                    "expected_state_id": state_provider_id,
                    "expected_state_name": expected_state_name,
                    "write_path": write_path,
                },
            }
        )

    return targets, operations, errors, advisory_warnings


def _roundtrip_targets(
    request: dict[str, Any],
) -> tuple[list[str], list[str]]:
    reviewers = request.get("reviewers")
    if not isinstance(reviewers, list):
        return [], ["reviewers must be a list"]

    actor = request["authenticated_actor"]
    actor_key = actor.casefold()
    by_identity: dict[str, str] = {}
    errors: list[str] = []

    for index, reviewer in enumerate(reviewers):
        prefix = f"reviewers[{index}]"
        if not isinstance(reviewer, dict):
            errors.append(f"{prefix} must be an object")
            continue

        login = reviewer.get("login")
        if not isinstance(login, str) or not login:
            if reviewer.get("deleted") is True:
                errors.append(f"{prefix} is deleted and cannot receive a handoff")
            else:
                errors.append(f"{prefix} has an unknown GitHub identity")
            continue

        if reviewer.get("deleted") is not False:
            if reviewer.get("deleted") is True:
                errors.append(f"reviewer {login!r} is deleted")
            else:
                errors.append(f"reviewer {login!r} existence is unknown")
            continue

        account_type = reviewer.get("account_type")
        if account_type != "User":
            if account_type in (None, "Unknown"):
                errors.append(f"reviewer {login!r} account type is unknown")
            else:
                errors.append(f"reviewer {login!r} is not a human user")
            continue

        # Account type is the identity truth.  Syntax validation is separate;
        # never infer bot identity from a display/login suffix.
        if GITHUB_LOGIN.fullmatch(login) is None:
            errors.append(f"reviewer {login!r} has an invalid GitHub login")
            continue

        # Never re-request or assign the authenticated actor to their own PR.
        if login.casefold() == actor_key:
            continue

        review_bodies = reviewer.get("review_bodies")
        inline_roots = reviewer.get("inline_roots")
        if not isinstance(review_bodies, dict):
            errors.append(f"reviewer {login!r} review_bodies must be an object")
            continue
        if not isinstance(inline_roots, dict):
            errors.append(f"reviewer {login!r} inline_roots must be an object")
            continue
        if not review_bodies and not inline_roots:
            errors.append(f"reviewer {login!r} has no feedback evidence")
            continue

        evidence_errors: list[str] = []
        live_review_ids, live_review_errors = _live_id_set(
            reviewer.get("current_review_body_ids"),
            f"reviewer {login!r} current_review_body_ids",
        )
        live_inline_ids, live_inline_errors = _live_id_set(
            reviewer.get("current_inline_root_ids"),
            f"reviewer {login!r} current_inline_root_ids",
        )
        evidence_errors.extend(live_review_errors)
        evidence_errors.extend(live_inline_errors)
        if not live_review_errors and live_review_ids != set(review_bodies):
            evidence_errors.append(
                "current review-body IDs do not exactly match stored evidence"
            )
        if not live_inline_errors and live_inline_ids != set(inline_roots):
            evidence_errors.append(
                "current inline-root IDs do not exactly match stored evidence"
            )

        for review_id, body in review_bodies.items():
            if (
                not isinstance(review_id, str)
                or not review_id
                or not isinstance(body, dict)
            ):
                evidence_errors.append("contains an invalid review-body record")
                continue
            updated_at = body.get("updated_at")
            evaluated_updated_at = body.get("evaluated_updated_at")
            updated_time = _iso_timestamp(updated_at)
            evaluated_time = _iso_timestamp(body.get("evaluated_at"))
            if updated_time is None:
                evidence_errors.append(
                    f"review body {review_id!r} has no valid current timestamp"
                )
            elif updated_at != evaluated_updated_at:
                evidence_errors.append(
                    f"review body {review_id!r} changed after evaluation"
                )
            if evaluated_time is None:
                evidence_errors.append(
                    f"review body {review_id!r} has no valid evaluation timestamp"
                )
            elif updated_time is not None and evaluated_time < updated_time:
                evidence_errors.append(
                    f"review body {review_id!r} was evaluated before its latest edit"
                )
            acknowledgment_id = body.get("acknowledgment_id")
            if (
                not isinstance(acknowledgment_id, (str, int))
                or isinstance(acknowledgment_id, bool)
                or acknowledgment_id == ""
            ):
                evidence_errors.append(
                    f"review body {review_id!r} has no verified acknowledgment"
                )
            acknowledgment_author = body.get("acknowledgment_author")
            if (
                not isinstance(acknowledgment_author, str)
                or acknowledgment_author.casefold() != actor_key
            ):
                evidence_errors.append(
                    f"review body {review_id!r} acknowledgment is not by the authenticated actor"
                )

        for comment_id, root in inline_roots.items():
            if (
                not isinstance(comment_id, str)
                or not comment_id
                or not isinstance(root, dict)
            ):
                evidence_errors.append("contains an invalid inline-root record")
                continue
            updated_at = root.get("updated_at")
            replied_to_updated_at = root.get("replied_to_updated_at")
            updated_time = _iso_timestamp(updated_at)
            replied_time = _iso_timestamp(root.get("replied_at"))
            if updated_time is None:
                evidence_errors.append(
                    f"inline root {comment_id!r} has no valid current timestamp"
                )
            elif updated_at != replied_to_updated_at:
                evidence_errors.append(
                    f"inline root {comment_id!r} changed after reply"
                )
            reply_id = root.get("reply_id")
            if (
                not isinstance(reply_id, (str, int))
                or isinstance(reply_id, bool)
                or reply_id == ""
            ):
                evidence_errors.append(
                    f"inline root {comment_id!r} has no verified reply"
                )
            if replied_time is None:
                evidence_errors.append(
                    f"inline root {comment_id!r} has no valid reply timestamp"
                )
            elif updated_time is not None and replied_time < updated_time:
                evidence_errors.append(
                    f"inline root {comment_id!r} was replied to before its latest edit"
                )
            reply_author = root.get("reply_author")
            if (
                not isinstance(reply_author, str)
                or reply_author.casefold() != actor_key
            ):
                evidence_errors.append(
                    f"inline root {comment_id!r} reply is not by the authenticated actor"
                )

        fix_shas = reviewer.get("fix_shas")
        pushed_fix_shas = reviewer.get("pushed_fix_shas")
        valid_fix_shas = isinstance(fix_shas, list) and all(
            _is_git_object_id(sha) for sha in fix_shas
        )
        valid_pushed_fix_shas = isinstance(pushed_fix_shas, list) and all(
            _is_git_object_id(sha) for sha in pushed_fix_shas
        )
        if not valid_fix_shas:
            evidence_errors.append(
                "fix_shas must be a list of 7-64 character hexadecimal Git object IDs"
            )
        if not valid_pushed_fix_shas:
            evidence_errors.append(
                "pushed_fix_shas must be a list of 7-64 character hexadecimal Git object IDs"
            )
        if valid_fix_shas and valid_pushed_fix_shas:
            unpushed = sorted(set(fix_shas) - set(pushed_fix_shas))
            if unpushed:
                evidence_errors.append("fixes are not pushed: " + ", ".join(unpushed))
            if fix_shas and not _is_git_object_id(reviewer.get("pushed_through_sha")):
                evidence_errors.append(
                    "pushed_through_sha must be a 7-64 character hexadecimal "
                    "Git object ID for fix evidence"
                )
        if reviewer.get("blocker_remaining") is not False:
            evidence_errors.append("a reviewer blocker remains or is unknown")

        if evidence_errors:
            errors.extend(f"reviewer {login!r} {error}" for error in evidence_errors)
            continue

        identity = login.casefold()
        previous = by_identity.get(identity)
        if previous is None or login < previous:
            by_identity[identity] = login

    targets = sorted(by_identity.values(), key=lambda login: (login.casefold(), login))
    return targets, errors


def roundtrip_generation(
    request: dict[str, Any], reviewers: list[str]
) -> str:
    """Digest of the feedback evidence a roundtrip plan answers.

    Embedded in every roundtrip operation ID so a completed earlier
    round's ledger can never satisfy a later round: fresh feedback (a new
    review ID, an edit timestamp, a newly pushed fix) changes the digest,
    which mints operations no prior record matches (R2 round-2 finding
    3737466450 — the identity-only IDs let the second round return
    "complete" with an empty call plan while nobody was re-pinged).
    Plan-level on purpose: the trailing assignee operations span the
    whole reviewer set, so the plan re-mints as one atomic unit.
    """

    # Pass-3 codex #2: the canonicalization+digest moved to
    # state_schema.roundtrip_generation (single source) so the runner-side
    # blocked-evidence predicate can recompute the CURRENT generation and
    # refuse a prior round's ledger. This wrapper keeps the planner's
    # request-shaped call site; the entry filtering, payload shape, and
    # digest are byte-identical to the pre-move implementation.
    repository = request.get("repository")
    name_with_owner = (
        repository.get("nameWithOwner") if isinstance(repository, dict) else None
    )
    return state_schema.roundtrip_generation(
        request.get("reviewers"),
        reviewers,
        name_with_owner,
        request.get("pull_request_number"),
    )


def reviewer_request_generation(
    name_with_owner: str,
    pull_request_number: int,
    reviewers: list[str],
    ball_holder: str,
) -> str:
    """Digest of the targets a reviewer-request plan mutates.

    R2 round-3 finding 3774515577: the draft-flip / R2-satisfaction
    reviewer request had no write-ahead record, so a crash between the
    flip and the request lost the request silently. These operations get
    the same target-digest treatment as the QA handoff: the digest covers
    repository, PR, the sorted reviewer set, and the ball-holder, so a
    ledger persisted for different targets never satisfies the current
    plan, while an unchanged round resumes its own pending operations.
    Unlike ``roundtrip_generation`` this digests TARGETS, not feedback
    evidence — at flip time no reviewer feedback exists yet. Scope: the
    NON-KEEPER draft-flip moment only — in Keeper repositories the
    R2-satisfied handback runs through the QA plan's ``code_reviewers``
    ledger (admin#1495 finding 3791925155), never this scenario.
    """

    payload = {
        "nameWithOwner": name_with_owner,
        "pull_request_number": pull_request_number,
        "reviewers": sorted(login.casefold() for login in reviewers),
        "ball_holder": ball_holder.casefold(),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _reviewer_request_operations(
    request: dict[str, Any],
    name_with_owner: str,
    pull_request_number: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    block = request.get("reviewer_requests")
    targets: dict[str, Any] = {
        "assignees": [],
        "reviewers": [],
        "linear_assignee": None,
    }
    if not isinstance(block, dict):
        return targets, [], ["reviewer_requests must be an object"]
    raw_reviewers = block.get("reviewers")
    if not isinstance(raw_reviewers, list) or not raw_reviewers:
        return targets, [], [
            "reviewer_requests.reviewers must be a non-empty list of logins"
        ]
    # Post-fix review F3/F4: the actor can never be asked to review their
    # own PR (the roundtrip path already excludes it — GitHub 422s a self
    # request into a permanent ledger failure), and every login is
    # canonicalized to its casefolded identity so the digest, payloads,
    # and targets all describe the SAME spelling — GitHub logins are
    # case-insensitive, and a raw-cased payload beside a casefolded digest
    # broke both the digest's rebind promise and the exact-array
    # verification.
    actor_identity = str(request.get("authenticated_actor", "")).casefold()
    seen: set[str] = set()
    reviewers: list[str] = []
    for login in raw_reviewers:
        if not isinstance(login, str) or not GITHUB_LOGIN.fullmatch(login):
            return targets, [], [
                f"reviewer_requests.reviewers contains an invalid login: {login!r}"
            ]
        identity = login.casefold()
        if identity == actor_identity:
            return targets, [], [
                "reviewer_requests.reviewers must not include the"
                " authenticated actor"
            ]
        if identity in seen:
            continue
        seen.add(identity)
        reviewers.append(identity)
    reviewers.sort()
    ball_holder_raw = block.get("ball_holder")
    if (
        not isinstance(ball_holder_raw, str)
        or not GITHUB_LOGIN.fullmatch(ball_holder_raw)
        or ball_holder_raw.casefold() not in seen
    ):
        return targets, [], [
            "reviewer_requests.ball_holder must be one of the requested reviewers"
        ]
    ball_holder = ball_holder_raw.casefold()

    targets["assignees"] = [ball_holder]
    targets["reviewers"] = reviewers
    generation = reviewer_request_generation(
        name_with_owner, pull_request_number, reviewers, ball_holder
    )
    operations: list[dict[str, Any]] = []
    verification_ids: list[str] = []
    previous_operation_id: str | None = None
    for login in reviewers:
        identity = login.casefold()
        request_id = f"reviewer.github.request_review:{identity}:g{generation}"
        verify_id = (
            f"reviewer.github.verify_review_request:{identity}:g{generation}"
        )
        operations.append(
            _github_operation(
                request_id,
                "request_pull_request_review",
                name_with_owner,
                pull_request_number,
                depends_on=(
                    [previous_operation_id]
                    if previous_operation_id is not None
                    else []
                ),
                reviewer=login,
            )
        )
        operations.append(
            _github_operation(
                verify_id,
                "verify_pull_request_review_request",
                name_with_owner,
                pull_request_number,
                depends_on=[request_id],
                expected_reviewer=login,
            )
        )
        verification_ids.append(verify_id)
        previous_operation_id = verify_id

    replace_id = f"reviewer.github.replace_assignees:g{generation}"
    operations.append(
        _github_operation(
            replace_id,
            "replace_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=verification_ids,
            assignees=[ball_holder],
        )
    )
    operations.append(
        _github_operation(
            f"reviewer.github.verify_assignees:g{generation}",
            "verify_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=[replace_id],
            expected_assignees=[ball_holder],
        )
    )
    return targets, operations, []


def _roundtrip_operations(
    request: dict[str, Any],
    name_with_owner: str,
    pull_request_number: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    reviewers, errors = _roundtrip_targets(request)
    targets = {
        "assignees": reviewers,
        "reviewers": reviewers,
        "linear_assignee": None,
    }
    if errors or not reviewers:
        return targets, [], errors

    generation = roundtrip_generation(request, reviewers)
    operations: list[dict[str, Any]] = []
    reviewer_verification_ids: list[str] = []
    previous_operation_id: str | None = None
    for login in reviewers:
        identity = login.casefold()
        request_id = f"roundtrip.github.request_review:{identity}:g{generation}"
        verify_id = (
            f"roundtrip.github.verify_review_request:{identity}:g{generation}"
        )
        operations.append(
            _github_operation(
                request_id,
                "request_pull_request_review",
                name_with_owner,
                pull_request_number,
                depends_on=(
                    [previous_operation_id] if previous_operation_id is not None else []
                ),
                reviewer=login,
            )
        )
        operations.append(
            _github_operation(
                verify_id,
                "verify_pull_request_review_request",
                name_with_owner,
                pull_request_number,
                depends_on=[request_id],
                expected_reviewer=login,
            )
        )
        reviewer_verification_ids.append(verify_id)
        previous_operation_id = verify_id

    replace_id = f"roundtrip.github.replace_assignees:g{generation}"
    operations.append(
        _github_operation(
            replace_id,
            "replace_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=reviewer_verification_ids,
            assignees=reviewers,
        )
    )
    operations.append(
        _github_operation(
            f"roundtrip.github.verify_assignees:g{generation}",
            "verify_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=[replace_id],
            expected_assignees=reviewers,
        )
    )
    return targets, operations, []


def _operation_results(
    request: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    raw_results = request.get("operation_results", {})
    if not isinstance(raw_results, dict):
        return {}, ["operation_results must be an object keyed by operation ID"]

    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    allowed_keys = state_schema.OPERATION_RESULT_ALLOWED_KEYS
    for operation_id, raw_result in raw_results.items():
        if not isinstance(operation_id, str) or not operation_id:
            errors.append("operation_results keys must be non-empty strings")
            continue
        # Canonical per-record contract — shared with state_schema so a state
        # file can never validate clean and then be rejected here on resume.
        _, record_errors = state_schema.validate_operation_result_record(
            raw_result, label=f"operation_results[{operation_id!r}]"
        )
        if record_errors:
            errors.extend(record_errors)
            continue
        results[operation_id] = {
            key: copy.deepcopy(raw_result[key])
            for key in allowed_keys
            if key in raw_result
        }
    return results, errors


def _resume_verification_spec(
    pending_id: str, operation_specs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    pending_spec = next(
        (spec for spec in operation_specs if spec["id"] == pending_id), None
    )
    if pending_spec is None:
        return None
    if pending_spec["action"].startswith("verify_"):
        return pending_spec
    return next(
        (
            spec
            for spec in operation_specs
            if pending_id in spec.get("depends_on", [])
            and spec["action"].startswith("verify_")
        ),
        None,
    )


def _apply_operation_state(
    scenario: str,
    targets: dict[str, Any],
    operation_specs: list[dict[str, Any]],
    request: dict[str, Any],
) -> dict[str, Any]:
    result_records, result_errors = _operation_results(request)
    errors = list(result_errors)

    known_ids = {operation["id"] for operation in operation_specs}
    canonical_ids = set(result_records)
    unknown = canonical_ids - known_ids
    if unknown:
        errors.append("unknown operation IDs: " + ", ".join(sorted(unknown)))
    automatic_failure_ids = {
        operation["id"]
        for operation in operation_specs
        if "automatic_failure" in operation
    }
    canonical_complete = {
        operation_id
        for operation_id, result in result_records.items()
        if result["status"] == "complete"
    }
    canonical_failed = {
        operation_id
        for operation_id, result in result_records.items()
        if result["status"] == "failed"
    }
    # Persisted skipped_dependency records are failed-CLASS for dependency
    # propagation (their postcondition is known false) but render as their
    # own status so a re-plan reproduces the terminal answer that was
    # persisted, never upgrading a non-attempt into a failure record.
    canonical_skipped = {
        operation_id
        for operation_id, result in result_records.items()
        if result["status"] == "skipped_dependency"
    }
    in_flight = {
        operation_id
        for operation_id, result in result_records.items()
        if result["status"] in {"pending", "retryable"}
    }
    invalid_completed = canonical_complete & automatic_failure_ids
    if invalid_completed:
        errors.append(
            "unavailable operations cannot be marked complete: "
            + ", ".join(sorted(invalid_completed))
        )

    completed_all = canonical_complete
    failed_all = canonical_failed
    # Portable collection rules (single in-flight; prefix with one in-flight
    # tail) are DELEGATED to the canonical validator so the two sides cannot
    # drift; the label is empty because this module's historic messages carry
    # no prefix. Scenario-specific checks (unavailable-op completion above)
    # stay here — they need planner context the schema does not have.
    errors.extend(
        state_schema.validate_operation_collection(
            [operation["id"] for operation in operation_specs],
            {
                operation_id: result_records[operation_id]["status"]
                for operation_id in result_records
            },
            label="",
        )
    )

    if errors:
        return _blocked(scenario, *errors)

    # A local unavailable record is a known failure, not a remote operation.
    # It becomes terminal only once every preceding operation has reached a
    # terminal result, preserving the same crash-safe sequence as remote work.
    effective_failed = set(failed_all) | set(canonical_skipped)
    preceding_terminal = True
    for spec in operation_specs:
        operation_id = spec["id"]
        if (
            preceding_terminal
            and "automatic_failure" in spec
            and operation_id not in completed_all
        ):
            effective_failed.add(operation_id)
        if operation_id not in completed_all and operation_id not in effective_failed:
            preceding_terminal = False

    # Cascade verified failures through declared dependencies: an operation
    # whose dependency terminally failed can never legitimately run (its
    # expected postcondition is already known to be false), so it fails closed
    # instead of being queued as the next call. Specs are topologically
    # ordered by construction, so transitive failures propagate in one pass.
    # A canonical result on a descendant of a failed dependency means the
    # caller executed an operation this planner would never have queued —
    # an inconsistent ledger, which blocks.
    dependency_failure_details: dict[str, str] = {}
    for spec in operation_specs:
        operation_id = spec["id"]
        failed_dependencies = sorted(
            dependency
            for dependency in spec.get("depends_on", [])
            if dependency in effective_failed
        )
        if not failed_dependencies:
            # Pass-3 codex F5: a persisted skipped_dependency is a claim
            # that a DECLARED dependency terminally failed. On a root (or
            # any op whose dependencies all succeeded) that claim is
            # fabricated - accepting it would cascade fake skips over the
            # real plan. Fail closed instead.
            if operation_id in canonical_skipped:
                errors.append(
                    f"operation {operation_id} cannot be skipped_dependency:"
                    " no declared dependency terminally failed"
                )
            continue
        detail = "dependency failed: " + ", ".join(failed_dependencies)
        # Post-merge codex F2: a recorded `failed` on a descendant is just
        # as impossible as a recorded `complete` - the executor stops at
        # the first terminal failure, so ANY attempt evidence below one
        # claims a mutation the planner never queued. Only the rendered
        # skipped_dependency non-attempt is consistent. Local
        # automatic_failure records are exempt: their `failed` IS the
        # planner-derived outcome this module itself renders and callers
        # legitimately persist on round-trip.
        inconsistent_attempt = (
            operation_id in completed_all
            or operation_id in in_flight
            or (
                operation_id in canonical_failed
                and "automatic_failure" not in spec
            )
        )
        if inconsistent_attempt:
            errors.append(f"operation {operation_id} cannot have results: {detail}")
            continue
        if operation_id not in effective_failed:
            effective_failed.add(operation_id)
            dependency_failure_details[operation_id] = detail

    if errors:
        return _blocked(scenario, *errors)

    operations: list[dict[str, Any]] = []
    pending_assigned = False
    retryable_id = next(
        (
            operation_id
            for operation_id, result in result_records.items()
            if result["status"] == "retryable"
        ),
        None,
    )
    pending_id = next(
        (
            operation_id
            for operation_id, result in result_records.items()
            if result["status"] == "pending"
        ),
        None,
    )
    skipped_ids = canonical_skipped | set(dependency_failure_details)
    for spec in operation_specs:
        operation = copy.deepcopy(spec)
        operation_id = operation["id"]
        if operation_id in completed_all:
            operation["status"] = "complete"
        elif operation_id in skipped_ids:
            # Never attempted: the dependency chain above it terminally
            # failed. Rendered as its own status so the caller persists a
            # non-attempt record (attempts 0, error naming the dependency)
            # instead of fabricating a failure it never observed.
            operation["status"] = "skipped_dependency"
            record = result_records.get(operation_id)
            if record is not None and isinstance(record.get("error"), str):
                operation["error"] = record["error"]
        elif operation_id in effective_failed:
            operation["status"] = "failed"
        elif operation_id == pending_id:
            operation["status"] = "in_flight"
            operation["result"] = copy.deepcopy(result_records[operation_id])
            pending_assigned = True
        elif operation_id == retryable_id:
            operation["status"] = "retryable"
            operation["result"] = copy.deepcopy(result_records[operation_id])
            pending_assigned = True
        elif not pending_assigned:
            operation["status"] = "pending"
            pending_assigned = True
        else:
            operation["status"] = "waiting"
        automatic_failure = operation.pop("automatic_failure", None)
        if automatic_failure is not None and operation["status"] == "failed":
            operation["error"] = automatic_failure
        dependency_detail = dependency_failure_details.get(operation_id)
        if dependency_detail is not None and operation["status"] == (
            "skipped_dependency"
        ):
            operation["error"] = dependency_detail
        operations.append(operation)

    reason = None
    if pending_id is not None:
        state = "resume_verification_required"
        reason = f"verify the postcondition for in-flight operation {pending_id} before retrying"
    elif pending_assigned:
        state = "pending"
    elif effective_failed:
        state = "failed"
    else:
        state = "complete"

    warnings = []
    for operation in operations:
        if operation["status"] not in ("failed", "skipped_dependency"):
            continue
        if operation["status"] == "skipped_dependency":
            detail = dependency_failure_details.get(operation["id"]) or (
                operation.get("error") or "dependency failed"
            )
            warnings.append(
                f"Operation {operation['id']} not executed "
                f"({detail}); complete it manually."
            )
        elif operation["service"] == "local":
            warnings.append(
                f"Local operation {operation['id']} recorded unavailable; complete it manually."
            )
        else:
            warnings.append(
                f"Remote operation {operation['id']} failed; complete it manually."
            )
    if pending_id is not None:
        verification = _resume_verification_spec(pending_id, operation_specs)
        if verification is None:
            return _blocked(
                scenario,
                f"pending operation {pending_id!r} has no deterministic verification step",
            )
        call_plan = [
            {
                "id": f"resume.verify_before_retry:{pending_id}",
                "service": "control",
                "action": "verify_before_retry",
                "depends_on": [],
                "payload": {
                    "operation_id": pending_id,
                    "attempts": result_records[pending_id]["attempts"],
                    "verification_operation": copy.deepcopy(verification),
                },
            }
        ]
    elif retryable_id is not None:
        retry_operation = next(
            copy.deepcopy(operation)
            for operation in operations
            if operation["id"] == retryable_id
        )
        retry_operation["status"] = "pending"
        retry_operation["attempt"] = result_records[retryable_id]["attempts"] + 1
        retry_operation["requires_pending_write"] = True
        retry_operation.pop("result", None)
        call_plan = [retry_operation]
    else:
        call_plan = [
            copy.deepcopy(operation)
            for operation in operations
            if operation["status"] == "pending"
        ]

    plan = _base_plan(scenario)
    plan.update(
        {
            "state": state,
            "reason": reason,
            "targets": targets,
            "operations": operations,
            "call_plan": call_plan,
            "warnings": warnings,
        }
    )
    return plan


def plan_handoff(request: Any) -> dict[str, Any]:
    """Return a deterministic remote-operation plan for one handoff scenario."""

    if not isinstance(request, dict):
        return _blocked(None, "input must be a JSON object")

    scenario = request.get("scenario")
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        return _blocked(
            scenario if isinstance(scenario, str) else None,
            "scenario must be one of: " + ", ".join(sorted(SCENARIOS)),
        )

    name_with_owner, pull_request_number, errors = _repository_and_pr(request, scenario)
    if errors:
        return _blocked(scenario, *errors)
    assert name_with_owner is not None
    assert pull_request_number is not None

    extra_warnings: list[str] = []
    # QA handoff fires at the FIRST clean exit: approved (monitor -> complete)
    # or clean-but-unapproved (monitor -> paused). Preview QA runs in parallel
    # with code review, so both scenarios plan the identical operation set; the
    # paused exit still never writes `complete` and never merges.
    if scenario in (APPROVED_QA, CLEAN_UNAPPROVED):
        owner = QA_OWNER_BY_REPOSITORY.get(name_with_owner)
        if owner is None:
            results, result_errors = _operation_results(request)
            state_errors = list(result_errors)
            if results:
                state_errors.append(
                    "unmapped repositories have no operations to resume"
                )
            if state_errors:
                return _blocked(scenario, *state_errors)
            return _idle(
                scenario,
                "repository.nameWithOwner is not in the exact QA-owner map",
            )

        targets, operations, errors, qa_advisory_warnings = (
            _approved_qa_operations(
                request, name_with_owner, pull_request_number, owner
            )
        )
        extra_warnings.extend(qa_advisory_warnings)
        if not errors and operations:
            # Target-digest-bound IDs make a ledger persisted for different
            # targets (another PR, a re-keyed ticket, a changed owner map or
            # QA user/state) an orphan of the current plan.  Same contract as
            # the roundtrip sweep below: terminal orphans — including
            # skipped_dependency, whose record proves the operation never
            # fired — are that target set's completed history, pruned with a
            # warning; an IN-FLIGHT orphan marks a mutation that may already
            # have fired remotely and fails closed with the recovery named.
            # Non-qa IDs stay unknown-ID errors downstream.  Shape-validate
            # EVERY record before classifying any as history: pruning an
            # invalid record would launder the exact malformed evidence the
            # record validation exists to reject.
            raw_results = request.get("operation_results")
            if isinstance(raw_results, dict):
                _, shape_errors = _operation_results(request)
                if shape_errors:
                    return _blocked(scenario, *shape_errors)
                known_ids = {operation["id"] for operation in operations}
                stale_terminal: list[str] = []
                stale_in_flight: list[str] = []
                for operation_id, record in raw_results.items():
                    if not isinstance(operation_id, str):
                        continue
                    if operation_id in known_ids:
                        continue
                    if not operation_id.startswith("qa."):
                        continue
                    # CR 3760683938 (keeper-agents#1328), applied to both
                    # sweeps: prunable history is ONLY an id whose FAMILY
                    # (the dotted verb before any ":" segment) names an
                    # operation family the current plan mints — a fabricated
                    # "qa.bogus:g..." id stays an unknown-ID error, never
                    # laundered as a prior-target record. admin#1495 finding
                    # 3806647937: the compare is identity-INDEPENDENT — a
                    # removed reviewer's terminal request ops must still be
                    # recognized as stale. The full-grammar parser goes one
                    # step further (pass-4 codex F1): it also enforces
                    # per-family identity ARITY, so a surplus identity on an
                    # identity-free family never launders as history either.
                    if parsed_generation_family(
                        operation_id, QA_OPERATION_FAMILIES
                    ) is None:
                        continue
                    status = (
                        record.get("status")
                        if isinstance(record, dict)
                        else None
                    )
                    if status in ("complete", "failed", "skipped_dependency"):
                        stale_terminal.append(operation_id)
                    else:
                        stale_in_flight.append(operation_id)
                if stale_in_flight:
                    return _blocked(
                        scenario,
                        "prior-target QA operation(s) still in flight: "
                        + ", ".join(sorted(stale_in_flight))
                        + " - verify each mutation's postcondition and record"
                        " a terminal result before planning the current"
                        " targets",
                    )
                if stale_terminal:
                    pruned = dict(request)
                    pruned["operation_results"] = {
                        operation_id: record
                        for operation_id, record in raw_results.items()
                        if operation_id not in set(stale_terminal)
                    }
                    plan = _apply_operation_state(
                        scenario, targets, operations, pruned
                    )
                    plan["warnings"].append(
                        "ignored "
                        + str(len(stale_terminal))
                        + " prior-target terminal QA record(s): "
                        + ", ".join(sorted(stale_terminal))
                    )
                    plan["warnings"].extend(extra_warnings)
                    return plan
    elif scenario == REVIEWER_REQUEST:
        targets, operations, errors = _reviewer_request_operations(
            request, name_with_owner, pull_request_number
        )
        if not errors and operations:
            # Same generation-turnover tolerance as the QA and roundtrip
            # sweeps: a prior-TARGET ledger (different reviewers or ball
            # holder) is that round's completed history — terminal records
            # are pruned with a warning, an in-flight record marks a
            # mutation that may already have fired remotely and fails
            # closed. Non-reviewer IDs stay unknown-ID errors downstream.
            raw_results = request.get("operation_results")
            if isinstance(raw_results, dict):
                _, shape_errors = _operation_results(request)
                if shape_errors:
                    return _blocked(scenario, *shape_errors)
                known_ids = {operation["id"] for operation in operations}
                stale_terminal = []
                stale_in_flight = []
                for operation_id, record in raw_results.items():
                    if not isinstance(operation_id, str):
                        continue
                    if operation_id in known_ids:
                        continue
                    if not operation_id.startswith("reviewer."):
                        continue
                    if parsed_generation_family(
                        operation_id, REVIEWER_REQUEST_FAMILIES
                    ) is None:
                        continue
                    status = (
                        record.get("status")
                        if isinstance(record, dict)
                        else None
                    )
                    if status in ("complete", "failed", "skipped_dependency"):
                        stale_terminal.append(operation_id)
                    else:
                        stale_in_flight.append(operation_id)
                if stale_in_flight:
                    return _blocked(
                        scenario,
                        "prior-target reviewer-request operation(s) still"
                        " in flight: "
                        + ", ".join(sorted(stale_in_flight))
                        + " - verify each mutation's postcondition and"
                        " record a terminal result before planning the"
                        " current targets",
                    )
                if stale_terminal:
                    pruned = dict(request)
                    pruned["operation_results"] = {
                        operation_id: record
                        for operation_id, record in raw_results.items()
                        if operation_id not in set(stale_terminal)
                    }
                    plan = _apply_operation_state(
                        scenario, targets, operations, pruned
                    )
                    plan["warnings"].append(
                        "ignored "
                        + str(len(stale_terminal))
                        + " prior-target terminal reviewer-request"
                        " record(s): "
                        + ", ".join(sorted(stale_terminal))
                    )
                    return plan
    else:
        targets, operations, errors = _roundtrip_operations(
            request, name_with_owner, pull_request_number
        )
        if not errors and not operations:
            # Route through _operation_results so a malformed (non-dict)
            # ledger fails closed exactly like the QA path, instead of
            # falling through to silent idle (R4-F3).
            persisted, ledger_errors = _operation_results(request)
            if ledger_errors:
                return _blocked(scenario, *ledger_errors)
            if persisted:
                # A zero-operation plan must not silently orphan persisted
                # write-ahead records: a pending record marks a mutation that
                # may already have fired remotely, and even a terminal record
                # is evidence of a mutation whose target is no longer
                # plannable. Fail closed so the resume verifies postconditions
                # instead of reporting nothing-to-do.
                return _blocked(
                    scenario,
                    "no eligible reviewers remain, but "
                    f"{len(persisted)} persisted operation result(s) exist - "
                    "verify the prior mutation's postcondition before "
                    "abandoning the roundtrip",
                )
            return _idle(scenario, "no eligible reviewers remain after actor exclusion")
        if not errors and operations:
            # Generation-bound IDs make an earlier round's records orphans
            # of the current plan. Terminal orphans are that round's
            # completed history — ignore them (with a warning) instead of
            # hard-erroring as unknown IDs. An IN-FLIGHT orphan marks a
            # mutation that may already have fired remotely: fail closed
            # with the recovery named. Non-roundtrip IDs stay unknown-ID
            # errors downstream — the tolerance is scoped to this
            # scenario's own generation turnover.
            raw_results = request.get("operation_results")
            if isinstance(raw_results, dict):
                # CR 3777197527: shape-gate BEFORE any pruning, exactly like
                # the QA sweep above — pruning a malformed record launders
                # the evidence record validation exists to reject.
                _, shape_errors = _operation_results(request)
                if shape_errors:
                    return _blocked(scenario, *shape_errors)
                known_ids = {operation["id"] for operation in operations}
                stale_terminal: list[str] = []
                stale_in_flight: list[str] = []
                for operation_id, record in raw_results.items():
                    if not isinstance(operation_id, str):
                        continue
                    if operation_id in known_ids:
                        continue
                    if not operation_id.startswith("roundtrip."):
                        continue
                    # CR 3760683938: same family restriction as the QA sweep
                    # — the base before the generation/identity segments must
                    # match a family the current plan mints. Roundtrip ids
                    # carry per-reviewer identity (base:identity:gDIGEST), so
                    # compare on the leading dotted family name alone.
                    if parsed_generation_family(
                        operation_id, ROUNDTRIP_FAMILIES
                    ) is None:
                        continue
                    status = (
                        record.get("status")
                        if isinstance(record, dict)
                        else None
                    )
                    if status in ("complete", "failed", "skipped_dependency"):
                        # skipped_dependency is terminal history too — its
                        # record proves the operation NEVER fired, so there
                        # is no remote postcondition to verify (series
                        # self-review: omitting it here blocked legitimate
                        # fresh rounds after any partial failure).
                        stale_terminal.append(operation_id)
                    else:
                        stale_in_flight.append(operation_id)
                if stale_in_flight:
                    return _blocked(
                        scenario,
                        "prior-generation roundtrip operation(s) still in"
                        " flight: "
                        + ", ".join(sorted(stale_in_flight))
                        + " - verify each mutation's postcondition and record"
                        " a terminal result before planning fresh feedback",
                    )
                if stale_terminal:
                    pruned = dict(request)
                    pruned["operation_results"] = {
                        operation_id: record
                        for operation_id, record in raw_results.items()
                        if operation_id not in set(stale_terminal)
                    }
                    plan = _apply_operation_state(
                        scenario, targets, operations, pruned
                    )
                    plan["warnings"].append(
                        "ignored "
                        + str(len(stale_terminal))
                        + " prior-generation terminal roundtrip record(s): "
                        + ", ".join(sorted(stale_terminal))
                    )
                    return plan

    if errors:
        return _blocked(scenario, *errors)
    plan = _apply_operation_state(scenario, targets, operations, request)
    plan["warnings"].extend(extra_warnings)
    return plan


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (ValueError, OSError) as error:
        plan = _blocked(None, f"input must be valid JSON: {error}")
    else:
        plan = plan_handoff(request)

    json.dump(plan, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 2 if plan["state"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
