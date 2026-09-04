from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from typing import Any

from model_policy import (
    BASE_EFFORT,
    BASE_MODEL,
    BASE_MODEL_ALIAS,
    CLASSIFY_EXIT_AUTH_ERROR,
    CLASSIFY_EXIT_TIMEOUT,
    CLASSIFY_EXIT_CLEAN,
    CLASSIFY_EXIT_INTERNAL_FAILURE,
    CODEX_EFFORT,
    CODEX_MODEL,
    LIVENESS_BACKOFF_LADDER_SECONDS,
    REVIEWER_EFFORT,
    REVIEWER_MODEL,
    REVIEWER_MODEL_ALIAS,
    apply_auth_recovery,
    auth_signature_offset,
    bounded_excerpt,
    build_descriptor,
    classify_stream_event,
    MAX_EXCERPT_BYTES,
    MAX_RAW_RECORD_BYTES,
    SOURCE_STDERR,
    SOURCE_STDOUT_JSON,
    evaluate_model_policy,
    main,
    monitor_orchestrator_binding,
    routing_fingerprint,
    strip_url_secrets,
    supervise_stream,
    validate_descriptor,
    verify_frozen_selection,
    waiver_gate_resolution,
    monitor_child_arguments,
    monitor_child_prompt,
    MONITOR_SLICE_BUDGET_SECONDS,
    MONITOR_SLICE_CLEANUP_MARGIN_SECONDS,
    MONITOR_CHILD_MIN_VIABLE_SECONDS,
    MONITOR_CHILD_IDLE_TIMEOUT_SECONDS,
    PER_ATTEMPT_CEILING_SECONDS,
)


def live_catalog(*, include_astra: bool = True, include_required_effort: bool = True) -> dict:
    models = [
        {
            "slug": "gpt-5.5-codex",
            "supported_reasoning_levels": [{"effort": "xhigh"}],
        }
    ]
    if include_astra:
        # high/xhigh alone never satisfy the gate: eligibility requires BOTH
        # max (the focused tier) and ultra (the breadth tier).
        levels = [{"effort": "high"}, {"effort": "xhigh"}]
        if include_required_effort:
            levels.append({"effort": CODEX_EFFORT})
            levels.append({"effort": "ultra"})
        models.append(
            {
                "slug": CODEX_MODEL,
                "supported_reasoning_levels": levels,
            }
        )
    return {"models": models}


def valid_codex(
    *,
    status: str = "success",
    attempts: int | None = None,
    quota_reset_at: object | None = None,
) -> dict:
    if attempts is None:
        attempts = 0 if status == "not_run" else 1
    invocation: dict = {"status": status, "attempts": attempts}
    if quota_reset_at is not None:
        invocation["quota_reset_at"] = quota_reset_at
    return {
        "installed": True,
        "version": "codex-cli 0.144.0",
        "live_catalog": live_catalog(),
        "first_real_invocation": invocation,
    }


def valid_base(**overrides: object) -> dict:
    config = {
        "installed": True,
        "version": "2.1.170 (Claude Code)",
        "fable_access": "available",
        "zero_data_retention": "compatible",
        "environment": {
            "CLAUDE_CODE_SUBAGENT_MODEL": None,
            "CLAUDE_CODE_EFFORT_LEVEL": None,
        },
        "host_capabilities": {
            "agent_model_selection": True,
            "agent_effort_selection": True,
            "agent_read_only_enforced": True,
        },
        "observed_models": ["claude-fable-5", "claude-mythos-5"],
        "explicit_waiver": False,
    }
    config.update(overrides)
    return config


def valid_reviewer(**overrides: object) -> dict:
    config = {
        "installed": True,
        "version": "2.1.170 (Claude Code)",
        "fable_access": "available",
        "zero_data_retention": "compatible",
        "environment": {
            "CLAUDE_CODE_SUBAGENT_MODEL": None,
            "CLAUDE_CODE_EFFORT_LEVEL": None,
        },
        "host_capabilities": {
            "agent_model_selection": True,
            "agent_effort_selection": True,
            "agent_read_only_enforced": True,
        },
        "observed_models": ["claude-fable-5-1", "claude-fable-5"],
        "explicit_waiver": False,
    }
    config.update(overrides)
    return config


def request(
    *,
    codex: dict | None = None,
    base: dict | None = None,
    reviewer: dict | None = None,
) -> dict:
    return {
        "codex": codex if codex is not None else valid_codex(),
        "claude": base if base is not None else valid_base(),
        "claude_reviewer": reviewer if reviewer is not None else valid_reviewer(),
    }


LEGS = (
    ("claude", "claude", valid_base, "fable_access"),
    ("claude_reviewer", "claude_reviewer", valid_reviewer, "fable_access"),
)


def leg_request(key: str, config: dict) -> dict:
    payload = request()
    payload[key] = config
    return payload


class ModelPolicyTest(unittest.TestCase):
    def test_ready_policy_pins_sol_max_fable_base_and_fable_reviewer(self) -> None:
        result = evaluate_model_policy(request())

        self.assertEqual(result["state"], "ready")
        self.assertEqual(
            (result["codex"]["model"], result["codex"]["effort"]),
            (CODEX_MODEL, CODEX_EFFORT),
        )
        self.assertFalse(result["codex"]["downgrade_allowed"])
        self.assertIsNone(result["codex"]["fallback_model"])

        base = result["claude"]
        self.assertEqual((base["model"], base["effort"]), (BASE_MODEL, BASE_EFFORT))
        self.assertEqual(base["execution_path"], "agent_tool")
        self.assertEqual(base["role"], "base")
        self.assertTrue(base["blocking"])

        reviewer = result["claude_reviewer"]
        self.assertEqual(
            (reviewer["model"], reviewer["effort"]),
            (REVIEWER_MODEL, REVIEWER_EFFORT),
        )
        self.assertEqual(reviewer["execution_path"], "agent_tool")
        self.assertEqual(reviewer["role"], "reviewer")
        self.assertTrue(reviewer["blocking"])

    def test_base_and_reviewer_are_different_models(self) -> None:
        """The reviewers judge work the base wrote; they must not be the base."""

        result = evaluate_model_policy(request())

        self.assertNotEqual(
            result["claude"]["model"], result["claude_reviewer"]["model"]
        )
        self.assertEqual(BASE_MODEL_ALIAS, "fable")
        self.assertEqual(REVIEWER_MODEL_ALIAS, "fable")

    def test_codex_missing_cli_blocks_with_install_action(self) -> None:
        result = evaluate_model_policy(request(codex={"installed": False}))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "cli_missing")
        self.assertEqual(result["next_action"], "install_codex_cli")

    def test_codex_old_cli_blocks_with_upgrade_action(self) -> None:
        codex = valid_codex()
        codex["version"] = "0.143.9"

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "cli_too_old")
        self.assertEqual(result["next_action"], "upgrade_codex_cli")

    def test_minimum_version_prerelease_is_not_accepted(self) -> None:
        codex = valid_codex()
        codex["version"] = "0.144.0-rc.1"
        base = valid_base(version="2.1.170-beta.1")
        reviewer = valid_reviewer(version="2.1.170-beta.1")

        result = evaluate_model_policy(
            request(codex=codex, base=base, reviewer=reviewer)
        )

        self.assertEqual(result["codex"]["reason_code"], "cli_too_old")
        self.assertEqual(result["claude"]["reason_code"], "cli_too_old")
        self.assertEqual(result["claude_reviewer"]["reason_code"], "cli_too_old")

    def test_codex_live_catalog_missing_astra_blocks(self) -> None:
        codex = valid_codex()
        codex["live_catalog"] = live_catalog(include_astra=False)

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "live_catalog_missing_capability")

    def test_codex_live_catalog_missing_required_effort_blocks(self) -> None:
        codex = valid_codex()
        codex["live_catalog"] = live_catalog(include_required_effort=False)

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "live_catalog_missing_capability")

    def test_codex_first_real_invocation_is_required(self) -> None:
        result = evaluate_model_policy(request(codex=valid_codex(status="not_run")))[
            "codex"
        ]

        self.assertEqual(result["state"], "probe_required")
        self.assertEqual(result["reason_code"], "first_real_invocation_required")
        self.assertEqual(result["next_action"], "run_first_real_invocation")

    def test_codex_rejects_unhashable_invocation_status(self) -> None:
        codex = valid_codex()
        codex["first_real_invocation"] = {
            "status": {"bad": "shape"},
            "attempts": 1,
        }

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "invalid_invocation_status")

    def test_codex_success_after_backoff_rounds_is_ready(self) -> None:
        """Attempts are unbounded under wait-and-retry: a success on the Nth
        try (after backoff waits) is a normal ready verdict, not a cap
        violation."""
        result = evaluate_model_policy(
            request(codex=valid_codex(status="success", attempts=7))
        )["codex"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["reason_code"], "authoritative_invocation_succeeded")

    def test_codex_entitlement_denial_blocks_without_retry(self) -> None:
        result = evaluate_model_policy(
            request(codex=valid_codex(status="entitlement_denied"))
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "entitlement_denied")
        self.assertEqual(result["retry"]["remaining"], 0)

    def test_blocking_reasons_name_the_selected_model_not_the_floor(self) -> None:
        """Auto-forward selection must reach the failure diagnostics: a newer
        eligible model's runtime failure must not be reported as the floor's."""
        codex = valid_codex(status="entitlement_denied")
        codex["live_catalog"]["models"].append(
            {
                "slug": "gpt-9.9-sol",
                "supported_reasoning_levels": [
                    {"effort": "max"},
                    {"effort": "ultra"},
                ],
            }
        )
        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["model"], "gpt-9.9-sol")
        self.assertIn("gpt-9.9-sol", result["reason"])
        self.assertNotIn("GPT-6 Astra", result["reason"])

    def test_codex_quota_exhaustion_blocks_until_reset_or_access_change(self) -> None:
        result = evaluate_model_policy(
            request(codex=valid_codex(status="quota_exhausted"))
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "quota_exhausted")
        self.assertEqual(result["next_action"], "wait_for_quota_reset_or_change_access")

    def test_codex_quota_exhaustion_with_reported_reset_waits_not_blocks(self) -> None:
        """SKILL.md failure matrix: quota with a USABLE reported reset WAITS
        and continues automatically; only a reset-less exhaustion (or a
        repeated-elapsed streak) blocks. SCHEMA_VERSION 6 contract: the
        observation carries observed_at and the post_invocation history."""
        codex = valid_codex(
            status="quota_exhausted",
            quota_reset_at="2026-08-03T21:00:00Z",
        )
        codex["first_real_invocation"]["observed_at"] = "2026-08-03T20:30:00Z"
        codex["post_invocation"] = []
        full = evaluate_model_policy(request(codex=codex))
        result = full["codex"]

        self.assertEqual(full["state"], "retry")  # caller-visible, not blocked
        self.assertEqual(result["state"], "retry")
        self.assertEqual(result["reason_code"], "quota_wait_for_reset")
        self.assertEqual(result["next_action"], "wait_for_quota_reset")
        self.assertEqual(result["quota"]["reset_at"], "2026-08-03T21:00:00Z")
        self.assertEqual(
            _parse_ts(result["quota"]["wait_until"]),
            _parse_ts("2026-08-03T21:00:00Z"),  # inside floor..ceiling: the reset itself
        )
        self.assertIs(result["quota"]["clamped"], False)
        self.assertIs(result["quota"]["reset_elapsed"], False)
        self.assertNotIn("wait_until_reset", result["quota"])
        self.assertEqual(
            (result["model"], result["effort"]), (CODEX_MODEL, CODEX_EFFORT)
        )
        self.assertFalse(result["downgrade_allowed"])
        self.assertIsNone(result["fallback_model"])

    def test_codex_quota_reset_time_must_parse_or_the_observation_blocks(self) -> None:
        """A present-but-unparseable reset time is a malformed observation to
        correct, never a value to guess a wait from."""
        for bad_reset in ("soon", 12345, "2026-99-99T25:61:61Z"):
            with self.subTest(quota_reset_at=bad_reset):
                result = evaluate_model_policy(
                    request(
                        codex=valid_codex(
                            status="quota_exhausted", quota_reset_at=bad_reset
                        )
                    )
                )["codex"]

                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], "invalid_quota_reset_at")
                self.assertEqual(result["next_action"], "correct_observation_input")

    def test_runaway_is_liveness_class_retry_then_backoff_never_downgrade(self) -> None:
        """A PER_ATTEMPT_CEILING kill is liveness-class exactly like an
        idle-stall: immediate retry first, then the backoff ladder — and the
        backoff branch fires at attempts == CODEX_MAX_ATTEMPTS, not above it."""
        ladder = LIVENESS_BACKOFF_LADDER_SECONDS

        full = evaluate_model_policy(
            request(codex=valid_codex(status="runaway", attempts=1))
        )
        self.assertEqual(full["state"], "retry")  # caller-visible, not blocked
        first = full["codex"]
        self.assertEqual(first["state"], "retry")
        self.assertEqual(first["next_action"], "retry_same_invocation_once")

        at_max = evaluate_model_policy(
            request(codex=valid_codex(status="runaway", attempts=2))
        )["codex"]
        self.assertEqual(at_max["state"], "retry")
        self.assertEqual(at_max["next_action"], "wait_and_retry_with_backoff")
        self.assertEqual(at_max["backoff"]["rung"], 0)
        self.assertEqual(at_max["backoff"]["wait_seconds"], ladder[0])

        late = evaluate_model_policy(
            request(codex=valid_codex(status="runaway", attempts=5))
        )["codex"]
        self.assertEqual(late["next_action"], "wait_and_retry_with_backoff")
        self.assertEqual(late["backoff"]["rung"], len(ladder) - 1)
        self.assertEqual(late["backoff"]["wait_seconds"], ladder[-1])
        self.assertTrue(late["backoff"]["last_rung_repeats"])

        for result in (first, at_max, late):
            self.assertEqual(
                (result["model"], result["effort"]), (CODEX_MODEL, CODEX_EFFORT)
            )
            self.assertFalse(result["downgrade_allowed"])
            self.assertIsNone(result["fallback_model"])

    def test_retryable_failures_retry_once_with_no_downgrade(self) -> None:
        for failure in ("timeout", "transport_error", "runaway"):
            with self.subTest(failure=failure):
                result = evaluate_model_policy(
                    request(codex=valid_codex(status=failure, attempts=1))
                )["codex"]

                self.assertEqual(result["state"], "retry")
                self.assertEqual(result["next_action"], "retry_same_invocation_once")
                self.assertEqual(
                    (result["model"], result["effort"]),
                    (CODEX_MODEL, CODEX_EFFORT),
                )
                self.assertFalse(result["downgrade_allowed"])
                self.assertIsNone(result["fallback_model"])
                self.assertEqual(result["retry"]["remaining"], 1)

    def test_retryable_failures_backoff_instead_of_blocking(self) -> None:
        """Liveness-class failures NEVER terminally block: once the immediate
        retry is spent they climb the backoff ladder, whose last rung repeats
        forever."""
        ladder = LIVENESS_BACKOFF_LADDER_SECONDS
        for failure in ("timeout", "transport_error", "runaway"):
            for attempts, rung in ((2, 0), (3, 1), (4, 2), (5, 3), (9, 3)):
                with self.subTest(failure=failure, attempts=attempts):
                    result = evaluate_model_policy(
                        request(codex=valid_codex(status=failure, attempts=attempts))
                    )["codex"]

                    self.assertEqual(result["state"], "retry")
                    self.assertEqual(result["reason_code"], f"{failure}_backoff")
                    self.assertEqual(
                        result["next_action"], "wait_and_retry_with_backoff"
                    )
                    self.assertEqual(result["backoff"]["rung"], rung)
                    self.assertEqual(
                        result["backoff"]["wait_seconds"], ladder[rung]
                    )
                    self.assertTrue(result["backoff"]["last_rung_repeats"])
                    self.assertEqual(
                        (result["model"], result["effort"]),
                        (CODEX_MODEL, CODEX_EFFORT),
                    )
                    self.assertFalse(result["downgrade_allowed"])
                    self.assertEqual(result["retry"]["remaining"], 0)

    def test_codex_never_downgrades_for_any_failure_matrix_row(self) -> None:
        cases = [
            {"installed": False},
            {**valid_codex(), "version": "0.143.0"},
            {**valid_codex(), "live_catalog": live_catalog(include_astra=False)},
            valid_codex(status="entitlement_denied"),
            valid_codex(status="quota_exhausted"),
            valid_codex(status="timeout", attempts=1),
            valid_codex(status="timeout", attempts=2),
        ]
        for codex in cases:
            with self.subTest(reason=codex):
                result = evaluate_model_policy(request(codex=codex))["codex"]
                self.assertEqual(result["model"], CODEX_MODEL)
                self.assertEqual(result["effort"], CODEX_EFFORT)
                self.assertFalse(result["downgrade_allowed"])
                self.assertIsNone(result["fallback_model"])

    def test_claude_legs_missing_and_old_cli_gate_by_leg(self) -> None:
        for key, _, factory, _ in LEGS:
            cases = (
                ({"installed": False}, "cli_missing"),
                (factory(version="2.1.169"), "cli_too_old"),
            )
            for config, reason_code in cases:
                with self.subTest(leg=key, reason_code=reason_code):
                    result = evaluate_model_policy(leg_request(key, config))[key]
                    if key == "claude_reviewer":
                        # Observed CLI failures on the reviewer degrade onto
                        # the ready base instead of blocking.
                        self.assertEqual(result["state"], "degraded")
                        self.assertEqual(
                            result["degradation"]["reason_code"], reason_code
                        )
                        continue
                    self.assertEqual(result["state"], "blocked")
                    self.assertEqual(result["reason_code"], reason_code)
                    self.assertTrue(result["waiver_required"])

    def test_claude_malformed_install_facts_cannot_be_waived(self) -> None:
        for key, _, factory, _ in LEGS:
            for field, value, reason_code in (
                ("installed", [], "invalid_installed_status"),
                ("version", [], "invalid_version_value"),
            ):
                with self.subTest(leg=key, field=field):
                    config = factory(explicit_waiver=True)
                    config[field] = value
                    result = evaluate_model_policy(leg_request(key, config))[key]
                    self.assertEqual(result["state"], "blocked")
                    self.assertEqual(result["reason_code"], reason_code)
                    self.assertFalse(result["waiver_granted"])

    def test_base_fable_access_failures_block_pending_waiver(self) -> None:
        for access in (
            "unavailable",
            "entitlement_denied",
            "provider_policy_denied",
            "unknown",
        ):
            with self.subTest(access=access):
                result = evaluate_model_policy(
                    request(base=valid_base(fable_access=access))
                )["claude"]
                self.assertEqual(result["state"], "blocked")
                self.assertTrue(result["waiver_required"])
                # Pin the leg's own code family: a stale table from the other
                # lineage shadowing this one would otherwise pass unnoticed.
                self.assertTrue(
                    result["reason_code"].startswith("fable_"),
                    f"base leg must report fable_* codes, got {result['reason_code']}",
                )

    def test_reviewer_fable_access_failures_degrade_observed_block_unverified(
        self,
    ) -> None:
        for access in ("unavailable", "entitlement_denied", "provider_policy_denied"):
            with self.subTest(access=access):
                result = evaluate_model_policy(
                    request(reviewer=valid_reviewer(fable_access=access))
                )["claude_reviewer"]
                self.assertEqual(result["state"], "degraded")
                self.assertEqual(
                    result["reason_code"], "reviewer_degraded_to_base"
                )
                self.assertTrue(
                    result["degradation"]["reason_code"].startswith("fable_"),
                    "reviewer leg must report fable_* codes, got "
                    f"{result['degradation']['reason_code']}",
                )

        unverified = evaluate_model_policy(
            request(reviewer=valid_reviewer(fable_access="unknown"))
        )["claude_reviewer"]
        self.assertEqual(unverified["state"], "blocked")
        self.assertTrue(unverified["waiver_required"])
        self.assertEqual(unverified["reason_code"], "fable_access_unverified")

    def test_claude_zdr_failures_gate_by_leg(self) -> None:
        for key, _, factory, _ in LEGS:
            for status in ("incompatible", "denied", "unknown"):
                with self.subTest(leg=key, status=status):
                    result = evaluate_model_policy(
                        leg_request(key, factory(zero_data_retention=status))
                    )[key]
                    if key == "claude_reviewer" and status != "unknown":
                        # Observed ZDR failure on the reviewer degrades onto
                        # the ready base instead of blocking.
                        self.assertEqual(result["state"], "degraded")
                        self.assertEqual(
                            result["degradation"]["reason_code"],
                            "zdr_incompatible",
                        )
                        continue
                    self.assertEqual(result["state"], "blocked")
                    self.assertTrue(result["waiver_required"])
                    self.assertIn(
                        result["reason_code"], {"zdr_incompatible", "zdr_unverified"}
                    )

    def test_malformed_claude_gate_observations_block_without_waiver(self) -> None:
        for key, _, factory, access_field in LEGS:
            cases = (
                {access_field: []},
                {"zero_data_retention": []},
                {"environment": []},
                {"environment": {"CLAUDE_CODE_SUBAGENT_MODEL": 123}},
            )
            for malformed in cases:
                with self.subTest(leg=key, malformed=malformed):
                    config = factory(explicit_waiver=True, **malformed)
                    result = evaluate_model_policy(leg_request(key, config))[key]
                    self.assertEqual(result["state"], "blocked")
                    self.assertFalse(result["waiver_granted"])
                    self.assertEqual(
                        result["next_action"], "correct_observation_input"
                    )

    def test_base_unavailability_can_only_continue_after_explicit_waiver(
        self,
    ) -> None:
        unavailable = valid_base(fable_access="unavailable")
        blocked = evaluate_model_policy(request(base=unavailable))["claude"]

        unavailable["explicit_waiver"] = True
        unavailable["waiver_fallback"] = {
            "model": "claude-mythos-5",
            "effort": "max",
            "available": True,
            "explicitly_authorized": True,
            "execution_path": "explicit_cli",
        }
        waived = evaluate_model_policy(request(base=unavailable))["claude"]

        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(
            blocked["next_action"], "request_explicit_waiver_or_restore_fable_access"
        )
        self.assertEqual(waived["state"], "waived")
        self.assertTrue(waived["waiver_granted"])
        self.assertEqual(waived["model"], "claude-mythos-5")
        self.assertEqual(waived["execution_path"], "explicit_cli")

    def test_reviewer_unavailability_degrades_and_waiver_still_preempts(
        self,
    ) -> None:
        unavailable = valid_reviewer(
            fable_access="unavailable",
            observed_models=["claude-fable-5-1", "claude-fable-6"],
        )
        degraded = evaluate_model_policy(request(reviewer=unavailable))[
            "claude_reviewer"
        ]

        unavailable["explicit_waiver"] = True
        unavailable["waiver_fallback"] = {
            "model": "claude-fable-6",
            "effort": "max",
            "available": True,
            "explicitly_authorized": True,
            "execution_path": "explicit_cli",
        }
        waived = evaluate_model_policy(request(reviewer=unavailable))[
            "claude_reviewer"
        ]

        self.assertEqual(degraded["state"], "degraded")
        self.assertEqual(degraded["model"], BASE_MODEL)
        self.assertEqual(
            degraded["degradation"]["reason_code"], "fable_unavailable"
        )
        self.assertEqual(waived["state"], "waived")
        self.assertTrue(waived["waiver_granted"])
        self.assertEqual(waived["model"], "claude-fable-6")
        self.assertEqual(waived["execution_path"], "explicit_cli")

    def test_base_waiver_rejects_unobserved_or_malformed_fallback(self) -> None:
        for model in (
            "claude-fable-",
            "claude-fable-malicious",
            "claude-fable-foo/bar",
            # Naming the leg's own floor primary as its fallback is not a
            # substitute for restoring access to it.
            "claude-fable-5",
            # Opus — any version — is never an autonomy fallback: reviews and
            # work stay on the fable/mythos lineage.
            "claude-opus-5",
            "claude-opus-6",
            # A waiver substitutes within the fable/mythos lineage; it may not
            # authorize a version below the floor. No path proposes a downgrade.
            "claude-fable-4-5",
            "claude-mythos-4",
        ):
            with self.subTest(model=model):
                base = valid_base(
                    fable_access="unavailable",
                    explicit_waiver=True,
                    waiver_fallback={
                        "model": model,
                        "effort": "max",
                        "available": True,
                        "explicitly_authorized": True,
                        "execution_path": "explicit_cli",
                    },
                )
                result = evaluate_model_policy(request(base=base))["claude"]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], "invalid_named_fallback")

    def test_base_waiver_accepts_a_different_fable_lineage_model(
        self,
    ) -> None:
        base = valid_base(
            fable_access="unavailable",
            explicit_waiver=True,
            observed_models=["claude-fable-5", "claude-fable-6"],
            waiver_fallback={
                "model": "claude-fable-6",
                "effort": "max",
                "available": True,
                "explicitly_authorized": True,
                "execution_path": "explicit_cli",
            },
        )

        result = evaluate_model_policy(request(base=base))["claude"]

        self.assertEqual(result["state"], "waived")
        self.assertEqual(result["model"], "claude-fable-6")

    def test_reviewer_waiver_rejects_unobserved_or_malformed_fallback(self) -> None:
        for model in (
            "claude-fable-",
            "claude-fable-malicious",
            "claude-fable-foo/bar",
            # Naming the reviewer's own floor primary as its fallback is not a
            # substitute for restoring access to it.
            "claude-fable-5-1",
            # Opus — any version — is never an autonomy fallback.
            "claude-opus-5",
            "claude-opus-6",
            # A waiver substitutes within the fable/mythos lineage; it may not
            # authorize a version below the reviewer's own 5.1 floor.
            "claude-fable-5",
            "claude-mythos-5",
            "claude-fable-1",
            "claude-fable-4-5",
            "claude-mythos-4",
        ):
            with self.subTest(model=model):
                reviewer = valid_reviewer(
                    fable_access="unavailable",
                    explicit_waiver=True,
                    waiver_fallback={
                        "model": model,
                        "effort": "max",
                        "available": True,
                        "explicitly_authorized": True,
                        "execution_path": "explicit_cli",
                    },
                )
                result = evaluate_model_policy(request(reviewer=reviewer))[
                    "claude_reviewer"
                ]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], "invalid_named_fallback")

    def test_missing_claude_cli_cannot_select_explicit_fallback(self) -> None:
        for key, fallback_model in (
            ("claude", "claude-mythos-5"),
            ("claude_reviewer", "claude-fable-6"),
        ):
            with self.subTest(leg=key):
                factory = valid_base if key == "claude" else valid_reviewer
                access_field = "fable_access"
                config = factory(
                    installed=False,
                    explicit_waiver=True,
                    waiver_fallback={
                        "model": fallback_model,
                        "effort": "max",
                        "available": True,
                        "explicitly_authorized": True,
                        "execution_path": "explicit_cli",
                    },
                    **{access_field: "unavailable"},
                )
                result = evaluate_model_policy(leg_request(key, config))[key]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], "invalid_named_fallback")

    def test_conflicting_subagent_model_selects_explicit_cli(self) -> None:
        base = valid_base(
            environment={"CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-4-6"}
        )

        result = evaluate_model_policy(request(base=base))["claude"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["reason_code"], "explicit_cli_required")
        self.assertEqual(result["execution_path"], "explicit_cli")
        self.assertEqual(
            result["arguments"],
            [
                "-p",
                "--model",
                "fable",
                "--effort",
                "max",
                "--permission-mode",
                "plan",
                "--allowedTools",
                "Read,Glob,Grep",
                "--disallowedTools",
                "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch,Agent,Task",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--no-chrome",
            ],
        )
        self.assertTrue(result["read_only"]["required"])
        self.assertEqual(result["read_only"]["permission_mode"], "plan")

        reviewer = valid_reviewer(
            environment={"CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-4-6"}
        )
        result = evaluate_model_policy(request(reviewer=reviewer))["claude_reviewer"]
        self.assertEqual(result["execution_path"], "explicit_cli")
        model_flag = result["arguments"][result["arguments"].index("--model") + 1]
        self.assertEqual(model_flag, "fable")

    def test_matching_base_override_keeps_agent_tool_path(self) -> None:
        for override in (None, "", "fable", "claude-fable-5"):
            with self.subTest(override=override):
                result = evaluate_model_policy(
                    request(
                        base=valid_base(
                            environment={"CLAUDE_CODE_SUBAGENT_MODEL": override}
                        )
                    )
                )["claude"]
                self.assertEqual(result["state"], "ready")
                self.assertEqual(result["execution_path"], "agent_tool")
                self.assertEqual(result["effort"], "max")

    def test_matching_reviewer_override_keeps_agent_tool_path(self) -> None:
        # The bare floor and its alias name the same model version, so neither
        # is a conflicting override.
        for override in (None, "", "fable", "claude-fable-5-1"):
            with self.subTest(override=override):
                result = evaluate_model_policy(
                    request(
                        reviewer=valid_reviewer(
                            environment={"CLAUDE_CODE_SUBAGENT_MODEL": override}
                        )
                    )
                )["claude_reviewer"]
                self.assertEqual(result["state"], "ready")
                self.assertEqual(result["execution_path"], "agent_tool")
                self.assertEqual(result["effort"], "max")

    def test_unverified_agent_host_uses_explicit_cli(self) -> None:
        for key, _, factory, _ in LEGS:
            with self.subTest(leg=key):
                result = evaluate_model_policy(
                    leg_request(key, factory(host_capabilities={}))
                )[key]
                self.assertEqual(result["state"], "ready")
                self.assertEqual(result["execution_path"], "explicit_cli")
                self.assertIn("CLAUDE_CODE_EFFORT_LEVEL", result["environment_unset"])

    def test_agent_host_without_read_only_enforcement_uses_explicit_cli(self) -> None:
        base = valid_base()
        base["host_capabilities"]["agent_read_only_enforced"] = False

        result = evaluate_model_policy(request(base=base))["claude"]

        self.assertEqual(result["execution_path"], "explicit_cli")
        self.assertIn("--permission-mode", result["arguments"])
        self.assertIn("--allowedTools", result["arguments"])
        self.assertIn("--disallowedTools", result["arguments"])

    def test_effort_environment_override_uses_clean_explicit_cli(self) -> None:
        base = valid_base(
            environment={
                "CLAUDE_CODE_SUBAGENT_MODEL": None,
                "CLAUDE_CODE_EFFORT_LEVEL": "high",
            }
        )

        result = evaluate_model_policy(request(base=base))["claude"]

        self.assertEqual(result["execution_path"], "explicit_cli")
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL", result["environment_unset"])

    def test_case_or_whitespace_variant_override_uses_explicit_cli(self) -> None:
        for override in ("FABLE", " fable ", "Claude-Fable-5"):
            with self.subTest(override=override):
                result = evaluate_model_policy(
                    request(
                        base=valid_base(
                            environment={"CLAUDE_CODE_SUBAGENT_MODEL": override}
                        )
                    )
                )["claude"]
                self.assertEqual(result["state"], "ready")
                self.assertEqual(result["execution_path"], "explicit_cli")

    def test_cross_family_override_is_never_equivalent(self) -> None:
        """Same floor eligibility does not make sibling slugs one override.

        Mythos sits at the base floor for SELECTION, but a Mythos override is a
        different model than the selected Fable — and a below-floor or
        sibling-family override is not the selected reviewer. Both directions
        must fall back to the explicit CLI.
        """

        for leg_key, fixture, override in (
            ("claude", valid_base, "claude-mythos-5"),
            ("claude", valid_base, "mythos"),
            ("claude_reviewer", valid_reviewer, "claude-mythos-5-1"),
            ("claude_reviewer", valid_reviewer, "claude-fable-5"),
        ):
            with self.subTest(leg=leg_key, override=override):
                config = fixture(
                    environment={"CLAUDE_CODE_SUBAGENT_MODEL": override}
                )
                kwargs = {"base" if fixture is valid_base else "reviewer": config}
                result = evaluate_model_policy(request(**kwargs))[leg_key]
                self.assertEqual(result["state"], "ready")
                self.assertEqual(result["execution_path"], "explicit_cli")

    def test_cli_reads_json_and_writes_only_the_decision(self) -> None:
        payload = request()
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), evaluate_model_policy(payload))

    def test_cli_invalid_json_blocks_with_nonzero_exit(self) -> None:
        stdin = io.StringIO("not-json")
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["state"], "blocked")


class AutoForwardSelectionTest(unittest.TestCase):
    """Floors, not pins: newest eligible model at or above the floor wins."""

    @staticmethod
    def model(slug: str, *efforts: str) -> dict:
        return {
            "slug": slug,
            "supported_reasoning_levels": [{"effort": effort} for effort in efforts],
        }

    def codex_with(self, *models: dict) -> dict:
        codex = valid_codex()
        codex["live_catalog"] = {
            "models": [*live_catalog()["models"], *models]
        }
        return codex

    def test_floor_only_catalog_selects_the_floor(self) -> None:
        result = evaluate_model_policy(request())["codex"]

        self.assertEqual(result["model"], CODEX_MODEL)
        self.assertEqual(result["selection"]["reason"], "floor_model")
        self.assertEqual(result["selection"]["floor_model"], CODEX_MODEL)

    def test_newer_codex_model_is_auto_selected(self) -> None:
        codex = self.codex_with(
            self.model("gpt-6.1", "high", CODEX_EFFORT, "ultra")
        )

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model"], "gpt-6.1")
        self.assertEqual(result["arguments"][:2], ["-m", "gpt-6.1"])
        self.assertEqual(result["selection"]["reason"], "newer_model_auto_selected")

    def test_generated_codex_arguments_pin_the_read_only_sandbox(self) -> None:
        # R2 round-2 finding 3737466478, verified: only the entry smoke
        # added `-s read-only` — the policy-generated argv pinned model and
        # effort alone, so a reviewer invocation reconstructed from it
        # inherited whatever sandbox the operator's ambient codex config
        # set (workspace-write would let a review voice modify the
        # implementation it judges). The generated argv itself now carries
        # the pin.
        result = evaluate_model_policy(request())["codex"]

        arguments = result["arguments"]
        self.assertIn("-s", arguments)
        self.assertEqual(arguments[arguments.index("-s") + 1], "read-only")

    def test_newest_version_wins_and_astra_lineage_breaks_ties(self) -> None:
        codex = self.codex_with(
            self.model("gpt-6.1", CODEX_EFFORT, "ultra"),
            self.model("gpt-6.1-astra", CODEX_EFFORT, "ultra"),
            self.model("gpt-7", CODEX_EFFORT, "ultra"),
        )
        result = evaluate_model_policy(request(codex=codex))["codex"]
        self.assertEqual(result["model"], "gpt-7")

        codex = self.codex_with(
            self.model("gpt-6.1", CODEX_EFFORT, "ultra"),
            self.model("gpt-6.1-astra", CODEX_EFFORT, "ultra"),
        )
        result = evaluate_model_policy(request(codex=codex))["codex"]
        self.assertEqual(result["model"], "gpt-6.1-astra")

    def test_down_tier_variants_and_missing_required_effort_are_not_upgrades(self) -> None:
        codex = self.codex_with(
            self.model("gpt-7-mini", CODEX_EFFORT, "ultra"),
            self.model("gpt-7-nano", CODEX_EFFORT, "ultra"),
            # Missing either required tier disqualifies: no max (depth) …
            self.model("gpt-7", "high", "xhigh", "ultra"),
            # … and no ultra (breadth) both leave the floor selected.
            self.model("gpt-8", CODEX_EFFORT),
        )

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["model"], CODEX_MODEL)
        self.assertEqual(result["selection"]["reason"], "floor_model")

    def test_same_version_sibling_is_not_an_upgrade(self) -> None:
        codex = self.codex_with(self.model("gpt-6", CODEX_EFFORT, "ultra"))

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["model"], CODEX_MODEL)

    def test_catalog_without_any_eligible_model_still_blocks(self) -> None:
        codex = valid_codex()
        codex["live_catalog"] = {
            "models": [
                self.model("gpt-5.9", CODEX_EFFORT, "ultra"),
                self.model("gpt-7-mini", CODEX_EFFORT, "ultra"),
                self.model("gpt-7", "high", "xhigh", "ultra"),
                self.model("gpt-8", CODEX_EFFORT),
            ]
        }

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "live_catalog_missing_capability")

    def test_newer_fable_is_auto_selected_for_agent_and_cli_paths(self) -> None:
        base = valid_base(
            observed_models=["claude-fable-5", "claude-fable-6", "claude-opus-5"]
        )
        result = evaluate_model_policy(request(base=base))["claude"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model"], "claude-fable-6")
        self.assertEqual(result["execution_path"], "agent_tool")
        self.assertIn("model=claude-fable-6", result["arguments"])
        self.assertEqual(result["selection"]["reason"], "newer_model_auto_selected")

        base["host_capabilities"] = {}
        result = evaluate_model_policy(request(base=base))["claude"]

        self.assertEqual(result["execution_path"], "explicit_cli")
        model_flag = result["arguments"][result["arguments"].index("--model") + 1]
        self.assertEqual(model_flag, "claude-fable-6")

    def test_newer_fable_is_auto_selected_for_the_reviewer(self) -> None:
        reviewer = valid_reviewer(
            observed_models=["claude-fable-5-1", "claude-fable-6", "claude-fable-5"]
        )

        result = evaluate_model_policy(request(reviewer=reviewer))["claude_reviewer"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model"], "claude-fable-6")
        self.assertIn("model=claude-fable-6", result["arguments"])
        self.assertEqual(result["selection"]["reason"], "newer_model_auto_selected")

    def test_reviewer_selection_never_advances_the_base(self) -> None:
        """The two legs auto-forward independently, each from its own list."""

        result = evaluate_model_policy(
            request(
                base=valid_base(observed_models=["claude-fable-5"]),
                reviewer=valid_reviewer(observed_models=["claude-fable-6"]),
            )
        )

        self.assertEqual(result["claude"]["model"], BASE_MODEL)
        self.assertEqual(result["claude"]["selection"]["reason"], "floor_model")
        self.assertEqual(result["claude_reviewer"]["model"], "claude-fable-6")
        self.assertEqual(
            result["claude_reviewer"]["selection"]["reason"],
            "newer_model_auto_selected",
        )

    def test_newer_fable_in_the_base_list_never_advances_the_reviewer(self) -> None:
        result = evaluate_model_policy(
            request(
                base=valid_base(observed_models=["claude-fable-6"]),
                reviewer=valid_reviewer(
                    observed_models=["claude-fable-5-1"]
                ),
            )
        )

        self.assertEqual(result["claude"]["model"], "claude-fable-6")
        self.assertEqual(
            result["claude"]["selection"]["reason"], "newer_model_auto_selected"
        )
        self.assertEqual(result["claude_reviewer"]["model"], REVIEWER_MODEL)
        self.assertEqual(
            result["claude_reviewer"]["selection"]["reason"], "floor_model"
        )

    def test_fable_family_preferred_over_mythos_on_version_tie(self) -> None:
        base = valid_base(observed_models=["claude-mythos-6", "claude-fable-6"])

        result = evaluate_model_policy(request(base=base))

        self.assertEqual(result["claude"]["model"], "claude-fable-6")

    def test_floor_override_conflicts_when_newer_model_selected(self) -> None:
        base = valid_base(
            observed_models=["claude-fable-6"],
            environment={"CLAUDE_CODE_SUBAGENT_MODEL": "fable"},
        )

        result = evaluate_model_policy(request(base=base))["claude"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["execution_path"], "explicit_cli")

    def test_malformed_observed_models_fall_back_to_the_floor(self) -> None:
        for observed in (None, "claude-fable-6", [123, {}, "claude-haiku-4-5"]):
            with self.subTest(observed=observed):
                base = valid_base(observed_models=observed)
                result = evaluate_model_policy(request(base=base))["claude"]
                self.assertEqual(result["model"], BASE_MODEL)
                self.assertEqual(result["selection"]["reason"], "floor_model")

    def test_cross_lineage_and_down_tier_models_are_never_selected(self) -> None:
        base_cases = (
            ["claude-opus-6"],
            ["claude-sonnet-5"],
            ["claude-haiku-4-5"],
            ["claude-fable-4-5"],
        )
        for observed in base_cases:
            with self.subTest(leg="claude", observed=observed):
                result = evaluate_model_policy(
                    request(base=valid_base(observed_models=observed))
                )["claude"]
                self.assertEqual(result["model"], BASE_MODEL)
                self.assertEqual(result["selection"]["reason"], "floor_model")

        reviewer_cases = (
            # Opus — any version — is never selected for the reviewer, and
            # claude-fable-5 sits below the reviewer's own 5.1 floor.
            ["claude-opus-6"],
            ["claude-fable-5"],
            ["claude-sonnet-5"],
            ["claude-haiku-4-5"],
        )
        for observed in reviewer_cases:
            with self.subTest(leg="claude_reviewer", observed=observed):
                result = evaluate_model_policy(
                    request(reviewer=valid_reviewer(observed_models=observed))
                )["claude_reviewer"]
                self.assertEqual(result["model"], REVIEWER_MODEL)
                self.assertEqual(result["selection"]["reason"], "floor_model")


class GatingAggregateTest(unittest.TestCase):
    """Base and Codex gate; the reviewer degrades onto a ready base instead."""

    def test_blocked_base_blocks_the_workflow(self) -> None:
        result = evaluate_model_policy(
            request(base=valid_base(fable_access="unavailable"))
        )

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["claude"]["state"], "blocked")
        self.assertEqual(result["claude"]["reason_code"], "fable_unavailable")
        self.assertTrue(result["claude"]["waiver_required"])

    def test_unavailable_reviewer_degrades_onto_the_base_instead_of_blocking(
        self,
    ) -> None:
        """An availability-class reviewer failure is a recorded degradation."""

        for field, value, reason_code in (
            ("fable_access", "unavailable", "fable_unavailable"),
            ("fable_access", "entitlement_denied", "fable_entitlement_denied"),
            ("zero_data_retention", "incompatible", "zdr_incompatible"),
            ("installed", False, "cli_missing"),
        ):
            with self.subTest(field=field, value=value):
                result = evaluate_model_policy(
                    request(reviewer=valid_reviewer(**{field: value}))
                )

                self.assertEqual(result["state"], "degraded")
                reviewer = result["claude_reviewer"]
                self.assertEqual(reviewer["state"], "degraded")
                self.assertFalse(reviewer["blocking"])
                self.assertEqual(
                    reviewer["reason_code"], "reviewer_degraded_to_base"
                )
                self.assertEqual(
                    reviewer["degradation"]["reason_code"], reason_code
                )
                self.assertEqual(reviewer["model"], BASE_MODEL)
                self.assertEqual(reviewer["effort"], "max")
                self.assertEqual(reviewer["fallback_model"], BASE_MODEL)
                self.assertEqual(
                    reviewer["next_action"], "invoke_reviewer_agent"
                )
                self.assertFalse(reviewer["waiver_required"])
                self.assertEqual(result["claude"]["state"], "ready")

    def test_reviewer_degradation_reuses_the_base_cli_decision(self) -> None:
        """The degraded voice runs exactly what the base leg proved out."""

        result = evaluate_model_policy(
            request(
                base=valid_base(host_capabilities={}),
                reviewer=valid_reviewer(fable_access="unavailable"),
            )
        )

        self.assertEqual(result["state"], "degraded")
        reviewer = result["claude_reviewer"]
        self.assertEqual(reviewer["execution_path"], "explicit_cli")
        self.assertEqual(
            reviewer["next_action"], "invoke_explicit_reviewer_cli"
        )
        self.assertEqual(reviewer["arguments"], result["claude"]["arguments"])
        self.assertIn("--permission-mode", reviewer["arguments"])

    def test_reviewer_never_degrades_onto_a_blocked_or_waived_base(self) -> None:
        """No ready base, no degradation: both-Claude-legs-down still blocks."""

        result = evaluate_model_policy(
            request(
                base=valid_base(fable_access="unavailable"),
                reviewer=valid_reviewer(fable_access="unavailable"),
            )
        )
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["claude_reviewer"]["state"], "blocked")

        waived_base = valid_base(
            fable_access="unavailable",
            explicit_waiver=True,
            waiver_fallback={
                "model": "claude-mythos-5",
                "effort": "max",
                "available": True,
                "explicitly_authorized": True,
                "execution_path": "explicit_cli",
            },
        )
        result = evaluate_model_policy(
            request(
                base=waived_base,
                reviewer=valid_reviewer(fable_access="unavailable"),
            )
        )
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["claude_reviewer"]["state"], "blocked")
        self.assertTrue(result["claude_reviewer"]["waiver_required"])

    def test_malformed_reviewer_observations_still_block(self) -> None:
        """Degradation never papers over garbage input."""

        for overrides, reason_code in (
            ({"installed": "yes"}, "invalid_installed_status"),
            ({"fable_access": "sometimes"}, "invalid_fable_access"),
            (
                {"fable_access": "unavailable", "explicit_waiver": True},
                "named_fallback_required",
            ),
        ):
            with self.subTest(overrides=overrides):
                result = evaluate_model_policy(
                    request(reviewer=valid_reviewer(**overrides))
                )

                self.assertEqual(result["state"], "blocked")
                self.assertEqual(
                    result["claude_reviewer"]["reason_code"], reason_code
                )

    def test_reviewer_waiver_preempts_auto_degradation(self) -> None:
        """An explicit waiver names the fallback; auto-degradation defers."""

        reviewer = valid_reviewer(
            fable_access="unavailable",
            observed_models=["claude-fable-5-1", "claude-fable-6"],
            explicit_waiver=True,
            waiver_fallback={
                "model": "claude-fable-6",
                "effort": "max",
                "available": True,
                "explicitly_authorized": True,
                "execution_path": "explicit_cli",
            },
        )

        result = evaluate_model_policy(request(reviewer=reviewer))

        self.assertEqual(result["state"], "waived")
        self.assertEqual(result["claude_reviewer"]["state"], "waived")

    def test_absent_leg_observation_blocks_instead_of_guessing(self) -> None:
        for missing in ("claude", "claude_reviewer"):
            with self.subTest(missing=missing):
                payload = request()
                del payload[missing]
                result = evaluate_model_policy(payload)

                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result[missing]["state"], "blocked")
                self.assertEqual(
                    result[missing]["reason_code"], "invalid_installed_status"
                )

    def test_waived_leg_yields_waived_aggregate(self) -> None:
        base = valid_base(
            fable_access="unavailable",
            explicit_waiver=True,
            waiver_fallback={
                "model": "claude-mythos-5",
                "effort": "max",
                "available": True,
                "explicitly_authorized": True,
                "execution_path": "explicit_cli",
            },
        )

        result = evaluate_model_policy(request(base=base))

        self.assertEqual(result["state"], "waived")
        self.assertEqual(result["claude"]["state"], "waived")
        self.assertEqual(result["claude_reviewer"]["state"], "ready")

    def test_both_claude_legs_are_read_only_on_both_execution_paths(self) -> None:
        for key, _, factory, _ in LEGS:
            with self.subTest(leg=key, path="agent_tool"):
                agent = evaluate_model_policy(leg_request(key, factory()))[key]
                self.assertEqual(agent["execution_path"], "agent_tool")
                self.assertTrue(agent["read_only"]["required"])

            with self.subTest(leg=key, path="explicit_cli"):
                cli = evaluate_model_policy(
                    leg_request(key, factory(host_capabilities={}))
                )[key]
                self.assertEqual(cli["execution_path"], "explicit_cli")
                self.assertIn("--permission-mode", cli["arguments"])
                self.assertIn("plan", cli["arguments"])
                denied = cli["arguments"][
                    cli["arguments"].index("--disallowedTools") + 1
                ]
                for tool in ("Edit", "Write", "Bash"):
                    self.assertIn(tool, denied)

    def test_unhashable_environment_overrides_block_instead_of_raising(self) -> None:
        # Unhashable env overrides must not raise out of the whole gate: a
        # traceback is strictly worse than a decision, for every leg.
        cases = (
            ({"CLAUDE_CODE_EFFORT_LEVEL": []}, "invalid_effort_override"),
            ({"CLAUDE_CODE_SUBAGENT_MODEL": []}, "invalid_subagent_override"),
            ({"CLAUDE_CODE_EFFORT_LEVEL": {}}, "invalid_effort_override"),
        )
        for key, _, factory, _ in LEGS:
            for environment, reason_code in cases:
                with self.subTest(leg=key, environment=environment):
                    result = evaluate_model_policy(
                        leg_request(key, factory(environment=environment))
                    )[key]
                    self.assertEqual(result["state"], "blocked")
                    self.assertEqual(result["reason_code"], reason_code)


class AuthenticationPolicyTests(unittest.TestCase):
    """A dead credential must block immediately, never burn the retry budget."""

    def test_authentication_error_blocks_without_retry(self) -> None:
        codex = valid_codex()
        codex["first_real_invocation"] = {
            "status": "authentication_error",
            "attempts": 1,
        }

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "authentication_error")
        self.assertEqual(result["next_action"], "repair_authentication")
        self.assertFalse(result.get("downgrade_allowed", False))

    def test_attempts_are_scoped_to_one_invocation_not_cumulative(self) -> None:
        # A later review round reports attempts=1 for its own sequence; the
        # helper must accept it rather than treating it as a third try.
        codex = valid_codex()
        codex["first_real_invocation"] = {"status": "success", "attempts": 1}
        self.assertEqual(
            evaluate_model_policy(request(codex=codex))["codex"]["state"], "ready"
        )

        codex["first_real_invocation"] = {"status": "timeout", "attempts": 1}
        retry = evaluate_model_policy(request(codex=codex))["codex"]
        self.assertEqual(retry["state"], "retry")
        self.assertEqual(retry["retry"]["remaining"], 1)


class StreamClassifierTests(unittest.TestCase):
    """The auth boundary: only structured errors/stderr may kill an invocation."""

    def test_assistant_text_mentioning_401_is_benign(self) -> None:
        event = json.dumps(
            {
                "type": "assistant_message",
                "message": "The plan adds a row for HTTP 401 invalid_refresh_token.",
            }
        )
        self.assertEqual(
            classify_stream_event(SOURCE_STDOUT_JSON, event), "benign"
        )

    def test_transport_error_with_401_is_auth_error(self) -> None:
        event = json.dumps({"type": "error", "status": 401, "message": "Unauthorized"})
        self.assertEqual(
            classify_stream_event(SOURCE_STDOUT_JSON, event), "auth_error"
        )

    def test_invalid_refresh_token_error_event_is_auth_error(self) -> None:
        event = json.dumps(
            {"type": "stream_error", "error": {"code": "invalid_refresh_token"}}
        )
        self.assertEqual(
            classify_stream_event(SOURCE_STDOUT_JSON, event), "auth_error"
        )

    def test_unknown_well_formed_event_is_benign(self) -> None:
        event = json.dumps({"type": "token_count", "tokens": 401})
        self.assertEqual(classify_stream_event(SOURCE_STDOUT_JSON, event), "benign")

    def test_invalid_json_on_json_channel_is_internal_failure(self) -> None:
        self.assertEqual(
            classify_stream_event(SOURCE_STDOUT_JSON, '{"type": "error"'),
            "internal_failure",
        )

    def test_diagnostic_stderr_auth_signature_is_auth_error(self) -> None:
        self.assertEqual(
            classify_stream_event(SOURCE_STDERR, "ERROR: 401 invalid_refresh_token"),
            "auth_error",
        )

    def test_embedded_source_field_cannot_forge_provenance(self) -> None:
        # An assistant event claiming to be stderr must still be benign: the
        # tag comes from the file descriptor, never from event content.
        event = json.dumps(
            {
                "type": "assistant_message",
                "source": SOURCE_STDERR,
                "message": "401 invalid_refresh_token",
            }
        )
        self.assertEqual(classify_stream_event(SOURCE_STDOUT_JSON, event), "benign")

    def test_oversized_record_is_internal_failure(self) -> None:
        oversized = "x" * (MAX_RAW_RECORD_BYTES + 1)
        self.assertEqual(
            classify_stream_event(SOURCE_STDOUT_JSON, oversized), "internal_failure"
        )

    def test_unknown_source_tag_is_internal_failure(self) -> None:
        self.assertEqual(classify_stream_event("smuggled", "{}"), "internal_failure")


class ExcerptRedactionTests(unittest.TestCase):
    def test_url_userinfo_query_and_fragment_are_stripped(self) -> None:
        cleaned = strip_url_secrets("https://user:token@host/path?key=SECRET#frag")
        self.assertNotIn("token", cleaned)
        self.assertNotIn("SECRET", cleaned)
        self.assertIn("https://host/path", cleaned)

    def test_excerpt_is_byte_capped(self) -> None:
        excerpt = bounded_excerpt("", "y" * (MAX_EXCERPT_BYTES * 2))
        self.assertLessEqual(len(excerpt.encode("utf-8")), MAX_EXCERPT_BYTES)


class _SteppingClock:
    """Deterministic time.monotonic stand-in: each call advances a fixed step."""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        now = self._now
        self._now += self._step
        return now


class _FakePipe:
    """Byte pipe stub that yields chunks then EOF, mimicking a real read()."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def fileno(self) -> int:  # pragma: no cover - selector stub only
        return -1

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _ImmediateSelector:
    """Selector stub: reports every registered pipe readable each pass."""

    def __init__(self) -> None:
        self._registry: list[tuple[Any, str]] = []

    def register(self, fileobj, _events, data):  # noqa: ANN001
        self._registry.append((fileobj, data))

    def unregister(self, fileobj):  # noqa: ANN001
        self._registry = [(f, d) for f, d in self._registry if f is not fileobj]

    def select(self, timeout=None):  # noqa: ANN001, ARG002
        return [(mock.Mock(fileobj=f, data=d), 1) for f, d in self._registry]

    def close(self) -> None:
        self._registry = []


class SuperviseStreamTests(unittest.TestCase):
    """supervise_stream owns the kill decision; all three exits are pinned."""

    def setUp(self) -> None:
        patcher = mock.patch("model_policy.selectors.DefaultSelector", _ImmediateSelector)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clean_stream_does_not_kill(self) -> None:
        stdout = _FakePipe([json.dumps({"type": "assistant_message", "message": "ok"}).encode() + b"\n"])
        killed = []

        result = supervise_stream(stdout, None, lambda: killed.append(True))

        self.assertEqual(result["outcome"], "clean")
        self.assertEqual(result["exit_code"], CLASSIFY_EXIT_CLEAN)
        self.assertEqual(killed, [])

    def test_auth_error_kills_and_reports(self) -> None:
        stdout = _FakePipe(
            [json.dumps({"type": "error", "status": 401}).encode() + b"\n"]
        )
        killed = []

        result = supervise_stream(stdout, None, lambda: killed.append(True))

        self.assertEqual(result["outcome"], "auth_error")
        self.assertEqual(result["exit_code"], CLASSIFY_EXIT_AUTH_ERROR)
        self.assertEqual(killed, [True])

    def test_internal_failure_kills_and_blocks(self) -> None:
        stdout = _FakePipe([b'{"type": "error"\n'])
        killed = []

        result = supervise_stream(stdout, None, lambda: killed.append(True))

        self.assertEqual(result["outcome"], "internal_failure")
        self.assertEqual(result["exit_code"], CLASSIFY_EXIT_INTERNAL_FAILURE)
        self.assertEqual(killed, [True])

    def test_wait_exception_after_clean_streams_kills_the_child(self) -> None:
        # R2 #1328 finding 3767068801, empirically verified: with clean
        # streams the cleanup branch was already skipped, so a child_wait
        # that RAISES left the (possibly credentialed) child running with
        # zero kill calls while supervision reported internal_failure — the
        # child could survive supervision and overlap the retry.
        stdout = _FakePipe([])
        killed = []

        def raising_wait() -> int | None:
            raise OSError("wait failed")

        result = supervise_stream(
            stdout, None, lambda: killed.append(True), raising_wait
        )

        self.assertEqual(result["outcome"], "internal_failure")
        self.assertEqual(result["exit_code"], CLASSIFY_EXIT_INTERNAL_FAILURE)
        self.assertEqual(killed, [True])

    def test_final_record_without_trailing_newline_is_processed(self) -> None:
        stdout = _FakePipe([json.dumps({"type": "error", "status": 401}).encode()])
        killed = []

        result = supervise_stream(stdout, None, lambda: killed.append(True))

        self.assertEqual(result["outcome"], "auth_error")
        self.assertEqual(killed, [True])

    def test_utf8_split_across_reads_is_decoded(self) -> None:
        payload = json.dumps(
            {"type": "assistant_message", "message": "né"}, ensure_ascii=False
        ).encode("utf-8")
        split = payload.index(b"\xc3") + 1
        stdout = _FakePipe([payload[:split], payload[split:] + b"\n"])

        result = supervise_stream(stdout, None, lambda: None)

        self.assertEqual(result["outcome"], "clean")

    def test_benign_json_payloads_never_enter_persisted_evidence(self) -> None:
        secret_text = "PROPRIETARY_REPOSITORY_SOURCE"
        stdout = _FakePipe(
            [json.dumps({"type": "assistant_message", "message": secret_text}).encode() + b"\n"]
        )

        result = supervise_stream(stdout, None, lambda: None)

        self.assertNotIn(secret_text, json.dumps(result["excerpts"]))

    def test_oversized_unterminated_record_is_internal_failure(self) -> None:
        stdout = _FakePipe([b"x" * (MAX_RAW_RECORD_BYTES + 10)])
        killed = []

        result = supervise_stream(stdout, None, lambda: killed.append(True))

        self.assertEqual(result["outcome"], "internal_failure")
        self.assertEqual(killed, [True])

    def test_runaway_ceiling_kills_stream_that_never_goes_idle(self) -> None:
        """PER_ATTEMPT_CEILING is a TOTAL-runtime backstop: a byte-emitting
        child resets the idle clock forever, so only the ceiling can stop it."""
        import model_policy as mp

        line = (
            json.dumps({"type": "assistant_message", "message": "tick"}).encode()
            + b"\n"
        )
        stdout = _FakePipe([line] * 500)
        killed = []
        with mock.patch.object(mp.time, "monotonic", _SteppingClock(10.0)):
            result = supervise_stream(
                stdout,
                None,
                lambda: killed.append(True),
                idle_timeout_seconds=180,
                max_runtime_seconds=25,
            )

        self.assertEqual(result["outcome"], "runaway")
        self.assertEqual(result["exit_code"], mp.CLASSIFY_EXIT_RUNAWAY)
        self.assertEqual(killed, [True])

    def test_ceiling_wins_over_idle_when_both_deadlines_have_expired(self) -> None:
        """Deterministic tie-break: the runaway check runs before the idle
        check, so a pass that finds both expired reports the ceiling."""
        import model_policy as mp

        stdout = _FakePipe([b'{"type": "assistant_message", "message": "x"}\n'])
        killed = []
        with mock.patch.object(mp.time, "monotonic", _SteppingClock(10.0)):
            result = supervise_stream(
                stdout,
                None,
                lambda: killed.append(True),
                idle_timeout_seconds=5,
                max_runtime_seconds=5,
            )

        self.assertEqual(result["outcome"], "runaway")
        self.assertEqual(killed, [True])

    def test_unreached_ceiling_leaves_clean_stream_clean(self) -> None:
        import model_policy as mp

        stdout = _FakePipe(
            [json.dumps({"type": "assistant_message", "message": "ok"}).encode() + b"\n"]
        )
        killed = []
        with mock.patch.object(mp.time, "monotonic", _SteppingClock(10.0)):
            result = supervise_stream(
                stdout,
                None,
                lambda: killed.append(True),
                max_runtime_seconds=10_000,
            )

        self.assertEqual(result["outcome"], "clean")
        self.assertEqual(killed, [])


class AuthRecoveryDescriptorTests(unittest.TestCase):
    """Recovery must re-prove access on the same route AND the same selection."""

    def descriptor(self, **overrides) -> dict:
        params = {
            "provider": "quotio",
            "model": CODEX_MODEL,
            "effort": CODEX_EFFORT,
            "routing": {"base_url": "http://127.0.0.1:8317/v1"},
        }
        params.update(overrides)
        return build_descriptor(
            params["provider"], params["model"], params["effort"], params["routing"]
        )

    def test_recovery_smoke_on_a_different_model_is_rejected(self) -> None:
        result = apply_auth_recovery(
            self.descriptor(),
            self.descriptor(model="gpt-5.5"),
            "none",
            "oauth",
            "success",
        )

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "descriptor_mismatch")

    def test_recovery_smoke_at_a_different_effort_is_rejected(self) -> None:
        result = apply_auth_recovery(
            self.descriptor(),
            self.descriptor(effort="high"),
            "none",
            "oauth",
            "success",
        )

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "descriptor_mismatch")

    def test_malformed_descriptor_is_rejected_before_any_comparison(self) -> None:
        bad = self.descriptor()
        bad["api_key"] = "sk-live-should-never-be-here"

        result = apply_auth_recovery(bad, bad, "none", "oauth", "success")

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "invalid_descriptor")


class DescriptorTests(unittest.TestCase):
    """The descriptor is persisted to state: it must never carry credentials."""

    def routing(self, **overrides) -> dict:
        base = {
            "base_url": "http://127.0.0.1:8317/v1",
            "wire_api": "responses",
            "profile": "default",
            "codex_home": "/home/user/.codex",
            "routing_env": {"CODEX_PROFILE": "default"},
        }
        base.update(overrides)
        return base

    def test_descriptor_has_closed_schema(self) -> None:
        descriptor = build_descriptor("quotio", CODEX_MODEL, CODEX_EFFORT, self.routing())
        self.assertEqual(validate_descriptor(descriptor), [])
        self.assertNotIn("credential_source", descriptor)

    def test_unknown_policy_override_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_descriptor(
                "quotio",
                CODEX_MODEL,
                CODEX_EFFORT,
                self.routing(),
                policy_overrides={"api_key": "sk-live"},
            )

    def test_fingerprint_excludes_credential_bearing_url_material(self) -> None:
        hostile = self.routing(base_url="https://user:token@host/v1?key=SECRET")
        fingerprint = routing_fingerprint(hostile)
        sanitized = routing_fingerprint(self.routing(base_url="https://host/v1?[STRIPPED]"))
        self.assertEqual(fingerprint, sanitized)

    def test_fingerprint_drops_secret_shaped_routing_env(self) -> None:
        with_secret = self.routing(
            routing_env={"CODEX_PROFILE": "default", "OPENAI_API_KEY": "sk-live"}
        )
        self.assertEqual(routing_fingerprint(with_secret), routing_fingerprint(self.routing()))

    def test_same_provider_name_with_changed_endpoint_mismatches(self) -> None:
        frozen = build_descriptor("quotio", CODEX_MODEL, CODEX_EFFORT, self.routing())
        moved = build_descriptor(
            "quotio",
            CODEX_MODEL,
            CODEX_EFFORT,
            self.routing(base_url="http://127.0.0.1:9999/v1"),
        )

        result = verify_frozen_selection(CODEX_MODEL, frozen, live_catalog(), moved)

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "descriptor_mismatch")


class FrozenSelectionTests(unittest.TestCase):
    def descriptor(self) -> dict:
        return build_descriptor(
            "quotio",
            CODEX_MODEL,
            CODEX_EFFORT,
            {"base_url": "http://127.0.0.1:8317/v1", "wire_api": "responses"},
        )

    def test_newer_catalog_model_is_not_adopted_mid_run(self) -> None:
        catalog = live_catalog()
        catalog["models"].append(
            {"slug": "gpt-6.1-astra", "supported_reasoning_levels": [{"effort": CODEX_EFFORT}]}
        )
        descriptor = self.descriptor()

        result = verify_frozen_selection(CODEX_MODEL, descriptor, catalog, descriptor)

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["selection"]["selected_model"], CODEX_MODEL)
        self.assertEqual(result["selection"]["reason"], "frozen_selection")

    def test_frozen_model_removed_from_catalog_blocks(self) -> None:
        descriptor = self.descriptor()

        result = verify_frozen_selection(
            CODEX_MODEL, descriptor, live_catalog(include_astra=False), descriptor
        )

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "frozen_model_ineligible")
        self.assertEqual(result["next_action"], "start_new_workflow_entry_preflight")


class AuthRecoveryTests(unittest.TestCase):
    """`none -> oauth` must be able to clear a human:codex-login block."""

    def descriptor(self, base_url: str = "http://127.0.0.1:8317/v1") -> dict:
        return build_descriptor(
            "quotio", CODEX_MODEL, CODEX_EFFORT, {"base_url": base_url}
        )

    def test_login_recovery_on_unchanged_route_clears_the_block(self) -> None:
        descriptor = self.descriptor()

        result = apply_auth_recovery(descriptor, descriptor, "none", "oauth", "success")

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["next_action"], "clear_human_codex_login_block")
        self.assertEqual(result["credential_source"], "oauth")

    def test_recovery_requires_a_successful_smoke(self) -> None:
        descriptor = self.descriptor()

        result = apply_auth_recovery(
            descriptor, descriptor, "none", "oauth", "authentication_error"
        )

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "authentication_error")

    def test_category_change_with_changed_routing_is_an_anomaly(self) -> None:
        result = apply_auth_recovery(
            self.descriptor(),
            self.descriptor("https://elsewhere.example/v1"),
            "none",
            "oauth",
            "success",
        )

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "descriptor_mismatch")


class BlockingBranchCoverageTests(unittest.TestCase):
    """Gate-blocking branches flagged as uncovered in review."""

    def test_codex_rejects_non_object_invocation(self) -> None:
        codex = valid_codex()
        codex["first_real_invocation"] = []
        result = evaluate_model_policy(request(codex=codex))["codex"]
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "invalid_invocation_observation")

    def test_codex_rejects_unknown_invocation_status(self) -> None:
        result = evaluate_model_policy(request(codex=valid_codex(status="teapot")))[
            "codex"
        ]
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "unknown_invocation_status")

    def test_401_boundary_through_the_stream_classifier(self) -> None:
        # r14 F13 re-eval: the boundary cases run through the REAL policy
        # path (classify_stream_event on diagnostic stderr), not a bare
        # regex probe — plus Unicode-adjacent forms.
        from model_policy import SOURCE_STDERR, classify_stream_event

        for text, expect_auth in (
            ("HTTP/1.1 401 Unauthorized", True),
            ("status 401", True),
            ("error: 401 unauthorized", True),
            # context keyword required by design — bare prose stays benign
            ("api returned 401", False),
            ("read timeout after 401ms", False),
            ("error: 401_foo retry queued", False),
            ("backoff 4011ms", False),
            ("latency\u00a0401ms", False),
            ("状态 401 未授权", False),
        ):
            verdict = classify_stream_event(SOURCE_STDERR, text)
            self.assertEqual(
                verdict == "auth_error", expect_auth, text
            )

    def test_internal_failure_requires_history_beyond_first_attempt(
        self,
    ) -> None:
        # r14 F8: a missing/malformed history restarted identical failures
        # at strike one forever — beyond the first attempt the explicit
        # list is required, mirroring the quota-streak contract.
        config = valid_codex(status="internal_failure", attempts=2)
        config["first_real_invocation"]["failure_signature"] = "boom"
        missing = evaluate_model_policy(request(codex=config))["codex"]
        self.assertEqual(missing["state"], "blocked")
        self.assertEqual(
            missing["reason_code"], "invalid_internal_failure_observation"
        )
        config2 = valid_codex(status="internal_failure", attempts=2)
        config2["first_real_invocation"]["failure_signature"] = "boom"
        config2["post_invocation"] = ["not a mapping"]
        malformed = evaluate_model_policy(request(codex=config2))["codex"]
        self.assertEqual(malformed["state"], "blocked")
        self.assertEqual(
            malformed["reason_code"], "invalid_internal_failure_observation"
        )
        # r14 F8 re-evaluation — (the reproduced CLI-level restart): `attempts`
        # DEFAULTS to 1 when omitted, so the attempts>1 guard alone never
        # fired for a caller omitting both fields — a defaulted attempts
        # with no history must block, while an EXPLICIT attempts=1 first
        # observation stays legal.
        config3 = valid_codex(status="internal_failure")
        config3["first_real_invocation"]["failure_signature"] = "boom"
        config3["first_real_invocation"].pop("attempts", None)
        config3.pop("post_invocation", None)
        defaulted = evaluate_model_policy(request(codex=config3))["codex"]
        self.assertEqual(defaulted["state"], "blocked")
        self.assertEqual(
            defaulted["reason_code"], "invalid_internal_failure_observation"
        )
        config4 = valid_codex(status="internal_failure", attempts=1)
        config4["first_real_invocation"]["failure_signature"] = "boom"
        config4.pop("post_invocation", None)
        explicit_first = evaluate_model_policy(request(codex=config4))["codex"]
        self.assertNotEqual(
            explicit_first.get("reason_code"),
            "invalid_internal_failure_observation",
            "an explicit attempts=1 first observation must not block",
        )

    def test_internal_failure_signature_bound_finite_retry(self) -> None:
        # r13 F10: internal_failure now takes the finite signature-bound
        # branch the prose promised — strikes 1-2 retry the exact same
        # configuration, the third consecutive same-signature strike
        # blocks, a changed normalized signature resets the streak, and
        # liveness noise between strikes neither forms nor breaks it.
        def observe(history=None, signature="proc exit 1: boom"):
            config = valid_codex(status="internal_failure")
            config["first_real_invocation"]["failure_signature"] = signature
            if history is not None:
                config["post_invocation"] = history
            return evaluate_model_policy(request(codex=config))["codex"]

        first = observe()
        self.assertEqual(first["state"], "retry")
        self.assertEqual(first["reason_code"], "internal_failure")
        self.assertEqual(first["internal_failure"]["strike"], 1)

        prior = {"status": "internal_failure", "failure_signature": "proc exit 1: boom"}
        second = observe(history=[dict(prior)])
        self.assertEqual(second["state"], "retry")
        self.assertEqual(second["internal_failure"]["strike"], 2)

        third = observe(history=[dict(prior), dict(prior)])
        self.assertEqual(third["state"], "blocked")
        self.assertEqual(third["reason_code"], "internal_failure")
        self.assertIn("three", third["reason"])

        # Whitespace/case differences normalize to the SAME signature.
        variant = {
            "status": "internal_failure",
            "failure_signature": "  PROC   exit 1:   BOOM ",
        }
        normalized_third = observe(history=[dict(prior), dict(variant)])
        self.assertEqual(normalized_third["state"], "blocked")

        # A changed signature resets the streak.
        other = {"status": "internal_failure", "failure_signature": "segfault"}
        reset = observe(history=[dict(prior), dict(other)])
        self.assertEqual(reset["state"], "retry")
        self.assertEqual(reset["internal_failure"]["strike"], 1)

        # Liveness noise between strikes neither forms nor breaks it.
        noisy = observe(
            history=[dict(prior), {"status": "timeout"}, dict(prior)]
        )
        self.assertEqual(noisy["state"], "blocked")

    def test_waiver_without_named_fallback_blocks(self) -> None:
        for key, _, factory, access_field in LEGS:
            with self.subTest(leg=key):
                config = factory(
                    explicit_waiver=True, **{access_field: "unavailable"}
                )
                result = evaluate_model_policy(leg_request(key, config))[key]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], "named_fallback_required")

    def test_non_boolean_waiver_blocks(self) -> None:
        for key, _, factory, _ in LEGS:
            with self.subTest(leg=key):
                result = evaluate_model_policy(
                    leg_request(key, factory(explicit_waiver="yes"))
                )[key]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], "invalid_waiver_value")

    def test_non_mapping_host_capabilities_blocks(self) -> None:
        for key, _, factory, _ in LEGS:
            with self.subTest(leg=key):
                result = evaluate_model_policy(
                    leg_request(key, factory(host_capabilities="broken"))
                )[key]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], "invalid_host_capabilities")

    def test_incidental_401_is_not_an_auth_signature(self) -> None:
        from model_policy import _has_auth_signature

        for text in (
            "read timeout after 401ms",
            "backoff 401ms then retry",
            "received 401 bytes on stream",
            "unauthorized_count=0",
        ):
            with self.subTest(text=text):
                self.assertFalse(_has_auth_signature(text))

    def test_incorrect_api_key_diagnostic_is_auth(self) -> None:
        from model_policy import _has_auth_signature

        self.assertTrue(
            _has_auth_signature("incorrect api key provided: sk-proj-abc...xyz")
        )

    def test_response_failed_nested_error_is_auth_scoped(self) -> None:
        from model_policy import _auth_scope_text

        scoped = _auth_scope_text(
            {
                "type": "response.failed",
                "response": {
                    "status": 401,
                    "error": {"message": "Incorrect API key provided"},
                },
            }
        )
        self.assertIn("status=401", scoped)
        self.assertIn("Incorrect API key provided", scoped)

    def test_frozen_verification_rejects_below_floor_and_excluded_models(self) -> None:
        from model_policy import _codex_model_is_eligible

        catalog = live_catalog()
        catalog["models"].extend(
            {
                "slug": slug,
                "supported_reasoning_levels": [
                    {"effort": CODEX_EFFORT},
                    {"effort": "ultra"},
                ],
            }
            for slug in ("gpt-5.9", "gpt-7-mini", "gpt-4o-mini")
        )
        for slug in ("gpt-5.9", "gpt-7-mini", "gpt-4o-mini"):
            with self.subTest(slug=slug):
                self.assertFalse(_codex_model_is_eligible(slug, catalog))
        self.assertTrue(_codex_model_is_eligible(CODEX_MODEL, catalog))

    def test_frozen_descriptor_must_pin_frozen_model_and_effort(self) -> None:
        descriptor = build_descriptor(
            "quotio",
            "gpt-4o-mini",
            "low",
            {"base_url": "http://127.0.0.1:8317/v1", "wire_api": "responses"},
        )
        result = verify_frozen_selection(
            CODEX_MODEL, descriptor, live_catalog(), descriptor
        )
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "descriptor_model_mismatch")

    def test_supervise_stream_with_no_channels_fails_closed(self) -> None:
        result = supervise_stream(None, None, lambda: None)
        self.assertEqual(result["outcome"], "internal_failure")

    def test_frozen_descriptor_rejects_contradicting_effort_override(self) -> None:
        descriptor = build_descriptor(
            "quotio",
            CODEX_MODEL,
            CODEX_EFFORT,
            {"base_url": "http://127.0.0.1:8317/v1", "wire_api": "responses"},
            policy_overrides={"model_reasoning_effort": "low"},
        )
        result = verify_frozen_selection(
            CODEX_MODEL, descriptor, live_catalog(), descriptor
        )
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "descriptor_model_mismatch")

    def test_contextual_401_is_an_auth_signature(self) -> None:
        from model_policy import _has_auth_signature

        for text in (
            "HTTP/1.1 401",
            "http 401 returned by provider",
            "status=401",
            "status code: 401",
            "error 401 from upstream",
            "401 Unauthorized",
        ):
            with self.subTest(text=text):
                self.assertTrue(_has_auth_signature(text))

    # The non-UTF-8 stdin CLI test lives in test_cli_fail_closed.py: the skill
    # scanner forbids pairing subprocess with eval-substring call names here.




def quota_request(
    *,
    reset: object,
    observed_at: object = "2026-08-04T12:00:00+00:00",
    history: object = "OMIT",
) -> dict:
    """Codex observation for the quota-wait bound contract (SCHEMA_VERSION 6).

    ``observed_at`` rides on the invocation record; ``post_invocation`` is the
    codex-level durable history feed.  ``history="OMIT"`` leaves the key out
    entirely — absence and an empty list are distinct tested cases.
    """

    codex = valid_codex(status="quota_exhausted", quota_reset_at=reset)
    if observed_at is not None:
        codex["first_real_invocation"]["observed_at"] = observed_at
    if history != "OMIT":
        codex["post_invocation"] = history
    return request(codex=codex)


def _parse_ts(value: str):
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class QuotaWaitBoundTests(unittest.TestCase):
    """R3-F2: the quota-with-reset wait is bounded IN CODE and the terminal
    no-usable-reset streak is decided by the helper from the fed records."""

    T = "2026-08-04T12:00:00+00:00"

    def _quota_record(self, *, reset: str, observed_at: object) -> dict:
        record = {"status": "quota_exhausted", "quota_reset_at": reset}
        if observed_at is not None:
            record["observed_at"] = observed_at
        return record

    def test_far_future_reset_is_clamped_to_the_single_wait_ceiling(self) -> None:
        result = evaluate_model_policy(
            quota_request(reset="2026-09-03T12:00:00+00:00", history=[])
        )["codex"]

        self.assertEqual(result["state"], "retry")
        quota = result.get("quota") or {}
        self.assertIn("wait_until", quota)
        self.assertEqual(
            _parse_ts(quota["wait_until"]),
            _parse_ts("2026-08-04T13:00:00+00:00"),  # observed_at + 3600s
        )
        self.assertIs(quota.get("clamped"), True)
        self.assertIs(quota.get("reset_elapsed"), False)
        self.assertEqual(quota.get("reset_at"), "2026-09-03T12:00:00+00:00")
        self.assertNotIn("wait_until_reset", quota)
        self.assertIn("clamp", result["reason"])

    def test_near_future_reset_floors_at_the_first_ladder_rung(self) -> None:
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T12:00:30+00:00", history=[])
        )["codex"]

        self.assertEqual(result["state"], "retry")
        quota = result.get("quota") or {}
        self.assertIn("wait_until", quota)
        self.assertEqual(
            _parse_ts(quota["wait_until"]),
            _parse_ts("2026-08-04T12:01:00+00:00"),  # observed_at + 60s floor
        )
        self.assertIs(quota.get("clamped"), False)

    def test_reset_exactly_at_the_ceiling_is_unclamped(self) -> None:
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T13:00:00+00:00", history=[])
        )["codex"]

        self.assertEqual(result["state"], "retry")
        quota = result.get("quota") or {}
        self.assertIn("wait_until", quota)
        self.assertEqual(
            _parse_ts(quota["wait_until"]), _parse_ts("2026-08-04T13:00:00+00:00")
        )
        self.assertIs(quota.get("clamped"), False)

    def test_reset_at_observed_time_is_elapsed_and_floors(self) -> None:
        result = evaluate_model_policy(
            quota_request(reset=self.T, history=[])
        )["codex"]

        self.assertEqual(result["state"], "retry")
        quota = result.get("quota") or {}
        self.assertIs(quota.get("reset_elapsed"), True)
        self.assertIn("wait_until", quota)
        self.assertEqual(
            _parse_ts(quota["wait_until"]), _parse_ts("2026-08-04T12:01:00+00:00")
        )

    def test_missing_observed_at_blocks_as_malformed_observation(self) -> None:
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T13:00:00+00:00", observed_at=None, history=[])
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "invalid_quota_observation")
        self.assertEqual(result["next_action"], "correct_observation_input")

    def test_omitted_history_blocks_as_malformed_observation(self) -> None:
        """Absence and an empty list are distinct: a [] default would silently
        preserve the omission path that disables the streak terminal."""
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T13:00:00+00:00")  # history OMITTED
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "invalid_quota_observation")
        self.assertEqual(result["next_action"], "correct_observation_input")

    def test_streak_terminal_survives_all_liveness_noise_statuses(self) -> None:
        history = [
            self._quota_record(
                reset="2026-08-04T10:00:00+00:00", observed_at="2026-08-04T11:00:00+00:00"
            ),
            {"status": "timeout", "observed_at": "2026-08-04T11:10:00+00:00"},
            {"status": "transport_error", "observed_at": "2026-08-04T11:20:00+00:00"},
            {"status": "runaway", "observed_at": "2026-08-04T11:30:00+00:00"},
        ]
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T11:50:00+00:00", history=history)
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "quota_exhausted")
        self.assertEqual(
            result["next_action"], "wait_for_quota_reset_or_change_access"
        )
        self.assertIn("elapsed", result["reason"])

    def test_success_between_quota_events_breaks_the_streak(self) -> None:
        history = [
            self._quota_record(
                reset="2026-08-04T10:00:00+00:00", observed_at="2026-08-04T11:00:00+00:00"
            ),
            {"status": "success", "observed_at": "2026-08-04T11:30:00+00:00"},
        ]
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T11:50:00+00:00", history=history)
        )["codex"]

        self.assertEqual(result["state"], "retry")

    def test_prior_nonelapsed_reset_does_not_form_a_streak(self) -> None:
        """The prior record's elapsed-ness is judged at ITS OWN observed_at."""
        nonelapsed_prior = self._quota_record(
            reset="2026-08-04T11:30:00+00:00", observed_at="2026-08-04T11:00:00+00:00"
        )
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T11:50:00+00:00", history=[nonelapsed_prior])
        )["codex"]
        self.assertEqual(result["state"], "retry")

        elapsed_second = self._quota_record(
            reset="2026-08-04T11:50:00+00:00", observed_at="2026-08-04T11:55:00+00:00"
        )
        result = evaluate_model_policy(
            quota_request(
                reset="2026-08-04T11:56:00+00:00",
                history=[nonelapsed_prior, elapsed_second],
            )
        )["codex"]
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "quota_exhausted")

    def test_unjudgeable_prior_record_conservatively_breaks_the_streak(self) -> None:
        history = [self._quota_record(reset="2026-08-04T10:00:00+00:00", observed_at=None)]
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T11:50:00+00:00", history=history)
        )["codex"]

        self.assertEqual(result["state"], "retry")

    def test_structurally_malformed_history_entry_blocks(self) -> None:
        result = evaluate_model_policy(
            quota_request(reset="2026-08-04T13:00:00+00:00", history=["garbage"])
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["next_action"], "correct_observation_input")

    def test_far_future_observed_at_blocks_instead_of_crashing(self) -> None:
        """A parseable year-9999 observed_at must fail closed, not overflow
        inside the timedelta arithmetic and escape as a traceback."""
        result = evaluate_model_policy(
            quota_request(
                reset="9999-12-31T23:00:00+00:00",
                observed_at="9999-12-31T23:59:59+00:00",
                history=[],
            )
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "invalid_quota_observation")
        self.assertEqual(result["next_action"], "correct_observation_input")

    def test_schema_version_is_bumped_for_the_contract_changes(self) -> None:
        result = evaluate_model_policy(request())
        self.assertEqual(result["version"], 8)  # literal pin, not the constant


class MonitorChildInvocationTests(unittest.TestCase):
    """Owner-pinned child invocation pins: the argv tail and the prompt are
    single-source contracts — a drifted flag or dropped clause here is a
    silently different execution boundary."""

    def test_first_launch_argv_is_exactly_the_working_tail(self) -> None:
        self.assertEqual(
            monitor_child_arguments("claude-fable-5-1"),
            [
                "-p",
                "--model",
                "claude-fable-5-1",
                "--effort",
                "max",
                "--output-format",
                "stream-json",
                "--verbose",
                "--disable-slash-commands",
                "--no-chrome",
                # admin#1495 finding 3806647922: user-level settings only —
                # the checked-out PR's project settings are untrusted.
                "--setting-sources",
                "user",
            ],
        )

    def test_resume_argv_prepends_the_session(self) -> None:
        arguments = monitor_child_arguments("claude-fable-5-1", resume_id="sid-1")
        self.assertEqual(arguments[:2], ["--resume", "sid-1"])
        self.assertEqual(arguments[2:], monitor_child_arguments("claude-fable-5-1"))

    def test_working_tail_never_carries_the_read_only_clamp(self) -> None:
        arguments = monitor_child_arguments("claude-fable-5-1")
        self.assertNotIn("--permission-mode", arguments)
        self.assertNotIn("--allowedTools", arguments)
        # The child session must PERSIST — resume is the owner cache lineage.
        self.assertNotIn("--no-session-persistence", arguments)

    def test_child_prompt_carries_every_load_bearing_clause(self) -> None:
        prompt = monitor_child_prompt(
            "/skill", "/state.md", "/state.md.attempt-abc.md", "abc123", 7
        )
        for clause in (
            "EXACTLY ONE monitor iteration",
            "NEVER write /state.md itself",
            "/state.md.attempt-abc.md",
            "--monitor-digest",
            '"attempt_id": "abc123"',
            '"tick_ordinal": 7',
            "value-identical",
            "Loading Contract",
        ):
            self.assertIn(clause, prompt)

    def test_child_prompt_digest_command_survives_hostile_paths(self) -> None:
        # admin#1495 r12 F17: the digest command interpolated raw local
        # paths — a space split the argv, and quotes/dollars/semicolons/
        # newlines injected shell syntax. The shlex-joined command must
        # round-trip to the exact argv for every hostile shape.
        import shlex as _shlex

        for hostile in (
            "/tmp/dir with space",
            '/tmp/dir"quote',
            "/tmp/dir$dollar",
            "/tmp/dir;semi",
            "/tmp/dir\nnewline",
        ):
            with self.subTest(path=hostile):
                candidate = hostile + "/state.md.attempt-x.md"
                prompt = monitor_child_prompt(
                    hostile, hostile + "/state.md", candidate, "abc", 1
                )
                start = prompt.index("digest with: ") + len("digest with: ")
                end = prompt.index(" . Your final message")
                argv = _shlex.split(prompt[start:end])
                self.assertEqual(
                    argv,
                    [
                        "python3",
                        f"{hostile}/scripts/state_schema.py",
                        "--monitor-digest",
                        candidate,
                    ],
                )

    def test_slice_constants_fit_inside_the_attempt_ceiling(self) -> None:
        # Literal boundary pins: the slice must return before a parent's own
        # 2700s attempt ceiling with margin to spare.
        self.assertEqual(MONITOR_SLICE_BUDGET_SECONDS, 2400)
        self.assertEqual(MONITOR_SLICE_CLEANUP_MARGIN_SECONDS, 120)
        self.assertEqual(MONITOR_CHILD_MIN_VIABLE_SECONDS, 240)
        self.assertEqual(MONITOR_CHILD_IDLE_TIMEOUT_SECONDS, 180)
        # CR 3760684029: the old bound (ceiling + 300) contradicted the
        # comment above by permitting a 300s overrun of the parent's own
        # attempt ceiling — the slice plus its cleanup margin must fit
        # STRICTLY inside the ceiling.
        self.assertLess(
            MONITOR_SLICE_BUDGET_SECONDS + MONITOR_SLICE_CLEANUP_MARGIN_SECONDS,
            PER_ATTEMPT_CEILING_SECONDS,
        )

    def test_child_failure_limit_is_the_schema_constant(self) -> None:
        # The runner consumes the 3-strike limit through model_policy's
        # re-export (it may not import state_schema); drift here would let
        # the runner and schema disagree on the blocking threshold.
        #
        # R7 codex #16: neither assertion below can catch a re-export replaced
        # by an independent literal `3` — CPython interns small ints, so
        # `assertIs(3, 3)` is True and the value still equals 3. The real
        # anti-substitution guard is the validator source-pin
        # (`MONITOR_CHILD_FAILURE_LIMIT = state_schema.MONITOR_CHILD_FAILURE_LIMIT`
        # in REQUIRED_GATE_MARKERS), exercised by
        # test_missing_gate_marker_is_rejected_per_file_and_marker. These two
        # lines remain as a value-equality + same-object sanity check only.
        import state_schema

        import model_policy

        self.assertIs(
            model_policy.MONITOR_CHILD_FAILURE_LIMIT,
            state_schema.MONITOR_CHILD_FAILURE_LIMIT,
        )
        self.assertEqual(model_policy.MONITOR_CHILD_FAILURE_LIMIT, 3)


class MonitorOrchestratorBindingTests(unittest.TestCase):
    """Phase 6 session ownership: the binding consumes the documented
    persisted contract (resolved_conventions.model_runtime leg records),
    re-checks floors because state is untrusted, records continuity
    truthfully, and never re-selects, never invents a model, and never
    turns ownership into a new block."""

    @staticmethod
    def _runtime(
        reviewer_status: str = "ready",
        base_status: str = "ready",
        reviewer_model: str = "claude-fable-5-1",
        base_model: str = "claude-fable-5",
        base_write_verified: bool = True,
    ) -> dict:
        # The persisted shape from references/state-and-safety.md — each leg
        # carries model + gate_status (the binding re-checks every landed
        # model against its leg's own floor; waived substitutes land within
        # the fable/mythos lineage, so no cross-floor evidence exists). The base
        # leg's host_agent_selection_verified flag is the write-capability
        # prerequisite reviewer ownership consumes.
        return {
            "codex": {"model": "gpt-6-astra", "effort": "max", "gate_status": "ready"},
            "claude": {
                "model": base_model,
                "gate_status": base_status,
                "host_agent_selection_verified": base_write_verified,
            },
            "claude_reviewer": {
                "model": reviewer_model,
                "gate_status": reviewer_status,
            },
        }

    def test_ready_reviewer_owns_the_monitor_session(self) -> None:
        binding = monitor_orchestrator_binding(self._runtime())
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "reviewer")
        # Literal pin: the owner is the reviewer leg's recorded selection,
        # never a re-derived or invented slug.
        self.assertEqual(binding["model"], "claude-fable-5-1")
        self.assertEqual(binding["effort"], "max")
        self.assertEqual(binding["reason_code"], "orchestrator_on_reviewer")
        self.assertIsNone(binding["pending_owner"])

    def test_below_floor_reviewer_model_never_binds_reviewer(self) -> None:
        # State is untrusted: a hand-edited record naming a below-floor
        # model must not own the session even with gate_status "ready".
        binding = monitor_orchestrator_binding(
            self._runtime(reviewer_model="claude-haiku-3")
        )
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertEqual(binding["model"], "claude-fable-5")

    def test_below_floor_base_model_fails_closed(self) -> None:
        binding = monitor_orchestrator_binding(
            self._runtime(reviewer_status="degraded", base_model="claude-haiku-3")
        )
        self.assertEqual(binding["state"], "invalid")
        self.assertTrue(binding["errors"])

    def test_degraded_reviewer_keeps_the_monitor_on_base(self) -> None:
        binding = monitor_orchestrator_binding(self._runtime(reviewer_status="degraded"))
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertEqual(binding["model"], "claude-fable-5")
        self.assertEqual(binding["reason_code"], "orchestrator_on_base")
        self.assertIn("degraded", binding["reason"])

    def test_pending_and_blocked_reviewer_bind_base(self) -> None:
        for status in ("pending", "blocked"):
            with self.subTest(reviewer_status=status):
                binding = monitor_orchestrator_binding(
                    self._runtime(reviewer_status=status)
                )
                self.assertEqual(binding["state"], "bound")
                self.assertEqual(binding["lineage"], "base")

    def test_mythos_waived_base_still_hosts_the_monitor(self) -> None:
        # A base waiver substitutes within the fable/mythos lineage; the
        # landed mythos fallback passes the base leg's own floor re-check
        # directly, with the waiver retained as policy_decision evidence.
        # admin#1495 r17 F4: the leg record flows through the defined
        # transition (granted waiver + consumed fallback success) instead
        # of hand-supplying gate_status "ready".
        config = valid_base(
            fable_access="unavailable",
            explicit_waiver=True,
            waiver_fallback={
                "model": "claude-mythos-5",
                "effort": "max",
                "available": True,
                "explicitly_authorized": True,
                "execution_path": "explicit_cli",
            },
        )
        decision = evaluate_model_policy(leg_request("claude", config))["claude"]
        runtime = self._runtime(reviewer_status="blocked")
        runtime["claude"] = waiver_gate_resolution(
            decision,
            {
                "leg": "claude",
                "status": "success",
                "model": "claude-mythos-5",
                "effort": "max",
                "attempts": 1,
            },
        )
        binding = monitor_orchestrator_binding(runtime)
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertEqual(binding["model"], "claude-mythos-5")

    def test_hand_edited_off_lineage_or_below_floor_models_fail_closed(
        self,
    ) -> None:
        # R2 round-2 finding 3737466436, re-scoped for the single-lineage
        # policy: every landed model is re-checked against its leg's OWN
        # floor. A hand-edited record putting an Opus slug on the base leg
        # and a below-reviewer-floor fable on the reviewer leg leaves no
        # landed leg at all and fails closed — no waiver evidence can
        # legitimize an off-lineage or below-floor owner.
        binding = monitor_orchestrator_binding(
            self._runtime(
                base_model="claude-opus-5", reviewer_model="claude-fable-5"
            )
        )
        self.assertEqual(binding["state"], "invalid")

    def test_unverified_base_write_path_keeps_monitor_on_base(self) -> None:
        # R2 round-2 finding 3737466426, empirically verified: the doc
        # makes a confirmed write-capable base worker a prerequisite of
        # reviewer ownership, but the binder returned
        # orchestrator_on_reviewer for the ROUTINE unverified-host shape
        # (host_agent_selection_verified false — the field's initialized
        # default). The prose veto is now the binder's own answer.
        binding = monitor_orchestrator_binding(
            self._runtime(base_write_verified=False)
        )
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertEqual(binding["reason_code"], "orchestrator_on_base")
        self.assertIn("write", binding["reason"])

    def test_unverified_base_write_path_without_base_leg_fails_closed(
        self,
    ) -> None:
        # No compliant write path and no landed base owner: reviewer
        # ownership is forbidden by the doc's prerequisite and there is
        # nothing to fall back to — fail closed, never bind.
        binding = monitor_orchestrator_binding(
            self._runtime(base_status="blocked", base_write_verified=False)
        )
        self.assertEqual(binding["state"], "invalid")

    def test_missing_reviewer_leg_falls_back_to_base_with_reason(self) -> None:
        runtime = self._runtime()
        del runtime["claude_reviewer"]
        binding = monitor_orchestrator_binding(runtime)
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertIn("missing", binding["reason"])

    def test_no_landed_leg_fails_closed_as_invalid(self) -> None:
        binding = monitor_orchestrator_binding(
            self._runtime(reviewer_status="blocked", base_status="blocked")
        )
        self.assertEqual(binding["state"], "invalid")

    def test_ready_codex_leg_session_continues_monitoring(self) -> None:
        # admin#1495 r12 F1: the OpenAI entry's Phase 6 controller runs on
        # the CODEX leg's recorded selection — a gate-ready leg, not an
        # unrecorded model. It continues as orchestrator (never re-model a
        # live session) while Claude child ownership is retained: the
        # nominal Claude owner rides in pending_owner, exactly the target
        # the runner's recompute cross-checks.
        binding = monitor_orchestrator_binding(
            self._runtime(), session_model="gpt-6-astra"
        )
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "codex")
        self.assertEqual(binding["model"], "gpt-6-astra")
        self.assertEqual(binding["effort"], "max")
        self.assertEqual(binding["reason_code"], "orchestrator_continuity")
        self.assertEqual(binding["pending_owner"], "claude-fable-5-1")

    def test_unready_codex_leg_session_stays_unrecorded(self) -> None:
        runtime = self._runtime()
        runtime["codex"]["gate_status"] = "blocked"
        binding = monitor_orchestrator_binding(
            runtime, session_model="gpt-6-astra"
        )
        self.assertEqual(binding["state"], "invalid")

    def test_codex_session_model_mismatch_fails_closed(self) -> None:
        # Only the EXACT recorded selection continues — a different GPT
        # slug (newer, older, variant) matches no landed leg.
        binding = monitor_orchestrator_binding(
            self._runtime(), session_model="gpt-6.1-astra"
        )
        self.assertEqual(binding["state"], "invalid")

    def test_below_floor_codex_record_never_continues(self) -> None:
        # State is untrusted: a hand-edited codex leg naming an excluded
        # down-tier variant must not continue even at gate_status ready.
        runtime = self._runtime()
        runtime["codex"]["model"] = "gpt-6-astra-mini"
        binding = monitor_orchestrator_binding(
            runtime, session_model="gpt-6-astra-mini"
        )
        self.assertEqual(binding["state"], "invalid")

    def test_codex_controller_requires_verified_base_write_path(self) -> None:
        # Unlike a live Claude reviewer-leg session (which demotes to inline
        # base-lineage work), a Codex session cannot take the Claude base
        # role — without a host-verified base worker path there is no
        # compliant write actor, so the binding fails closed.
        binding = monitor_orchestrator_binding(
            self._runtime(base_write_verified=False),
            session_model="gpt-6-astra",
        )
        self.assertEqual(binding["state"], "invalid")
        self.assertTrue(
            any("write" in error for error in binding["errors"]),
            binding["errors"],
        )
        self.assertTrue(binding["errors"])

    def test_malformed_runtime_fails_closed(self) -> None:
        for garbage in (None, [], "ready", 7):
            with self.subTest(garbage=garbage):
                binding = monitor_orchestrator_binding(garbage)
                self.assertEqual(binding["state"], "invalid")

    def test_empty_model_string_never_binds(self) -> None:
        binding = monitor_orchestrator_binding(
            self._runtime(reviewer_model="", base_model="")
        )
        self.assertEqual(binding["state"], "invalid")

    def test_base_session_gets_truthful_continuity_binding(self) -> None:
        # The F5 case: a base working session reaches Phase 6 while the
        # reviewer leg is ready. The record must stay truthful (base owns
        # THIS session) and carry the nominal owner for the next boundary.
        binding = monitor_orchestrator_binding(
            self._runtime(), session_model="claude-fable-5"
        )
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertEqual(binding["model"], "claude-fable-5")
        self.assertEqual(binding["reason_code"], "orchestrator_continuity")
        self.assertEqual(binding["pending_owner"], "claude-fable-5-1")

    def test_owner_session_binds_nominally_with_session_model(self) -> None:
        binding = monitor_orchestrator_binding(
            self._runtime(), session_model="claude-fable-5-1"
        )
        self.assertEqual(binding["reason_code"], "orchestrator_on_reviewer")
        self.assertIsNone(binding["pending_owner"])

    def test_unrecorded_session_model_fails_closed(self) -> None:
        binding = monitor_orchestrator_binding(
            self._runtime(), session_model="claude-sonnet-5"
        )
        self.assertEqual(binding["state"], "invalid")
        self.assertTrue(binding["errors"])

    def test_live_reviewer_session_without_write_path_binds_base_lineage(
        self,
    ) -> None:
        # CR 3760683975 (keeper-agents#1328), verified against the binder:
        # with host_agent_selection_verified false, a LIVE session on the
        # reviewer model still received a reviewer-lineage continuity
        # binding — but that lineage's capability boundary dispatches all
        # fix work to base workers, which is exactly the unverified path.
        # The truthful record keeps the live model and binds under the base
        # lineage's unrestricted inline capability set, mirroring the
        # nominal no-write-path demotion.
        binding = monitor_orchestrator_binding(
            self._runtime(base_write_verified=False),
            session_model="claude-fable-5-1",
        )
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertEqual(binding["model"], "claude-fable-5-1")
        self.assertEqual(binding["reason_code"], "orchestrator_continuity")
        self.assertEqual(binding["pending_owner"], "claude-fable-5")


class WaiverGateResolutionTests(unittest.TestCase):
    """admin#1495 r17 F4: ``waived`` is a pre-invocation policy decision, and
    waiver_gate_resolution is the one transition that lands the persisted
    gate from the named fallback's real invocation - ready only on the exact
    route's success, blocked on failure or garbage, with the waiver retained
    as policy_decision evidence. The matrix below pins decision ->
    invocation -> state validation -> monitor binding for both Claude legs."""

    @staticmethod
    def _waived_decision(leg: str) -> dict:
        if leg == "claude":
            config = valid_base(fable_access="unavailable")
            fallback_model = "claude-mythos-5"
        else:
            config = valid_reviewer(
                fable_access="unavailable",
                observed_models=["claude-fable-5-1", "claude-fable-6"],
            )
            fallback_model = "claude-fable-6"
        config["explicit_waiver"] = True
        config["waiver_fallback"] = {
            "model": fallback_model,
            "effort": "max",
            "available": True,
            "explicitly_authorized": True,
            "execution_path": "explicit_cli",
        }
        return evaluate_model_policy(leg_request(leg, config))[leg]

    @staticmethod
    def _observation(leg: str, model: str, status: str = "success") -> dict:
        return {
            "leg": leg,
            "status": status,
            "model": model,
            "effort": "max",
            "attempts": 1,
        }

    def test_downgraded_effort_decision_fails_closed_on_rehydration(self) -> None:
        # mm#3551 dawid-r8 F8: the decision round-trips through persisted
        # state, and any-non-empty effort let a hand-edited effort:"low"
        # decision with a matching observation land ready - contradicting
        # the max-effort waiver contract. Rehydration now re-verifies the
        # canonical effort exactly like the model floor.
        from model_policy import waiver_gate_resolution

        for leg in ("claude", "claude_reviewer"):
            with self.subTest(leg=leg):
                decision = dict(self._waived_decision(leg))
                decision["effort"] = "low"
                observation = self._observation(
                    leg, decision["model"]
                )
                observation["effort"] = "low"
                result = waiver_gate_resolution(decision, observation)
                self.assertEqual(result.get("state"), "invalid", result)
                self.assertTrue(
                    any(
                        "max effort only" in error
                        for error in result.get("errors", [])
                    ),
                    result,
                )

    @staticmethod
    def _runtime_with(leg: str, record: dict) -> dict:
        # The persisted-contract shape MonitorOrchestratorBindingTests
        # consumes, with the transitioned record standing in for its leg.
        runtime = {
            "codex": {
                "model": "gpt-6-astra",
                "effort": "max",
                "gate_status": "ready",
            },
            "claude": {
                "model": "claude-fable-5",
                "gate_status": "ready",
                "host_agent_selection_verified": True,
            },
            "claude_reviewer": {
                "model": "claude-fable-5-1",
                "gate_status": "blocked",
            },
        }
        runtime[leg] = record
        return runtime

    def test_base_fallback_success_lands_ready_validates_and_binds(
        self,
    ) -> None:
        from state_schema import validate_model_runtime_shape

        decision = self._waived_decision("claude")
        observation = self._observation("claude", "claude-mythos-5")
        record = waiver_gate_resolution(decision, observation)
        self.assertEqual(record["gate_status"], "ready")
        self.assertEqual(record["model"], "claude-mythos-5")
        self.assertEqual(record["effort"], "max")
        # The waiver decision is retained as evidence, never discarded.
        self.assertEqual(record["policy_decision"]["waiver"], decision)
        self.assertEqual(
            record["policy_decision"]["fallback_invocation"], observation
        )
        # The substitute-selection evidence retained by the transition.
        self.assertEqual(
            record["policy_decision"]["selection"]["selected_model"],
            "claude-mythos-5",
        )
        self.assertEqual(
            record["policy_decision"]["resolution"]["reason_code"],
            "waived_fallback_ready",
        )
        runtime = self._runtime_with("claude", record)
        self.assertEqual(validate_model_runtime_shape(runtime), [])
        binding = monitor_orchestrator_binding(runtime)
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertEqual(binding["model"], "claude-mythos-5")

    def test_base_fallback_failure_lands_blocked_and_never_binds(self) -> None:
        from state_schema import validate_model_runtime_shape

        decision = self._waived_decision("claude")
        record = waiver_gate_resolution(
            decision,
            self._observation("claude", "claude-mythos-5", status="error"),
        )
        self.assertEqual(record["gate_status"], "blocked")
        resolution = record["policy_decision"]["resolution"]
        self.assertEqual(
            resolution["reason_code"], "waived_fallback_invocation_failed"
        )
        # The failure reason records the observed status verbatim.
        self.assertIn("error", resolution["reason"])
        self.assertEqual(record["policy_decision"]["waiver"], decision)
        runtime = self._runtime_with("claude", record)
        self.assertEqual(validate_model_runtime_shape(runtime), [])
        # No landed Claude leg remains (the reviewer leg is blocked too):
        # the monitor binder fails closed on the blocked record.
        self.assertEqual(
            monitor_orchestrator_binding(runtime)["state"], "invalid"
        )

    def test_reviewer_fallback_success_lands_ready_validates_and_binds(
        self,
    ) -> None:
        from state_schema import validate_model_runtime_shape

        decision = self._waived_decision("claude_reviewer")
        observation = self._observation("claude_reviewer", "claude-fable-6")
        record = waiver_gate_resolution(decision, observation)
        self.assertEqual(record["gate_status"], "ready")
        self.assertEqual(record["model"], "claude-fable-6")
        self.assertEqual(record["policy_decision"]["waiver"], decision)
        self.assertEqual(
            record["policy_decision"]["fallback_invocation"], observation
        )
        runtime = self._runtime_with("claude_reviewer", record)
        self.assertEqual(validate_model_runtime_shape(runtime), [])
        # The same-lineage collapse the waiver authorized: the transitioned
        # reviewer record satisfies the binder's gate_status == "ready"
        # requirement and owns the session on its recorded fallback.
        binding = monitor_orchestrator_binding(runtime)
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "reviewer")
        self.assertEqual(binding["model"], "claude-fable-6")

    def test_reviewer_fallback_failure_lands_blocked_monitor_on_base(
        self,
    ) -> None:
        from state_schema import validate_model_runtime_shape

        decision = self._waived_decision("claude_reviewer")
        record = waiver_gate_resolution(
            decision,
            self._observation(
                "claude_reviewer", "claude-fable-6", status="timeout"
            ),
        )
        self.assertEqual(record["gate_status"], "blocked")
        self.assertEqual(
            record["policy_decision"]["resolution"]["reason_code"],
            "waived_fallback_invocation_failed",
        )
        runtime = self._runtime_with("claude_reviewer", record)
        self.assertEqual(validate_model_runtime_shape(runtime), [])
        binding = monitor_orchestrator_binding(runtime)
        self.assertEqual(binding["state"], "bound")
        self.assertEqual(binding["lineage"], "base")
        self.assertEqual(binding["model"], "claude-fable-5")

    def test_success_for_a_different_model_never_lands_ready(self) -> None:
        # A success observation proves only the route it ran: a sibling
        # slug, another lineage-family member, or an Opus slug fails
        # closed as a mismatch, never opens the gate.
        decision = self._waived_decision("claude")
        for wrong_model in (
            "claude-mythos-5-1",
            "claude-fable-5",
            "claude-opus-5",
            "gpt-6-astra",
        ):
            with self.subTest(model=wrong_model):
                record = waiver_gate_resolution(
                    decision, self._observation("claude", wrong_model)
                )
                self.assertEqual(record["gate_status"], "blocked")
                self.assertEqual(
                    record["policy_decision"]["resolution"]["reason_code"],
                    "fallback_observation_mismatch",
                )

    def test_wrong_leg_effort_or_route_failure_is_a_mismatch(self) -> None:
        decision = self._waived_decision("claude")
        cross_leg = self._observation("claude_reviewer", "claude-mythos-5")
        wrong_effort = self._observation("claude", "claude-mythos-5")
        wrong_effort["effort"] = "xhigh"
        # Identity precedes outcome - a failure on another route is a
        # mismatch, never this waiver's recorded invocation failure.
        failed_elsewhere = self._observation(
            "claude", "claude-fable-5", status="error"
        )
        for observation in (cross_leg, wrong_effort, failed_elsewhere):
            with self.subTest(observation=observation):
                record = waiver_gate_resolution(decision, observation)
                self.assertEqual(record["gate_status"], "blocked")
                self.assertEqual(
                    record["policy_decision"]["resolution"]["reason_code"],
                    "fallback_observation_mismatch",
                )

    def test_malformed_observation_blocks_instead_of_raising(self) -> None:
        from state_schema import validate_model_runtime_shape

        decision = self._waived_decision("claude")
        missing_field = self._observation("claude", "claude-mythos-5")
        del missing_field["status"]
        for garbage in (None, [], "success", 7, {}, missing_field):
            with self.subTest(garbage=garbage):
                record = waiver_gate_resolution(decision, garbage)
                self.assertEqual(record["gate_status"], "blocked")
                resolution = record["policy_decision"]["resolution"]
                self.assertEqual(
                    resolution["reason_code"], "invalid_fallback_observation"
                )
                self.assertEqual(
                    resolution["next_action"], "correct_observation_input"
                )
        # Even a garbage-evidence record persists shape-clean: the evidence
        # rides inside free-form policy_decision.
        record = waiver_gate_resolution(decision, missing_field)
        self.assertEqual(
            validate_model_runtime_shape(self._runtime_with("claude", record)),
            [],
        )

    def test_unusable_decision_record_is_invalid_with_no_leg_record(
        self,
    ) -> None:
        # A non-waiver or garbage decision names no trustworthy leg to
        # write: the error shape carries NO gate mutation, and "state" is
        # not a leg key - persisting it by mistake fails shape validation
        # loudly instead of landing a forged gate.
        from state_schema import validate_model_runtime_shape

        ready = evaluate_model_policy(request())["claude"]
        for garbage in (None, [], "waived", 7, ready):
            with self.subTest(garbage=garbage):
                result = waiver_gate_resolution(
                    garbage, self._observation("claude", "claude-mythos-5")
                )
                self.assertEqual(result["state"], "invalid")
                self.assertTrue(result["errors"])
                self.assertNotIn("gate_status", result)
        self.assertTrue(
            validate_model_runtime_shape(
                {"claude": waiver_gate_resolution(None, None)}
            )
        )

    def test_hand_edited_below_floor_fallback_is_invalid(self) -> None:
        # The decision may round-trip through persisted state before its
        # invocation lands - state is untrusted, so the transition
        # re-checks the floor exactly as the monitor binder does.
        decision = self._waived_decision("claude")
        for key in ("model", "fallback_model"):
            decision[key] = "claude-haiku-3"
        decision["selection"] = dict(decision["selection"])
        decision["selection"]["selected_model"] = "claude-haiku-3"
        result = waiver_gate_resolution(
            decision, self._observation("claude", "claude-haiku-3")
        )
        self.assertEqual(result["state"], "invalid")
        self.assertTrue(any("floor" in error for error in result["errors"]))

    def test_inconsistent_decision_naming_two_models_is_invalid(self) -> None:
        decision = self._waived_decision("claude")
        decision["fallback_model"] = "claude-mythos-6"
        result = waiver_gate_resolution(
            decision, self._observation("claude", "claude-mythos-5")
        )
        self.assertEqual(result["state"], "invalid")


class AuthSignatureOffsetUnicodeTests(unittest.TestCase):
    """Pin that auth_signature_offset indexes the ORIGINAL string (pass-3 opus
    #9 / codex #7). The monitor_runner sticky excerpt anchors on this offset to
    retain what detection fired on; a ``text.lower()``-derived offset drifts
    when Unicode lowercasing changes length, which would push the excerpt past
    the marker it means to preserve."""

    def test_offset_indexes_original_text_under_length_changing_lowercasing(
        self,
    ) -> None:
        marker = "authentication_error"
        # U+0130 (dotted capital I) lowercases to TWO code points ("i" + a
        # combining dot), so a 50-char prefix becomes 100 chars under
        # str.lower(); an offset derived from the lowercased text would land 50
        # chars past the marker in the ORIGINAL string. The trailing space gives
        # the \b before the signature a non-word char to anchor on.
        prefix = "İ" * 50 + " "
        text = prefix + marker + ": credentials revoked"
        offset = auth_signature_offset(text)
        self.assertIsNotNone(offset)
        # text.index is the true original-string offset; a lowercased-text
        # implementation returns a larger, drifted value.
        self.assertEqual(offset, text.index(marker))
        self.assertTrue(
            text[offset:].startswith(marker), (offset, text[offset : offset + 24])
        )

    def test_bare_401_without_context_is_not_a_signature(self) -> None:
        # Companion guardrail: an incidental millisecond count must NOT read as
        # an auth marker — that would turn a retryable transient into a kill.
        self.assertIsNone(auth_signature_offset("read timeout after 401ms"))


class BoundedExcerptRedactionTests(unittest.TestCase):
    def test_bearer_header_is_redacted_in_code(self) -> None:
        # Finding 3806595004: prose deferral let the header survive — the
        # canonical patterns now run inside bounded_excerpt itself.
        excerpt = bounded_excerpt(
            "", "error: Authorization: Bearer abc123def456ghi789 rejected"
        )
        self.assertNotIn("abc123def456ghi789", excerpt)
        self.assertIn("[REDACTED: authorization_bearer_header]", excerpt)

    def test_password_assignment_is_redacted_in_code(self) -> None:
        excerpt = bounded_excerpt("", "DB_PASSWORD=prodsecret99")
        self.assertNotIn("prodsecret99", excerpt)

    def test_pii_email_phone_and_webhook_secret_are_redacted(self) -> None:
        # admin#1495 finding 3807823274: the probe showed email and phone
        # surviving the sanitizer verbatim; whsec_ was called out with them.
        excerpt = bounded_excerpt(
            "",
            "auth failed for jane.doe+qa@example.com, callback "
            "+14155550123 / (415) 555-0123, whsec_abcdefghijklmnop1234",
        )
        self.assertNotIn("jane.doe", excerpt)
        self.assertNotIn("4155550123", excerpt)
        self.assertNotIn("555-0123", excerpt)
        self.assertNotIn("abcdefghijklmnop1234", excerpt)
        self.assertIn("[REDACTED: email_address]", excerpt)
        self.assertIn("[REDACTED: phone_number]", excerpt)
        self.assertIn("[REDACTED: stripe_webhook_secret]", excerpt)
        # Anchoring guard: a bare numeric id has no separators and must
        # survive — over-matching ordinary integers would shred evidence.
        self.assertIn(
            "request 4155550123999 stayed",
            bounded_excerpt("", "request 4155550123999 stayed"),
        )


class SanitizePublicationChokepointTests(unittest.TestCase):
    def test_every_canonical_fixture_is_redacted(self) -> None:
        # admin#1495 finding 3813789220: the publication sanitizer is one
        # public chokepoint applying the COMPLETE canonical pattern list —
        # validated against validate_package's fixture samples so a new
        # pattern can never land without this path covering it.
        import model_policy
        import validate_package

        for kind, (
            _pattern,
            samples,
        ) in validate_package.REQUIRED_REDACTION_PATTERNS.items():
            for sample in samples:
                cleaned = model_policy.sanitize_for_publication(
                    f"prompt text {sample} more text"
                )
                # The security property is sample ABSENCE; the label may
                # be a sibling kind when patterns overlap (an sk-ant key
                # legitimately matches the earlier openai pattern).
                self.assertIn("[REDACTED:", cleaned, kind)
                self.assertNotIn(sample, cleaned, kind)

    # The subprocess test of the `--sanitize` CLI mode lives in
    # test_cli_fail_closed.py: the AI Skill Security Scan flags any single
    # file pairing subprocess with an eval-substring call name, and this
    # file imports evaluate_model_policy (see that file's docstring).


if __name__ == "__main__":
    unittest.main()
