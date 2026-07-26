from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from model_policy import (
    CLAUDE_EFFORT,
    CLAUDE_MODEL,
    CLAUDE_MODEL_ALIAS,
    CODEX_EFFORT,
    CODEX_MODEL,
    THIRD_VOICE_EFFORT,
    THIRD_VOICE_MODEL,
    THIRD_VOICE_MODEL_ALIAS,
    evaluate_model_policy,
    main,
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


if __name__ == "__main__":
    unittest.main()
