from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from handoff_decision import (
    QA_OWNER_BY_REPOSITORY,
    QA_STATE_NAME_BY_TEAM,
    main,
    plan_handoff,
)


REPOSITORY = {"nameWithOwner": "example-org/web-app"}
PR_NUMBER = 3219
TIMESTAMP = "2026-07-09T20:09:07Z"
FIX_SHA = "a" * 40
REMOTE_HEAD_SHA = "b" * 40
LINEAR_QA_ASSIGNEE = {
    "provider_id": "linear-user-alice-qa",
    "name": "Alice Example",
}
LINEAR_QA_STATE_WEB = {
    "provider_id": "linear-state-vercel-preview-qa",
    "name": "Preview QA",
}
LINEAR_QA_STATE_ADM = {
    "provider_id": "linear-state-dev-ready-for-qa",
    "name": "Ready for QA",
}


def reviewer(
    login: str | None,
    *,
    account_type: str = "User",
    deleted: bool = False,
    review_bodies: dict[str, object] | None = None,
    inline_roots: dict[str, object] | None = None,
    fix_shas: list[object] | None = None,
    pushed_fix_shas: list[object] | None = None,
    blocker_remaining: bool = False,
    current_review_body_ids: list[str] | None = None,
    current_inline_root_ids: list[str] | None = None,
) -> dict[str, object]:
    resolved_review_bodies = (
        review_bodies
        if review_bodies is not None
        else {
            "review-1": {
                "updated_at": TIMESTAMP,
                "evaluated_updated_at": TIMESTAMP,
                "evaluated_at": TIMESTAMP,
                "acknowledgment_id": "ack-1",
                "acknowledgment_author": "dev-author",
            }
        }
    )
    resolved_inline_roots = (
        inline_roots
        if inline_roots is not None
        else {
            "comment-1": {
                "updated_at": TIMESTAMP,
                "replied_to_updated_at": TIMESTAMP,
                "reply_id": "reply-1",
                "replied_at": TIMESTAMP,
                "reply_author": "dev-author",
            }
        }
    )
    resolved_fix_shas = fix_shas if fix_shas is not None else [FIX_SHA]
    resolved_pushed_fix_shas = (
        pushed_fix_shas if pushed_fix_shas is not None else [FIX_SHA]
    )
    return {
        "login": login,
        "account_type": account_type,
        "deleted": deleted,
        "review_bodies": resolved_review_bodies,
        "inline_roots": resolved_inline_roots,
        "current_review_body_ids": current_review_body_ids
        if current_review_body_ids is not None
        else list(resolved_review_bodies),
        "current_inline_root_ids": current_inline_root_ids
        if current_inline_root_ids is not None
        else list(resolved_inline_roots),
        "fix_shas": resolved_fix_shas,
        "pushed_fix_shas": resolved_pushed_fix_shas,
        "pushed_through_sha": REMOTE_HEAD_SHA if resolved_fix_shas else None,
        "blocker_remaining": blocker_remaining,
    }


def github_operation(
    operation_id: str,
    action: str,
    payload: dict[str, object],
    status: str,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": operation_id,
        "service": "github",
        "action": action,
        "depends_on": depends_on or [],
        "payload": {
            "nameWithOwner": "example-org/web-app",
            "pull_request_number": PR_NUMBER,
            **payload,
        },
        "status": status,
    }


def operation_result(status: str, *, error: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "status": status,
        "attempts": 1,
        "started_at": TIMESTAMP,
        "verified_at": TIMESTAMP,
    }
    if status == "complete":
        result["evidence"] = {"postcondition": "verified"}
    if error is not None:
        result["error"] = error
    return result


class HandoffDecisionTest(unittest.TestCase):
    def test_qa_repository_mapping_is_exact(self) -> None:
        self.assertEqual(
            QA_OWNER_BY_REPOSITORY,
            {
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
            },
        )

    def test_approved_qa_plans_exact_replacement_then_linear_assignment(self) -> None:
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "existing_assignees": ["stale-owner", "dev-author", "stale-owner"],
            "issue_tracker": {
                "type": "linear",
                "qa_assignee": LINEAR_QA_ASSIGNEE,
                "qa_state": LINEAR_QA_STATE_WEB,
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "environment_tool",
            },
        }

        github = github_operation(
            "qa.github.replace_assignees",
            "replace_pull_request_assignees",
            {"assignees": ["alice-qa"]},
            "pending",
        )
        verify_github = github_operation(
            "qa.github.verify_assignees",
            "verify_pull_request_assignees",
            {"expected_assignees": ["alice-qa"]},
            "waiting",
            ["qa.github.replace_assignees"],
        )
        linear = {
            "id": "qa.linear.assign_ticket",
            "service": "linear",
            "action": "assign_ticket",
            "depends_on": ["qa.github.verify_assignees"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "assignee_id": "linear-user-alice-qa",
                "assignee_name": "Alice Example",
                "write_path": "environment_tool",
            },
            "status": "waiting",
        }
        verify_linear = {
            "id": "qa.linear.verify_ticket_assignee",
            "service": "linear",
            "action": "verify_ticket_assignee",
            "depends_on": ["qa.linear.assign_ticket"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "expected_assignee_id": "linear-user-alice-qa",
                "expected_assignee_name": "Alice Example",
                "write_path": "environment_tool",
            },
            "status": "waiting",
        }
        set_state = {
            "id": "qa.linear.set_ticket_state",
            "service": "linear",
            "action": "set_ticket_state",
            "depends_on": ["qa.linear.verify_ticket_assignee"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "state_id": "linear-state-vercel-preview-qa",
                "state_name": "Preview QA",
                "write_path": "environment_tool",
            },
            "status": "waiting",
        }
        verify_state = {
            "id": "qa.linear.verify_ticket_state",
            "service": "linear",
            "action": "verify_ticket_state",
            "depends_on": ["qa.linear.set_ticket_state"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "expected_state_id": "linear-state-vercel-preview-qa",
                "expected_state_name": "Preview QA",
                "write_path": "environment_tool",
            },
            "status": "waiting",
        }
        self.assertEqual(
            plan_handoff(request),
            {
                "version": 1,
                "scenario": "approved_qa",
                "state": "pending",
                "reason": None,
                "targets": {
                    "assignees": ["alice-qa"],
                    "reviewers": [],
                    "linear_assignee": LINEAR_QA_ASSIGNEE,
                },
                "operations": [
                    github,
                    verify_github,
                    linear,
                    verify_linear,
                    set_state,
                    verify_state,
                ],
                "call_plan": [github],
                "warnings": [],
                "errors": [],
            },
        )

    def test_qa_state_mapping_is_exact(self) -> None:
        self.assertEqual(
            QA_STATE_NAME_BY_TEAM,
            {
                "ADM": "Ready for QA",
                "WEB": "Preview QA",
            },
        )

    def test_adm_ticket_moves_to_dev_ready_for_qa(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "qa_state": LINEAR_QA_STATE_ADM,
                    "ticket_identifier": "ADM-769",
                    "ticket_provider_id": "linear-ticket-adm-769",
                    "ticket_validated": True,
                    "write_path": "environment_tool",
                },
            }
        )

        self.assertEqual(plan["state"], "pending")
        set_state = next(
            operation
            for operation in plan["operations"]
            if operation["id"] == "qa.linear.set_ticket_state"
        )
        self.assertEqual(set_state["payload"]["state_name"], "Ready for QA")
        self.assertEqual(
            set_state["payload"]["state_id"], "linear-state-dev-ready-for-qa"
        )

    def test_qa_state_name_must_match_ticket_team(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    # WEB's state supplied for an ADM ticket.
                    "qa_state": LINEAR_QA_STATE_WEB,
                    "ticket_identifier": "ADM-769",
                    "ticket_provider_id": "linear-ticket-adm-769",
                    "ticket_validated": True,
                    "write_path": "environment_tool",
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(
            plan["errors"],
            [
                "issue_tracker.qa_state.name must resolve exactly to "
                "'Ready for QA' for team 'ADM'"
            ],
        )

    def test_mapped_team_requires_qa_state_or_unresolved_reason(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "environment_tool",
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(
            plan["errors"],
            [
                "issue_tracker.qa_state must contain the resolved "
                "'Preview QA' workflow state for team 'WEB'; "
                "pass qa_state_unresolved_reason to record a manual state move"
            ],
        )

    def test_unresolved_qa_state_records_nonblocking_local_failure(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "qa_state": None,
                    "qa_state_unresolved_reason": "state renamed in Linear",
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "environment_tool",
                },
                "operation_results": {
                    "qa.github.replace_assignees": operation_result("complete"),
                    "qa.github.verify_assignees": operation_result("complete"),
                    "qa.linear.assign_ticket": operation_result("complete"),
                    "qa.linear.verify_ticket_assignee": operation_result("complete"),
                },
            }
        )

        self.assertEqual(plan["state"], "failed")
        record = next(
            operation
            for operation in plan["operations"]
            if operation["id"] == "qa.linear.record_state_unavailable"
        )
        self.assertEqual(record["service"], "local")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "state renamed in Linear")
        self.assertEqual(
            record["payload"]["expected_state_name"], "Preview QA"
        )
        self.assertEqual(
            plan["warnings"],
            [
                "Local operation qa.linear.record_state_unavailable recorded "
                "unavailable; complete it manually."
            ],
        )

    def test_unmapped_team_plans_no_state_move(self) -> None:
        base_issue_tracker = {
            "type": "linear",
            "qa_assignee": LINEAR_QA_ASSIGNEE,
            "ticket_identifier": "AI-2627",
            "ticket_provider_id": "linear-ticket-ai-2627",
            "ticket_validated": True,
            "write_path": "environment_tool",
        }
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": dict(base_issue_tracker),
            }
        )

        self.assertEqual(plan["state"], "pending")
        self.assertEqual(
            [
                operation["id"]
                for operation in plan["operations"]
                if operation["service"] == "linear"
            ],
            ["qa.linear.assign_ticket", "qa.linear.verify_ticket_assignee"],
        )

        supplied_state = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    **base_issue_tracker,
                    "qa_state": LINEAR_QA_STATE_WEB,
                },
            }
        )
        self.assertEqual(supplied_state["state"], "blocked")
        self.assertEqual(
            supplied_state["errors"],
            [
                "issue_tracker.qa_state must be omitted for team 'AI', "
                "which has no mapped QA workflow state"
            ],
        )

    def test_clean_unapproved_plans_the_same_qa_handoff_as_approved(self) -> None:
        # QA handoff fires at the FIRST clean exit: the clean-but-unapproved
        # paused exit plans the identical operations as the approved exit —
        # preview QA runs in parallel with code review. Only the echoed
        # scenario differs; the paused exit still never writes `complete`.
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "existing_assignees": ["stale-owner", "dev-author"],
            "issue_tracker": {
                "type": "linear",
                "qa_assignee": LINEAR_QA_ASSIGNEE,
                "qa_state": LINEAR_QA_STATE_WEB,
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "environment_tool",
            },
        }

        approved = plan_handoff(request)
        paused = plan_handoff({**request, "scenario": "clean_unapproved"})

        self.assertEqual(approved["state"], "pending")
        self.assertEqual(paused, {**approved, "scenario": "clean_unapproved"})

    def test_clean_unapproved_unmapped_repository_stays_idle(self) -> None:
        self.assertEqual(
            plan_handoff(
                {
                    "scenario": "clean_unapproved",
                    "repository": {"nameWithOwner": "another-owner/web-app"},
                    "pull_request_number": PR_NUMBER,
                }
            ),
            {
                "version": 1,
                "scenario": "clean_unapproved",
                "state": "idle",
                "reason": "repository.nameWithOwner is not in the exact QA-owner map",
                "targets": {
                    "assignees": [],
                    "reviewers": [],
                    "linear_assignee": None,
                },
                "operations": [],
                "call_plan": [],
                "warnings": [],
                "errors": [],
            },
        )

    def _qa_request_with_results(
        self, operation_results: dict[str, object]
    ) -> dict[str, object]:
        return {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "issue_tracker": {
                "type": "linear",
                "qa_assignee": LINEAR_QA_ASSIGNEE,
                "qa_state": LINEAR_QA_STATE_WEB,
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "environment_tool",
            },
            "operation_results": operation_results,
        }

    def test_failed_mutation_cascades_to_dependents(self) -> None:
        # A terminally failed mutation must never queue its dependents: the
        # verification's expected postcondition is already known to be false,
        # and the chained state move is declared to depend on it. All
        # descendants fail closed with dependency errors instead of becoming
        # the next call.
        plan = plan_handoff(
            self._qa_request_with_results(
                {
                    "qa.github.replace_assignees": operation_result("complete"),
                    "qa.github.verify_assignees": operation_result("complete"),
                    "qa.linear.assign_ticket": operation_result(
                        "failed", error="Linear returned 500"
                    ),
                }
            )
        )

        self.assertEqual(plan["state"], "failed")
        self.assertEqual(plan["call_plan"], [])
        statuses = {op["id"]: op["status"] for op in plan["operations"]}
        self.assertEqual(
            statuses,
            {
                "qa.github.replace_assignees": "complete",
                "qa.github.verify_assignees": "complete",
                "qa.linear.assign_ticket": "failed",
                "qa.linear.verify_ticket_assignee": "failed",
                "qa.linear.set_ticket_state": "failed",
                "qa.linear.verify_ticket_state": "failed",
            },
        )
        errors_by_id = {
            op["id"]: op.get("error")
            for op in plan["operations"]
            if op["status"] == "failed"
        }
        self.assertEqual(
            errors_by_id,
            {
                "qa.linear.assign_ticket": None,
                "qa.linear.verify_ticket_assignee": "dependency failed: qa.linear.assign_ticket",
                "qa.linear.set_ticket_state": "dependency failed: qa.linear.verify_ticket_assignee",
                "qa.linear.verify_ticket_state": "dependency failed: qa.linear.set_ticket_state",
            },
        )
        self.assertEqual(
            plan["warnings"],
            [
                "Remote operation qa.linear.assign_ticket failed; complete it manually.",
                "Operation qa.linear.verify_ticket_assignee not executed "
                "(dependency failed: qa.linear.assign_ticket); complete it manually.",
                "Operation qa.linear.set_ticket_state not executed "
                "(dependency failed: qa.linear.verify_ticket_assignee); complete it manually.",
                "Operation qa.linear.verify_ticket_state not executed "
                "(dependency failed: qa.linear.set_ticket_state); complete it manually.",
            ],
        )

    def test_descendant_result_after_failed_dependency_is_blocked(self) -> None:
        # A canonical result on a descendant of a failed dependency means the
        # caller executed an operation this planner would never have queued —
        # an inconsistent ledger fails closed.
        plan = plan_handoff(
            self._qa_request_with_results(
                {
                    "qa.github.replace_assignees": operation_result("complete"),
                    "qa.github.verify_assignees": operation_result("complete"),
                    "qa.linear.assign_ticket": operation_result(
                        "failed", error="Linear returned 500"
                    ),
                    "qa.linear.verify_ticket_assignee": operation_result("complete"),
                }
            )
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(
            plan["errors"],
            [
                "operation qa.linear.verify_ticket_assignee cannot have results: "
                "dependency failed: qa.linear.assign_ticket"
            ],
        )

    def test_managed_environment_tool_needs_no_raw_key(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "session_environment": "managed",
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "qa_state": LINEAR_QA_STATE_WEB,
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "environment_tool",
                },
            }
        )

        self.assertEqual(plan["state"], "pending")
        self.assertEqual(
            [
                operation["payload"]["write_path"]
                for operation in plan["operations"]
                if operation["service"] == "linear"
            ],
            ["environment_tool"] * 4,
        )
        self.assertNotIn("api_key", json.dumps(plan))

    def test_managed_environment_rejects_local_api_route(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "session_environment": "managed",
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "local_api",
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["operations"], [])
        self.assertEqual(plan["call_plan"], [])
        self.assertEqual(
            plan["errors"],
            ["issue_tracker.write_path local_api requires session_environment='local'"],
        )

    def test_local_api_route_is_allowed_only_in_local_session(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "session_environment": "local",
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "qa_state": LINEAR_QA_STATE_WEB,
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "local_api",
                },
            }
        )

        self.assertEqual(plan["state"], "pending")
        self.assertEqual(
            [
                operation["payload"]["write_path"]
                for operation in plan["operations"]
                if operation["service"] == "linear"
            ],
            ["local_api"] * 4,
        )

    def test_unknown_tracker_type_is_rejected(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {"type": "linera"},
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(
            plan["errors"],
            ["issue_tracker.type must be one of: github, jira, linear, none"],
        )

    def test_no_tracker_write_path_records_nonblocking_local_failure(self) -> None:
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "session_environment": "managed",
            "issue_tracker": {
                "type": "linear",
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "none",
            },
            "operation_results": {
                "qa.github.replace_assignees": operation_result("complete"),
                "qa.github.verify_assignees": operation_result("complete"),
            },
        }

        self.assertEqual(
            plan_handoff(request),
            {
                "version": 1,
                "scenario": "approved_qa",
                "state": "failed",
                "reason": None,
                "targets": {
                    "assignees": ["alice-qa"],
                    "reviewers": [],
                    "linear_assignee": None,
                },
                "operations": [
                    github_operation(
                        "qa.github.replace_assignees",
                        "replace_pull_request_assignees",
                        {"assignees": ["alice-qa"]},
                        "complete",
                    ),
                    github_operation(
                        "qa.github.verify_assignees",
                        "verify_pull_request_assignees",
                        {"expected_assignees": ["alice-qa"]},
                        "complete",
                        ["qa.github.replace_assignees"],
                    ),
                    {
                        "id": "qa.linear.record_unavailable",
                        "service": "local",
                        "action": "record_unavailable",
                        "depends_on": ["qa.github.verify_assignees"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "expected_assignee_name": "Alice Example",
                            "expected_state_name": "Preview QA",
                            "write_path": "none",
                        },
                        "status": "failed",
                        "error": "No authorized Linear write path is available.",
                    },
                ],
                "call_plan": [],
                "warnings": [
                    "Local operation qa.linear.record_unavailable recorded unavailable; complete it manually."
                ],
                "errors": [],
            },
        )

    def test_unavailable_tracker_operation_cannot_be_marked_complete(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    "type": "linear",
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "none",
                },
                "operation_results": {
                    "qa.github.replace_assignees": operation_result("complete"),
                    "qa.github.verify_assignees": operation_result("complete"),
                    "qa.linear.record_unavailable": operation_result("complete"),
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["call_plan"], [])
        self.assertEqual(
            plan["errors"],
            [
                "unavailable operations cannot be marked complete: qa.linear.record_unavailable"
            ],
        )

    def test_same_name_fork_does_not_match_qa_mapping(self) -> None:
        self.assertEqual(
            plan_handoff(
                {
                    "scenario": "approved_qa",
                    "repository": {"nameWithOwner": "another-owner/web-app"},
                    "pull_request_number": PR_NUMBER,
                }
            ),
            {
                "version": 1,
                "scenario": "approved_qa",
                "state": "idle",
                "reason": "repository.nameWithOwner is not in the exact QA-owner map",
                "targets": {
                    "assignees": [],
                    "reviewers": [],
                    "linear_assignee": None,
                },
                "operations": [],
                "call_plan": [],
                "warnings": [],
                "errors": [],
            },
        )

    def test_roundtrip_sorts_deduplicates_and_excludes_actor(self) -> None:
        request = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "dev-author",
            "reviewers": [
                reviewer("zoe"),
                reviewer("dev-author"),
                reviewer("alice"),
                reviewer("zoe"),
            ],
        }

        alice = github_operation(
            "roundtrip.github.request_review:alice",
            "request_pull_request_review",
            {"reviewer": "alice"},
            "pending",
        )
        verify_alice = github_operation(
            "roundtrip.github.verify_review_request:alice",
            "verify_pull_request_review_request",
            {"expected_reviewer": "alice"},
            "waiting",
            ["roundtrip.github.request_review:alice"],
        )
        zoe = github_operation(
            "roundtrip.github.request_review:zoe",
            "request_pull_request_review",
            {"reviewer": "zoe"},
            "waiting",
            ["roundtrip.github.verify_review_request:alice"],
        )
        verify_zoe = github_operation(
            "roundtrip.github.verify_review_request:zoe",
            "verify_pull_request_review_request",
            {"expected_reviewer": "zoe"},
            "waiting",
            ["roundtrip.github.request_review:zoe"],
        )
        replace = github_operation(
            "roundtrip.github.replace_assignees",
            "replace_pull_request_assignees",
            {"assignees": ["alice", "zoe"]},
            "waiting",
            [
                "roundtrip.github.verify_review_request:alice",
                "roundtrip.github.verify_review_request:zoe",
            ],
        )
        verify_assignees = github_operation(
            "roundtrip.github.verify_assignees",
            "verify_pull_request_assignees",
            {"expected_assignees": ["alice", "zoe"]},
            "waiting",
            ["roundtrip.github.replace_assignees"],
        )
        self.assertEqual(
            plan_handoff(request),
            {
                "version": 1,
                "scenario": "human_review_roundtrip",
                "state": "pending",
                "reason": None,
                "targets": {
                    "assignees": ["alice", "zoe"],
                    "reviewers": ["alice", "zoe"],
                    "linear_assignee": None,
                },
                "operations": [
                    alice,
                    verify_alice,
                    zoe,
                    verify_zoe,
                    replace,
                    verify_assignees,
                ],
                "call_plan": [alice],
                "warnings": [],
                "errors": [],
            },
        )

    def test_roundtrip_rejects_malformed_authenticated_actor(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": " me ",
                "reviewers": [reviewer("me")],
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(
            plan["errors"], ["authenticated_actor must be a valid GitHub login"]
        )

    def test_multi_reviewer_partial_resume_advances_one_operation_at_a_time(
        self,
    ) -> None:
        request = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "dev-author",
            "reviewers": [reviewer("zoe"), reviewer("alice")],
            "operation_results": {
                "roundtrip.github.request_review:alice": operation_result("complete"),
                "roundtrip.github.verify_review_request:alice": operation_result(
                    "complete"
                ),
            },
        }

        alice = github_operation(
            "roundtrip.github.request_review:alice",
            "request_pull_request_review",
            {"reviewer": "alice"},
            "complete",
        )
        verify_alice = github_operation(
            "roundtrip.github.verify_review_request:alice",
            "verify_pull_request_review_request",
            {"expected_reviewer": "alice"},
            "complete",
            ["roundtrip.github.request_review:alice"],
        )
        zoe = github_operation(
            "roundtrip.github.request_review:zoe",
            "request_pull_request_review",
            {"reviewer": "zoe"},
            "pending",
            ["roundtrip.github.verify_review_request:alice"],
        )
        verify_zoe = github_operation(
            "roundtrip.github.verify_review_request:zoe",
            "verify_pull_request_review_request",
            {"expected_reviewer": "zoe"},
            "waiting",
            ["roundtrip.github.request_review:zoe"],
        )
        replace = github_operation(
            "roundtrip.github.replace_assignees",
            "replace_pull_request_assignees",
            {"assignees": ["alice", "zoe"]},
            "waiting",
            [
                "roundtrip.github.verify_review_request:alice",
                "roundtrip.github.verify_review_request:zoe",
            ],
        )
        verify_assignees = github_operation(
            "roundtrip.github.verify_assignees",
            "verify_pull_request_assignees",
            {"expected_assignees": ["alice", "zoe"]},
            "waiting",
            ["roundtrip.github.replace_assignees"],
        )
        self.assertEqual(
            plan_handoff(request),
            {
                "version": 1,
                "scenario": "human_review_roundtrip",
                "state": "pending",
                "reason": None,
                "targets": {
                    "assignees": ["alice", "zoe"],
                    "reviewers": ["alice", "zoe"],
                    "linear_assignee": None,
                },
                "operations": [
                    alice,
                    verify_alice,
                    zoe,
                    verify_zoe,
                    replace,
                    verify_assignees,
                ],
                "call_plan": [zoe],
                "warnings": [],
                "errors": [],
            },
        )

        request["operation_results"] = {
            "roundtrip.github.request_review:alice": operation_result("complete"),
            "roundtrip.github.verify_review_request:alice": operation_result(
                "complete"
            ),
            "roundtrip.github.request_review:zoe": operation_result("complete"),
            "roundtrip.github.verify_review_request:zoe": operation_result("complete"),
        }
        resumed = plan_handoff(request)
        self.assertEqual(resumed["state"], "pending")
        self.assertEqual(
            resumed["call_plan"],
            [
                github_operation(
                    "roundtrip.github.replace_assignees",
                    "replace_pull_request_assignees",
                    {"assignees": ["alice", "zoe"]},
                    "pending",
                    [
                        "roundtrip.github.verify_review_request:alice",
                        "roundtrip.github.verify_review_request:zoe",
                    ],
                )
            ],
        )

    def test_in_flight_mutation_requires_verification_before_retry(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    "qa.github.replace_assignees": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                        "response_id": None,
                    }
                },
            }
        )

        self.assertEqual(plan["state"], "resume_verification_required")
        self.assertEqual(plan["operations"][0]["status"], "in_flight")
        self.assertEqual(plan["call_plan"][0]["action"], "verify_before_retry")
        self.assertEqual(
            plan["call_plan"][0]["payload"]["verification_operation"]["id"],
            "qa.github.verify_assignees",
        )

    def test_failed_resume_verification_can_retry_with_write_ahead(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    "qa.github.replace_assignees": {
                        "status": "retryable",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                        "verified_at": TIMESTAMP,
                        "error": "postcondition absent",
                    }
                },
            }
        )

        self.assertEqual(plan["state"], "pending")
        self.assertEqual(plan["call_plan"][0]["id"], "qa.github.replace_assignees")
        self.assertEqual(plan["call_plan"][0]["attempt"], 2)
        self.assertTrue(plan["call_plan"][0]["requires_pending_write"])

    def test_malformed_operation_status_blocks_without_crashing(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    "qa.github.replace_assignees": {
                        "status": [],
                        "attempts": 1,
                    }
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertIn(".status must be one of", plan["errors"][0])

    def test_terminal_operation_requires_verification_metadata(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    "qa.github.replace_assignees": {
                        "status": "complete",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    }
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertIn("complete state requires verified_at", plan["errors"][0])

    def test_verified_at_cannot_precede_started_at(self) -> None:
        for status in ("retryable", "complete", "failed"):
            result: dict[str, object] = {
                "status": status,
                "attempts": 1,
                "started_at": "2026-07-09T20:09:08Z",
                "verified_at": TIMESTAMP,
            }
            if status == "complete":
                result["evidence"] = {"postcondition": "verified"}
            else:
                result["error"] = "postcondition absent"

            with self.subTest(status=status):
                plan = plan_handoff(
                    {
                        "scenario": "approved_qa",
                        "repository": REPOSITORY,
                        "pull_request_number": PR_NUMBER,
                        "operation_results": {
                            "qa.github.replace_assignees": result,
                        },
                    }
                )

                self.assertEqual(plan["state"], "blocked")
                self.assertEqual(
                    plan["errors"],
                    [
                        "operation_results['qa.github.replace_assignees'].verified_at "
                        "cannot precede started_at"
                    ],
                )

    def test_complete_operation_requires_postcondition_evidence(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    "qa.github.replace_assignees": {
                        "status": "complete",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                        "verified_at": TIMESTAMP,
                    }
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertIn("requires verification evidence", plan["errors"][0])

    def test_operation_results_reject_unknown_secret_fields(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    "qa.github.replace_assignees": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                        "api_key": "secret-value",
                    }
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertIn("unknown field(s): api_key", plan["errors"][0])
        self.assertNotIn("secret-value", json.dumps(plan))

    def test_github_success_linear_failure_is_terminal_warning(self) -> None:
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "issue_tracker": {
                "type": "linear",
                "qa_assignee": LINEAR_QA_ASSIGNEE,
                "qa_state": LINEAR_QA_STATE_WEB,
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "environment_tool",
            },
            "operation_results": {},
        }

        # GitHub mutation and verification succeeded. Every Linear mutation and
        # verification reached terminal failure without undoing GitHub.
        request["operation_results"] = {
            "qa.github.replace_assignees": operation_result("complete"),
            "qa.github.verify_assignees": operation_result("complete"),
            "qa.linear.assign_ticket": operation_result(
                "failed", error="assignment failed"
            ),
            "qa.linear.verify_ticket_assignee": operation_result(
                "failed", error="verification failed"
            ),
            "qa.linear.set_ticket_state": operation_result(
                "failed", error="state move failed"
            ),
            "qa.linear.verify_ticket_state": operation_result(
                "failed", error="state verification failed"
            ),
        }

        self.assertEqual(
            plan_handoff(request),
            {
                "version": 1,
                "scenario": "approved_qa",
                "state": "failed",
                "reason": None,
                "targets": {
                    "assignees": ["alice-qa"],
                    "reviewers": [],
                    "linear_assignee": LINEAR_QA_ASSIGNEE,
                },
                "operations": [
                    github_operation(
                        "qa.github.replace_assignees",
                        "replace_pull_request_assignees",
                        {"assignees": ["alice-qa"]},
                        "complete",
                    ),
                    github_operation(
                        "qa.github.verify_assignees",
                        "verify_pull_request_assignees",
                        {"expected_assignees": ["alice-qa"]},
                        "complete",
                        ["qa.github.replace_assignees"],
                    ),
                    {
                        "id": "qa.linear.assign_ticket",
                        "service": "linear",
                        "action": "assign_ticket",
                        "depends_on": ["qa.github.verify_assignees"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "assignee_id": "linear-user-alice-qa",
                            "assignee_name": "Alice Example",
                            "write_path": "environment_tool",
                        },
                        "status": "failed",
                    },
                    {
                        "id": "qa.linear.verify_ticket_assignee",
                        "service": "linear",
                        "action": "verify_ticket_assignee",
                        "depends_on": ["qa.linear.assign_ticket"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "expected_assignee_id": "linear-user-alice-qa",
                            "expected_assignee_name": "Alice Example",
                            "write_path": "environment_tool",
                        },
                        "status": "failed",
                    },
                    {
                        "id": "qa.linear.set_ticket_state",
                        "service": "linear",
                        "action": "set_ticket_state",
                        "depends_on": ["qa.linear.verify_ticket_assignee"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "state_id": "linear-state-vercel-preview-qa",
                            "state_name": "Preview QA",
                            "write_path": "environment_tool",
                        },
                        "status": "failed",
                    },
                    {
                        "id": "qa.linear.verify_ticket_state",
                        "service": "linear",
                        "action": "verify_ticket_state",
                        "depends_on": ["qa.linear.set_ticket_state"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "expected_state_id": "linear-state-vercel-preview-qa",
                            "expected_state_name": "Preview QA",
                            "write_path": "environment_tool",
                        },
                        "status": "failed",
                    },
                ],
                "call_plan": [],
                "warnings": [
                    "Remote operation qa.linear.assign_ticket failed; complete it manually.",
                    "Remote operation qa.linear.verify_ticket_assignee failed; complete it manually.",
                    "Remote operation qa.linear.set_ticket_state failed; complete it manually.",
                    "Remote operation qa.linear.verify_ticket_state failed; complete it manually.",
                ],
                "errors": [],
            },
        )

    def test_reviewer_identity_failures_close_without_remote_calls(self) -> None:
        invalid_reviewers = {
            "unknown": reviewer("mystery", account_type="Unknown"),
            "deleted": reviewer("former-user", deleted=True),
            "bot_type": reviewer("automation", account_type="Bot"),
            "invalid_login_syntax": reviewer("automation[bot]"),
        }
        for label, invalid in invalid_reviewers.items():
            with self.subTest(label=label):
                plan = plan_handoff(
                    {
                        "scenario": "human_review_roundtrip",
                        "repository": REPOSITORY,
                        "pull_request_number": PR_NUMBER,
                        "authenticated_actor": "dev-author",
                        "reviewers": [invalid],
                    }
                )
                self.assertEqual(plan["state"], "blocked")
                self.assertEqual(plan["operations"], [])
                self.assertEqual(plan["call_plan"], [])
                self.assertTrue(plan["errors"])

    def test_edited_review_timestamp_invalidates_roundtrip(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [
                    reviewer(
                        "alice",
                        inline_roots={
                            "comment-1": {
                                "updated_at": "2026-07-09T21:00:00Z",
                                "replied_to_updated_at": TIMESTAMP,
                                "reply_id": "reply-1",
                                "replied_at": "2026-07-09T21:00:00Z",
                                "reply_author": "dev-author",
                            }
                        },
                    )
                ],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["operations"], [])
        self.assertEqual(plan["call_plan"], [])
        self.assertEqual(
            plan["errors"],
            ["reviewer 'alice' inline root 'comment-1' changed after reply"],
        )

    def test_missing_inline_reply_invalidates_roundtrip(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [
                    reviewer(
                        "alice",
                        inline_roots={
                            "comment-1": {
                                "updated_at": TIMESTAMP,
                                "replied_to_updated_at": TIMESTAMP,
                                "reply_id": None,
                                "replied_at": TIMESTAMP,
                                "reply_author": "dev-author",
                            }
                        },
                    )
                ],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("has no verified reply", plan["errors"][0])

    def test_unevaluated_review_body_invalidates_roundtrip(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [
                    reviewer(
                        "alice",
                        review_bodies={
                            "review-1": {
                                "updated_at": TIMESTAMP,
                                "evaluated_updated_at": TIMESTAMP,
                                "evaluated_at": None,
                                "acknowledgment_id": "ack-1",
                                "acknowledgment_author": "dev-author",
                            }
                        },
                    )
                ],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("has no valid evaluation timestamp", plan["errors"][0])

    def test_reply_before_latest_edit_invalidates_roundtrip(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [
                    reviewer(
                        "alice",
                        inline_roots={
                            "comment-1": {
                                "updated_at": TIMESTAMP,
                                "replied_to_updated_at": TIMESTAMP,
                                "reply_id": "reply-1",
                                "replied_at": "2026-07-09T19:00:00Z",
                                "reply_author": "dev-author",
                            }
                        },
                    )
                ],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("was replied to before its latest edit", plan["errors"][0])

    def test_incomplete_live_feedback_id_set_invalidates_roundtrip(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [
                    reviewer(
                        "alice",
                        current_inline_root_ids=["comment-1", "comment-2"],
                    )
                ],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn(
            "current inline-root IDs do not exactly match stored evidence",
            plan["errors"][0],
        )

    def test_reply_from_another_actor_invalidates_roundtrip(self) -> None:
        root = reviewer("alice")
        root["inline_roots"]["comment-1"]["reply_author"] = "someone-else"
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [root],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("reply is not by the authenticated actor", plan["errors"][0])

    def test_unpushed_fix_invalidates_roundtrip(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [
                    reviewer("alice", fix_shas=[FIX_SHA], pushed_fix_shas=[])
                ],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn(f"fixes are not pushed: {FIX_SHA}", plan["errors"][0])

    def test_remaining_blocker_invalidates_roundtrip(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [reviewer("alice", blocker_remaining=True)],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("a reviewer blocker remains or is unknown", plan["errors"][0])

    def test_malformed_fix_sha_list_blocks_without_crashing(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [reviewer("alice", fix_shas=[{"bad": "shape"}])],
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("fix_shas must be a list", plan["errors"][0])

    def test_malformed_fix_sha_evidence_cannot_qualify_roundtrip(self) -> None:
        cases = (
            ("fix_shas", [" "]),
            ("fix_shas", ["not-hex"]),
            ("pushed_fix_shas", [" "]),
            ("pushed_fix_shas", ["not-hex"]),
            ("pushed_through_sha", " "),
            ("pushed_through_sha", "not-hex"),
        )
        for field, value in cases:
            reviewer_record = reviewer("alice")
            reviewer_record[field] = value
            with self.subTest(field=field, value=value):
                plan = plan_handoff(
                    {
                        "scenario": "human_review_roundtrip",
                        "repository": REPOSITORY,
                        "pull_request_number": PR_NUMBER,
                        "authenticated_actor": "dev-author",
                        "reviewers": [reviewer_record],
                    }
                )

                self.assertEqual(plan["state"], "blocked")
                self.assertTrue(
                    any(field in error for error in plan["errors"]), plan["errors"]
                )

    def test_unvalidated_linear_ticket_blocks_qa_plan(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "ticket_identifier": "WEB-8877",
                    "ticket_validated": False,
                    "write_path": "environment_tool",
                },
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(
            plan["errors"],
            ["a Linear QA handoff requires a currently validated ticket"],
        )

    def test_validated_linear_ticket_requires_identifier_and_provider_id(self) -> None:
        tracker = {
            "type": "linear",
            "qa_assignee": LINEAR_QA_ASSIGNEE,
            "ticket_identifier": "WEB-8877",
            "ticket_provider_id": "linear-ticket-web-8877",
            "ticket_validated": True,
            "write_path": "environment_tool",
        }
        for missing_field in ("ticket_identifier", "ticket_provider_id"):
            incomplete_tracker = dict(tracker)
            del incomplete_tracker[missing_field]
            with self.subTest(missing_field=missing_field):
                plan = plan_handoff(
                    {
                        "scenario": "approved_qa",
                        "repository": REPOSITORY,
                        "pull_request_number": PR_NUMBER,
                        "issue_tracker": incomplete_tracker,
                    }
                )

                self.assertEqual(plan["state"], "blocked")
                self.assertEqual(
                    plan["errors"],
                    [
                        f"issue_tracker.{missing_field} must be stripped and "
                        "non-empty when a Linear ticket is validated"
                    ],
                )

    def test_linear_provider_identifiers_must_be_stripped_and_nonempty(self) -> None:
        base_tracker = {
            "type": "linear",
            "qa_assignee": LINEAR_QA_ASSIGNEE,
            "ticket_identifier": "WEB-8877",
            "ticket_provider_id": "linear-ticket-web-8877",
            "ticket_validated": True,
            "write_path": "environment_tool",
        }
        for field in ("ticket_identifier", "ticket_provider_id", "qa_assignee"):
            tracker = dict(base_tracker)
            tracker["qa_assignee"] = dict(LINEAR_QA_ASSIGNEE)
            if field == "qa_assignee":
                tracker["qa_assignee"]["provider_id"] = " linear-user-alice-qa "
                expected_field = "qa_assignee.provider_id"
            else:
                tracker[field] = " "
                expected_field = field
            with self.subTest(field=field):
                plan = plan_handoff(
                    {
                        "scenario": "approved_qa",
                        "repository": REPOSITORY,
                        "pull_request_number": PR_NUMBER,
                        "issue_tracker": tracker,
                    }
                )

                self.assertEqual(plan["state"], "blocked")
                self.assertTrue(
                    any(
                        f"issue_tracker.{expected_field}" in error
                        for error in plan["errors"]
                    ),
                    plan["errors"],
                )

    def test_ticket_exemption_requires_persisted_reason(self) -> None:
        for reason in (None, "", "   "):
            tracker = {
                "type": "linear",
                "ticket_required": False,
                "ticket_validated": False,
                "write_path": "environment_tool",
            }
            if reason is not None:
                tracker["ticket_exemption_reason"] = reason

            with self.subTest(reason=reason):
                plan = plan_handoff(
                    {
                        "scenario": "approved_qa",
                        "repository": REPOSITORY,
                        "pull_request_number": PR_NUMBER,
                        "issue_tracker": tracker,
                    }
                )

                self.assertEqual(plan["state"], "blocked")
                self.assertEqual(
                    plan["errors"],
                    [
                        "issue_tracker.ticket_exemption_reason must be non-empty when "
                        "a Linear ticket is not required"
                    ],
                )

    def test_validated_exempt_ticket_still_requires_exemption_reason(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    "type": "linear",
                    "ticket_required": False,
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "environment_tool",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                },
            }
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(
            plan["errors"],
            [
                "issue_tracker.ticket_exemption_reason must be non-empty when a "
                "Linear ticket is not required"
            ],
        )

    def test_ticket_exempt_linear_pr_keeps_github_qa_handoff(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": {
                    "type": "linear",
                    "ticket_required": False,
                    "ticket_validated": False,
                    "ticket_exemption_reason": "branch matches chore/*",
                    "write_path": "environment_tool",
                },
            }
        )

        self.assertEqual(plan["state"], "pending")
        self.assertEqual(plan["targets"]["linear_assignee"], None)
        self.assertTrue(
            all(operation["service"] == "github" for operation in plan["operations"])
        )

    def test_out_of_order_result_is_rejected(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "dev-author",
                "reviewers": [reviewer("alice"), reviewer("zoe")],
                "operation_results": {
                    "roundtrip.github.replace_assignees": operation_result("complete")
                },
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["operations"], [])
        self.assertEqual(plan["call_plan"], [])
        self.assertEqual(
            plan["errors"],
            ["operation results must form a prefix with at most one in-flight tail"],
        )

    def test_cli_reads_json_and_writes_only_the_plan(self) -> None:
        request = {
            "scenario": "clean_unapproved",
            "repository": {"nameWithOwner": "another-owner/web-app"},
            "pull_request_number": PR_NUMBER,
        }
        stdin = io.StringIO(json.dumps(request))
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            plan_handoff(request),
        )


if __name__ == "__main__":
    unittest.main()
