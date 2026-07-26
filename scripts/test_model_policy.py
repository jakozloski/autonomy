from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

import os
import signal
import subprocess
import textwrap
import time

from model_policy import (
    CLASSIFY_EXIT_AUTH_ERROR,
    CLASSIFY_EXIT_CLEAN,
    CLASSIFY_EXIT_INTERNAL_FAILURE,
    CLAUDE_EFFORT,
    CLAUDE_MODEL,
    CLAUDE_MODEL_ALIAS,
    CODEX_EFFORT,
    CODEX_MODEL,
    apply_auth_recovery,
    bounded_excerpt,
    build_descriptor,
    classify_stream_event,
    MAX_EXCERPT_BYTES,
    MAX_RAW_RECORD_BYTES,
    SOURCE_STDERR,
    SOURCE_STDOUT_JSON,
    THIRD_VOICE_EFFORT,
    THIRD_VOICE_MODEL,
    THIRD_VOICE_MODEL_ALIAS,
    evaluate_model_policy,
    main,
    routing_fingerprint,
    strip_url_secrets,
    supervise_stream,
    validate_descriptor,
    verify_frozen_selection,
)


def live_catalog(*, include_sol: bool = True, include_xhigh: bool = True) -> dict:
    models = [
        {
            "slug": "gpt-5.5-codex",
            "supported_reasoning_levels": [{"effort": "xhigh"}],
        }
    ]
    if include_sol:
        # ultra stays present: the breadth mode never satisfies the xhigh gate.
        levels = [{"effort": "high"}, {"effort": "ultra"}]
        if include_xhigh:
            levels.append({"effort": "xhigh"})
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


def valid_claude(**overrides: object) -> dict:
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


def valid_third_voice(**overrides: object) -> dict:
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
        "observed_models": ["claude-fable-5"],
    }
    config.update(overrides)
    return config


def request(
    *,
    codex: dict | None = None,
    claude: dict | None = None,
    third_voice: dict | None = None,
) -> dict:
    return {
        "codex": codex if codex is not None else valid_codex(),
        "claude": claude if claude is not None else valid_claude(),
        "claude_third_voice": (
            third_voice if third_voice is not None else valid_third_voice()
        ),
    }


class ModelPolicyTest(unittest.TestCase):
    def test_ready_policy_pins_sol_xhigh_opus_max_and_fable_third_voice(self) -> None:
        result = evaluate_model_policy(request())

        self.assertEqual(result["state"], "ready")
        self.assertEqual(
            (result["codex"]["model"], result["codex"]["effort"]),
            (CODEX_MODEL, CODEX_EFFORT),
        )
        self.assertEqual(
            (result["claude"]["model"], result["claude"]["effort"]),
            (CLAUDE_MODEL, CLAUDE_EFFORT),
        )
        self.assertEqual(result["claude"]["execution_path"], "agent_tool")
        self.assertFalse(result["codex"]["downgrade_allowed"])
        self.assertIsNone(result["codex"]["fallback_model"])

        third = result["claude_third_voice"]
        self.assertEqual(
            (third["model"], third["effort"]),
            (THIRD_VOICE_MODEL, THIRD_VOICE_EFFORT),
        )
        self.assertEqual(third["state"], "ready")
        self.assertEqual(third["role"], "supplementary")
        self.assertFalse(third["blocking"])

    def test_primary_and_third_voice_are_different_models(self) -> None:
        """The escalation opinion must not be the model it is escalating from."""

        result = evaluate_model_policy(request())

        self.assertNotEqual(
            result["claude"]["model"], result["claude_third_voice"]["model"]
        )
        self.assertEqual(CLAUDE_MODEL_ALIAS, "opus")
        self.assertEqual(THIRD_VOICE_MODEL_ALIAS, "fable")

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
        claude = valid_claude(version="2.1.170-beta.1")

        result = evaluate_model_policy(request(codex=codex, claude=claude))

        self.assertEqual(result["codex"]["reason_code"], "cli_too_old")
        self.assertEqual(result["claude"]["reason_code"], "cli_too_old")

    def test_codex_live_catalog_missing_sol_blocks(self) -> None:
        codex = valid_codex()
        codex["live_catalog"] = live_catalog(include_sol=False)

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "live_catalog_missing_capability")

    def test_codex_live_catalog_missing_xhigh_blocks(self) -> None:
        codex = valid_codex()
        codex["live_catalog"] = live_catalog(include_xhigh=False)

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

    def test_claude_missing_and_old_cli_block_pending_waiver(self) -> None:
        cases = (
            ({"installed": False}, "cli_missing"),
            (valid_claude(version="2.1.169"), "cli_too_old"),
        )
        for claude, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = evaluate_model_policy(request(claude=claude))["claude"]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], reason_code)
                self.assertTrue(result["waiver_required"])

    def test_claude_malformed_install_facts_cannot_be_waived(self) -> None:
        for field, value, reason_code in (
            ("installed", [], "invalid_installed_status"),
            ("version", [], "invalid_version_value"),
        ):
            with self.subTest(field=field):
                claude = valid_claude(explicit_waiver=True)
                claude[field] = value
                result = evaluate_model_policy(request(claude=claude))["claude"]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], reason_code)
                self.assertFalse(result["waiver_granted"])

    def test_claude_opus_access_failures_block_pending_waiver(self) -> None:
        for access in (
            "unavailable",
            "entitlement_denied",
            "provider_policy_denied",
            "unknown",
        ):
            with self.subTest(access=access):
                result = evaluate_model_policy(
                    request(claude=valid_claude(opus_access=access))
                )["claude"]
                self.assertEqual(result["state"], "blocked")
                self.assertTrue(result["waiver_required"])

    def test_claude_zdr_failures_block_pending_waiver(self) -> None:
        for status in ("incompatible", "denied", "unknown"):
            with self.subTest(status=status):
                result = evaluate_model_policy(
                    request(claude=valid_claude(zero_data_retention=status))
                )["claude"]
                self.assertEqual(result["state"], "blocked")
                self.assertTrue(result["waiver_required"])
                self.assertIn(
                    result["reason_code"], {"zdr_incompatible", "zdr_unverified"}
                )

    def test_malformed_claude_gate_observations_block_without_waiver(self) -> None:
        cases = (
            {"opus_access": []},
            {"zero_data_retention": []},
            {"environment": []},
            {"environment": {"CLAUDE_CODE_SUBAGENT_MODEL": 123}},
        )
        for malformed in cases:
            with self.subTest(malformed=malformed):
                claude = valid_claude(explicit_waiver=True, **malformed)
                result = evaluate_model_policy(request(claude=claude))["claude"]
                self.assertEqual(result["state"], "blocked")
                self.assertFalse(result["waiver_granted"])
                self.assertEqual(result["next_action"], "correct_observation_input")

    def test_claude_unavailability_can_only_continue_after_explicit_waiver(
        self,
    ) -> None:
        unavailable = valid_claude(opus_access="unavailable")
        blocked = evaluate_model_policy(request(claude=unavailable))["claude"]

        unavailable["explicit_waiver"] = True
        unavailable["waiver_fallback"] = {
            "model": "claude-fable-5",
            "effort": "max",
            "available": True,
            "explicitly_authorized": True,
            "execution_path": "explicit_cli",
        }
        waived = evaluate_model_policy(request(claude=unavailable))["claude"]

        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(waived["state"], "waived")
        self.assertTrue(waived["waiver_granted"])
        self.assertEqual(waived["model"], "claude-fable-5")
        self.assertEqual(waived["execution_path"], "explicit_cli")

    def test_waiver_rejects_unobserved_or_malformed_fallback(self) -> None:
        for model in (
            "claude-fable-",
            "claude-fable-malicious",
            "claude-fable-foo/bar",
            # Opus is the primary now; naming it as its own fallback is not a
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
                claude = valid_claude(
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
                result = evaluate_model_policy(request(claude=claude))["claude"]
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["reason_code"], "invalid_named_fallback")

    def test_missing_claude_cli_cannot_select_explicit_fallback(self) -> None:
        claude = valid_claude(
            installed=False,
            opus_access="unavailable",
            explicit_waiver=True,
            waiver_fallback={
                "model": "claude-fable-5",
                "effort": "max",
                "available": True,
                "explicitly_authorized": True,
                "execution_path": "explicit_cli",
            },
        )

        result = evaluate_model_policy(request(claude=claude))["claude"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "invalid_named_fallback")

    def test_conflicting_subagent_model_selects_explicit_cli(self) -> None:
        claude = valid_claude(
            environment={"CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-4-6"}
        )

        result = evaluate_model_policy(request(claude=claude))["claude"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["reason_code"], "explicit_cli_required")
        self.assertEqual(result["execution_path"], "explicit_cli")
        self.assertEqual(
            result["arguments"],
            [
                "-p",
                "--model",
                "opus",
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

    def test_matching_opus_override_keeps_agent_tool_path(self) -> None:
        # The bare floor, its alias, and its context-window variant all name the
        # same model version, so none of them is a conflicting override.
        for override in (None, "", "opus", "claude-opus-5", "claude-opus-5[1m]"):
            with self.subTest(override=override):
                result = evaluate_model_policy(
                    request(
                        claude=valid_claude(
                            environment={"CLAUDE_CODE_SUBAGENT_MODEL": override}
                        )
                    )
                )["claude"]
                self.assertEqual(result["state"], "ready")
                self.assertEqual(result["execution_path"], "agent_tool")
                self.assertEqual(result["effort"], "max")

    def test_unverified_agent_host_uses_explicit_cli(self) -> None:
        claude = valid_claude(host_capabilities={})

        result = evaluate_model_policy(request(claude=claude))["claude"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["execution_path"], "explicit_cli")
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL", result["environment_unset"])

    def test_agent_host_without_read_only_enforcement_uses_explicit_cli(self) -> None:
        claude = valid_claude()
        claude["host_capabilities"]["agent_read_only_enforced"] = False

        result = evaluate_model_policy(request(claude=claude))["claude"]

        self.assertEqual(result["execution_path"], "explicit_cli")
        self.assertIn("--permission-mode", result["arguments"])
        self.assertIn("--allowedTools", result["arguments"])
        self.assertIn("--disallowedTools", result["arguments"])

    def test_effort_environment_override_uses_clean_explicit_cli(self) -> None:
        claude = valid_claude(
            environment={
                "CLAUDE_CODE_SUBAGENT_MODEL": None,
                "CLAUDE_CODE_EFFORT_LEVEL": "high",
            }
        )

        result = evaluate_model_policy(request(claude=claude))["claude"]

        self.assertEqual(result["execution_path"], "explicit_cli")
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL", result["environment_unset"])

    def test_case_or_whitespace_variant_override_uses_explicit_cli(self) -> None:
        for override in ("FABLE", " fable ", "Claude-Fable-5"):
            with self.subTest(override=override):
                result = evaluate_model_policy(
                    request(
                        claude=valid_claude(
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
        codex = self.codex_with(self.model("gpt-5.7", "high", "xhigh"))

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model"], "gpt-5.7")
        self.assertEqual(result["arguments"][:2], ["-m", "gpt-5.7"])
        self.assertEqual(result["selection"]["reason"], "newer_model_auto_selected")

    def test_newest_version_wins_and_sol_lineage_breaks_ties(self) -> None:
        codex = self.codex_with(
            self.model("gpt-5.7", "xhigh"),
            self.model("gpt-5.7-sol", "xhigh"),
            self.model("gpt-6", "xhigh"),
        )
        result = evaluate_model_policy(request(codex=codex))["codex"]
        self.assertEqual(result["model"], "gpt-6")

        codex = self.codex_with(
            self.model("gpt-5.7", "xhigh"),
            self.model("gpt-5.7-sol", "xhigh"),
        )
        result = evaluate_model_policy(request(codex=codex))["codex"]
        self.assertEqual(result["model"], "gpt-5.7-sol")

    def test_down_tier_variants_and_missing_xhigh_are_not_upgrades(self) -> None:
        codex = self.codex_with(
            self.model("gpt-6-mini", "xhigh"),
            self.model("gpt-6-nano", "xhigh"),
            # breadth-only sibling: ultra support never satisfies the xhigh gate
            self.model("gpt-7", "high", "ultra"),
        )

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["model"], CODEX_MODEL)
        self.assertEqual(result["selection"]["reason"], "floor_model")

    def test_same_version_sibling_is_not_an_upgrade(self) -> None:
        codex = self.codex_with(self.model("gpt-5.6", "xhigh"))

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["model"], CODEX_MODEL)

    def test_catalog_without_any_eligible_model_still_blocks(self) -> None:
        codex = valid_codex()
        codex["live_catalog"] = {
            "models": [
                self.model("gpt-5.5", "xhigh"),
                self.model("gpt-6-mini", "xhigh"),
                self.model("gpt-7", "high", "ultra"),
            ]
        }

        result = evaluate_model_policy(request(codex=codex))["codex"]

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "live_catalog_missing_capability")

    def test_newer_opus_is_auto_selected_for_agent_and_cli_paths(self) -> None:
        claude = valid_claude(
            observed_models=["claude-opus-5", "claude-opus-6", "claude-fable-5"]
        )
        result = evaluate_model_policy(request(claude=claude))["claude"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model"], "claude-opus-6")
        self.assertEqual(result["execution_path"], "agent_tool")
        self.assertIn("model=claude-opus-6", result["arguments"])
        self.assertEqual(result["selection"]["reason"], "newer_model_auto_selected")

        claude["host_capabilities"] = {}
        result = evaluate_model_policy(request(claude=claude))["claude"]

        self.assertEqual(result["execution_path"], "explicit_cli")
        model_flag = result["arguments"][result["arguments"].index("--model") + 1]
        self.assertEqual(model_flag, "claude-opus-6")

    def test_newer_fable_forwards_the_third_voice_not_the_primary(self) -> None:
        """The two legs auto-forward independently along their own lineages."""

        result = evaluate_model_policy(
            request(
                claude=valid_claude(observed_models=["claude-opus-5", "claude-fable-6"]),
                third_voice=valid_third_voice(observed_models=["claude-fable-6"]),
            )
        )

        self.assertEqual(result["claude"]["model"], CLAUDE_MODEL)
        self.assertEqual(result["claude"]["selection"]["reason"], "floor_model")
        self.assertEqual(result["claude_third_voice"]["model"], "claude-fable-6")
        self.assertEqual(
            result["claude_third_voice"]["selection"]["reason"],
            "newer_model_auto_selected",
        )

    def test_fable_family_preferred_over_mythos_on_version_tie(self) -> None:
        third = valid_third_voice(
            observed_models=["claude-mythos-6", "claude-fable-6"]
        )

        result = evaluate_model_policy(request(third_voice=third))

        self.assertEqual(result["claude_third_voice"]["model"], "claude-fable-6")

    def test_context_window_variant_is_the_same_version_not_an_upgrade(self) -> None:
        only_variant = evaluate_model_policy(
            request(claude=valid_claude(observed_models=["claude-opus-5[1m]"]))
        )["claude"]

        self.assertEqual(only_variant["model"], "claude-opus-5[1m]")
        self.assertEqual(only_variant["selection"]["reason"], "floor_model_variant")

        # With both present the bare slug wins — the standard-cost default.
        both = evaluate_model_policy(
            request(
                claude=valid_claude(
                    observed_models=["claude-opus-5[1m]", "claude-opus-5"]
                )
            )
        )["claude"]

        self.assertEqual(both["model"], CLAUDE_MODEL)
        self.assertEqual(both["selection"]["reason"], "floor_model")

    def test_floor_override_conflicts_when_newer_model_selected(self) -> None:
        claude = valid_claude(
            observed_models=["claude-opus-6"],
            environment={"CLAUDE_CODE_SUBAGENT_MODEL": "opus"},
        )

        result = evaluate_model_policy(request(claude=claude))["claude"]

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["execution_path"], "explicit_cli")

    def test_malformed_observed_models_fall_back_to_the_floor(self) -> None:
        for observed in (None, "claude-opus-6", [123, {}, "claude-haiku-4-5"]):
            with self.subTest(observed=observed):
                claude = valid_claude(observed_models=observed)
                result = evaluate_model_policy(request(claude=claude))["claude"]
                self.assertEqual(result["model"], CLAUDE_MODEL)
                self.assertEqual(result["selection"]["reason"], "floor_model")

    def test_down_tier_claude_models_are_never_selected(self) -> None:
        for observed in (["claude-opus-4-8"], ["claude-sonnet-5"], ["claude-haiku-4-5"]):
            with self.subTest(observed=observed):
                result = evaluate_model_policy(
                    request(claude=valid_claude(observed_models=observed))
                )["claude"]
                self.assertEqual(result["model"], CLAUDE_MODEL)
                self.assertEqual(result["selection"]["reason"], "floor_model")


class ThirdVoiceTest(unittest.TestCase):
    """The escalation voice is a supplement: it degrades, it never blocks."""

    def test_unavailable_third_voice_does_not_block_the_workflow(self) -> None:
        for field, value, reason_code in (
            ("fable_access", "unavailable", "fable_unavailable"),
            ("fable_access", "entitlement_denied", "fable_entitlement_denied"),
            ("zero_data_retention", "incompatible", "zdr_incompatible"),
            ("installed", False, "cli_missing"),
        ):
            with self.subTest(field=field, value=value):
                result = evaluate_model_policy(
                    request(third_voice=valid_third_voice(**{field: value}))
                )

                self.assertEqual(result["state"], "ready")
                third = result["claude_third_voice"]
                self.assertEqual(third["state"], "unavailable")
                self.assertEqual(third["reason_code"], reason_code)
                self.assertFalse(third["blocking"])
                self.assertEqual(third["next_action"], "continue_without_third_voice")

    def test_absent_third_voice_observation_is_reported_not_guessed(self) -> None:
        result = evaluate_model_policy(
            {"codex": valid_codex(), "claude": valid_claude()}
        )

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["claude_third_voice"]["state"], "unavailable")
        self.assertEqual(result["claude_third_voice"]["reason_code"], "not_observed")

    def test_third_voice_is_read_only_on_both_execution_paths(self) -> None:
        agent = evaluate_model_policy(request())["claude_third_voice"]
        self.assertEqual(agent["execution_path"], "agent_tool")
        self.assertTrue(agent["read_only"]["required"])

        cli = evaluate_model_policy(
            request(third_voice=valid_third_voice(host_capabilities={}))
        )["claude_third_voice"]
        self.assertEqual(cli["execution_path"], "explicit_cli")
        self.assertIn("--permission-mode", cli["arguments"])
        self.assertIn("plan", cli["arguments"])
        for denied in ("Edit", "Write", "Bash"):
            self.assertIn(denied, cli["arguments"][cli["arguments"].index("--disallowedTools") + 1])

    def test_opus_does_not_forward_the_third_voice(self) -> None:
        """The third voice stays on its own lineage even when Opus is newer."""

        for observed in (
            ["claude-opus-6"],
            ["claude-opus-5", "claude-opus-6"],
            ["claude-sonnet-5"],
            ["claude-fable-4-5"],
            ["claude-haiku-4-5"],
        ):
            with self.subTest(observed=observed):
                result = evaluate_model_policy(
                    request(third_voice=valid_third_voice(observed_models=observed))
                )["claude_third_voice"]
                self.assertEqual(result["model"], THIRD_VOICE_MODEL)
                self.assertEqual(result["selection"]["reason"], "floor_model")

    def test_third_voice_never_reports_a_blocked_state(self) -> None:
        for third in (
            valid_third_voice(installed=False),
            valid_third_voice(version="0.0.1"),
            valid_third_voice(fable_access=[]),
            valid_third_voice(zero_data_retention=[]),
            # Unhashable env overrides must not raise out of the whole gate: a
            # traceback is strictly worse than a decision, for every leg.
            valid_third_voice(environment={"CLAUDE_CODE_EFFORT_LEVEL": []}),
            valid_third_voice(environment={"CLAUDE_CODE_SUBAGENT_MODEL": []}),
            valid_third_voice(environment={"CLAUDE_CODE_EFFORT_LEVEL": {}}),
            None,
        ):
            with self.subTest(third=third):
                result = evaluate_model_policy(
                    {
                        "codex": valid_codex(),
                        "claude": valid_claude(),
                        "claude_third_voice": third,
                    }
                )
                self.assertIn(
                    result["claude_third_voice"]["state"], {"ready", "unavailable"}
                )
                self.assertEqual(result["state"], "ready")


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


class SuperviseStreamLiveProcessTests(unittest.TestCase):
    """Integration: the real API against a real child process.

    Covers the deadlock case the reviewer flagged — one channel flooded past
    kernel pipe capacity while the auth event arrives on the other.
    """

    def test_flooding_one_channel_does_not_prevent_prompt_termination(self) -> None:
        script = textwrap.dedent(
            """
            import json, sys, time
            # Flood stderr well past a pipe buffer, then emit the auth event on
            # stdout. A sequential reader would deadlock here.
            sys.stderr.write("flood line\\n" * 40000)
            sys.stderr.flush()
            sys.stdout.write(json.dumps({"type": "error", "status": 401}) + "\\n")
            sys.stdout.flush()
            time.sleep(120)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def kill_group() -> None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        started = time.monotonic()
        try:
            result = supervise_stream(process.stdout, process.stderr, kill_group)
            elapsed = time.monotonic() - started

            self.assertEqual(result["outcome"], "auth_error")
            self.assertLess(elapsed, 30, "supervisor deadlocked instead of terminating")
            self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                kill_group()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


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
            {"slug": "gpt-5.7-sol", "supported_reasoning_levels": [{"effort": CODEX_EFFORT}]}
        )
        descriptor = self.descriptor()

        result = verify_frozen_selection(CODEX_MODEL, descriptor, catalog, descriptor)

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["selection"]["selected_model"], CODEX_MODEL)
        self.assertEqual(result["selection"]["reason"], "frozen_selection")

    def test_frozen_model_removed_from_catalog_blocks(self) -> None:
        descriptor = self.descriptor()

        result = verify_frozen_selection(
            CODEX_MODEL, descriptor, live_catalog(include_sol=False), descriptor
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


if __name__ == "__main__":
    unittest.main()
