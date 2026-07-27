from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from model_policy import (
    BASE_EFFORT,
    BASE_MODEL,
    BASE_MODEL_ALIAS,
    CODEX_EFFORT,
    CODEX_MODEL,
    REVIEWER_EFFORT,
    REVIEWER_MODEL,
    REVIEWER_MODEL_ALIAS,
    evaluate_model_policy,
    main,
)


def live_catalog(*, include_sol: bool = True, include_required_effort: bool = True) -> dict:
    models = [
        {
            "slug": "gpt-5.5-codex",
            "supported_reasoning_levels": [{"effort": "xhigh"}],
        }
    ]
    if include_sol:
        # ultra stays present (delegation breadth) and xhigh stays present (a
        # sub-max depth tier): neither ever satisfies the required-effort gate.
        levels = [{"effort": "high"}, {"effort": "xhigh"}, {"effort": "ultra"}]
        if include_required_effort:
            levels.append({"effort": CODEX_EFFORT})
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
) -> dict:
    if attempts is None:
        attempts = 0 if status == "not_run" else 1
    return {
        "installed": True,
        "version": "codex-cli 0.144.0",
        "live_catalog": live_catalog(),
        "first_real_invocation": {"status": status, "attempts": attempts},
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
        "observed_models": ["claude-fable-5", "claude-opus-5"],
        "explicit_waiver": False,
    }
    config.update(overrides)
    return config


def valid_reviewer(**overrides: object) -> dict:
    config = {
        "installed": True,
        "version": "2.1.170 (Claude Code)",
        "opus_access": "available",
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
        "observed_models": ["claude-opus-5", "claude-fable-5"],
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
    ("claude_reviewer", "claude_reviewer", valid_reviewer, "opus_access"),
)


def leg_request(key: str, config: dict) -> dict:
    payload = request()
    payload[key] = config
    return payload


class ModelPolicyTest(unittest.TestCase):
    def test_ready_policy_pins_sol_max_fable_base_and_opus_reviewer(self) -> None:
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
        self.assertEqual(REVIEWER_MODEL_ALIAS, "opus")

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

    def test_codex_live_catalog_missing_sol_blocks(self) -> None:
        codex = valid_codex()
        codex["live_catalog"] = live_catalog(include_sol=False)

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

    def test_codex_success_after_attempt_cap_is_rejected(self) -> None:
        result = evaluate_model_policy(
            request(codex=valid_codex(status="success", attempts=3))
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "invalid_invocation_attempts")

    def test_codex_entitlement_denial_blocks_without_retry(self) -> None:
        result = evaluate_model_policy(
            request(codex=valid_codex(status="entitlement_denied"))
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "entitlement_denied")
        self.assertEqual(result["retry"]["remaining"], 0)

    def test_codex_quota_exhaustion_blocks_until_reset_or_access_change(self) -> None:
        result = evaluate_model_policy(
            request(codex=valid_codex(status="quota_exhausted"))
        )["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "quota_exhausted")
        self.assertEqual(result["next_action"], "wait_for_quota_reset_or_change_access")

    def test_retryable_failures_retry_once_with_no_downgrade(self) -> None:
        for failure in ("timeout", "transport_error"):
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

    def test_retryable_failures_block_after_the_single_retry(self) -> None:
        for failure in ("timeout", "transport_error"):
            with self.subTest(failure=failure):
                result = evaluate_model_policy(
                    request(codex=valid_codex(status=failure, attempts=2))
                )["codex"]

                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], f"{failure}_retry_exhausted")
                self.assertEqual(result["retry"]["remaining"], 0)

    def test_codex_never_downgrades_for_any_failure_matrix_row(self) -> None:
        cases = [
            {"installed": False},
            {**valid_codex(), "version": "0.143.0"},
            {**valid_codex(), "live_catalog": live_catalog(include_sol=False)},
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

    def test_claude_legs_missing_and_old_cli_block_pending_waiver(self) -> None:
        for key, _, factory, _ in LEGS:
            cases = (
                ({"installed": False}, "cli_missing"),
                (factory(version="2.1.169"), "cli_too_old"),
            )
            for config, reason_code in cases:
                with self.subTest(leg=key, reason_code=reason_code):
                    result = evaluate_model_policy(leg_request(key, config))[key]
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
                self.assertTrue(result["reason_code"].startswith("fable_"))

    def test_reviewer_opus_access_failures_block_pending_waiver(self) -> None:
        for access in (
            "unavailable",
            "entitlement_denied",
            "provider_policy_denied",
            "unknown",
        ):
            with self.subTest(access=access):
                result = evaluate_model_policy(
                    request(reviewer=valid_reviewer(opus_access=access))
                )["claude_reviewer"]
                self.assertEqual(result["state"], "blocked")
                self.assertTrue(result["waiver_required"])
                self.assertTrue(result["reason_code"].startswith("opus_"))

    def test_claude_zdr_failures_block_pending_waiver(self) -> None:
        for key, _, factory, _ in LEGS:
            for status in ("incompatible", "denied", "unknown"):
                with self.subTest(leg=key, status=status):
                    result = evaluate_model_policy(
                        leg_request(key, factory(zero_data_retention=status))
                    )[key]
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
            "model": "claude-opus-5",
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
        self.assertEqual(waived["model"], "claude-opus-5")
        self.assertEqual(waived["execution_path"], "explicit_cli")

    def test_reviewer_unavailability_can_only_continue_after_explicit_waiver(
        self,
    ) -> None:
        unavailable = valid_reviewer(opus_access="unavailable")
        blocked = evaluate_model_policy(request(reviewer=unavailable))[
            "claude_reviewer"
        ]

        unavailable["explicit_waiver"] = True
        unavailable["waiver_fallback"] = {
            "model": "claude-fable-5",
            "effort": "max",
            "available": True,
            "explicitly_authorized": True,
            "execution_path": "explicit_cli",
        }
        waived = evaluate_model_policy(request(reviewer=unavailable))[
            "claude_reviewer"
        ]

        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(
            blocked["next_action"], "request_explicit_waiver_or_restore_opus_access"
        )
        self.assertEqual(waived["state"], "waived")
        self.assertTrue(waived["waiver_granted"])
        self.assertEqual(waived["model"], "claude-fable-5")
        self.assertEqual(waived["execution_path"], "explicit_cli")

    def test_base_waiver_rejects_unobserved_or_malformed_fallback(self) -> None:
        for model in (
            "claude-opus-",
            "claude-opus-malicious",
            "claude-opus-foo/bar",
            # Fable is the base; naming its own lineage as the fallback is not
            # a substitute for restoring access to it.
            "claude-fable-5",
            "claude-fable-6",
            "claude-mythos-5",
            # A waiver may authorize the opus lineage; it may not authorize a
            # version below the Opus 5 floor. No path proposes a downgrade.
            "claude-opus-4-8",
            "claude-opus-4-5",
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

    def test_base_waiver_accepts_a_context_window_variant_of_the_opus_floor(
        self,
    ) -> None:
        base = valid_base(
            fable_access="unavailable",
            explicit_waiver=True,
            observed_models=["claude-fable-5", "claude-opus-5[1m]"],
            waiver_fallback={
                "model": "claude-opus-5[1m]",
                "effort": "max",
                "available": True,
                "explicitly_authorized": True,
                "execution_path": "explicit_cli",
            },
        )

        result = evaluate_model_policy(request(base=base))["claude"]

        self.assertEqual(result["state"], "waived")
        self.assertEqual(result["model"], "claude-opus-5[1m]")

    def test_reviewer_waiver_rejects_unobserved_or_malformed_fallback(self) -> None:
        for model in (
            "claude-fable-",
            "claude-fable-malicious",
            "claude-fable-foo/bar",
            # Opus is the reviewer; naming it as its own fallback is not a
            # substitute for restoring access to it.
            "claude-opus-4-8",
            "claude-opus-5",
            # A waiver may authorize a different lineage; it may not authorize a
            # version below a floor. No path in this module proposes a downgrade.
            "claude-fable-1",
            "claude-fable-4-5",
            "claude-mythos-4",
        ):
            with self.subTest(model=model):
                reviewer = valid_reviewer(
                    opus_access="unavailable",
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
            ("claude", "claude-opus-5"),
            ("claude_reviewer", "claude-fable-5"),
        ):
            with self.subTest(leg=key):
                factory = valid_base if key == "claude" else valid_reviewer
                access_field = (
                    "fable_access" if key == "claude" else "opus_access"
                )
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
        self.assertEqual(model_flag, "opus")

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
        # The bare floor, its alias, and its context-window variant all name the
        # same model version, so none of them is a conflicting override.
        for override in (None, "", "opus", "claude-opus-5", "claude-opus-5[1m]"):
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
        codex = self.codex_with(self.model("gpt-5.7", "high", CODEX_EFFORT))

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model"], "gpt-5.7")
        self.assertEqual(result["arguments"][:2], ["-m", "gpt-5.7"])
        self.assertEqual(result["selection"]["reason"], "newer_model_auto_selected")

    def test_newest_version_wins_and_sol_lineage_breaks_ties(self) -> None:
        codex = self.codex_with(
            self.model("gpt-5.7", CODEX_EFFORT),
            self.model("gpt-5.7-sol", CODEX_EFFORT),
            self.model("gpt-6", CODEX_EFFORT),
        )
        result = evaluate_model_policy(request(codex=codex))["codex"]
        self.assertEqual(result["model"], "gpt-6")

        codex = self.codex_with(
            self.model("gpt-5.7", CODEX_EFFORT),
            self.model("gpt-5.7-sol", CODEX_EFFORT),
        )
        result = evaluate_model_policy(request(codex=codex))["codex"]
        self.assertEqual(result["model"], "gpt-5.7-sol")

    def test_down_tier_variants_and_missing_required_effort_are_not_upgrades(self) -> None:
        codex = self.codex_with(
            self.model("gpt-6-mini", CODEX_EFFORT),
            self.model("gpt-6-nano", CODEX_EFFORT),
            # ultra adds delegation, not depth, and xhigh sits below max:
            # neither ever satisfies the required-effort gate.
            self.model("gpt-7", "high", "xhigh", "ultra"),
        )

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["model"], CODEX_MODEL)
        self.assertEqual(result["selection"]["reason"], "floor_model")

    def test_same_version_sibling_is_not_an_upgrade(self) -> None:
        codex = self.codex_with(self.model("gpt-5.6", CODEX_EFFORT))

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["model"], CODEX_MODEL)

    def test_catalog_without_any_eligible_model_still_blocks(self) -> None:
        codex = valid_codex()
        codex["live_catalog"] = {
            "models": [
                self.model("gpt-5.5", CODEX_EFFORT),
                self.model("gpt-6-mini", CODEX_EFFORT),
                self.model("gpt-7", "high", "xhigh", "ultra"),
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

    def test_newer_opus_is_auto_selected_for_the_reviewer(self) -> None:
        reviewer = valid_reviewer(
            observed_models=["claude-opus-5", "claude-opus-6", "claude-fable-5"]
        )

        result = evaluate_model_policy(request(reviewer=reviewer))["claude_reviewer"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model"], "claude-opus-6")
        self.assertIn("model=claude-opus-6", result["arguments"])
        self.assertEqual(result["selection"]["reason"], "newer_model_auto_selected")

    def test_newer_opus_advances_the_reviewer_not_the_base(self) -> None:
        """The two legs auto-forward independently along their own lineages."""

        result = evaluate_model_policy(
            request(
                base=valid_base(observed_models=["claude-fable-5", "claude-opus-6"]),
                reviewer=valid_reviewer(observed_models=["claude-opus-6"]),
            )
        )

        self.assertEqual(result["claude"]["model"], BASE_MODEL)
        self.assertEqual(result["claude"]["selection"]["reason"], "floor_model")
        self.assertEqual(result["claude_reviewer"]["model"], "claude-opus-6")
        self.assertEqual(
            result["claude_reviewer"]["selection"]["reason"],
            "newer_model_auto_selected",
        )

    def test_newer_fable_advances_the_base_not_the_reviewer(self) -> None:
        result = evaluate_model_policy(
            request(
                base=valid_base(observed_models=["claude-fable-6"]),
                reviewer=valid_reviewer(
                    observed_models=["claude-opus-5", "claude-fable-6"]
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

    def test_context_window_variant_is_the_same_version_not_an_upgrade(self) -> None:
        only_variant = evaluate_model_policy(
            request(reviewer=valid_reviewer(observed_models=["claude-opus-5[1m]"]))
        )["claude_reviewer"]

        self.assertEqual(only_variant["model"], "claude-opus-5[1m]")
        self.assertEqual(only_variant["selection"]["reason"], "floor_model_variant")

        # With both present the bare slug wins — the standard-cost default.
        both = evaluate_model_policy(
            request(
                reviewer=valid_reviewer(
                    observed_models=["claude-opus-5[1m]", "claude-opus-5"]
                )
            )
        )["claude_reviewer"]

        self.assertEqual(both["model"], REVIEWER_MODEL)
        self.assertEqual(both["selection"]["reason"], "floor_model")

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
            ["claude-fable-6"],
            ["claude-opus-4-8"],
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
    """All three legs gate: the base writes, both reviewers must be able to judge."""

    def test_blocked_base_blocks_the_workflow(self) -> None:
        result = evaluate_model_policy(
            request(base=valid_base(fable_access="unavailable"))
        )

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["claude"]["state"], "blocked")
        self.assertEqual(result["claude"]["reason_code"], "fable_unavailable")
        self.assertTrue(result["claude"]["waiver_required"])

    def test_blocked_reviewer_blocks_the_workflow(self) -> None:
        """An unavailable reviewer is a block, not a recorded degradation."""

        for field, value, reason_code in (
            ("opus_access", "unavailable", "opus_unavailable"),
            ("opus_access", "entitlement_denied", "opus_entitlement_denied"),
            ("zero_data_retention", "incompatible", "zdr_incompatible"),
            ("installed", False, "cli_missing"),
        ):
            with self.subTest(field=field, value=value):
                result = evaluate_model_policy(
                    request(reviewer=valid_reviewer(**{field: value}))
                )

                self.assertEqual(result["state"], "blocked")
                reviewer = result["claude_reviewer"]
                self.assertEqual(reviewer["state"], "blocked")
                self.assertEqual(reviewer["reason_code"], reason_code)
                self.assertTrue(reviewer["blocking"])

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
                "model": "claude-opus-5",
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


if __name__ == "__main__":
    unittest.main()
