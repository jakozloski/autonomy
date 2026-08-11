from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from handoff_decision import (
    qa_generation,
    QA_OWNER_BY_REPOSITORY,
    QA_STATE_NAME_BY_TEAM,
    main,
    plan_handoff,
    roundtrip_generation,
)


REPOSITORY = {"nameWithOwner": "Keeper-Dating/matchmaking"}
PR_NUMBER = 3219
TIMESTAMP = "2026-07-09T20:09:07Z"
FIX_SHA = "a" * 40
REMOTE_HEAD_SHA = "b" * 40
LINEAR_QA_ASSIGNEE = {
    "provider_id": "linear-user-tjkeeper",
    "name": "Timothy Jhon Pascual",
}
LINEAR_QA_STATE_WEB = {
    "provider_id": "linear-state-vercel-preview-qa",
    "name": "Vercel Preview QA",
}
LINEAR_QA_STATE_ADM = {
    "provider_id": "linear-state-dev-ready-for-qa",
    "name": "Dev - Ready for QA",
}

# Target digest for the canonical fixture request most tests share; variant
# tests (other repos/tickets/write paths) compute their own via qa_generation.
QA_G = qa_generation(
    {
        "repository": REPOSITORY,
        "pull_request_number": PR_NUMBER,
        "issue_tracker": {
            "qa_assignee": LINEAR_QA_ASSIGNEE,
            "qa_state": LINEAR_QA_STATE_WEB,
            "ticket_identifier": "WEB-8877",
            "ticket_provider_id": "linear-ticket-web-8877",
            "write_path": "environment_tool",
        },
    }
)


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
                "acknowledgment_author": "jakozloski",
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
                "reply_author": "jakozloski",
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
            "nameWithOwner": "Keeper-Dating/matchmaking",
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
                "Keeper-Dating/admin-portal": {
                    "github_login": "shafqatukhan",
                    "linear_name": "Shafqat",
                },
                "Keeper-Dating/calculator-api": {
                    "github_login": "tjkeeper",
                    "linear_name": "Timothy Jhon Pascual",
                },
                "Keeper-Dating/keeper-lead-generator": {
                    "github_login": "tjkeeper",
                    "linear_name": "Timothy Jhon Pascual",
                },
                "Keeper-Dating/matchmaking": {
                    "github_login": "tjkeeper",
                    "linear_name": "Timothy Jhon Pascual",
                },
            },
        )

    def test_approved_qa_plans_exact_replacement_then_linear_assignment(self) -> None:
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "existing_assignees": ["stale-owner", "jakozloski", "stale-owner"],
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
            f"qa.github.replace_assignees:g{QA_G}",
            "replace_pull_request_assignees",
            {"assignees": ["tjkeeper"]},
            "pending",
        )
        verify_github = github_operation(
            f"qa.github.verify_assignees:g{QA_G}",
            "verify_pull_request_assignees",
            {"expected_assignees": ["tjkeeper"]},
            "waiting",
            [f"qa.github.replace_assignees:g{QA_G}"],
        )
        linear = {
            "id": f"qa.linear.assign_ticket:g{QA_G}",
            "service": "linear",
            "action": "assign_ticket",
            "depends_on": [f"qa.github.verify_assignees:g{QA_G}"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "assignee_id": "linear-user-tjkeeper",
                "assignee_name": "Timothy Jhon Pascual",
                "write_path": "environment_tool",
            },
            "status": "waiting",
        }
        verify_linear = {
            "id": f"qa.linear.verify_ticket_assignee:g{QA_G}",
            "service": "linear",
            "action": "verify_ticket_assignee",
            "depends_on": [f"qa.linear.assign_ticket:g{QA_G}"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "expected_assignee_id": "linear-user-tjkeeper",
                "expected_assignee_name": "Timothy Jhon Pascual",
                "write_path": "environment_tool",
            },
            "status": "waiting",
        }
        set_state = {
            "id": f"qa.linear.set_ticket_state:g{QA_G}",
            "service": "linear",
            "action": "set_ticket_state",
            "depends_on": [f"qa.linear.verify_ticket_assignee:g{QA_G}"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "state_id": "linear-state-vercel-preview-qa",
                "state_name": "Vercel Preview QA",
                "write_path": "environment_tool",
            },
            "status": "waiting",
        }
        verify_state = {
            "id": f"qa.linear.verify_ticket_state:g{QA_G}",
            "service": "linear",
            "action": "verify_ticket_state",
            "depends_on": [f"qa.linear.set_ticket_state:g{QA_G}"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "expected_state_id": "linear-state-vercel-preview-qa",
                "expected_state_name": "Vercel Preview QA",
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
                    "assignees": ["tjkeeper"],
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
                "ADM": "Dev - Ready for QA",
                "WEB": "Vercel Preview QA",
            },
        )

    def test_adm_ticket_moves_to_dev_ready_for_qa(self) -> None:
        request = {
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
        g = qa_generation(request)
        plan = plan_handoff(request)

        self.assertEqual(plan["state"], "pending")
        set_state = next(
            operation
            for operation in plan["operations"]
            if operation["id"] == f"qa.linear.set_ticket_state:g{g}"
        )
        self.assertEqual(set_state["payload"]["state_name"], "Dev - Ready for QA")
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
                "'Dev - Ready for QA' for team 'ADM'"
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
                "'Vercel Preview QA' workflow state for team 'WEB'; "
                "pass qa_state_unresolved_reason to record a manual state move"
            ],
        )

    def test_unresolved_qa_state_records_nonblocking_local_failure(self) -> None:
        request = {
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
        }
        g = qa_generation(request)
        request["operation_results"] = {
            f"qa.github.replace_assignees:g{g}": operation_result("complete"),
            f"qa.github.verify_assignees:g{g}": operation_result("complete"),
            f"qa.linear.assign_ticket:g{g}": operation_result("complete"),
            f"qa.linear.verify_ticket_assignee:g{g}": operation_result("complete"),
        }
        plan = plan_handoff(request)

        self.assertEqual(plan["state"], "failed")
        record = next(
            operation
            for operation in plan["operations"]
            if operation["id"] == f"qa.linear.record_state_unavailable:g{g}"
        )
        self.assertEqual(record["service"], "local")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "state renamed in Linear")
        self.assertEqual(
            record["payload"]["expected_state_name"], "Vercel Preview QA"
        )
        self.assertEqual(
            plan["warnings"],
            [
                f"Local operation qa.linear.record_state_unavailable:g{g} "
                "recorded unavailable; complete it manually."
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
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "issue_tracker": dict(base_issue_tracker),
        }
        g = qa_generation(request)
        plan = plan_handoff(request)

        self.assertEqual(plan["state"], "pending")
        self.assertEqual(
            [
                operation["id"]
                for operation in plan["operations"]
                if operation["service"] == "linear"
            ],
            [f"qa.linear.assign_ticket:g{g}", f"qa.linear.verify_ticket_assignee:g{g}"],
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
            "existing_assignees": ["stale-owner", "jakozloski"],
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
                    "repository": {"nameWithOwner": "another-owner/matchmaking"},
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
                    f"qa.github.replace_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.github.verify_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.linear.assign_ticket:g{QA_G}": operation_result(
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
                f"qa.github.replace_assignees:g{QA_G}": "complete",
                f"qa.github.verify_assignees:g{QA_G}": "complete",
                f"qa.linear.assign_ticket:g{QA_G}": "failed",
                f"qa.linear.verify_ticket_assignee:g{QA_G}": "skipped_dependency",
                f"qa.linear.set_ticket_state:g{QA_G}": "skipped_dependency",
                f"qa.linear.verify_ticket_state:g{QA_G}": "skipped_dependency",
            },
        )
        errors_by_id = {
            op["id"]: op.get("error")
            for op in plan["operations"]
            if op["status"] in ("failed", "skipped_dependency")
        }
        self.assertEqual(
            errors_by_id,
            {
                f"qa.linear.assign_ticket:g{QA_G}": None,
                f"qa.linear.verify_ticket_assignee:g{QA_G}": f"dependency failed: qa.linear.assign_ticket:g{QA_G}",
                f"qa.linear.set_ticket_state:g{QA_G}": f"dependency failed: qa.linear.verify_ticket_assignee:g{QA_G}",
                f"qa.linear.verify_ticket_state:g{QA_G}": f"dependency failed: qa.linear.set_ticket_state:g{QA_G}",
            },
        )
        self.assertEqual(
            plan["warnings"],
            [
                f"Remote operation qa.linear.assign_ticket:g{QA_G} failed; complete it manually.",
                f"Operation qa.linear.verify_ticket_assignee:g{QA_G} not executed "
                f"(dependency failed: qa.linear.assign_ticket:g{QA_G}); complete it manually.",
                f"Operation qa.linear.set_ticket_state:g{QA_G} not executed "
                f"(dependency failed: qa.linear.verify_ticket_assignee:g{QA_G}); complete it manually.",
                f"Operation qa.linear.verify_ticket_state:g{QA_G} not executed "
                f"(dependency failed: qa.linear.set_ticket_state:g{QA_G}); complete it manually.",
            ],
        )

    def test_cascade_descendants_render_skipped_dependency(self) -> None:
        # R2 round-2 finding 3737466456, empirically verified: descendants
        # rendered "failed" with no persistable record — the schema derives
        # missing records as pending and rejects the terminal monitor, so
        # the planner's own terminal answer could not be persisted
        # truthfully in ANY monitor state. Never-attempted descendants now
        # render "skipped_dependency", whose record proves a non-attempt.
        plan = plan_handoff(
            self._qa_request_with_results(
                {
                    f"qa.github.replace_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.github.verify_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.linear.assign_ticket:g{QA_G}": operation_result(
                        "failed", error="Linear returned 500"
                    ),
                }
            )
        )
        self.assertEqual(plan["state"], "failed")
        statuses = {op["id"]: op["status"] for op in plan["operations"]}
        self.assertEqual(statuses[f"qa.linear.assign_ticket:g{QA_G}"], "failed")
        for descendant in (
            f"qa.linear.verify_ticket_assignee:g{QA_G}",
            f"qa.linear.set_ticket_state:g{QA_G}",
            f"qa.linear.verify_ticket_state:g{QA_G}",
        ):
            self.assertEqual(statuses[descendant], "skipped_dependency")

    def test_skipped_dependency_records_replan_to_same_terminal_state(
        self,
    ) -> None:
        # The persistence round-trip's planner side: records persisted at
        # the rendered statuses — attempts 0 and no attempt evidence for
        # the skipped descendants — must be accepted on re-plan and derive
        # the same terminal failed state with an empty call plan.
        plan = plan_handoff(
            self._qa_request_with_results(
                {
                    f"qa.github.replace_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.github.verify_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.linear.assign_ticket:g{QA_G}": operation_result(
                        "failed", error="Linear returned 500"
                    ),
                    f"qa.linear.verify_ticket_assignee:g{QA_G}": {
                        "status": "skipped_dependency",
                        "attempts": 0,
                        "error": f"dependency failed: qa.linear.assign_ticket:g{QA_G}",
                    },
                    f"qa.linear.set_ticket_state:g{QA_G}": {
                        "status": "skipped_dependency",
                        "attempts": 0,
                        "error": (
                            "dependency failed:"
                            f" qa.linear.verify_ticket_assignee:g{QA_G}"
                        ),
                    },
                    f"qa.linear.verify_ticket_state:g{QA_G}": {
                        "status": "skipped_dependency",
                        "attempts": 0,
                        "error": f"dependency failed: qa.linear.set_ticket_state:g{QA_G}",
                    },
                }
            )
        )
        self.assertEqual(plan["state"], "failed")
        self.assertEqual(plan["call_plan"], [])
        self.assertEqual(plan["errors"], [])
        statuses = {op["id"]: op["status"] for op in plan["operations"]}
        self.assertEqual(
            statuses[f"qa.linear.verify_ticket_state:g{QA_G}"], "skipped_dependency"
        )

    def test_descendant_result_after_failed_dependency_is_blocked(self) -> None:
        # A canonical result on a descendant of a failed dependency means the
        # caller executed an operation this planner would never have queued —
        # an inconsistent ledger fails closed.
        plan = plan_handoff(
            self._qa_request_with_results(
                {
                    f"qa.github.replace_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.github.verify_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.linear.assign_ticket:g{QA_G}": operation_result(
                        "failed", error="Linear returned 500"
                    ),
                    f"qa.linear.verify_ticket_assignee:g{QA_G}": operation_result("complete"),
                }
            )
        )

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(
            plan["errors"],
            [
                f"operation qa.linear.verify_ticket_assignee:g{QA_G} cannot have results: "
                f"dependency failed: qa.linear.assign_ticket:g{QA_G}"
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
        }
        g = qa_generation(request)
        request["operation_results"] = {
            f"qa.github.replace_assignees:g{g}": operation_result("complete"),
            f"qa.github.verify_assignees:g{g}": operation_result("complete"),
        }

        self.assertEqual(
            plan_handoff(request),
            {
                "version": 1,
                "scenario": "approved_qa",
                "state": "failed",
                "reason": None,
                "targets": {
                    "assignees": ["tjkeeper"],
                    "reviewers": [],
                    "linear_assignee": None,
                },
                "operations": [
                    github_operation(
                        f"qa.github.replace_assignees:g{g}",
                        "replace_pull_request_assignees",
                        {"assignees": ["tjkeeper"]},
                        "complete",
                    ),
                    github_operation(
                        f"qa.github.verify_assignees:g{g}",
                        "verify_pull_request_assignees",
                        {"expected_assignees": ["tjkeeper"]},
                        "complete",
                        [f"qa.github.replace_assignees:g{g}"],
                    ),
                    {
                        "id": f"qa.linear.record_unavailable:g{g}",
                        "service": "local",
                        "action": "record_unavailable",
                        "depends_on": [f"qa.github.verify_assignees:g{g}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "expected_assignee_name": "Timothy Jhon Pascual",
                            "expected_state_name": "Vercel Preview QA",
                            "write_path": "none",
                        },
                        "status": "failed",
                        "error": "No authorized Linear write path is available.",
                    },
                ],
                "call_plan": [],
                "warnings": [
                    f"Local operation qa.linear.record_unavailable:g{g} recorded unavailable; complete it manually."
                ],
                "errors": [],
            },
        )

    def test_unavailable_tracker_operation_cannot_be_marked_complete(self) -> None:
        request = {
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
        }
        g = qa_generation(request)
        request["operation_results"] = {
            f"qa.github.replace_assignees:g{g}": operation_result("complete"),
            f"qa.github.verify_assignees:g{g}": operation_result("complete"),
            f"qa.linear.record_unavailable:g{g}": operation_result("complete"),
        }
        plan = plan_handoff(request)

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["call_plan"], [])
        self.assertEqual(
            plan["errors"],
            [
                f"unavailable operations cannot be marked complete: qa.linear.record_unavailable:g{g}"
            ],
        )

    def test_same_name_fork_does_not_match_qa_mapping(self) -> None:
        self.assertEqual(
            plan_handoff(
                {
                    "scenario": "approved_qa",
                    "repository": {"nameWithOwner": "another-owner/matchmaking"},
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
            "authenticated_actor": "jakozloski",
            "reviewers": [
                reviewer("zoe"),
                reviewer("jakozloski"),
                reviewer("alice"),
                reviewer("zoe"),
            ],
        }

        generation = roundtrip_generation(request, ["alice", "zoe"])
        alice = github_operation(
            f"roundtrip.github.request_review:alice:g{generation}",
            "request_pull_request_review",
            {"reviewer": "alice"},
            "pending",
        )
        verify_alice = github_operation(
            f"roundtrip.github.verify_review_request:alice:g{generation}",
            "verify_pull_request_review_request",
            {"expected_reviewer": "alice"},
            "waiting",
            [f"roundtrip.github.request_review:alice:g{generation}"],
        )
        zoe = github_operation(
            f"roundtrip.github.request_review:zoe:g{generation}",
            "request_pull_request_review",
            {"reviewer": "zoe"},
            "waiting",
            [f"roundtrip.github.verify_review_request:alice:g{generation}"],
        )
        verify_zoe = github_operation(
            f"roundtrip.github.verify_review_request:zoe:g{generation}",
            "verify_pull_request_review_request",
            {"expected_reviewer": "zoe"},
            "waiting",
            [f"roundtrip.github.request_review:zoe:g{generation}"],
        )
        replace = github_operation(
            f"roundtrip.github.replace_assignees:g{generation}",
            "replace_pull_request_assignees",
            {"assignees": ["alice", "zoe"]},
            "waiting",
            [
                f"roundtrip.github.verify_review_request:alice:g{generation}",
                f"roundtrip.github.verify_review_request:zoe:g{generation}",
            ],
        )
        verify_assignees = github_operation(
            f"roundtrip.github.verify_assignees:g{generation}",
            "verify_pull_request_assignees",
            {"expected_assignees": ["alice", "zoe"]},
            "waiting",
            [f"roundtrip.github.replace_assignees:g{generation}"],
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

    def test_second_round_feedback_mints_fresh_operations(self) -> None:
        # R2 round-2 finding 3737466450, verified by executing it: with
        # operation IDs keyed only by reviewer identity, a completed
        # first-round ledger satisfied the second round's entire plan —
        # state "complete", empty call_plan, nobody re-pinged. Fresh
        # feedback must mint operations no earlier ledger can satisfy,
        # and the prior round's terminal records must be ignored (with a
        # warning), not treated as unknown IDs.
        first_round = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [reviewer("alice")],
        }
        first_plan = plan_handoff(first_round)
        self.assertEqual(first_plan["state"], "pending")
        first_ids = [
            operation["id"] for operation in first_plan["operations"]
        ]

        second_round = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [
                reviewer(
                    "alice",
                    review_bodies={
                        "review-2": {
                            "updated_at": "2026-07-10T09:00:00Z",
                            "evaluated_updated_at": "2026-07-10T09:00:00Z",
                            "evaluated_at": "2026-07-10T09:05:00Z",
                            "acknowledgment_id": "ack-2",
                            "acknowledgment_author": "jakozloski",
                        }
                    },
                    current_review_body_ids=["review-2"],
                )
            ],
            "operation_results": {
                operation_id: operation_result("complete")
                for operation_id in first_ids
            },
        }
        second_plan = plan_handoff(second_round)

        self.assertEqual(second_plan["state"], "pending")
        self.assertNotEqual(second_plan["call_plan"], [])
        second_ids = [
            operation["id"] for operation in second_plan["operations"]
        ]
        self.assertEqual(set(first_ids) & set(second_ids), set())
        self.assertTrue(
            any(
                "prior-generation" in warning
                for warning in second_plan["warnings"]
            )
        )

    def test_prior_generation_skipped_records_are_pruned_as_terminal(
        self,
    ) -> None:
        # Series self-review finding: the pruner's terminal classifier
        # said ("complete", "failed") while the sibling commit made
        # skipped_dependency a first-class terminal status — so the
        # standard partial-failure shape (failed head, skipped
        # descendants) BLOCKED the next fresh round, telling the operator
        # to verify postconditions of operations that provably never
        # fired (attempts 0). Skipped orphans are that round's completed
        # history: pruned with the warning, never in-flight.
        first_round = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [reviewer("alice")],
        }
        first_ids = [
            operation["id"]
            for operation in plan_handoff(first_round)["operations"]
        ]

        second_round = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [
                reviewer(
                    "alice",
                    review_bodies={
                        "review-2": {
                            "updated_at": "2026-07-10T09:00:00Z",
                            "evaluated_updated_at": "2026-07-10T09:00:00Z",
                            "evaluated_at": "2026-07-10T09:05:00Z",
                            "acknowledgment_id": "ack-2",
                            "acknowledgment_author": "jakozloski",
                        }
                    },
                    current_review_body_ids=["review-2"],
                )
            ],
            "operation_results": {
                first_ids[0]: operation_result(
                    "failed", error="GitHub returned 500"
                ),
                first_ids[1]: {
                    "status": "skipped_dependency",
                    "attempts": 0,
                    "error": f"dependency failed: {first_ids[0]}",
                },
            },
        }
        second_plan = plan_handoff(second_round)

        self.assertEqual(second_plan["state"], "pending")
        self.assertNotEqual(second_plan["call_plan"], [])
        self.assertTrue(
            any(
                "prior-generation" in warning
                for warning in second_plan["warnings"]
            )
        )

    def test_prior_generation_in_flight_record_blocks_fresh_round(
        self,
    ) -> None:
        # The inverse guard: a prior-generation record still pending marks
        # a mutation that may have fired remotely — a fresh round must not
        # plan past it. The block names the sanctioned recovery (verify
        # the postcondition, record a terminal result), not just "stop".
        first_round = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [reviewer("alice")],
        }
        first_ids = [
            operation["id"]
            for operation in plan_handoff(first_round)["operations"]
        ]

        second_round = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [
                reviewer(
                    "alice",
                    review_bodies={
                        "review-2": {
                            "updated_at": "2026-07-10T09:00:00Z",
                            "evaluated_updated_at": "2026-07-10T09:00:00Z",
                            "evaluated_at": "2026-07-10T09:05:00Z",
                            "acknowledgment_id": "ack-2",
                            "acknowledgment_author": "jakozloski",
                        }
                    },
                    current_review_body_ids=["review-2"],
                )
            ],
            "operation_results": {first_ids[0]: operation_result("pending")},
        }
        second_plan = plan_handoff(second_round)

        self.assertEqual(second_plan["state"], "blocked")
        self.assertTrue(
            any(
                "verify each mutation's postcondition" in error
                for error in second_plan["errors"]
            )
        )

    def test_multi_reviewer_partial_resume_advances_one_operation_at_a_time(
        self,
    ) -> None:
        request = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [reviewer("zoe"), reviewer("alice")],
        }
        generation = roundtrip_generation(request, ["alice", "zoe"])
        request["operation_results"] = {
            f"roundtrip.github.request_review:alice:g{generation}": (
                operation_result("complete")
            ),
            f"roundtrip.github.verify_review_request:alice:g{generation}": (
                operation_result("complete")
            ),
        }

        alice = github_operation(
            f"roundtrip.github.request_review:alice:g{generation}",
            "request_pull_request_review",
            {"reviewer": "alice"},
            "complete",
        )
        verify_alice = github_operation(
            f"roundtrip.github.verify_review_request:alice:g{generation}",
            "verify_pull_request_review_request",
            {"expected_reviewer": "alice"},
            "complete",
            [f"roundtrip.github.request_review:alice:g{generation}"],
        )
        zoe = github_operation(
            f"roundtrip.github.request_review:zoe:g{generation}",
            "request_pull_request_review",
            {"reviewer": "zoe"},
            "pending",
            [f"roundtrip.github.verify_review_request:alice:g{generation}"],
        )
        verify_zoe = github_operation(
            f"roundtrip.github.verify_review_request:zoe:g{generation}",
            "verify_pull_request_review_request",
            {"expected_reviewer": "zoe"},
            "waiting",
            [f"roundtrip.github.request_review:zoe:g{generation}"],
        )
        replace = github_operation(
            f"roundtrip.github.replace_assignees:g{generation}",
            "replace_pull_request_assignees",
            {"assignees": ["alice", "zoe"]},
            "waiting",
            [
                f"roundtrip.github.verify_review_request:alice:g{generation}",
                f"roundtrip.github.verify_review_request:zoe:g{generation}",
            ],
        )
        verify_assignees = github_operation(
            f"roundtrip.github.verify_assignees:g{generation}",
            "verify_pull_request_assignees",
            {"expected_assignees": ["alice", "zoe"]},
            "waiting",
            [f"roundtrip.github.replace_assignees:g{generation}"],
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
            f"roundtrip.github.request_review:alice:g{generation}": (
                operation_result("complete")
            ),
            f"roundtrip.github.verify_review_request:alice:g{generation}": (
                operation_result("complete")
            ),
            f"roundtrip.github.request_review:zoe:g{generation}": (
                operation_result("complete")
            ),
            f"roundtrip.github.verify_review_request:zoe:g{generation}": (
                operation_result("complete")
            ),
        }
        resumed = plan_handoff(request)
        self.assertEqual(resumed["state"], "pending")
        self.assertEqual(
            resumed["call_plan"],
            [
                github_operation(
                    f"roundtrip.github.replace_assignees:g{generation}",
                    "replace_pull_request_assignees",
                    {"assignees": ["alice", "zoe"]},
                    "pending",
                    [
                        f"roundtrip.github.verify_review_request:alice:g{generation}",
                        f"roundtrip.github.verify_review_request:zoe:g{generation}",
                    ],
                )
            ],
        )

    def test_in_flight_mutation_requires_verification_before_retry(self) -> None:
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
        }
        g = qa_generation(request)
        request["operation_results"] = {
            f"qa.github.replace_assignees:g{g}": {
                "status": "pending",
                "attempts": 1,
                "started_at": TIMESTAMP,
                "response_id": None,
            }
        }
        plan = plan_handoff(request)

        self.assertEqual(plan["state"], "resume_verification_required")
        self.assertEqual(plan["operations"][0]["status"], "in_flight")
        self.assertEqual(plan["call_plan"][0]["action"], "verify_before_retry")
        self.assertEqual(
            plan["call_plan"][0]["payload"]["verification_operation"]["id"],
            f"qa.github.verify_assignees:g{g}",
        )

    def test_completed_ledger_for_different_pr_never_satisfies_plan(self) -> None:
        # algo#1216 R2 finding 3722492998, reproduced on the pre-digest
        # planner: a fully completed ledger persisted for one PR returned
        # "complete" with zero calls when re-planned for another PR.
        # Target-digest-bound IDs orphan that ledger: terminal history is
        # pruned with a warning and every operation replans fresh.
        def request_for(pr_number: int) -> dict[str, object]:
            return {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": pr_number,
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

        prior_g = qa_generation(request_for(1495))
        request = request_for(9999)
        request["operation_results"] = {
            f"{base}:g{prior_g}": operation_result("complete")
            for base in (
                "qa.github.replace_assignees",
                "qa.github.verify_assignees",
                "qa.linear.assign_ticket",
                "qa.linear.verify_ticket_assignee",
                "qa.linear.set_ticket_state",
                "qa.linear.verify_ticket_state",
            )
        }
        self.assertNotEqual(prior_g, qa_generation(request))

        plan = plan_handoff(request)

        self.assertEqual(plan["state"], "pending")
        self.assertTrue(plan["call_plan"])
        current_g = qa_generation(request)
        self.assertEqual(
            plan["call_plan"][0]["id"],
            f"qa.github.replace_assignees:g{current_g}",
        )
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("6 prior-target terminal QA record(s)", plan["warnings"][0])

    def test_in_flight_prior_target_qa_record_fails_closed(self) -> None:
        # An in-flight record persisted for different targets marks a
        # mutation that may already have fired remotely: never prune it —
        # block with the recovery named, mirroring the roundtrip contract.
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
        }
        stale_id = "qa.github.replace_assignees:gdeadbeef0123"
        request["operation_results"] = {
            stale_id: {
                "status": "pending",
                "attempts": 1,
                "started_at": TIMESTAMP,
            }
        }
        plan = plan_handoff(request)

        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["call_plan"], [])
        self.assertIn("prior-target QA operation(s) still in flight", plan["errors"][0])
        self.assertIn(stale_id, plan["errors"][0])
        self.assertIn("verify each mutation's postcondition", plan["errors"][0])

    def test_failed_resume_verification_can_retry_with_write_ahead(self) -> None:
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
        }
        g = qa_generation(request)
        request["operation_results"] = {
            f"qa.github.replace_assignees:g{g}": {
                "status": "retryable",
                "attempts": 1,
                "started_at": TIMESTAMP,
                "verified_at": TIMESTAMP,
                "error": "postcondition absent",
            }
        }
        plan = plan_handoff(request)

        self.assertEqual(plan["state"], "pending")
        self.assertEqual(plan["call_plan"][0]["id"], f"qa.github.replace_assignees:g{g}")
        self.assertEqual(plan["call_plan"][0]["attempt"], 2)
        self.assertTrue(plan["call_plan"][0]["requires_pending_write"])

    def test_malformed_operation_status_blocks_without_crashing(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    f"qa.github.replace_assignees:g{QA_G}": {
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
                    f"qa.github.replace_assignees:g{QA_G}": {
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
                            f"qa.github.replace_assignees:g{QA_G}": result,
                        },
                    }
                )

                self.assertEqual(plan["state"], "blocked")
                self.assertEqual(
                    plan["errors"],
                    [
                        f"operation_results['qa.github.replace_assignees:g{QA_G}'].verified_at "
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
                    f"qa.github.replace_assignees:g{QA_G}": {
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
                    f"qa.github.replace_assignees:g{QA_G}": {
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
            f"qa.github.replace_assignees:g{QA_G}": operation_result("complete"),
            f"qa.github.verify_assignees:g{QA_G}": operation_result("complete"),
            f"qa.linear.assign_ticket:g{QA_G}": operation_result(
                "failed", error="assignment failed"
            ),
            f"qa.linear.verify_ticket_assignee:g{QA_G}": operation_result(
                "failed", error="verification failed"
            ),
            f"qa.linear.set_ticket_state:g{QA_G}": operation_result(
                "failed", error="state move failed"
            ),
            f"qa.linear.verify_ticket_state:g{QA_G}": operation_result(
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
                    "assignees": ["tjkeeper"],
                    "reviewers": [],
                    "linear_assignee": LINEAR_QA_ASSIGNEE,
                },
                "operations": [
                    github_operation(
                        f"qa.github.replace_assignees:g{QA_G}",
                        "replace_pull_request_assignees",
                        {"assignees": ["tjkeeper"]},
                        "complete",
                    ),
                    github_operation(
                        f"qa.github.verify_assignees:g{QA_G}",
                        "verify_pull_request_assignees",
                        {"expected_assignees": ["tjkeeper"]},
                        "complete",
                        [f"qa.github.replace_assignees:g{QA_G}"],
                    ),
                    {
                        "id": f"qa.linear.assign_ticket:g{QA_G}",
                        "service": "linear",
                        "action": "assign_ticket",
                        "depends_on": [f"qa.github.verify_assignees:g{QA_G}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "assignee_id": "linear-user-tjkeeper",
                            "assignee_name": "Timothy Jhon Pascual",
                            "write_path": "environment_tool",
                        },
                        "status": "failed",
                    },
                    {
                        "id": f"qa.linear.verify_ticket_assignee:g{QA_G}",
                        "service": "linear",
                        "action": "verify_ticket_assignee",
                        "depends_on": [f"qa.linear.assign_ticket:g{QA_G}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "expected_assignee_id": "linear-user-tjkeeper",
                            "expected_assignee_name": "Timothy Jhon Pascual",
                            "write_path": "environment_tool",
                        },
                        "status": "failed",
                    },
                    {
                        "id": f"qa.linear.set_ticket_state:g{QA_G}",
                        "service": "linear",
                        "action": "set_ticket_state",
                        "depends_on": [f"qa.linear.verify_ticket_assignee:g{QA_G}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "state_id": "linear-state-vercel-preview-qa",
                            "state_name": "Vercel Preview QA",
                            "write_path": "environment_tool",
                        },
                        "status": "failed",
                    },
                    {
                        "id": f"qa.linear.verify_ticket_state:g{QA_G}",
                        "service": "linear",
                        "action": "verify_ticket_state",
                        "depends_on": [f"qa.linear.set_ticket_state:g{QA_G}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "ticket_provider_id": "linear-ticket-web-8877",
                            "expected_state_id": "linear-state-vercel-preview-qa",
                            "expected_state_name": "Vercel Preview QA",
                            "write_path": "environment_tool",
                        },
                        "status": "failed",
                    },
                ],
                "call_plan": [],
                "warnings": [
                    f"Remote operation qa.linear.assign_ticket:g{QA_G} failed; complete it manually.",
                    f"Remote operation qa.linear.verify_ticket_assignee:g{QA_G} failed; complete it manually.",
                    f"Remote operation qa.linear.set_ticket_state:g{QA_G} failed; complete it manually.",
                    f"Remote operation qa.linear.verify_ticket_state:g{QA_G} failed; complete it manually.",
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
                        "authenticated_actor": "jakozloski",
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
                "authenticated_actor": "jakozloski",
                "reviewers": [
                    reviewer(
                        "alice",
                        inline_roots={
                            "comment-1": {
                                "updated_at": "2026-07-09T21:00:00Z",
                                "replied_to_updated_at": TIMESTAMP,
                                "reply_id": "reply-1",
                                "replied_at": "2026-07-09T21:00:00Z",
                                "reply_author": "jakozloski",
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
                "authenticated_actor": "jakozloski",
                "reviewers": [
                    reviewer(
                        "alice",
                        inline_roots={
                            "comment-1": {
                                "updated_at": TIMESTAMP,
                                "replied_to_updated_at": TIMESTAMP,
                                "reply_id": None,
                                "replied_at": TIMESTAMP,
                                "reply_author": "jakozloski",
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
                "authenticated_actor": "jakozloski",
                "reviewers": [
                    reviewer(
                        "alice",
                        review_bodies={
                            "review-1": {
                                "updated_at": TIMESTAMP,
                                "evaluated_updated_at": TIMESTAMP,
                                "evaluated_at": None,
                                "acknowledgment_id": "ack-1",
                                "acknowledgment_author": "jakozloski",
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
                "authenticated_actor": "jakozloski",
                "reviewers": [
                    reviewer(
                        "alice",
                        inline_roots={
                            "comment-1": {
                                "updated_at": TIMESTAMP,
                                "replied_to_updated_at": TIMESTAMP,
                                "reply_id": "reply-1",
                                "replied_at": "2026-07-09T19:00:00Z",
                                "reply_author": "jakozloski",
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
                "authenticated_actor": "jakozloski",
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
                "authenticated_actor": "jakozloski",
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
                "authenticated_actor": "jakozloski",
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
                "authenticated_actor": "jakozloski",
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
                "authenticated_actor": "jakozloski",
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
                        "authenticated_actor": "jakozloski",
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
                tracker["qa_assignee"]["provider_id"] = " linear-user-tjkeeper "
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
        request = {
            "scenario": "human_review_roundtrip",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [reviewer("alice"), reviewer("zoe")],
        }
        generation = roundtrip_generation(request, ["alice", "zoe"])
        request["operation_results"] = {
            f"roundtrip.github.replace_assignees:g{generation}": (
                operation_result("complete")
            )
        }
        plan = plan_handoff(request)
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["operations"], [])
        self.assertEqual(plan["call_plan"], [])
        self.assertEqual(
            plan["errors"],
            ["operation results must form a prefix with at most one in-flight tail"],
        )

    def test_zero_reviewer_roundtrip_with_persisted_results_fails_closed(self) -> None:
        """A resumed roundtrip whose reviewer set emptied (actor exclusion /
        ineligibility) must not silently orphan a persisted write-ahead
        record - the mutation it marks may already have fired remotely."""
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "alice",
                "reviewers": [],
                "operation_results": {
                    "roundtrip.github.request_review:alice": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    }
                },
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertTrue(
            any("persisted operation result" in error for error in plan["errors"]),
            plan["errors"],
        )

    def test_zero_reviewer_roundtrip_without_results_stays_idle(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "human_review_roundtrip",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "alice",
                "reviewers": [],
            }
        )
        self.assertEqual(plan["state"], "idle")

    def test_cli_reads_json_and_writes_only_the_plan(self) -> None:
        request = {
            "scenario": "clean_unapproved",
            "repository": {"nameWithOwner": "another-owner/matchmaking"},
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

    def test_cli_invalid_json_blocks_with_nonzero_exit(self) -> None:
        stdin = io.StringIO("not-json")
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["state"], "blocked")
        self.assertTrue(payload["errors"][0].startswith("input must be valid JSON"))


class QaOwnerDocumentationSyncTest(unittest.TestCase):
    """references/monitor-exit-handoffs.md restates the module's QA mappings.

    QA_OWNER_BY_REPOSITORY / QA_STATE_NAME_BY_TEAM are canonical at runtime;
    these tests fail whenever the human-readable table drifts from them.
    """

    @property
    def reference_text(self) -> str:
        reference_path = (
            Path(__file__).resolve().parent.parent
            / "references"
            / "monitor-exit-handoffs.md"
        )
        return reference_path.read_text(encoding="utf-8")

    def test_qa_owner_table_matches_module_mapping(self) -> None:
        documented: dict[str, dict[str, str]] = {}
        for line in self.reference_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) != 3 or not cells[0].startswith("`Keeper-Dating/"):
                continue
            documented[cells[0].strip("`")] = {
                "github_login": cells[1].strip("`"),
                "linear_name": cells[2],
            }

        self.assertEqual(documented, QA_OWNER_BY_REPOSITORY)

    def test_qa_state_names_match_module_mapping(self) -> None:
        for team_key, state_name in QA_STATE_NAME_BY_TEAM.items():
            self.assertIn(f'`{team_key}` → **"{state_name}"**', self.reference_text)


class RetryGuardCoverageTests(unittest.TestCase):
    """The two invariants that prevent a blind fourth mutation."""

    def test_retryable_at_attempt_cap_blocks(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    f"qa.github.replace_assignees:g{QA_G}": {
                        "status": "retryable",
                        "attempts": 3,
                        "started_at": TIMESTAMP,
                        "verified_at": TIMESTAMP,
                        "error": "postcondition absent",
                    }
                },
            }
        )
        self.assertEqual(plan["state"], "blocked")

    def test_two_in_flight_operations_block(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "operation_results": {
                    f"qa.github.replace_assignees:g{QA_G}": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    },
                    f"qa.github.verify_assignees:g{QA_G}": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    },
                },
            }
        )
        self.assertEqual(plan["state"], "blocked")

    def test_fractional_second_timestamps_match_state_schema_verdict(self) -> None:
        from handoff_decision import _iso_timestamp

        for ts in (
            "2026-07-30T19:30:00.1Z",
            "2026-07-30T19:30:00.12345Z",
            "2026-07-30T19:30:00.123456789+00:00",
        ):
            with self.subTest(ts=ts):
                self.assertIsNotNone(_iso_timestamp(ts))
        for ts in ("2026-07-30T19:30:00", "2026-07-30 19:30:00Z", "not-a-time"):
            with self.subTest(ts=ts):
                self.assertIsNone(_iso_timestamp(ts))

    # The non-UTF-8 stdin CLI test lives in test_cli_fail_closed.py: the skill
    # scanner forbids pairing subprocess with eval-substring call names here.




class MalformedLedgerZeroReviewerTests(unittest.TestCase):
    """R4-F3: a malformed operation_results (non-dict) on the zero-reviewer
    roundtrip path must fail closed like the QA path, never silent idle."""

    def test_non_dict_ledger_shapes_fail_closed(self) -> None:
        for malformed in (["not", "a", "dict"], "not-a-dict", 7):
            with self.subTest(shape=type(malformed).__name__):
                plan = plan_handoff(
                    {
                        "scenario": "human_review_roundtrip",
                        "repository": REPOSITORY,
                        "pull_request_number": PR_NUMBER,
                        "authenticated_actor": "alice",
                        "reviewers": [],
                        "operation_results": malformed,
                    }
                )
                self.assertEqual(plan["state"], "blocked", plan)
                self.assertTrue(plan["errors"], plan)


if __name__ == "__main__":
    unittest.main()
