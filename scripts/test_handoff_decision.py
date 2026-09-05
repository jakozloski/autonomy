from __future__ import annotations

import hashlib
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
    reviewer_request_generation,
    roundtrip_generation,
)


REPOSITORY = {"nameWithOwner": "Keeper-Dating/matchmaking"}
PR_NUMBER = 3219
TIMESTAMP = "2026-07-09T20:09:07Z"
FIX_SHA = "a" * 40
REMOTE_HEAD_SHA = "b" * 40
LINEAR_QA_ASSIGNEE = {
    "provider_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
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
            # dawid-r9 F3 (+ the v3 validated gate): the digest folds
            # tracker sub-fields unless the type is linear AND the ticket
            # is validated (the consumer's read condition), so the
            # derivation input carries both like every plan fixture.
            "type": "linear",
            "ticket_validated": True,
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
            },
        )

    def test_qa_repository_membership_derives_from_the_targets_leaf(self) -> None:
        # admin#1495 r19 F7: the Linear-mapped MEMBERSHIP lives once in
        # handoff_targets; this module keeps owner-VALUE authority and
        # derives its key set (and iteration order) from the leaf. The
        # literal test above still pins the complete map, so a leaf edit
        # cannot silently reshape routing.
        import handoff_targets

        self.assertEqual(
            tuple(QA_OWNER_BY_REPOSITORY),
            handoff_targets.LINEAR_MAPPED_REPOSITORIES,
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
            {
                "assignees": ["tjkeeper"],
                # finding 3813491647: the deduped observed set rides along
                # as the write-ahead precondition for resume's three-way
                # compare.
                "precondition": {
                    "assignees": ["jakozloski", "stale-owner"]
                },
            },
            "pending",
        )
        verify_github = github_operation(
            f"qa.github.verify_assignees:g{QA_G}",
            "verify_pull_request_assignees",
            {"expected_assignees": ["tjkeeper"]},
            "waiting",
            [f"qa.github.replace_assignees:g{QA_G}"],
        )
        binding = {
            "id": f"qa.linear.verify_ticket_binding:g{QA_G}",
            "service": "linear",
            "action": "verify_ticket_binding",
            "depends_on": [f"qa.github.verify_assignees:g{QA_G}"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "expected_ticket_provider_id": "linear-ticket-web-8877",
                "expected_repository": "Keeper-Dating/matchmaking",
                "expected_pull_request_number": PR_NUMBER,
                "write_path": "environment_tool",
            },
            "status": "waiting",
        }
        linear = {
            "id": f"qa.linear.assign_ticket:g{QA_G}",
            "service": "linear",
            "action": "assign_ticket",
            "depends_on": [f"qa.linear.verify_ticket_binding:g{QA_G}"],
            "payload": {
                "ticket_identifier": "WEB-8877",
                "assignee_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
                "assignee_email": "tj@keeper.ai",
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
                "expected_ticket_provider_id": "linear-ticket-web-8877",
                "expected_assignee_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
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
                "expected_ticket_provider_id": "linear-ticket-web-8877",
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
                    binding,
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
            "existing_assignees": ["jakozloski"],
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

    def test_wrong_linear_user_id_blocks_the_handoff(self) -> None:
        # admin-portal#1495 R2 finding 3722356257, resolved structurally:
        # binding is by the STABLE mapped Linear user id — resolving a
        # different user is a wrong-target handoff and hard-fails.
        request = {
            "scenario": "approved_qa",
            "existing_assignees": ["jakozloski"],
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "issue_tracker": {
                "type": "linear",
                "qa_assignee": {
                    "provider_id": "00000000-0000-0000-0000-000000000000",
                    "name": "Timothy Jhon Pascual",
                },
                "qa_state": LINEAR_QA_STATE_WEB,
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "environment_tool",
            },
        }
        plan = plan_handoff(request)
        self.assertEqual(plan["state"], "blocked")
        self.assertTrue(
            any("must be the mapped Linear user id" in e for e in plan["errors"])
        )

    def test_non_object_issue_tracker_blocks_instead_of_raising(self) -> None:
        # CodeRabbit keeper-agents#1328: every other path of
        # _approved_qa_operations returns four values, so the three-value
        # early return crashed the planner with ValueError on a QA-mapped
        # repository instead of returning a blocked plan.
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "existing_assignees": ["jakozloski"],
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "issue_tracker": "linear",
            }
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("issue_tracker must be an object", plan["errors"])

    def test_display_name_drift_warns_but_proceeds(self) -> None:
        # The display name is an ADVISORY cross-check: labels drift, the id
        # binding is the guard. A mismatch warns and the plan proceeds.
        request = {
            "scenario": "approved_qa",
            "existing_assignees": ["jakozloski"],
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "issue_tracker": {
                "type": "linear",
                "qa_assignee": {
                    "provider_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
                    "name": "TJ Pascual (renamed)",
                },
                "qa_state": LINEAR_QA_STATE_WEB,
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "environment_tool",
            },
        }
        plan = plan_handoff(request)
        self.assertEqual(plan["state"], "pending")
        self.assertTrue(
            any("display label" in w and "proceeds" in w for w in plan["warnings"])
        )
        assign = next(
            op for op in plan["operations"] if op["action"] == "assign_ticket"
        )
        self.assertEqual(assign["payload"]["assignee_email"], "tj@keeper.ai")

    def test_name_only_qa_state_plans_mutation_by_name(self) -> None:
        # admin#1495 r12 F7: a managed (environment_tool) child cannot
        # list Linear workflow states, so the provider id is optional -
        # the broker resolves the canonical NAME server-side. A name-only
        # qa_state plans the mutation by name and a verify step keyed on
        # the expected name (the observed id lands in verify evidence);
        # a supplied id still pins both payloads exactly as before.
        for team, ticket, state in (
            ("WEB", "WEB-8877", {"name": "Vercel Preview QA"}),
            ("ADM", "ADM-953", {"name": "Dev - Ready for QA"}),
        ):
            with self.subTest(team=team):
                request = {
                    "scenario": "approved_qa",
                    "repository": REPOSITORY,
                    "pull_request_number": PR_NUMBER,
                    "existing_assignees": ["jakozloski"],
                    "issue_tracker": {
                        "type": "linear",
                        "qa_assignee": LINEAR_QA_ASSIGNEE,
                        "qa_state": state,
                        "ticket_identifier": ticket,
                        "ticket_provider_id": f"linear-ticket-{ticket}",
                        "ticket_validated": True,
                        "write_path": "environment_tool",
                    },
                }
                plan = plan_handoff(request)
                self.assertEqual(plan["state"], "pending", plan.get("errors"))
                by_action = {
                    operation["action"]: operation
                    for operation in plan["operations"]
                }
                set_payload = by_action["set_ticket_state"]["payload"]
                self.assertEqual(
                    set_payload["state_name"], state["name"]
                )
                self.assertNotIn("state_id", set_payload)
                verify_payload = by_action["verify_ticket_state"]["payload"]
                self.assertEqual(
                    verify_payload["expected_state_name"], state["name"]
                )
                self.assertNotIn("expected_state_id", verify_payload)

    def test_blank_supplied_qa_state_provider_id_is_rejected(self) -> None:
        # Supplied-but-blank is neither a name-only plan nor an id plan.
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "existing_assignees": ["jakozloski"],
            "issue_tracker": {
                "type": "linear",
                "qa_assignee": LINEAR_QA_ASSIGNEE,
                "qa_state": {"provider_id": " ", "name": "Vercel Preview QA"},
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "environment_tool",
            },
        }
        plan = plan_handoff(request)
        self.assertEqual(plan["state"], "blocked", plan)
        self.assertTrue(
            any(
                "provider_id must be stripped and non-empty" in error
                for error in plan["errors"]
            ),
            plan["errors"],
        )

    def test_qa_state_name_must_match_ticket_team(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
            f"qa.linear.verify_ticket_binding:g{g}": operation_result("complete"),
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
            "existing_assignees": ["jakozloski"],
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
            [
                f"qa.linear.verify_ticket_binding:g{g}",
                f"qa.linear.assign_ticket:g{g}",
                f"qa.linear.verify_ticket_assignee:g{g}",
            ],
        )

        supplied_state = plan_handoff(
            {
                "scenario": "approved_qa",
                "existing_assignees": ["jakozloski"],
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

    def test_resume_adjudicates_against_the_persisted_precondition(
        self,
    ) -> None:
        # algo#1216 finding 3813491647 (exact repro class): resume replayed
        # replace_pull_request_assignees with only the target set — no
        # pre-mutation fingerprint — so a crash-then-human-reassignment
        # window got overwritten. The PERSISTED record's precondition now
        # rides verify_before_retry and the retry replay; the request's
        # fresh observation must NOT win (it may already contain the very
        # drift being adjudicated).
        def request(results):
            return {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                # Fresh observation AFTER the crash: a human moved the PR.
                "existing_assignees": ["drifted-human"],
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "qa_state": LINEAR_QA_STATE_WEB,
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "environment_tool",
                },
                "operation_results": results,
            }

        g = qa_generation(request({}))
        persisted = {"assignees": ["jakozloski", "stale-owner"]}
        pending_plan = plan_handoff(
            request(
                {
                    f"qa.github.replace_assignees:g{g}": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": "2026-07-14T16:59:00Z",
                        "precondition": dict(persisted),
                    }
                }
            )
        )
        self.assertEqual(
            pending_plan["state"], "resume_verification_required", pending_plan
        )
        control = pending_plan["call_plan"][0]
        self.assertEqual(control["action"], "verify_before_retry")
        self.assertEqual(control["payload"]["precondition"], persisted)
        retry_plan = plan_handoff(
            request(
                {
                    f"qa.github.replace_assignees:g{g}": {
                        "status": "retryable",
                        "attempts": 1,
                        "started_at": "2026-07-14T16:59:00Z",
                        "verified_at": "2026-07-14T17:00:00Z",
                        "error": "postcondition absent",
                        "precondition": dict(persisted),
                    }
                }
            )
        )
        self.assertEqual(retry_plan["state"], "pending", retry_plan)
        replay = retry_plan["call_plan"][0]
        self.assertEqual(replay["action"], "replace_pull_request_assignees")
        self.assertEqual(replay["payload"]["precondition"], persisted)
        # r13 F4 (Critical): the r19 legacy fallback — surfacing the
        # spec's fresh observation for a record without a persisted
        # precondition — is REMOVED. The fresh observation is the
        # post-crash state, and adopting it as the baseline blesses a
        # human reassignment made during the crash window as "the
        # original". A pre-upgrade assignee-mutation record now fails
        # closed with the manual recovery named.
        legacy_plan = plan_handoff(
            request(
                {
                    f"qa.github.replace_assignees:g{g}": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": "2026-07-14T16:59:00Z",
                    }
                }
            )
        )
        self.assertEqual(legacy_plan["state"], "blocked", legacy_plan)
        self.assertTrue(
            any(
                "carries no precondition" in error
                and "verify the mutation's postcondition manually" in error
                for error in legacy_plan["errors"]
            ),
            legacy_plan["errors"],
        )

    def test_reviewer_and_roundtrip_resumes_use_only_persisted_baselines(
        self,
    ) -> None:
        # r13 F4: the crash→human-reassignment pin for the OTHER two
        # assignee-mutation builders. Each pending record resumes with its
        # PERSISTED baseline (never the request's fresh observation), and
        # a record without one fails closed.
        rr_request = {
            "scenario": "reviewer_request",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "existing_assignees": ["drifted-human"],
            "reviewer_requests": {
                "reviewers": ["alice"],
                "ball_holder": "alice",
            },
        }
        rr_plan = plan_handoff(rr_request)
        self.assertEqual(rr_plan["state"], "pending", rr_plan)
        replace = next(
            op
            for op in rr_plan["operations"]
            if op["action"] == "replace_pull_request_assignees"
        )
        persisted = {"assignees": ["original-owner"]}
        pending = dict(rr_request)
        pending["operation_results"] = {
            replace["id"]: {
                "status": "pending",
                "attempts": 1,
                "started_at": "2026-07-14T16:59:00Z",
                "precondition": dict(persisted),
            }
        }
        # earlier chain ops must be complete for the prefix rule; mark them
        for op in rr_plan["operations"]:
            if op["id"] == replace["id"]:
                break
            pending["operation_results"][op["id"]] = {
                "status": "complete",
                "attempts": 1,
                "started_at": "2026-07-14T16:58:00Z",
                "verified_at": "2026-07-14T16:58:30Z",
                "evidence": {"postcondition": "verified"},
            }
        resumed = plan_handoff(pending)
        self.assertEqual(
            resumed["state"], "resume_verification_required", resumed
        )
        self.assertEqual(
            resumed["call_plan"][0]["payload"]["precondition"], persisted
        )
        legacy = dict(pending)
        legacy["operation_results"] = dict(pending["operation_results"])
        legacy["operation_results"][replace["id"]] = {
            "status": "pending",
            "attempts": 1,
            "started_at": "2026-07-14T16:59:00Z",
        }
        blocked = plan_handoff(legacy)
        self.assertEqual(blocked["state"], "blocked", blocked)
        self.assertTrue(
            any("carries no precondition" in e for e in blocked["errors"]),
            blocked["errors"],
        )

    def test_git_object_id_grammar_is_schema_owned(self) -> None:
        # algo#1216 r17 F5: one shared grammar — this consumer binds the
        # schema-owned compiled pattern itself (assertIs is meaningful for
        # compiled regex objects, unlike interned small ints), so a
        # re-declared local range can never drift. The 6/7/64/65
        # boundaries hold through the shared fragment with full-match
        # anchoring preserved.
        import state_schema

        from handoff_decision import GIT_OBJECT_ID

        self.assertIs(GIT_OBJECT_ID, state_schema.GIT_OBJECT_ID)
        for length, ok in ((6, False), (7, True), (64, True), (65, False)):
            with self.subTest(length=length):
                self.assertEqual(
                    GIT_OBJECT_ID.fullmatch("a" * length) is not None, ok
                )
        self.assertIsNone(GIT_OBJECT_ID.fullmatch("g" * 7))

    def test_qa_generation_rolls_over_when_reviewers_change(self) -> None:
        # admin#1495 R2 finding 3791925153 (executed repro): the reviewer set
        # shapes operation IDs, so it is a target fact — omitting it from the
        # digest reused a generation across reviewer changes and stranded
        # resumes on unknown-operation errors. Order and case are normalized
        # so a reorder is never a spurious rollover.
        base = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "existing_assignees": ["jakozloski"],
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
        gen_none = qa_generation(base)
        gen_alice = qa_generation({**base, "code_reviewers": ["alice"]})
        gen_bob = qa_generation({**base, "code_reviewers": ["bob"]})
        gen_both = qa_generation({**base, "code_reviewers": ["alice", "bob"]})
        gen_both_reordered = qa_generation(
            {**base, "code_reviewers": ["Bob", "ALICE", "bob"]}
        )
        self.assertEqual(
            len({gen_none, gen_alice, gen_bob, gen_both}), 4,
            "reviewer set changes must roll the generation",
        )
        self.assertEqual(
            gen_both, gen_both_reordered,
            "ordering/case/duplicates must not roll the generation",
        )

    def test_qa_generation_digests_the_actor_filtered_reviewer_set(
        self,
    ) -> None:
        # #3551 finding 3808151926: the operation builder drops the
        # authenticated actor AFTER the digest ran, so hashing the raw list
        # let an actor rotation change the minted operation set while the
        # generation stayed fixed — the prior actor's completed ledger then
        # satisfied a plan that now includes that login as a real reviewer,
        # silently skipping their review request on resume.
        base = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "existing_assignees": ["jakozloski"],
            "issue_tracker": {
                "type": "linear",
                "qa_assignee": LINEAR_QA_ASSIGNEE,
                "qa_state": LINEAR_QA_STATE_WEB,
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "ticket_validated": True,
                "write_path": "environment_tool",
            },
            "code_reviewers": ["alice", "bob"],
        }
        as_bob = qa_generation({**base, "authenticated_actor": "bob"})
        as_carol = qa_generation({**base, "authenticated_actor": "carol"})
        self.assertNotEqual(
            as_bob, as_carol,
            "actor rotation changes the effective reviewer set and must "
            "roll the generation",
        )
        filtered_equivalent = qa_generation(
            {**base, "code_reviewers": ["alice"], "authenticated_actor": "bob"}
        )
        self.assertEqual(
            as_bob, filtered_equivalent,
            "the digest must hash the post-filter set: actor-in-list and "
            "actor-absent requests mint the same plan",
        )
        self.assertEqual(
            as_bob,
            qa_generation({**base, "authenticated_actor": "BOB"}),
            "the actor filter matches the builder's casefolded skip",
        )

    def test_qa_missing_or_malformed_actor_with_code_reviewers_blocks(
        self,
    ) -> None:
        # algo#1216 r16 F10: the self-review filter (and its digest mirror)
        # is identity-keyed, so a missing or malformed authenticated_actor
        # silently disabled filtering and could mint a self-review request
        # GitHub 422s into a permanent ledger failure. With code reviewers
        # routed, a valid actor is a required input, rejected before the
        # filter and generation digest run.
        def request(**overrides):
            payload = {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "existing_assignees": ["jakozloski"],
                "code_reviewers": ["alice", "jakozloski"],
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
            payload.update(overrides)
            return payload

        for label, overrides in (
            ("absent", {}),
            ("non-string", {"authenticated_actor": 42}),
            ("malformed", {"authenticated_actor": " me "}),
            (
                "clean-unapproved-absent",
                {"scenario": "clean_unapproved"},
            ),
        ):
            with self.subTest(actor=label):
                plan = plan_handoff(request(**overrides))
                self.assertEqual(plan["state"], "blocked", plan)
                self.assertTrue(
                    any(
                        "authenticated_actor must be a valid GitHub login"
                        in error
                        for error in plan["errors"]
                    ),
                    plan["errors"],
                )
        # algo#1216 r16 F2 updated this side: an unmapped repository now
        # plans the universal handback, so with code reviewers routed the
        # actor gate applies there too — the actor-less clean exit
        # survives only when no handback target resolves at all.
        unmapped = plan_handoff(
            request(repository={"nameWithOwner": "Keeper-Dating/unmapped"})
        )
        self.assertEqual(unmapped["state"], "blocked", unmapped)
        # The valid-actor plan proceeds, and the actor's own login is
        # filtered out of the minted reviewer operations (casefold match).
        plan = plan_handoff(request(authenticated_actor="Jakozloski"))
        self.assertEqual(plan["state"], "pending", plan.get("errors"))
        ids = [operation["id"] for operation in plan["operations"]]
        self.assertTrue(any(":alice:" in op_id for op_id in ids), ids)
        self.assertFalse(
            any(":jakozloski:" in op_id for op_id in ids), ids
        )

    def test_qa_actor_optional_without_code_reviewers(self) -> None:
        # The other side of the r16 F10 gate: with no routed code reviewers
        # the plan mints no reviewer operations, so the actor-less QA plan
        # (the historical shape) keeps planning - the requirement is scoped
        # to exactly the input that makes self-review filtering load-bearing.
        for label, reviewers in (("absent", None), ("empty", [])):
            with self.subTest(code_reviewers=label):
                payload = {
                    "scenario": "approved_qa",
                    "repository": REPOSITORY,
                    "pull_request_number": PR_NUMBER,
                    "existing_assignees": ["jakozloski"],
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
                if reviewers is not None:
                    payload["code_reviewers"] = reviewers
                plan = plan_handoff(payload)
                self.assertEqual(plan["state"], "pending", plan.get("errors"))
                self.assertFalse(
                    [
                        operation
                        for operation in plan["operations"]
                        if ".request_review:" in operation["id"]
                    ]
                )

    def test_qa_actor_era_rollover_prunes_unfiltered_generation_records(
        self,
    ) -> None:
        # algo#1216 r16 F10 upgrade path: ledgers minted BEFORE the actor
        # gate hashed the UNFILTERED reviewer set (no actor supplied). Once
        # the actor arrives the generation rolls, and the old era's
        # terminal records prune as prior-target history instead of
        # stranding the resume on unknown-ID errors.
        base = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "existing_assignees": ["jakozloski"],
            "code_reviewers": ["alice", "bob"],
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
        unfiltered_gen = qa_generation(base)
        old_id = f"qa.github.request_review:bob:g{unfiltered_gen}"
        plan = plan_handoff(
            {
                **base,
                "authenticated_actor": "bob",
                "operation_results": {old_id: operation_result("complete")},
            }
        )
        self.assertEqual(plan["state"], "pending", plan.get("errors"))
        self.assertTrue(
            any("prior-target" in warning for warning in plan["warnings"]),
            plan["warnings"],
        )
        ids = [operation["id"] for operation in plan["operations"]]
        self.assertFalse(any(":bob:" in op_id for op_id in ids), ids)

    def test_reviewer_removal_prunes_terminal_request_ops(self) -> None:
        # admin#1495 finding 3806647937: after removing bob from a completed
        # alice,bob handoff, bob's terminal request/verify records must
        # prune as prior-target history — the ":g"-based compare kept the
        # identity in the base and blocked forever on unknown IDs.
        def request(reviewers, results=None):
            payload = {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "jakozloski",
                "existing_assignees": ["jakozloski"],
                "code_reviewers": reviewers,
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
            if results is not None:
                payload["operation_results"] = results
            return payload

        first = plan_handoff(request(["alice", "bob"]))
        completed = {
            operation["id"]: operation_result("complete")
            for operation in first["operations"]
            if operation["service"] != "local"
        }
        shrunk = plan_handoff(request(["alice"], completed))
        self.assertEqual(shrunk["state"], "pending", shrunk.get("errors"))
        self.assertTrue(
            any("prior-target" in warning for warning in shrunk["warnings"]),
            shrunk["warnings"],
        )

    def test_qa_reviewer_order_is_canonical(self) -> None:
        # admin#1495 finding 3806647929: reordering reviewers kept the same
        # generation while operations followed request order — a partially
        # completed ledger then failed the prefix rule. Operations now use
        # the digest's canonical (casefold-sorted) order.
        def request(reviewers):
            return {
                "scenario": "approved_qa",
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "jakozloski",
                "existing_assignees": ["jakozloski"],
                "code_reviewers": reviewers,
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

        first = plan_handoff(request(["alice", "bob"]))
        second = plan_handoff(request(["bob", "alice"]))
        self.assertEqual(first, second)

    def test_qa_code_reviewers_mint_write_ahead_request_operations(
        self,
    ) -> None:
        # R2 #3551 finding 3737466462: the post-flip reviewer request had no
        # write-ahead record — a crash between the ready flip and the request
        # lost it with nothing for resume to replay. Routed code reviewers
        # now mint request+verify operations (roundtrip's shapes) ahead of
        # the assignee replacement. Pass-3 codex F3 / opus F1: the reviewer
        # set IS a plan target - qa_generation digests its normalized form,
        # so a reviewer change re-mints the plan and the prior round's
        # identity-bearing records take the prune path instead of hard-
        # blocking as unknown IDs.
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "existing_assignees": ["jakozloski"],
            # Uppercase FIRST (pass-3 opus F6): dedup keeps the first-seen
            # raw spelling, so this ordering is the one that can fail if
            # the id-level casefold normalization is dropped.
            "code_reviewers": ["Motykadaw", "motykadaw"],
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
        plan = plan_handoff(request)
        self.assertEqual(plan["state"], "pending")
        # Targets keep the FIRST-SEEN raw spelling (GitHub-canonical-ish);
        # only the operation ids casefold. With uppercase first, this
        # assertion fails if either half of that split is dropped.
        self.assertEqual(plan["targets"]["reviewers"], ["Motykadaw"])
        ids = [operation["id"] for operation in plan["operations"]]
        request_ids = [i for i in ids if ".request_review:" in i]
        verify_ids = [i for i in ids if ".verify_review_request:" in i]
        self.assertEqual(len(request_ids), 1)
        self.assertEqual(len(verify_ids), 1)
        self.assertIn("qa.github.request_review:motykadaw:g", request_ids[0])
        by_id = {operation["id"]: operation for operation in plan["operations"]}
        self.assertEqual(
            by_id[verify_ids[0]]["depends_on"], [request_ids[0]]
        )
        replace_id = next(i for i in ids if "replace_assignees" in i)
        self.assertEqual(
            by_id[replace_id]["depends_on"], verify_ids,
            "assignee replacement must wait for the reviewer request",
        )

    def test_qa_without_code_reviewers_plans_no_request_operations(
        self,
    ) -> None:
        # Pass-through side: repos without routed reviewers keep the exact
        # pre-existing plan shape.
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "existing_assignees": ["jakozloski"],
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
        plan = plan_handoff(request)
        self.assertEqual(plan["state"], "pending")
        self.assertEqual(plan["targets"]["reviewers"], [])
        ids = [operation["id"] for operation in plan["operations"]]
        self.assertFalse([i for i in ids if "request_review" in i])
        by_id = {operation["id"]: operation for operation in plan["operations"]}
        replace_id = next(i for i in ids if "replace_assignees" in i)
        self.assertEqual(by_id[replace_id]["depends_on"], [])

    def test_qa_invalid_code_reviewer_login_blocks(self) -> None:
        request = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "existing_assignees": ["jakozloski"],
            "code_reviewers": ["not a login!"],
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
        plan = plan_handoff(request)
        self.assertEqual(plan["state"], "blocked")
        self.assertTrue(
            any("valid GitHub logins" in error for error in plan["errors"])
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
                "reason": "no handback targets resolved and"
                " repository.nameWithOwner is not in the exact QA-owner map",
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
            "existing_assignees": ["jakozloski"],
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
        # the next call. (Also pins R2 round-2 finding 3737466456: the
        # never-attempted descendants render skipped_dependency, whose
        # record proves a non-attempt — formerly a separate subset test,
        # deduplicated per CR 3760684006.)
        plan = plan_handoff(
            self._qa_request_with_results(
                {
                    f"qa.github.replace_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.github.verify_assignees:g{QA_G}": operation_result("complete"),
                    f"qa.linear.verify_ticket_binding:g{QA_G}": operation_result(
                        "complete"
                    ),
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
                f"qa.linear.verify_ticket_binding:g{QA_G}": "complete",
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
                    f"qa.linear.verify_ticket_binding:g{QA_G}": operation_result(
                        "complete"
                    ),
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
                    f"qa.linear.verify_ticket_binding:g{QA_G}": operation_result(
                        "complete"
                    ),
                    f"qa.linear.assign_ticket:g{QA_G}": operation_result(
                        "failed", error="Linear returned 500"
                    ),
                    f"qa.linear.verify_ticket_assignee:g{QA_G}": operation_result("complete"),
                }
            )
        )

        self.assertEqual(plan["state"], "blocked")
        # algo#1216 finding 3792942228: the dependency check moved into the
        # SHARED validate_operation_collection (schema + runner + planner all
        # reject this ledger via one definition), so the shared error text
        # fires first.
        self.assertEqual(
            plan["errors"],
            [
                f"operation 'qa.linear.verify_ticket_assignee:g{QA_G}' is"
                " complete after failed/skipped predecessor"
                f" 'qa.linear.assign_ticket:g{QA_G}' — the ordered executor"
                " cannot produce this ledger"
            ],
        )

    def test_managed_environment_tool_needs_no_raw_key(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "existing_assignees": ["jakozloski"],
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
            ["environment_tool"] * 5,
        )
        self.assertNotIn("api_key", json.dumps(plan))

    def test_managed_environment_rejects_local_api_route(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
            ["local_api"] * 5,
        )

    def test_unknown_tracker_type_is_rejected(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
                        {
                            "assignees": ["tjkeeper"],
                            "precondition": {"assignees": ["jakozloski"]},
                        },
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
            "existing_assignees": ["jakozloski"],
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
                    "existing_assignees": ["jakozloski"],
                    "repository": {"nameWithOwner": "another-owner/matchmaking"},
                    "pull_request_number": PR_NUMBER,
                }
            ),
            {
                "version": 1,
                "scenario": "approved_qa",
                "state": "idle",
                "reason": "no handback targets resolved and"
                " repository.nameWithOwner is not in the exact QA-owner map",
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

    def test_unmapped_repository_plans_universal_handback(self) -> None:
        # algo#1216 r16 F2: reviewer/ball-holder handback is UNIVERSAL —
        # both clean-exit scenarios on an unmapped repository (the algo
        # lane itself) plan reviewer request/verify plus the assignee
        # transfer to the validated ball_holder, with NO Linear leg and no
        # Keeper QA-owner assignment.
        for scenario in ("approved_qa", "clean_unapproved"):
            with self.subTest(scenario=scenario):
                plan = plan_handoff(
                    {
                        "scenario": scenario,
                        "repository": {"nameWithOwner": "Keeper-Dating/algo"},
                        "pull_request_number": PR_NUMBER,
                        "authenticated_actor": "jakozloski",
                        "existing_assignees": ["jakozloski"],
                        "code_reviewers": ["michal-janicki"],
                        "ball_holder": "michal-janicki",
                    }
                )
                self.assertEqual(plan["state"], "pending", plan.get("errors"))
                actions = [op["action"] for op in plan["operations"]]
                self.assertIn("request_pull_request_review", actions)
                self.assertIn("replace_pull_request_assignees", actions)
                self.assertFalse(
                    [
                        op
                        for op in plan["operations"]
                        if op["service"] == "linear"
                    ]
                )
                replace = next(
                    op
                    for op in plan["operations"]
                    if op["action"] == "replace_pull_request_assignees"
                )
                self.assertEqual(
                    replace["payload"]["assignees"], ["michal-janicki"]
                )

    def test_unmapped_handback_without_ball_holder_warns(self) -> None:
        # No resolvable ball holder: reviewer requests still plan, the
        # ownership transfer is skipped, and the drop is surfaced.
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": {"nameWithOwner": "Keeper-Dating/algo"},
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "jakozloski",
                "existing_assignees": ["jakozloski"],
                "code_reviewers": ["michal-janicki"],
            }
        )
        self.assertEqual(plan["state"], "pending", plan.get("errors"))
        actions = [op["action"] for op in plan["operations"]]
        self.assertIn("request_pull_request_review", actions)
        self.assertNotIn("replace_pull_request_assignees", actions)
        self.assertTrue(
            any(
                "without an assignee transfer" in warning
                for warning in plan["warnings"]
            ),
            plan["warnings"],
        )

    def test_unmapped_invalid_ball_holder_blocks(self) -> None:
        plan = plan_handoff(
            {
                "scenario": "approved_qa",
                "repository": {"nameWithOwner": "Keeper-Dating/algo"},
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "jakozloski",
                "existing_assignees": ["jakozloski"],
                "code_reviewers": ["michal-janicki"],
                "ball_holder": "not a login!",
            }
        )
        self.assertEqual(plan["state"], "blocked", plan)
        self.assertTrue(
            any(
                "ball_holder must be a valid GitHub login" in error
                for error in plan["errors"]
            ),
            plan["errors"],
        )
        # mm#3551 dawid-r8 F22: the rejection precedes the generation
        # digest and every operation mint - a malformed holder can never
        # be baked into (or silently dropped from) a minted plan.
        self.assertEqual(plan["operations"], [])
        self.assertEqual(plan["call_plan"], [])

    def test_roundtrip_sorts_deduplicates_and_excludes_actor(self) -> None:
        request = {
            "scenario": "human_review_roundtrip",
            "existing_assignees": ["jakozloski"],
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
            {
                "assignees": ["alice", "zoe"],
                "precondition": {"assignees": ["jakozloski"]},
            },
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
                "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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

    def test_second_round_malformed_prior_record_blocks_not_prunes(
        self,
    ) -> None:
        # CR 3777197527: the roundtrip stale sweep pruned prior-generation
        # records by raw status BEFORE shape validation, while the QA sweep
        # shape-gates first — a malformed record whose status said
        # "complete" was silently laundered out of the ledger. The sweep
        # must block on shape errors exactly like the QA path.
        first_round = {
            "scenario": "human_review_roundtrip",
            "existing_assignees": ["jakozloski"],
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [reviewer("alice")],
        }
        first_ids = [
            operation["id"]
            for operation in plan_handoff(first_round)["operations"]
        ]
        malformed = {"status": "complete"}  # no attempts/timestamps/evidence
        second_round = {
            "scenario": "human_review_roundtrip",
            "existing_assignees": ["jakozloski"],
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
            "operation_results": {first_ids[0]: malformed},
        }
        second_plan = plan_handoff(second_round)
        self.assertEqual(second_plan["state"], "blocked")
        # CR 3791614716: pin the SHAPE error itself — blocked-for-any-reason
        # (an unknown-ID error, say) must not satisfy this regression.
        self.assertTrue(
            any(
                first_ids[0] in error and "attempts" in error
                for error in second_plan["errors"]
            ),
            second_plan["errors"],
        )
        self.assertFalse(
            any(
                "prior-generation" in warning
                for warning in second_plan.get("warnings", [])
            ),
            "a malformed record must never be pruned as terminal history",
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
            "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
            {
                "assignees": ["alice", "zoe"],
                "precondition": {"assignees": ["jakozloski"]},
            },
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
                    {
                        "assignees": ["alice", "zoe"],
                        "precondition": {"assignees": ["jakozloski"]},
                    },
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
            "existing_assignees": ["jakozloski"],
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
                # r13 F4: assignee mutations resume only with their
                # persisted pre-mutation baseline.
                "precondition": {"assignees": ["jakozloski"]},
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
                "existing_assignees": ["jakozloski"],
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
                "qa.linear.verify_ticket_binding",
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
        # CR 3760684014: pin the suffix FORMAT too, not only disjointness —
        # a digest helper regressing to a constant would keep the sets
        # disjoint from prior_g while silently unbinding targets.
        self.assertRegex(current_g, r"^[0-9a-f]{12}$")
        self.assertEqual(
            plan["call_plan"][0]["id"],
            f"qa.github.replace_assignees:g{current_g}",
        )
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("7 prior-target terminal QA record(s)", plan["warnings"][0])

    def test_in_flight_prior_target_qa_record_fails_closed(self) -> None:
        # An in-flight record persisted for different targets marks a
        # mutation that may already have fired remotely: never prune it —
        # block with the recovery named, mirroring the roundtrip contract.
        request = {
            "scenario": "approved_qa",
            "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
        }
        g = qa_generation(request)
        request["operation_results"] = {
            f"qa.github.replace_assignees:g{g}": {
                "status": "retryable",
                "precondition": {"assignees": ["jakozloski"]},
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                        "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
        # Post-merge codex F2: the executor stops at the first terminal
        # failure, so the descendants of the failed mutation carry rendered
        # skipped_dependency non-attempt records - persisted `failed`
        # records below a failed dependency are now rejected as an
        # inconsistent ledger (see the failed-descendant test above).
        request["operation_results"] = {
            f"qa.github.replace_assignees:g{QA_G}": operation_result("complete"),
            f"qa.github.verify_assignees:g{QA_G}": operation_result("complete"),
            f"qa.linear.verify_ticket_binding:g{QA_G}": operation_result("complete"),
            f"qa.linear.assign_ticket:g{QA_G}": operation_result(
                "failed", error="assignment failed"
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
                        {
                            "assignees": ["tjkeeper"],
                            "precondition": {"assignees": ["jakozloski"]},
                        },
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
                        "id": f"qa.linear.verify_ticket_binding:g{QA_G}",
                        "service": "linear",
                        "action": "verify_ticket_binding",
                        "depends_on": [f"qa.github.verify_assignees:g{QA_G}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "expected_ticket_provider_id": "linear-ticket-web-8877",
                            "expected_repository": "Keeper-Dating/matchmaking",
                            "expected_pull_request_number": PR_NUMBER,
                            "write_path": "environment_tool",
                        },
                        "status": "complete",
                    },
                    {
                        "id": f"qa.linear.assign_ticket:g{QA_G}",
                        "service": "linear",
                        "action": "assign_ticket",
                        "depends_on": [f"qa.linear.verify_ticket_binding:g{QA_G}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "assignee_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
                            "assignee_email": "tj@keeper.ai",
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
                            "expected_ticket_provider_id": "linear-ticket-web-8877",
                            "expected_assignee_id": "4d5aed4e-076c-47e5-94a1-0a39287364e1",
                            "expected_assignee_name": "Timothy Jhon Pascual",
                            "write_path": "environment_tool",
                        },
                        "status": "skipped_dependency",
                        "error": (
                            f"dependency failed: qa.linear.assign_ticket:g{QA_G}"
                        ),
                    },
                    {
                        "id": f"qa.linear.set_ticket_state:g{QA_G}",
                        "service": "linear",
                        "action": "set_ticket_state",
                        "depends_on": [f"qa.linear.verify_ticket_assignee:g{QA_G}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "state_id": "linear-state-vercel-preview-qa",
                            "state_name": "Vercel Preview QA",
                            "write_path": "environment_tool",
                        },
                        "status": "skipped_dependency",
                        "error": (
                            "dependency failed:"
                            f" qa.linear.verify_ticket_assignee:g{QA_G}"
                        ),
                    },
                    {
                        "id": f"qa.linear.verify_ticket_state:g{QA_G}",
                        "service": "linear",
                        "action": "verify_ticket_state",
                        "depends_on": [f"qa.linear.set_ticket_state:g{QA_G}"],
                        "payload": {
                            "ticket_identifier": "WEB-8877",
                            "expected_ticket_provider_id": "linear-ticket-web-8877",
                            "expected_state_id": "linear-state-vercel-preview-qa",
                            "expected_state_name": "Vercel Preview QA",
                            "write_path": "environment_tool",
                        },
                        "status": "skipped_dependency",
                        "error": (
                            f"dependency failed: qa.linear.set_ticket_state:g{QA_G}"
                        ),
                    },
                ],
                "call_plan": [],
                "warnings": [
                    f"Remote operation qa.linear.assign_ticket:g{QA_G} failed; complete it manually.",
                    f"Operation qa.linear.verify_ticket_assignee:g{QA_G} not executed "
                    f"(dependency failed: qa.linear.assign_ticket:g{QA_G}); complete it manually.",
                    f"Operation qa.linear.set_ticket_state:g{QA_G} not executed "
                    f"(dependency failed: qa.linear.verify_ticket_assignee:g{QA_G}); complete it manually.",
                    f"Operation qa.linear.verify_ticket_state:g{QA_G} not executed "
                    f"(dependency failed: qa.linear.set_ticket_state:g{QA_G}); complete it manually.",
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
                        "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                        "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                        "existing_assignees": ["jakozloski"],
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
                tracker["qa_assignee"]["provider_id"] = " 4d5aed4e-076c-47e5-94a1-0a39287364e1 "
                expected_field = "qa_assignee.provider_id"
            else:
                tracker[field] = " "
                expected_field = field
            with self.subTest(field=field):
                plan = plan_handoff(
                    {
                        "scenario": "approved_qa",
                        "existing_assignees": ["jakozloski"],
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
                        "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
            "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
            if len(cells) != 5 or not cells[0].startswith("`Keeper-Dating/"):
                continue
            documented[cells[0].strip("`")] = {
                "github_login": cells[1].strip("`"),
                "linear_email": cells[2].strip("`"),
                "linear_user_id": cells[3].strip("`"),
                "linear_name": cells[4],
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
                "existing_assignees": ["jakozloski"],
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
                "existing_assignees": ["jakozloski"],
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
                        "existing_assignees": ["jakozloski"],
                        "repository": REPOSITORY,
                        "pull_request_number": PR_NUMBER,
                        "authenticated_actor": "alice",
                        "reviewers": [],
                        "operation_results": malformed,
                    }
                )
                self.assertEqual(plan["state"], "blocked", plan)
                self.assertTrue(plan["errors"], plan)


class TicketBindingGateTests(unittest.TestCase):
    """R2 round-3 finding 3774514905: the pure planner need not fetch, but
    the EXECUTION boundary must re-fetch and bind the ticket before any
    tracker mutation - otherwise a stale/mismatched ``validated_ticket``
    provider ID drives Linear mutations against an unrelated ticket. The
    plan therefore opens the Linear chain with a read-only
    ``verify_ticket_binding`` operation whose failure cascades
    ``skipped_dependency`` over every Linear mutation."""

    def _qa_request(
        self, operation_results: dict[str, object] | None = None
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "scenario": "approved_qa",
            "existing_assignees": ["jakozloski"],
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
        }
        if operation_results is not None:
            request["operation_results"] = operation_results
        return request

    def test_binding_verification_precedes_every_linear_mutation(self) -> None:
        plan = plan_handoff(self._qa_request())
        operations = {op["id"]: op for op in plan["operations"]}
        binding_id = f"qa.linear.verify_ticket_binding:g{QA_G}"
        self.assertIn(binding_id, operations, sorted(operations))
        binding = operations[binding_id]
        self.assertEqual(binding["service"], "linear")
        self.assertEqual(binding["action"], "verify_ticket_binding")
        # The binding is the execution-boundary re-fetch: it carries every
        # fact the executor must confirm against the freshly fetched
        # ticket and PR body BEFORE the first mutation fires (admin#1495
        # r20 F2: the PR body's first-line ticket link is the canonical
        # linkage source; Linear-side attachments are never required).
        payload = binding["payload"]
        self.assertEqual(payload["ticket_identifier"], "WEB-8877")
        # Identifier-keyed read (algo#1216 finding 3792942223): the broker
        # resolves the identifier; the recorded provider id is the
        # COMPARISON value the read must confirm, not a mutation key.
        self.assertEqual(
            payload["expected_ticket_provider_id"], "linear-ticket-web-8877"
        )
        self.assertEqual(
            payload["expected_repository"], "Keeper-Dating/matchmaking"
        )
        self.assertEqual(payload["expected_pull_request_number"], PR_NUMBER)
        self.assertEqual(payload["write_path"], "environment_tool")
        # The first Linear mutation depends on the binding, so a mismatch
        # can never be ordered after the mutation it exists to prevent.
        assign = operations[f"qa.linear.assign_ticket:g{QA_G}"]
        self.assertIn(binding_id, assign["depends_on"])
        # The binding itself runs only after GitHub assignment verified -
        # the plan's existing chain stays intact ahead of it.
        self.assertIn(
            f"qa.github.verify_assignees:g{QA_G}", binding["depends_on"]
        )

    def test_binding_payload_is_the_complete_source_fingerprint(self) -> None:
        # admin#1495 r20 F2: the executor's canonical linkage check reads
        # the freshly fetched PR body's first-line ticket link, so every
        # fact it binds - repository, PR number, ticket identifier,
        # provider id, write path - must ride the payload itself. Exact
        # equality pins the no-side-channel contract from both sides: a
        # dropped field would force the executor back onto ambient state,
        # a surplus field would smuggle in a fact the target digest never
        # covers.
        plan = plan_handoff(self._qa_request())
        operations = {op["id"]: op for op in plan["operations"]}
        binding = operations[f"qa.linear.verify_ticket_binding:g{QA_G}"]
        self.assertEqual(
            binding["payload"],
            {
                "ticket_identifier": "WEB-8877",
                "expected_ticket_provider_id": "linear-ticket-web-8877",
                "expected_repository": "Keeper-Dating/matchmaking",
                "expected_pull_request_number": PR_NUMBER,
                "write_path": "environment_tool",
            },
        )

    def test_mismatched_fingerprint_component_orphans_prior_binding(
        self,
    ) -> None:
        # admin#1495 r20 F2: the binding is bound to the COMPLETE source
        # fingerprint. Flip any one component - repository, PR number,
        # ticket identifier, provider id - and the plan re-mints: the
        # fresh binding payload tracks the changed component, and a
        # `complete` binding persisted under the prior fingerprint is
        # prior-target history (pruned with a warning), never proof the
        # current fingerprint was verified.
        stale_id = f"qa.linear.verify_ticket_binding:g{QA_G}"

        def mismatched_request(**overrides: object) -> dict[str, object]:
            request = self._qa_request(
                {stale_id: operation_result("complete")}
            )
            tracker = dict(request["issue_tracker"])  # type: ignore[arg-type]
            for key, value in overrides.items():
                if key in ("repository", "pull_request_number"):
                    request[key] = value
                else:
                    tracker[key] = value
            request["issue_tracker"] = tracker
            return request

        cases = [
            (
                "repository",
                mismatched_request(
                    repository={
                        "nameWithOwner": "Keeper-Dating/calculator-api"
                    }
                ),
                "expected_repository",
                "Keeper-Dating/calculator-api",
            ),
            (
                "pull_request_number",
                mismatched_request(pull_request_number=PR_NUMBER + 1),
                "expected_pull_request_number",
                PR_NUMBER + 1,
            ),
            (
                "ticket_identifier",
                mismatched_request(ticket_identifier="WEB-9999"),
                "ticket_identifier",
                "WEB-9999",
            ),
            (
                "ticket_provider_id",
                mismatched_request(
                    ticket_provider_id="linear-ticket-web-9999"
                ),
                "expected_ticket_provider_id",
                "linear-ticket-web-9999",
            ),
        ]
        for component, request, payload_key, expected in cases:
            with self.subTest(component=component):
                generation = qa_generation(request)
                self.assertNotEqual(generation, QA_G)
                plan = plan_handoff(request)
                operations = {op["id"]: op for op in plan["operations"]}
                binding = operations[
                    f"qa.linear.verify_ticket_binding:g{generation}"
                ]
                self.assertEqual(binding["payload"][payload_key], expected)
                # The pruned prior record satisfied nothing: the fresh
                # binding still awaits execution and the plan is pending.
                self.assertNotEqual(binding["status"], "complete")
                self.assertEqual(plan["state"], "pending", plan)
                self.assertTrue(
                    any(
                        "prior-target terminal QA record" in warning
                        and stale_id in warning
                        for warning in plan["warnings"]
                    ),
                    plan["warnings"],
                )

    def test_failed_descendant_of_failed_binding_is_blocked(self) -> None:
        # Post-merge codex F2: the cascade guard rejected complete/pending/
        # retryable descendants of a failed dependency but silently ACCEPTED
        # a recorded `failed` - yet the executor may never attempt a
        # mutation whose dependency terminally failed, so a failed record
        # there claims an attempt the plan forbade. Only skipped_dependency
        # is a consistent descendant record.
        plan = plan_handoff(
            self._qa_request(
                {
                    f"qa.github.replace_assignees:g{QA_G}": operation_result(
                        "complete"
                    ),
                    f"qa.github.verify_assignees:g{QA_G}": operation_result(
                        "complete"
                    ),
                    f"qa.linear.verify_ticket_binding:g{QA_G}": operation_result(
                        "failed", error="identifier mismatch"
                    ),
                    f"qa.linear.assign_ticket:g{QA_G}": operation_result(
                        "failed", error="Linear returned 500"
                    ),
                }
            )
        )
        self.assertEqual(plan["state"], "blocked", plan)
        self.assertTrue(
            any(
                "cannot have results" in error
                or "after failed/skipped predecessor" in error
                for error in plan["errors"]
            ),
            plan["errors"],
        )

    def test_failed_binding_skips_every_linear_mutation(self) -> None:
        plan = plan_handoff(
            self._qa_request(
                {
                    f"qa.github.replace_assignees:g{QA_G}": operation_result(
                        "complete"
                    ),
                    f"qa.github.verify_assignees:g{QA_G}": operation_result(
                        "complete"
                    ),
                    f"qa.linear.verify_ticket_binding:g{QA_G}": operation_result(
                        "failed",
                        error=(
                            "fetched identifier WEB-9999 does not match"
                            " validated WEB-8877"
                        ),
                    ),
                }
            )
        )
        self.assertEqual(plan["state"], "failed", plan)
        rendered = {op["id"]: op for op in plan["operations"]}
        for suffix in (
            "assign_ticket",
            "verify_ticket_assignee",
            "set_ticket_state",
            "verify_ticket_state",
        ):
            record = rendered[f"qa.linear.{suffix}:g{QA_G}"]
            self.assertEqual(
                record["status"], "skipped_dependency", (suffix, record)
            )

    def test_write_path_none_plans_no_binding_operation(self) -> None:
        # With no authorized write path there is no tracker mutation to
        # protect: the plan records the unavailable outcome and must not
        # demand a Linear read it has no path to perform.
        request = self._qa_request()
        tracker = dict(request["issue_tracker"])  # type: ignore[arg-type]
        tracker["write_path"] = "none"
        tracker.pop("qa_assignee")
        tracker.pop("qa_state")
        request["issue_tracker"] = tracker
        plan = plan_handoff(request)
        ids = {op["id"] for op in plan["operations"]}
        self.assertFalse(
            {op_id for op_id in ids if "verify_ticket_binding" in op_id}, ids
        )


class TicketBindingContractSyncTest(unittest.TestCase):
    """admin#1495 r20 F2: the reference's binding contract must be
    satisfiable by the managed executor. Keeper's pinned managed
    ``get_issue``/``get_issue_with_context`` projections return issue
    fields, comments, and relations - never attachment URLs - so a step
    that made Linear-side attachments/links MANDATORY failed every
    otherwise valid managed Linear QA handoff even while a live linkage
    source (the PR body's first-line ticket link, fully identified by
    the payload's source fingerprint) existed. These assertions pin the
    documented contract's discriminating pair: the canonical PR-body
    definition present at both sites (step 1 defines, step 6
    references), the attachments-mandatory wording gone."""

    @property
    def reference_text(self) -> str:
        reference_path = (
            Path(__file__).resolve().parent.parent
            / "references"
            / "monitor-exit-handoffs.md"
        )
        return reference_path.read_text(encoding="utf-8")

    def test_canonical_linkage_check_defined_once_and_referenced(self) -> None:
        text = self.reference_text
        # Step 1 DEFINES the check against the payload's complete source
        # fingerprint...
        self.assertIn("**canonical linkage check** (admin#1495 r20 F2)", text)
        self.assertIn(
            "complete source fingerprint (repository, PR number, ticket"
            " identifier, provider id)",
            text,
        )
        # ...and step 6 REFERENCES that single definition instead of
        # restating (or re-contradicting) it.
        self.assertIn(
            "canonical linkage check step 1 defines for"
            " `qa.linear.verify_ticket_binding`",
            text,
        )
        # The canonical source is the PR body's template-mandated
        # first-line ticket link; arbitrary description URLs are
        # documented as rejected.
        self.assertIn("first-line ticket link", text)
        self.assertIn("NEVER satisfies the check", text)

    def test_linear_attachments_are_supplementary_never_required(self) -> None:
        text = self.reference_text
        self.assertIn("Linear-side attachments/links are NOT required", text)
        self.assertIn("supplementary corroboration", text)
        # The old contract's two halves - a mandatory Linear-side check
        # the managed projections cannot satisfy, and step 6's unordered
        # attachments-first either/or - must stay gone: either phrase
        # reappearing re-opens the contradiction.
        self.assertNotIn("attachments/links include THIS pull request", text)
        self.assertNotIn("appear in the ticket's attachments/links", text)


class ReviewerRequestPlanTests(unittest.TestCase):
    """R2 round-3 finding 3774515577: the NON-KEEPER draft-flip reviewer
    request was a bare ``gh pr edit`` with no write-ahead record - a crash
    between the flip and the request lost the request silently. The
    ``reviewer_request`` scenario plans it as ledgered operations with the
    same write-ahead -> execute -> verify lifecycle as the handoffs. The
    Keeper R2-satisfied handback is NOT this scenario: it runs through the
    QA plan's code_reviewers ledger (admin#1495 finding 3791925155)."""

    def _request(
        self,
        reviewers: list[str] | None = None,
        ball_holder: str | None = "motykadaw",
        operation_results: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "scenario": "reviewer_request",
            "existing_assignees": ["jakozloski"],
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewer_requests": {
                "reviewers": (
                    reviewers if reviewers is not None else ["motykadaw"]
                ),
                "ball_holder": ball_holder,
            },
        }
        if operation_results is not None:
            request["operation_results"] = operation_results
        return request

    def test_plans_request_verify_and_ball_holder_operations(self) -> None:
        plan = plan_handoff(self._request(reviewers=["tjkeeper", "motykadaw"]))
        self.assertEqual(plan["state"], "pending", plan)
        generation = reviewer_request_generation(
            "Keeper-Dating/matchmaking",
            PR_NUMBER,
            ["tjkeeper", "motykadaw"],
            "motykadaw",
        )
        operations = {op["id"]: op for op in plan["operations"]}
        for login in ("motykadaw", "tjkeeper"):
            request_id = (
                f"reviewer.github.request_review:{login}:g{generation}"
            )
            verify_id = (
                f"reviewer.github.verify_review_request:{login}:g{generation}"
            )
            self.assertIn(request_id, operations, sorted(operations))
            self.assertIn(verify_id, operations, sorted(operations))
            self.assertEqual(
                operations[verify_id]["depends_on"], [request_id]
            )
        replace = operations[
            f"reviewer.github.replace_assignees:g{generation}"
        ]
        # Exactly ONE ball-holder assignee, atomic replacement.
        self.assertEqual(replace["payload"]["assignees"], ["motykadaw"])
        verify_assignees = operations[
            f"reviewer.github.verify_assignees:g{generation}"
        ]
        self.assertEqual(
            verify_assignees["payload"]["expected_assignees"], ["motykadaw"]
        )

    def test_generation_rebinds_when_targets_change(self) -> None:
        first = reviewer_request_generation(
            "Keeper-Dating/matchmaking", PR_NUMBER, ["motykadaw"], "motykadaw"
        )
        second = reviewer_request_generation(
            "Keeper-Dating/matchmaking", PR_NUMBER, ["tjkeeper"], "tjkeeper"
        )
        self.assertNotEqual(first, second)

    def test_requires_nonempty_reviewers_and_member_ball_holder(self) -> None:
        empty = plan_handoff(self._request(reviewers=[]))
        self.assertEqual(empty["state"], "blocked", empty)
        outsider = plan_handoff(
            self._request(reviewers=["motykadaw"], ball_holder="tjkeeper")
        )
        self.assertEqual(outsider["state"], "blocked", outsider)

    def test_malformed_generation_ids_are_never_pruned_as_history(self) -> None:
        # Post-merge codex F3 (opus F3 corroborating): the sweeps matched
        # prior-target records by leading operation family alone, so a
        # malformed ID with a known family but no valid :g<12-hex> digest
        # tail was laundered as prunable history instead of failing closed
        # as an unknown ID. All three sweeps require the full digest tail.
        malformed = {
            "reviewer_request": (
                self._request(
                    operation_results={
                        "reviewer.github.replace_assignees:not-a-valid-generation": (
                            operation_result("complete")
                        )
                    }
                )
            ),
            "qa": {
                "scenario": "approved_qa",
                "existing_assignees": ["jakozloski"],
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
                "operation_results": {
                    "qa.linear.assign_ticket:gNOTHEXNOTHEX": operation_result(
                        "complete"
                    )
                },
            },
            # Pass-3 opus F7: the roundtrip sweep's malformed-id path was
            # the uncovered third of the "all three sweeps" claim.
            "roundtrip": {
                "scenario": "human_review_roundtrip",
                "existing_assignees": ["jakozloski"],
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "jakozloski",
                "reviewers": [reviewer("motykadaw")],
                "operation_results": {
                    "roundtrip.github.request_review:motykadaw:gBADBADBAD": (
                        operation_result("complete")
                    )
                },
            },
        }
        for label, request in malformed.items():
            with self.subTest(label=label):
                plan = plan_handoff(request)
                self.assertEqual(plan["state"], "blocked", plan)
                self.assertTrue(
                    any(
                        "unknown operation IDs" in error
                        for error in plan["errors"]
                    ),
                    plan["errors"],
                )

    def test_reviewer_turnover_reminted_digest_prunes_prior_round(self) -> None:
        # Pass-3 codex F3 / opus F1: switching the routed reviewer must
        # re-mint the QA digest so the prior round's identity-bearing
        # records become prunable prior-generation history - not unknown
        # IDs that hard-block the terminal exit.
        def qa_request(reviewers: list[str], results: dict[str, object]) -> dict[str, object]:
            return {
                "scenario": "approved_qa",
                "authenticated_actor": "jakozloski",
                "existing_assignees": ["jakozloski"],
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "code_reviewers": reviewers,
                "issue_tracker": {
                    "type": "linear",
                    "qa_assignee": LINEAR_QA_ASSIGNEE,
                    "qa_state": LINEAR_QA_STATE_WEB,
                    "ticket_identifier": "WEB-8877",
                    "ticket_provider_id": "linear-ticket-web-8877",
                    "ticket_validated": True,
                    "write_path": "environment_tool",
                },
                "operation_results": results,
            }

        old_generation = qa_generation(qa_request(["motykadaw"], {}))
        new_generation = qa_generation(qa_request(["michal-janicki"], {}))
        self.assertNotEqual(old_generation, new_generation)

        # Terminal prior-reviewer records prune with a warning.
        plan = plan_handoff(
            qa_request(
                ["michal-janicki"],
                {
                    "qa.github.request_review:motykadaw:g"
                    f"{old_generation}": operation_result("complete"),
                    "qa.github.verify_review_request:motykadaw:g"
                    f"{old_generation}": operation_result("complete"),
                },
            )
        )
        self.assertEqual(plan["state"], "pending", plan)
        self.assertTrue(
            any("prior-target" in warning for warning in plan["warnings"]),
            plan["warnings"],
        )

        # An in-flight prior-reviewer record fails closed.
        in_flight = plan_handoff(
            qa_request(
                ["michal-janicki"],
                {
                    "qa.github.request_review:motykadaw:g"
                    f"{old_generation}": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    },
                },
            )
        )
        self.assertEqual(in_flight["state"], "blocked", in_flight)

    def test_extra_segment_ids_are_never_pruned_as_history(self) -> None:
        # Pass-3 codex F4: a tail-only check accepted extra-segment ids
        # like family:gBAD:g<hex> as prunable history. The full grammar
        # (family, optional identity, digest - nothing else) rejects them
        # in ALL THREE sweeps, so they stay unknown-ID errors.
        good_tail = "0" * 12
        qa_request = {
            "scenario": "approved_qa",
            "existing_assignees": ["jakozloski"],
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
            "operation_results": {
                f"qa.linear.assign_ticket:gBAD:g{good_tail}": operation_result(
                    "complete"
                )
            },
        }
        reviewer_request = self._request(
            operation_results={
                "reviewer.github.request_review:motykadaw:extra:g"
                f"{good_tail}": operation_result("complete")
            }
        )
        roundtrip_request = {
            "scenario": "human_review_roundtrip",
            "existing_assignees": ["jakozloski"],
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [reviewer("motykadaw")],
            "operation_results": {
                f"roundtrip.github.request_review:gBAD:g{good_tail}": (
                    operation_result("complete")
                )
            },
        }
        for label, request in {
            "qa": qa_request,
            "reviewer_request": reviewer_request,
            "roundtrip": roundtrip_request,
        }.items():
            with self.subTest(label=label):
                plan = plan_handoff(request)
                self.assertEqual(plan["state"], "blocked", plan)
                self.assertTrue(
                    any(
                        "unknown operation IDs" in error
                        for error in plan["errors"]
                    ),
                    plan["errors"],
                )

    def test_wrong_identity_arity_is_never_pruned_as_history(self) -> None:
        # Pass-4 codex F1: identity arity is per-family. A surplus identity
        # on an identity-free family and a missing identity on a
        # per-reviewer family both stay unknown-ID errors in all sweeps.
        current_reviewer_generation = reviewer_request_generation(
            "Keeper-Dating/matchmaking", PR_NUMBER, ["motykadaw"], "motykadaw"
        )
        cases = {
            "qa_surplus": {
                "scenario": "approved_qa",
                "existing_assignees": ["jakozloski"],
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
                "operation_results": {
                    "qa.github.replace_assignees:bogus:g"
                    + "0" * 12: operation_result("complete")
                },
            },
            "reviewer_missing": self._request(
                operation_results={
                    "reviewer.github.request_review:g"
                    f"{current_reviewer_generation}": operation_result(
                        "complete"
                    )
                }
            ),
            "roundtrip_surplus": {
                "scenario": "human_review_roundtrip",
                "existing_assignees": ["jakozloski"],
                "repository": REPOSITORY,
                "pull_request_number": PR_NUMBER,
                "authenticated_actor": "jakozloski",
                "reviewers": [reviewer("motykadaw")],
                "operation_results": {
                    "roundtrip.github.replace_assignees:bogus:g"
                    + "0" * 12: operation_result("complete")
                },
            },
        }
        for label, request in cases.items():
            with self.subTest(label=label):
                plan = plan_handoff(request)
                self.assertEqual(plan["state"], "blocked", plan)
                self.assertTrue(
                    any(
                        "unknown operation IDs" in error
                        for error in plan["errors"]
                    ),
                    plan["errors"],
                )

    def test_fabricated_root_skip_is_blocked(self) -> None:
        # Pass-3 codex F5: a persisted skipped_dependency on an operation
        # with no failed declared dependency claims a skip the plan never
        # produced - accepting it cascades fabricated skips.
        plan = plan_handoff(
            self._request(
                operation_results={
                    "reviewer.github.request_review:motykadaw:g"
                    + reviewer_request_generation(
                        "Keeper-Dating/matchmaking",
                        PR_NUMBER,
                        ["motykadaw"],
                        "motykadaw",
                    ): {
                        "status": "skipped_dependency",
                        "attempts": 0,
                        "error": "dependency failed: fabricated",
                    }
                }
            )
        )
        self.assertEqual(plan["state"], "blocked", plan)
        self.assertTrue(
            any(
                "cannot be skipped_dependency" in error
                for error in plan["errors"]
            ),
            plan["errors"],
        )

    def test_newline_suffixed_ids_are_never_pruned_as_history(self) -> None:
        # Post-merge pass-2 codex F1: $ matches before a trailing newline,
        # so a completed CURRENT id suffixed with '\n' pruned as history
        # and the same mutation re-queued. \Z closes that in ALL THREE
        # sweeps (qa, reviewer_request, roundtrip).
        qa_request = {
            "scenario": "approved_qa",
            "existing_assignees": ["jakozloski"],
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
            "operation_results": {
                f"qa.github.replace_assignees:g{QA_G}\n": operation_result(
                    "complete"
                )
            },
        }
        reviewer_generation = reviewer_request_generation(
            "Keeper-Dating/matchmaking", PR_NUMBER, ["motykadaw"], "motykadaw"
        )
        reviewer_request = self._request(
            operation_results={
                "reviewer.github.request_review:motykadaw:g"
                f"{reviewer_generation}\n": operation_result("complete")
            }
        )
        roundtrip_request = {
            "scenario": "human_review_roundtrip",
            "existing_assignees": ["jakozloski"],
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "reviewers": [reviewer("motykadaw")],
            "operation_results": {
                "roundtrip.github.request_review:motykadaw:g"
                + "0" * 12
                + "\n": operation_result("complete")
            },
        }
        cases = {
            "qa": qa_request,
            "reviewer_request": reviewer_request,
            "roundtrip": roundtrip_request,
        }
        for label, request in cases.items():
            with self.subTest(label=label):
                plan = plan_handoff(request)
                self.assertEqual(plan["state"], "blocked", plan)
                self.assertTrue(
                    any(
                        "unknown operation IDs" in error
                        for error in plan["errors"]
                    ),
                    plan["errors"],
                )

    def test_rejects_the_authenticated_actor_as_reviewer(self) -> None:
        # Post-fix review F3: GitHub 422s a self review-request, which the
        # ledger would then carry as a permanent failure. The plan rejects
        # it up front instead - including by case-variant spelling.
        for spelling in ("jakozloski", "JakOzloski"):
            with self.subTest(spelling=spelling):
                plan = plan_handoff(
                    self._request(
                        reviewers=[spelling, "motykadaw"],
                        ball_holder="motykadaw",
                    )
                )
                self.assertEqual(plan["state"], "blocked", plan)
                self.assertTrue(
                    any("authenticated actor" in e for e in plan["errors"]),
                    plan["errors"],
                )
        missing_actor = dict(self._request())
        missing_actor.pop("authenticated_actor")
        plan = plan_handoff(missing_actor)
        self.assertEqual(plan["state"], "blocked", plan)

    def test_case_variants_canonicalize_to_one_plan(self) -> None:
        # Post-fix review F4: the digest casefolds, so payloads and targets
        # must too - otherwise two case-variant requests share a generation
        # while their payload spellings differ, breaking both the rebind
        # promise and the exact-array assignee verification.
        upper = plan_handoff(
            self._request(reviewers=["MotykaDaw"], ball_holder="MOTYKADAW")
        )
        lower = plan_handoff(
            self._request(reviewers=["motykadaw"], ball_holder="motykadaw")
        )
        self.assertEqual(upper, lower)
        replace = next(
            op
            for op in lower["operations"]
            if op["action"] == "replace_pull_request_assignees"
        )
        self.assertEqual(replace["payload"]["assignees"], ["motykadaw"])


class QaSurfaceGateTests(unittest.TestCase):
    """User correction 2026-08-25 (WEB-9971/mm#3934), re-landed 2026-09:
    ``qa_surface_present: false`` suppresses the mapped QA-owner assignee
    and every tracker leg — ownership routes to the request ball_holder,
    the plan surfaces an advisory warning, and the digest mirrors the
    resolution through the consumer-exact fold (no flag key): identical
    minted operations share a digest across the flag, and a suppressed
    digest is invariant across every tracker input the suppressed
    builder never reads."""

    def _request(self, **overrides: object) -> dict[str, object]:
        request: dict[str, object] = {
            "scenario": "approved_qa",
            "repository": REPOSITORY,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "existing_assignees": ["jakozloski"],
            "code_reviewers": ["motykadaw"],
            "issue_tracker": {
                "type": "linear",
                "ticket_validated": True,
                "qa_assignee": LINEAR_QA_ASSIGNEE,
                "qa_state": LINEAR_QA_STATE_WEB,
                "ticket_identifier": "WEB-8877",
                "ticket_provider_id": "linear-ticket-web-8877",
                "write_path": "environment_tool",
            },
        }
        request.update(overrides)
        return request

    def test_surface_false_routes_ball_holder_and_skips_linear(self) -> None:
        plan = plan_handoff(
            self._request(qa_surface_present=False, ball_holder="motykadaw")
        )
        self.assertEqual(plan["errors"], [])
        self.assertEqual(plan["state"], "pending")
        self.assertEqual(plan["targets"]["assignees"], ["motykadaw"])
        self.assertIsNone(plan["targets"]["linear_assignee"])
        operation_ids = [op["id"] for op in plan["operations"]]
        self.assertFalse(
            any(op_id.startswith("qa.linear.") for op_id in operation_ids)
        )
        replace = next(
            op
            for op in plan["operations"]
            if op["action"] == "replace_pull_request_assignees"
        )
        self.assertEqual(replace["payload"]["assignees"], ["motykadaw"])
        self.assertTrue(
            any("qa_surface_present is false" in w for w in plan["warnings"])
        )
        # Phase-4 review F3 (2026-09 re-land): in a Keeper repository the
        # suppressed first clean exit is the ONLY moment the post-R2
        # reviewer request runs, so "reviewer requests plan unchanged"
        # must be pinned — a suppression that drops the reviewer loop
        # silently never requests the human reviewer.
        self.assertEqual(plan["targets"]["reviewers"], ["motykadaw"])
        self.assertTrue(
            any(
                op_id.startswith("qa.github.request_review:motykadaw:g")
                for op_id in operation_ids
            )
        )

    def test_surface_default_keeps_mapped_owner_and_linear_legs(self) -> None:
        plan = plan_handoff(self._request())
        self.assertEqual(plan["state"], "pending", plan.get("errors"))
        self.assertEqual(plan["targets"]["assignees"], ["tjkeeper"])
        operation_ids = [op["id"] for op in plan["operations"]]
        self.assertTrue(
            any(op_id.startswith("qa.linear.") for op_id in operation_ids)
        )

    def test_surface_false_re_mints_the_generation(self) -> None:
        # Validated fixture: the tracker fold moves the digest even with
        # the same handback login as the mapped owner.
        suppressed = qa_generation(
            self._request(qa_surface_present=False, ball_holder="tjkeeper")
        )
        self.assertNotEqual(suppressed, qa_generation(self._request()))

    def test_surface_non_boolean_blocks(self) -> None:
        plan = plan_handoff(self._request(qa_surface_present="no"))
        self.assertEqual(plan["state"], "blocked")
        self.assertTrue(
            any("must be a boolean" in error for error in plan["errors"])
        )

    def test_surface_false_with_no_targets_is_idle(self) -> None:
        plan = plan_handoff(
            self._request(qa_surface_present=False, code_reviewers=[])
        )
        self.assertEqual(plan["state"], "idle")
        self.assertIn("qa_surface_present is false", plan["reason"] or "")

    def test_surface_false_reuses_ball_holder_validation(self) -> None:
        plan = plan_handoff(
            self._request(qa_surface_present=False, ball_holder="not a login!")
        )
        self.assertEqual(plan["state"], "blocked")
        self.assertTrue(
            any("ball_holder" in error for error in plan["errors"])
        )

    def test_suppressed_digest_equals_trackerless_suppressed(self) -> None:
        with_tracker = qa_generation(
            self._request(qa_surface_present=False, ball_holder="motykadaw")
        )
        request = self._request(
            qa_surface_present=False, ball_holder="motykadaw"
        )
        del request["issue_tracker"]
        self.assertEqual(with_tracker, qa_generation(request))

    def test_suppressed_digest_invariant_across_tracker_inputs(self) -> None:
        reference = qa_generation(
            self._request(qa_surface_present=False, ball_holder="motykadaw")
        )
        variants: list[dict[str, object]] = [
            {"ticket_validated": False},
            {
                "ticket_identifier": "WEB-9999",
                "ticket_provider_id": "linear-ticket-web-9999",
            },
            {"write_path": "local_api"},
            {"qa_assignee": {"provider_id": "someone-else"}},
        ]
        for delta in variants:
            request = self._request(
                qa_surface_present=False, ball_holder="motykadaw"
            )
            tracker = dict(request["issue_tracker"])
            tracker.update(delta)
            request["issue_tracker"] = tracker
            with self.subTest(delta=sorted(delta)):
                self.assertEqual(reference, qa_generation(request))

    def test_flag_inert_on_unmapped_digests(self) -> None:
        base = self._request(ball_holder="motykadaw")
        base["repository"] = {"nameWithOwner": "Keeper-Dating/algo"}
        absent = qa_generation(base)
        self.assertEqual(
            absent, qa_generation({**base, "qa_surface_present": True})
        )
        self.assertEqual(
            absent, qa_generation({**base, "qa_surface_present": False})
        )

    def test_identical_operation_plans_share_digest_nonlinear(self) -> None:
        # Round-1 F2: mapped repository, non-linear tracker, holder ==
        # mapped owner — both flag values mint the identical GitHub pair,
        # so the digest must not move and a completed ledger is reused.
        base = self._request(ball_holder="tjkeeper", code_reviewers=[])
        base["issue_tracker"] = {"type": "none"}
        self.assertEqual(
            qa_generation(base),
            qa_generation({**base, "qa_surface_present": False}),
        )
        first = plan_handoff(base)
        self.assertEqual(first["state"], "pending", first.get("errors"))
        ledger = {
            op["id"]: operation_result("complete")
            for op in first["operations"]
        }
        replan = plan_handoff(
            {**base, "qa_surface_present": False, "operation_results": ledger}
        )
        self.assertEqual(replan["state"], "complete", replan)
        self.assertEqual(replan["call_plan"], [])

    def test_exempt_unvalidated_flip_reuses_completed_ledger(self) -> None:
        # Round-2 residual: the exempt-unvalidated builder returns the
        # GitHub-only plan before reading write_path, so the flag flip
        # must keep the digest and never requeue the completed pair.
        base = self._request(ball_holder="tjkeeper", code_reviewers=[])
        base["issue_tracker"] = {
            "type": "linear",
            "ticket_required": False,
            "ticket_validated": False,
            "ticket_exemption_reason": "branch matches chore/*",
            "write_path": "environment_tool",
        }
        self.assertEqual(
            qa_generation(base),
            qa_generation({**base, "qa_surface_present": False}),
        )
        first = plan_handoff(base)
        self.assertEqual(first["state"], "pending", first.get("errors"))
        self.assertEqual(
            [
                op["id"]
                for op in first["operations"]
                if not op["id"].startswith("qa.github.")
            ],
            [],
        )
        ledger = {
            op["id"]: operation_result("complete")
            for op in first["operations"]
        }
        replan = plan_handoff(
            {**base, "qa_surface_present": False, "operation_results": ledger}
        )
        self.assertEqual(replan["state"], "complete", replan)
        self.assertEqual(replan["call_plan"], [])

    def test_suppressed_pending_record_resumes_verify_before_retry(
        self,
    ) -> None:
        # Round-3 P2: tracker changes under unchanged suppression (a
        # validation flip AND a ticket re-key here) keep the generation,
        # so a pending record resumes postcondition-first instead of
        # tripping the in-flight prior-target guard.
        base = self._request(
            qa_surface_present=False,
            ball_holder="motykadaw",
            code_reviewers=[],
        )
        first = plan_handoff(base)
        self.assertEqual(first["state"], "pending", first.get("errors"))
        first_op = first["operations"][0]
        pending_record: dict[str, object] = {
            "status": "pending",
            "attempts": 1,
            "started_at": TIMESTAMP,
        }
        # r13 F4: an assignee-replacement write-ahead record must carry
        # the observed precondition; mirror the planner's own payload.
        if "precondition" in first_op["payload"]:
            pending_record["precondition"] = first_op["payload"]["precondition"]
        resumed = self._request(
            qa_surface_present=False,
            ball_holder="motykadaw",
            code_reviewers=[],
            operation_results={first_op["id"]: pending_record},
        )
        tracker = dict(resumed["issue_tracker"])
        tracker["ticket_validated"] = False
        tracker["ticket_identifier"] = "WEB-9999"
        resumed["issue_tracker"] = tracker
        plan = plan_handoff(resumed)
        self.assertEqual(
            plan["state"], "resume_verification_required", plan.get("errors")
        )
        self.assertEqual(plan["call_plan"][0]["action"], "verify_before_retry")

    def test_validated_flip_moves_digest_envtool_and_nonepath(self) -> None:
        # A validated tracker's legs vanish under suppression, so the
        # digest must move — through the qa-user slot on the
        # environment_tool path and the write_path fold on the "none"
        # path (the raw string "none" folds to null).
        env = self._request(code_reviewers=[])
        self.assertNotEqual(
            qa_generation(env),
            qa_generation(
                {**env, "qa_surface_present": False, "ball_holder": "tjkeeper"}
            ),
        )
        nonepath = self._request(code_reviewers=[])
        nonepath["issue_tracker"] = {
            "type": "linear",
            "ticket_validated": True,
            "ticket_identifier": "WEB-8877",
            "ticket_provider_id": "linear-ticket-web-8877",
            "write_path": "none",
        }
        self.assertNotEqual(
            qa_generation(nonepath),
            qa_generation(
                {
                    **nonepath,
                    "qa_surface_present": False,
                    "ball_holder": "tjkeeper",
                }
            ),
        )

    def test_targetless_suppression_prunes_terminal_history_to_idle(
        self,
    ) -> None:
        # Round-1 F3: a suppressed, targetless replan over an
        # all-terminal qa ledger prunes the prior-generation history and
        # stays idle instead of mis-blocking with the unmapped message.
        first = plan_handoff(self._request(code_reviewers=[]))
        self.assertEqual(first["state"], "pending", first.get("errors"))
        ledger = {
            op["id"]: operation_result("complete")
            for op in first["operations"]
        }
        replan = plan_handoff(
            self._request(
                qa_surface_present=False,
                code_reviewers=[],
                operation_results=ledger,
            )
        )
        self.assertEqual(replan["state"], "idle", replan)
        self.assertTrue(
            any(
                "prior-target terminal QA record" in warning
                for warning in replan["warnings"]
            )
        )

    def test_targetless_suppression_foreign_record_fails_closed(
        self,
    ) -> None:
        # Phase-4 review F6 (2026-09 re-land): a fabricated non-family ID
        # in the ledger must keep the fail-closed block — pruning it as
        # history would launder exactly the record class the family
        # grammar exists to reject.
        replan = plan_handoff(
            self._request(
                qa_surface_present=False,
                code_reviewers=[],
                operation_results={
                    "bogus.qa.done": operation_result("complete")
                },
            )
        )
        self.assertEqual(replan["state"], "blocked", replan)
        self.assertTrue(
            any("outside the qa families" in e for e in replan["errors"])
        )

    def test_suppressed_mapped_missing_holder_warns_about_holder(
        self,
    ) -> None:
        # Phase-4 review F7 (2026-09 re-land): a suppressed MAPPED plan
        # with reviewers but no ball_holder must not claim the repository
        # is unmapped — the actionable omission is the ball_holder input.
        plan = plan_handoff(self._request(qa_surface_present=False))
        self.assertEqual(plan["state"], "pending", plan.get("errors"))
        self.assertEqual(plan["targets"]["assignees"], [])
        self.assertTrue(
            any(
                "suppressed path routes ownership" in w
                for w in plan["warnings"]
            )
        )
        self.assertFalse(
            any("unmapped repository" in w for w in plan["warnings"])
        )

    def test_targetless_suppression_in_flight_record_fails_closed(
        self,
    ) -> None:
        first = plan_handoff(self._request(code_reviewers=[]))
        pending_id = first["operations"][0]["id"]
        replan = plan_handoff(
            self._request(
                qa_surface_present=False,
                code_reviewers=[],
                operation_results={
                    pending_id: {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    }
                },
            )
        )
        self.assertEqual(replan["state"], "blocked", replan)
        self.assertTrue(
            any("still in flight" in error for error in replan["errors"])
        )


class QaPlanVersionRolloutTests(unittest.TestCase):
    """Post-fix review F1: inserting the binding op changed the plan SHAPE
    without changing the targets, so a pre-upgrade ledger kept its digest
    and died on the opaque prefix rule. ``plan_version`` in the digest
    re-mints the IDs, routing pre-upgrade ledgers through the documented
    prior-generation path instead.

    Pass-2 convergent finding (codex P2 / opus MEDIUM): a synthetic
    ``"0" * 12`` digest tested arbitrary generation turnover, not the
    version bump - both tests passed with ``plan_version`` ablated. The
    stale ledger is therefore keyed on the REAL pre-v2 digest, computed
    from the version-less payload, so removing the version component
    collapses the two digests and both tests fail at the prefix rule."""

    def _request(self, operation_results: dict[str, object]) -> dict[str, object]:
        return {
            "scenario": "approved_qa",
            "existing_assignees": ["jakozloski"],
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

    @property
    def OLD_G(self) -> str:
        # The pre-upgrade digest: byte-identical canonicalization over the
        # CURRENT payload minus ONLY the plan_version component - that
        # exact mirror is what keeps the premise assert sensitive to a
        # plan_version ablation and nothing else (pass-3 opus F8: when a
        # field is added to qa_generation, add it here too, else the
        # digests differ for two reasons and the ablation goes blind).
        payload = {
            "nameWithOwner": "Keeper-Dating/matchmaking",
            "pull_request_number": PR_NUMBER,
            "github_login": "tjkeeper",
            "ticket_identifier": "WEB-8877",
            "ticket_provider_id": "linear-ticket-web-8877",
            "write_path": "environment_tool",
            "qa_assignee_provider_id": LINEAR_QA_ASSIGNEE["provider_id"],
            "qa_state_provider_id": LINEAR_QA_STATE_WEB["provider_id"],
            # mm#3551 dawid-r8 follow-through (worker-flagged): the r19 F9
            # field was missing here, so the premise assert held for TWO
            # reasons and the plan_version ablation this mirror exists to
            # catch was blind - exactly the drift the comment above warns
            # about. This fixture's state carries a provider id, so the
            # current fold hashes the name slot as None.
            "qa_state_name": None,
            "code_reviewers": [],
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        old = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        # The rollout premise itself: the version component must move the
        # digest for UNCHANGED targets, else pre-upgrade ledgers stay
        # same-generation and die on the opaque prefix rule.
        assert old != QA_G, "plan_version no longer changes the digest"
        return old

    @property
    def REAL_V2_G(self) -> str:
        # The REAL pre-upgrade v2 digest for this validated fixture —
        # OLD_G above deliberately omits the version key (the ablation
        # mirror), so the migration tests key their stale ledgers HERE
        # to exercise the promised v2 -> v3 turnover (2026-09 re-land
        # plan-review round-5 detail 2).
        payload = {
            "plan_version": 2,
            "nameWithOwner": "Keeper-Dating/matchmaking",
            "pull_request_number": PR_NUMBER,
            "github_login": "tjkeeper",
            "ticket_identifier": "WEB-8877",
            "ticket_provider_id": "linear-ticket-web-8877",
            "write_path": "environment_tool",
            "qa_assignee_provider_id": LINEAR_QA_ASSIGNEE["provider_id"],
            "qa_state_provider_id": LINEAR_QA_STATE_WEB["provider_id"],
            "qa_state_name": None,
            "code_reviewers": [],
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        old = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        assert old != QA_G, "the v2 cohort no longer re-mints under v3"
        return old

    def test_version_component_alone_moves_the_digest(self) -> None:
        # OLD_G's premise assert IS the ablation pin — touching the
        # property runs it against the current payload-minus-version.
        self.assertNotEqual(self.OLD_G, QA_G)

    def test_pre_upgrade_terminal_ledger_prunes_with_warning(self) -> None:
        plan = plan_handoff(
            self._request(
                {
                    f"qa.github.replace_assignees:g{self.REAL_V2_G}": operation_result(
                        "complete"
                    ),
                    f"qa.github.verify_assignees:g{self.REAL_V2_G}": operation_result(
                        "complete"
                    ),
                }
            )
        )
        self.assertEqual(plan["state"], "pending", plan)
        self.assertTrue(
            any("prior-target terminal QA record" in w for w in plan["warnings"]),
            plan["warnings"],
        )
        # The fresh plan re-mints under the current digest and starts over.
        self.assertEqual(
            plan["call_plan"][0]["id"],
            f"qa.github.replace_assignees:g{QA_G}",
        )

    def test_pre_upgrade_in_flight_ledger_fails_closed(self) -> None:
        plan = plan_handoff(
            self._request(
                {
                    f"qa.github.replace_assignees:g{self.REAL_V2_G}": operation_result(
                        "complete"
                    ),
                    f"qa.linear.assign_ticket:g{self.REAL_V2_G}": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    },
                }
            )
        )
        self.assertEqual(plan["state"], "blocked", plan)
        self.assertTrue(
            any("still in flight" in e for e in plan["errors"]),
            plan["errors"],
        )


if __name__ == "__main__":
    unittest.main()


class QaStateNameGenerationTests(unittest.TestCase):
    """algo#1216 r19 F9: a name-only workflow state is mutated by NAME, so
    renaming it must re-mint the generation; provider-id generations stay
    byte-identical to pre-fix."""

    def _request(self, qa_state):
        return {
            "repository": {"nameWithOwner": "Keeper-Dating/matchmaking"},
            "pull_request_number": 7,
            "authenticated_actor": "jakozloski",
            "code_reviewers": [],
            "issue_tracker": {
                # dawid-r9 F3: name-only state renames are plan targets
                # only on the linear branch — the digest folds untyped
                # trackers, so the fixture carries the consumer's type.
                "type": "linear",
                "ticket_validated": True,
                "ticket_identifier": "WEB-1234",
                "ticket_provider_id": "abc",
                "write_path": "local_api",
                "qa_assignee": {"provider_id": "qa-1"},
                "qa_state": qa_state,
            },
        }

    def test_name_only_rename_re_mints_the_generation(self) -> None:
        old = qa_generation(self._request({"name": "Vercel Preview QA"}))
        new = qa_generation(self._request({"name": "Preview QA v2"}))
        self.assertNotEqual(old, new)

    def test_provider_id_states_ignore_the_name(self) -> None:
        with_name = qa_generation(
            self._request({"provider_id": "st-1", "name": "Vercel Preview QA"})
        )
        renamed = qa_generation(
            self._request({"provider_id": "st-1", "name": "Preview QA v2"})
        )
        self.assertEqual(with_name, renamed)


class UnmappedGenerationTrackerScopeTests(unittest.TestCase):
    """mm#3551 dawid-r8 F7: the unmapped QA path never reads
    ``issue_tracker`` (``_approved_qa_operations`` returns before its
    tracker leg), yet the digest folded tracker fields unconditionally -
    an unrelated Linear change rolled the generation, re-minted the
    qa.github.* IDs, pruned the prior target's terminal records as
    history, and re-executed unchanged GitHub mutations. dawid-r9 F3:
    the same defect recurred one branch deeper — every tracker sub-field
    read in ``_approved_qa_operations`` sits inside its
    ``tracker_type == "linear"`` branch, so a MAPPED plan with a
    non-linear or absent type consumes none of them either. Tracker
    slots now fold as None unless the repository is mapped AND the
    tracker type is linear — the exact condition under which the
    consumer reads them."""

    UNMAPPED = {"nameWithOwner": "Keeper-Dating/algo"}
    TRACKER_A = {
        "ticket_validated": True,
        "ticket_identifier": "AI-1111",
        "ticket_provider_id": "linear-ticket-ai-1111",
        "write_path": "environment_tool",
        "qa_assignee": {"provider_id": "qa-user-1"},
        "qa_state": {"name": "Vercel Preview QA"},
    }
    TRACKER_B = {
        "ticket_validated": True,
        "ticket_identifier": "AI-2222",
        "ticket_provider_id": "linear-ticket-ai-2222",
        "write_path": "local_api",
        "qa_assignee": {"provider_id": "qa-user-2"},
        "qa_state": {"name": "Renamed QA Lane"},
    }

    def _request(
        self,
        repository: dict[str, object],
        issue_tracker: dict[str, object] | None,
        operation_results: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "scenario": "approved_qa",
            "repository": repository,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "existing_assignees": ["jakozloski"],
            "code_reviewers": ["michal-janicki"],
            "ball_holder": "michal-janicki",
        }
        if issue_tracker is not None:
            request["issue_tracker"] = dict(issue_tracker)
        if operation_results is not None:
            request["operation_results"] = operation_results
        return request

    def test_unmapped_generation_ignores_tracker_fields(self) -> None:
        # The finding's exact scenario: an unrelated Linear change (every
        # tracker slot re-keyed or renamed at once) on an unmapped
        # repository leaves the generation untouched.
        no_tracker = qa_generation(self._request(self.UNMAPPED, None))
        tracker_a = qa_generation(self._request(self.UNMAPPED, self.TRACKER_A))
        tracker_b = qa_generation(self._request(self.UNMAPPED, self.TRACKER_B))
        self.assertEqual(no_tracker, tracker_a)
        self.assertEqual(tracker_a, tracker_b)

    def test_unmapped_digest_folds_tracker_slots_as_none(self) -> None:
        # Migration pin, mirroring QaPlanVersionRolloutTests.OLD_G: the
        # tracker slots stay IN the unmapped payload folded as None (they
        # do not vanish), so an unmapped request that never carried
        # issue_tracker keeps its pre-F7 digest and never re-mints on
        # upgrade. When a field is added to qa_generation, add it here
        # too.
        payload = {
            "plan_version": 3,
            "nameWithOwner": "Keeper-Dating/algo",
            "pull_request_number": PR_NUMBER,
            "github_login": "michal-janicki",
            "ticket_identifier": None,
            "ticket_provider_id": None,
            "write_path": None,
            "qa_assignee_provider_id": None,
            "qa_state_provider_id": None,
            "qa_state_name": None,
            "code_reviewers": ["michal-janicki"],
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(
            qa_generation(self._request(self.UNMAPPED, self.TRACKER_A)),
            expected,
        )

    def test_mapped_linear_generation_rolls_on_each_tracker_field(self) -> None:
        # The consumer reads tracker sub-fields only inside its
        # tracker_type == "linear" branch, so each is a plan target —
        # moving the digest on its own — exactly when the fixture carries
        # that type. The "type" variant pins the flip itself: leaving the
        # linear branch changes the minted operation set, so it must roll
        # even though every sub-field stays byte-identical.
        linear_a = {"type": "linear", **self.TRACKER_A}
        reference = qa_generation(self._request(REPOSITORY, linear_a))
        variants: dict[str, dict[str, object]] = {
            "ticket_identifier": {
                **linear_a,
                "ticket_identifier": "AI-2222",
            },
            "ticket_provider_id": {
                **linear_a,
                "ticket_provider_id": "linear-ticket-ai-2222",
            },
            "write_path": {**linear_a, "write_path": "local_api"},
            "qa_assignee_provider_id": {
                **linear_a,
                "qa_assignee": {"provider_id": "qa-user-2"},
            },
            "qa_state_name": {
                **linear_a,
                "qa_state": {"name": "Renamed QA Lane"},
            },
            "qa_state_provider_id": {
                **linear_a,
                "qa_state": {
                    "provider_id": "st-1",
                    "name": "Vercel Preview QA",
                },
            },
            "type": {**linear_a, "type": "none"},
        }
        for slot, tracker in variants.items():
            with self.subTest(slot=slot):
                self.assertNotEqual(
                    reference,
                    qa_generation(self._request(REPOSITORY, tracker)),
                    "a mapped linear tracker change must roll the generation",
                )

    def test_mapped_nonlinear_generation_ignores_tracker_subfields(
        self,
    ) -> None:
        # dawid-r9 F3's exact scenario: a mapped repository whose tracker
        # is non-linear (or untyped — the consumer defaults absent to
        # "none") consumes no tracker sub-field, so re-keying every slot
        # at once must leave the generation untouched, and each such
        # digest must equal the tracker-less one (the slots fold as None,
        # they do not vanish).
        no_tracker = qa_generation(self._request(REPOSITORY, None))
        for tracker_type in (None, "none", "github", "jira"):
            with self.subTest(tracker_type=tracker_type):
                tag = (
                    {} if tracker_type is None else {"type": tracker_type}
                )
                tracker_a = qa_generation(
                    self._request(REPOSITORY, {**tag, **self.TRACKER_A})
                )
                tracker_b = qa_generation(
                    self._request(REPOSITORY, {**tag, **self.TRACKER_B})
                )
                self.assertEqual(no_tracker, tracker_a)
                self.assertEqual(tracker_a, tracker_b)

    def test_mapped_completed_ledger_survives_nonlinear_tracker_change(
        self,
    ) -> None:
        # End to end on the mapped path (consumer parity, not just digest
        # parity): with a "none"-typed tracker the plan mints GitHub
        # operations only, and a completed ledger stays satisfied across
        # a full tracker re-key — no re-mint, no prune, zero calls —
        # where the mapped-only fold re-executed every mutation.
        first = plan_handoff(
            self._request(REPOSITORY, {"type": "none", **self.TRACKER_A})
        )
        self.assertEqual(first["state"], "pending", first.get("errors"))
        self.assertEqual(
            [
                operation["id"]
                for operation in first["operations"]
                if not operation["id"].startswith("qa.github.")
            ],
            [],
            "a non-linear tracker must plan no Linear leg",
        )
        ledger = {
            operation["id"]: operation_result("complete")
            for operation in first["operations"]
        }
        replan = plan_handoff(
            self._request(
                REPOSITORY, {"type": "none", **self.TRACKER_B}, ledger
            )
        )
        self.assertEqual(replan["state"], "complete", replan)
        self.assertEqual(replan["call_plan"], [])
        self.assertEqual(replan["warnings"], [])

    def test_unmapped_completed_ledger_survives_unrelated_tracker_change(
        self,
    ) -> None:
        # End to end: the completed GitHub ledger stays satisfied across
        # the Linear change - no re-mint, no prune, zero calls - where
        # the unconditional fold re-executed every mutation.
        first = plan_handoff(self._request(self.UNMAPPED, self.TRACKER_A))
        self.assertEqual(first["state"], "pending", first.get("errors"))
        ledger = {
            operation["id"]: operation_result("complete")
            for operation in first["operations"]
        }
        replan = plan_handoff(
            self._request(self.UNMAPPED, self.TRACKER_B, ledger)
        )
        self.assertEqual(replan["state"], "complete", replan)
        self.assertEqual(replan["call_plan"], [])
        self.assertEqual(replan["warnings"], [])

    @property
    def PRE_F7_G(self) -> str:
        # The pre-upgrade digest an unmapped plan minted while tracker
        # fields still leaked in: byte-identical canonicalization over
        # the current payload with TRACKER_A folded (the OLD_G
        # convention). The premise assert keeps this mirror honest: if
        # the slots ever leak back in, the two digests collapse and the
        # migration tests fail here, not vacuously downstream.
        payload = {
            "plan_version": 3,
            "nameWithOwner": "Keeper-Dating/algo",
            "pull_request_number": PR_NUMBER,
            "github_login": "michal-janicki",
            "ticket_identifier": "AI-1111",
            "ticket_provider_id": "linear-ticket-ai-1111",
            "write_path": "environment_tool",
            "qa_assignee_provider_id": "qa-user-1",
            "qa_state_provider_id": None,
            "qa_state_name": "name:Vercel Preview QA",
            "code_reviewers": ["michal-janicki"],
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        old = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        assert old != qa_generation(
            self._request(self.UNMAPPED, self.TRACKER_A)
        ), "tracker fields have leaked back into the unmapped digest"
        return old

    def test_pre_upgrade_unmapped_terminal_ledger_prunes_as_history(
        self,
    ) -> None:
        # One-time upgrade turnover for the cohort that DID carry tracker
        # fields: the old generation's terminal records are prior-target
        # history (pruned with a warning), never unknown-ID errors.
        old_g = self.PRE_F7_G
        plan = plan_handoff(
            self._request(
                self.UNMAPPED,
                self.TRACKER_A,
                {
                    f"qa.github.request_review:michal-janicki:g{old_g}": (
                        operation_result("complete")
                    ),
                    f"qa.github.replace_assignees:g{old_g}": (
                        operation_result("complete")
                    ),
                },
            )
        )
        self.assertEqual(plan["state"], "pending", plan)
        self.assertTrue(
            any(
                "prior-target terminal QA record" in warning
                for warning in plan["warnings"]
            ),
            plan["warnings"],
        )

    def test_pre_upgrade_unmapped_in_flight_ledger_fails_closed(self) -> None:
        # An in-flight pre-upgrade record marks a mutation that may
        # already have fired remotely: blocked with the recovery named,
        # exactly like every other generation turnover.
        plan = plan_handoff(
            self._request(
                self.UNMAPPED,
                self.TRACKER_A,
                {
                    f"qa.github.replace_assignees:g{self.PRE_F7_G}": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    }
                },
            )
        )
        self.assertEqual(plan["state"], "blocked", plan)
        self.assertTrue(
            any("still in flight" in error for error in plan["errors"]),
            plan["errors"],
        )

    NONLINEAR_A = {"type": "github", **TRACKER_A}

    @property
    def PRE_F3_G(self) -> str:
        # dawid-r9 F3's upgrade cohort: the digest a MAPPED plan minted
        # for a non-linear tracker while its sub-fields still leaked in
        # (pre-F3 the fold was gated on mapped alone). Byte-identical
        # canonicalization with NONLINEAR_A's fields hashed raw, the
        # PRE_F7_G convention. The premise assert keeps the mirror
        # honest: if mapped non-linear sub-fields ever leak back into
        # the digest, the two collapse and the transition tests fail
        # here, not vacuously downstream.
        payload = {
            "plan_version": 3,
            "nameWithOwner": REPOSITORY["nameWithOwner"],
            "pull_request_number": PR_NUMBER,
            "github_login": "tjkeeper",
            "ticket_identifier": "AI-1111",
            "ticket_provider_id": "linear-ticket-ai-1111",
            "write_path": "environment_tool",
            "qa_assignee_provider_id": "qa-user-1",
            "qa_state_provider_id": None,
            "qa_state_name": "name:Vercel Preview QA",
            "code_reviewers": ["michal-janicki"],
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        old = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        assert old != qa_generation(
            self._request(REPOSITORY, self.NONLINEAR_A)
        ), "tracker sub-fields have leaked back into the mapped non-linear digest"
        return old

    def test_pre_upgrade_mapped_nonlinear_terminal_ledger_prunes_as_history(
        self,
    ) -> None:
        # One-time upgrade turnover for the mapped cohort whose
        # non-linear tracker DID carry sub-fields: old-generation
        # terminal records are prior-target history (pruned with a
        # warning), never unknown-ID errors — same path the unmapped
        # cohort pins above.
        old_g = self.PRE_F3_G
        plan = plan_handoff(
            self._request(
                REPOSITORY,
                self.NONLINEAR_A,
                {
                    f"qa.github.request_review:michal-janicki:g{old_g}": (
                        operation_result("complete")
                    ),
                    f"qa.github.replace_assignees:g{old_g}": (
                        operation_result("complete")
                    ),
                },
            )
        )
        self.assertEqual(plan["state"], "pending", plan)
        self.assertTrue(
            any(
                "prior-target terminal QA record" in warning
                for warning in plan["warnings"]
            ),
            plan["warnings"],
        )

    def test_pre_upgrade_mapped_nonlinear_in_flight_ledger_fails_closed(
        self,
    ) -> None:
        # An in-flight pre-upgrade record on the mapped non-linear
        # cohort fails closed with the recovery named, exactly like the
        # unmapped turnover.
        plan = plan_handoff(
            self._request(
                REPOSITORY,
                self.NONLINEAR_A,
                {
                    f"qa.github.replace_assignees:g{self.PRE_F3_G}": {
                        "status": "pending",
                        "attempts": 1,
                        "started_at": TIMESTAMP,
                    }
                },
            )
        )
        self.assertEqual(plan["state"], "blocked", plan)
        self.assertTrue(
            any("still in flight" in error for error in plan["errors"]),
            plan["errors"],
        )


class BallHolderResolutionParityTests(unittest.TestCase):
    """mm#3551 dawid-r8 F22: the digest's handback slot and
    ``_approved_qa_operations``' github_login resolve identically on
    every plan-minting request; the supplied-but-malformed holder is the
    one documented divergence, unreachable in a minted plan because the
    builder error-returns before the digest runs."""

    _OMIT: object = object()

    def _request(
        self, repository: dict[str, object], ball_holder: object
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "scenario": "approved_qa",
            "repository": repository,
            "pull_request_number": PR_NUMBER,
            "authenticated_actor": "jakozloski",
            "existing_assignees": ["jakozloski"],
            "code_reviewers": ["michal-janicki"],
        }
        if ball_holder is not self._OMIT:
            request["ball_holder"] = ball_holder
        return request

    def test_malformed_holder_folds_as_none_in_the_digest(self) -> None:
        # The divergence's digest side: a malformed holder folds exactly
        # like an absent one (the malformed-segments rule), while the
        # builder side rejects it before any plan mints - pinned in
        # test_unmapped_invalid_ball_holder_blocks.
        unmapped = {"nameWithOwner": "Keeper-Dating/algo"}
        self.assertEqual(
            qa_generation(self._request(unmapped, "not a login!")),
            qa_generation(self._request(unmapped, self._OMIT)),
        )

    def test_digest_tracks_the_builder_resolution_on_minting_requests(
        self,
    ) -> None:
        # Owner wins in both resolutions: a mapped ball_holder is inert
        # in the digest AND absent from the planned assignee.
        mapped_with_holder = self._request(REPOSITORY, "michal-janicki")
        self.assertEqual(
            qa_generation(mapped_with_holder),
            qa_generation(self._request(REPOSITORY, self._OMIT)),
        )
        plan = plan_handoff(mapped_with_holder)
        self.assertEqual(plan["state"], "pending", plan.get("errors"))
        replace = next(
            operation
            for operation in plan["operations"]
            if operation["action"] == "replace_pull_request_assignees"
        )
        self.assertEqual(replace["payload"]["assignees"], ["tjkeeper"])
        # An unmapped plan keys on its validated holder in both places.
        unmapped = {"nameWithOwner": "Keeper-Dating/algo"}
        self.assertNotEqual(
            qa_generation(self._request(unmapped, "alice")),
            qa_generation(self._request(unmapped, "bob")),
        )
