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
import json
import re
import sys
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 1

APPROVED_QA = "approved_qa"
CLEAN_UNAPPROVED = "clean_unapproved"
HUMAN_REVIEW_ROUNDTRIP = "human_review_roundtrip"
SCENARIOS = {APPROVED_QA, CLEAN_UNAPPROVED, HUMAN_REVIEW_ROUNDTRIP}
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
# Git accepts unambiguous abbreviated object IDs. Require at least seven hex
# characters while allowing full SHA-1 and SHA-256 object IDs.
GIT_OBJECT_ID = re.compile(r"[0-9a-fA-F]{7,64}")
LINEAR_WRITE_PATHS = {"environment_tool", "local_api", "none"}
ISSUE_TRACKER_TYPES = {"linear", "jira", "github", "none"}
OPERATION_RESULT_STATUSES = {"pending", "retryable", "complete", "failed"}
MAX_OPERATION_ATTEMPTS = 3

# Match nameWithOwner exactly.  Repository basename matching would incorrectly
# hand off forks such as another-owner/web-app.  The entries below are
# placeholder examples — replace them with your organization's mapping.
QA_OWNER_BY_REPOSITORY = {
    "example-org/admin-portal": {
        "github_login": "bob-qa",
        "linear_name": "Bob Example",
    },
    "example-org/api-service": {
        "github_login": "alice-qa",
        "linear_name": "Alice Example",
    },
    "example-org/marketing-site": {
        "github_login": "alice-qa",
        "linear_name": "Alice Example",
    },
    "example-org/web-app": {
        "github_login": "alice-qa",
        "linear_name": "Alice Example",
    },
}

# QA workflow state the validated ticket moves to during the first-clean-exit
# handoff, keyed by Linear team key (the ticket-identifier prefix).  The
# handoff transfers ownership AND stage: assign-only handoffs left tickets
# reading as in-progress after QA already owned them.  Workflow-state IDs are
# team-scoped, so callers resolve the ID by this exact name within the
# ticket's own team and pass it as ``issue_tracker.qa_state``.  Teams absent
# from this map get no state operation.  The shipped team keys and state
# names are placeholder examples — replace them with your tracker's values.
QA_STATE_NAME_BY_TEAM = {
    "ADM": "Ready for QA",
    "WEB": "Preview QA",
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
    if not isinstance(value, str) or not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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

    if scenario == HUMAN_REVIEW_ROUNDTRIP:
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


def _approved_qa_operations(
    request: dict[str, Any],
    name_with_owner: str,
    pull_request_number: int,
    owner: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    github_login = owner["github_login"]
    linear_name = owner["linear_name"]
    targets = {
        "assignees": [github_login],
        "reviewers": [],
        "linear_assignee": None,
    }
    operations = [
        _github_operation(
            "qa.github.replace_assignees",
            "replace_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=[],
            # This is the complete desired set, not an additive update.  Stale
            # assignees supplied by GitHub are intentionally absent.
            assignees=[github_login],
        )
    ]
    operations.append(
        _github_operation(
            "qa.github.verify_assignees",
            "verify_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=["qa.github.replace_assignees"],
            expected_assignees=[github_login],
        )
    )
    errors: list[str] = []

    issue_tracker = request.get("issue_tracker", {})
    if not isinstance(issue_tracker, dict):
        return targets, operations, ["issue_tracker must be an object"]

    tracker_type = issue_tracker.get("type", "none")
    if not isinstance(tracker_type, str):
        errors.append("issue_tracker.type must be a string")
        return targets, operations, errors
    if tracker_type not in ISSUE_TRACKER_TYPES:
        errors.append("issue_tracker.type must be one of: github, jira, linear, none")
        return targets, operations, errors

    if tracker_type == "linear":
        ticket_required = issue_tracker.get("ticket_required", True)
        if not isinstance(ticket_required, bool):
            errors.append("issue_tracker.ticket_required must be a boolean")
            return targets, operations, errors
        ticket_validated = issue_tracker.get("ticket_validated") is True
        if not ticket_required:
            ticket_exemption_reason = issue_tracker.get("ticket_exemption_reason")
            if not _is_stripped_nonempty_string(ticket_exemption_reason):
                errors.append(
                    "issue_tracker.ticket_exemption_reason must be non-empty "
                    "when a Linear ticket is not required"
                )
                return targets, operations, errors
            if not ticket_validated:
                return targets, operations, errors
        if not ticket_validated:
            errors.append("a Linear QA handoff requires a currently validated ticket")
            return targets, operations, errors
        ticket_identifier = issue_tracker.get("ticket_identifier")
        if not _is_stripped_nonempty_string(ticket_identifier):
            errors.append(
                "issue_tracker.ticket_identifier must be stripped and non-empty "
                "when a Linear ticket is validated"
            )
            return targets, operations, errors
        ticket_provider_id = issue_tracker.get("ticket_provider_id")
        if not _is_stripped_nonempty_string(ticket_provider_id):
            errors.append(
                "issue_tracker.ticket_provider_id must be stripped and non-empty "
                "when a Linear ticket is validated"
            )
            return targets, operations, errors

        write_path = issue_tracker.get("write_path")
        if write_path not in LINEAR_WRITE_PATHS:
            errors.append(
                "issue_tracker.write_path must be one of: environment_tool, local_api, none"
            )
            return targets, operations, errors

        session_environment = request.get("session_environment")
        if write_path == "local_api" and session_environment != "local":
            errors.append(
                "issue_tracker.write_path local_api requires session_environment='local'"
            )
            return targets, operations, errors

        if write_path == "none":
            operations.append(
                {
                    "id": "qa.linear.record_unavailable",
                    "service": "local",
                    "action": "record_unavailable",
                    "depends_on": ["qa.github.verify_assignees"],
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
            return targets, operations, errors

        qa_assignee = issue_tracker.get("qa_assignee")
        if not isinstance(qa_assignee, dict):
            errors.append(
                "issue_tracker.qa_assignee must contain the resolved Linear provider ID"
            )
            return targets, operations, errors
        linear_provider_id = qa_assignee.get("provider_id")
        resolved_name = qa_assignee.get("name")
        if not _is_stripped_nonempty_string(linear_provider_id):
            errors.append(
                "issue_tracker.qa_assignee.provider_id must be stripped and non-empty"
            )
            return targets, operations, errors
        if resolved_name != linear_name:
            errors.append(
                f"issue_tracker.qa_assignee.name must resolve exactly to {linear_name!r}"
            )
            return targets, operations, errors
        targets["linear_assignee"] = {
            "provider_id": linear_provider_id,
            "name": linear_name,
        }

        operations.append(
            {
                "id": "qa.linear.assign_ticket",
                "service": "linear",
                "action": "assign_ticket",
                "depends_on": ["qa.github.verify_assignees"],
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "ticket_provider_id": ticket_provider_id,
                    "assignee_id": linear_provider_id,
                    "assignee_name": linear_name,
                    "write_path": write_path,
                },
            }
        )
        operations.append(
            {
                "id": "qa.linear.verify_ticket_assignee",
                "service": "linear",
                "action": "verify_ticket_assignee",
                "depends_on": ["qa.linear.assign_ticket"],
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "ticket_provider_id": ticket_provider_id,
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
            return targets, operations, errors
        if qa_state is None:
            unresolved_reason = issue_tracker.get("qa_state_unresolved_reason")
            if not _is_stripped_nonempty_string(unresolved_reason):
                errors.append(
                    "issue_tracker.qa_state must contain the resolved "
                    f"{expected_state_name!r} workflow state for team {team_key!r}; "
                    "pass qa_state_unresolved_reason to record a manual state move"
                )
                return targets, operations, errors
            operations.append(
                {
                    "id": "qa.linear.record_state_unavailable",
                    "service": "local",
                    "action": "record_unavailable",
                    "depends_on": ["qa.linear.verify_ticket_assignee"],
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
            return targets, operations, errors
        if not isinstance(qa_state, dict):
            errors.append(
                "issue_tracker.qa_state must contain the resolved Linear "
                "workflow-state provider ID"
            )
            return targets, operations, errors
        state_provider_id = qa_state.get("provider_id")
        if not _is_stripped_nonempty_string(state_provider_id):
            errors.append(
                "issue_tracker.qa_state.provider_id must be stripped and non-empty"
            )
            return targets, operations, errors
        if qa_state.get("name") != expected_state_name:
            errors.append(
                "issue_tracker.qa_state.name must resolve exactly to "
                f"{expected_state_name!r} for team {team_key!r}"
            )
            return targets, operations, errors

        operations.append(
            {
                "id": "qa.linear.set_ticket_state",
                "service": "linear",
                "action": "set_ticket_state",
                "depends_on": ["qa.linear.verify_ticket_assignee"],
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "ticket_provider_id": ticket_provider_id,
                    "state_id": state_provider_id,
                    "state_name": expected_state_name,
                    "write_path": write_path,
                },
            }
        )
        operations.append(
            {
                "id": "qa.linear.verify_ticket_state",
                "service": "linear",
                "action": "verify_ticket_state",
                "depends_on": ["qa.linear.set_ticket_state"],
                "payload": {
                    "ticket_identifier": ticket_identifier,
                    "ticket_provider_id": ticket_provider_id,
                    "expected_state_id": state_provider_id,
                    "expected_state_name": expected_state_name,
                    "write_path": write_path,
                },
            }
        )

    return targets, operations, errors


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

    operations: list[dict[str, Any]] = []
    reviewer_verification_ids: list[str] = []
    previous_operation_id: str | None = None
    for login in reviewers:
        identity = login.casefold()
        request_id = f"roundtrip.github.request_review:{identity}"
        verify_id = f"roundtrip.github.verify_review_request:{identity}"
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

    operations.append(
        _github_operation(
            "roundtrip.github.replace_assignees",
            "replace_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=reviewer_verification_ids,
            assignees=reviewers,
        )
    )
    operations.append(
        _github_operation(
            "roundtrip.github.verify_assignees",
            "verify_pull_request_assignees",
            name_with_owner,
            pull_request_number,
            depends_on=["roundtrip.github.replace_assignees"],
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
    allowed_keys = {
        "status",
        "attempts",
        "started_at",
        "response_id",
        "verified_at",
        "error",
        "evidence",
    }
    for operation_id, raw_result in raw_results.items():
        if not isinstance(operation_id, str) or not operation_id:
            errors.append("operation_results keys must be non-empty strings")
            continue
        if not isinstance(raw_result, dict):
            errors.append(f"operation_results[{operation_id!r}] must be an object")
            continue
        unknown_keys = sorted(set(raw_result) - allowed_keys)
        if unknown_keys:
            errors.append(
                f"operation_results[{operation_id!r}] has unknown field(s): "
                + ", ".join(unknown_keys)
            )
            continue
        status = raw_result.get("status")
        attempts = raw_result.get("attempts")
        if not isinstance(status, str) or status not in OPERATION_RESULT_STATUSES:
            errors.append(
                f"operation_results[{operation_id!r}].status must be one of: "
                "complete, failed, pending, retryable"
            )
            continue
        if (
            not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < 1
            or attempts > MAX_OPERATION_ATTEMPTS
        ):
            errors.append(
                f"operation_results[{operation_id!r}].attempts must be between 1 and 3"
            )
            continue
        started_at = _iso_timestamp(raw_result.get("started_at"))
        if started_at is None:
            errors.append(
                f"operation_results[{operation_id!r}] requires the write-ahead started_at timestamp"
            )
            continue
        verified_at = _iso_timestamp(raw_result.get("verified_at"))
        if status in {"retryable", "complete", "failed"} and verified_at is None:
            errors.append(
                f"operation_results[{operation_id!r}] {status} state requires verified_at"
            )
            continue
        if (
            status in {"retryable", "complete", "failed"}
            and verified_at is not None
            and verified_at < started_at
        ):
            errors.append(
                f"operation_results[{operation_id!r}].verified_at cannot precede started_at"
            )
            continue
        if status in {"retryable", "failed"} and (
            not isinstance(raw_result.get("error"), str) or not raw_result["error"]
        ):
            errors.append(
                f"operation_results[{operation_id!r}] {status} state requires error evidence"
            )
            continue
        if status == "complete" and (
            not isinstance(raw_result.get("evidence"), dict)
            or not raw_result["evidence"]
        ):
            errors.append(
                f"operation_results[{operation_id!r}] complete state requires verification evidence"
            )
            continue
        if status == "retryable" and attempts >= MAX_OPERATION_ATTEMPTS:
            errors.append(
                f"operation_results[{operation_id!r}] exhausted the three-attempt limit"
            )
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
    in_flight = {
        operation_id
        for operation_id, result in result_records.items()
        if result["status"] in {"pending", "retryable"}
    }
    if len(in_flight) > 1:
        errors.append("only one operation may be pending or retryable at a time")
    invalid_completed = canonical_complete & automatic_failure_ids
    if invalid_completed:
        errors.append(
            "unavailable operations cannot be marked complete: "
            + ", ".join(sorted(invalid_completed))
        )

    completed_all = canonical_complete
    failed_all = canonical_failed
    terminal_ids = completed_all | failed_all
    saw_unfinished = False
    for operation in operation_specs:
        operation_id = operation["id"]
        has_result = operation_id in terminal_ids or operation_id in in_flight
        if saw_unfinished and has_result:
            errors.append(
                "operation results must form a prefix with at most one in-flight tail"
            )
            break
        if operation_id in in_flight or not has_result:
            saw_unfinished = True

    if errors:
        return _blocked(scenario, *errors)

    # A local unavailable record is a known failure, not a remote operation.
    # It becomes terminal only once every preceding operation has reached a
    # terminal result, preserving the same crash-safe sequence as remote work.
    effective_failed = set(failed_all)
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
            continue
        detail = "dependency failed: " + ", ".join(failed_dependencies)
        if operation_id in completed_all or operation_id in in_flight:
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
    for spec in operation_specs:
        operation = copy.deepcopy(spec)
        operation_id = operation["id"]
        if operation_id in completed_all:
            operation["status"] = "complete"
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
        if dependency_detail is not None and operation["status"] == "failed":
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
        if operation["status"] != "failed":
            continue
        if operation["id"] in dependency_failure_details:
            warnings.append(
                f"Operation {operation['id']} not executed "
                f"({dependency_failure_details[operation['id']]}); complete it manually."
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

        targets, operations, errors = _approved_qa_operations(
            request, name_with_owner, pull_request_number, owner
        )
    else:
        targets, operations, errors = _roundtrip_operations(
            request, name_with_owner, pull_request_number
        )
        if not errors and not operations:
            return _idle(scenario, "no eligible reviewers remain after actor exclusion")

    if errors:
        return _blocked(scenario, *errors)
    return _apply_operation_state(scenario, targets, operations, request)


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        plan = _blocked(None, f"input must be valid JSON: {error}")
    else:
        plan = plan_handoff(request)

    json.dump(plan, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 2 if plan["state"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
