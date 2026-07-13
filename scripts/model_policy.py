#!/usr/bin/env python3
"""Evaluate the mandatory model policy without making remote calls.

The command reads one JSON object from stdin and writes one JSON object to
stdout.  Every field in the input is observed by the caller; this module never
looks at the process environment, executes a vendor CLI, or uses the network.

Expected input shape::

    {
      "codex": {
        "installed": true,
        "version": "codex-cli 0.144.0",
        "live_catalog": {
          "models": [{
            "slug": "gpt-5.6-sol",
            "supported_reasoning_levels": [{"effort": "ultra"}]
          }]
        },
        "first_real_invocation": {"status": "success", "attempts": 1}
      },
      "claude": {
        "installed": true,
        "version": "2.1.170 (Claude Code)",
        "fable_access": "available",
        "zero_data_retention": "compatible",
        "environment": {
          "CLAUDE_CODE_SUBAGENT_MODEL": null,
          "CLAUDE_CODE_EFFORT_LEVEL": null
        },
        "host_capabilities": {
          "agent_model_selection": false,
          "agent_effort_selection": false,
          "agent_read_only_enforced": false
        },
        "explicit_waiver": false
      }
    }

The live catalog is a preflight signal.  The first real Codex invocation is
the authoritative entitlement/quota signal.  A timeout or transport failure
may retry once with the exact same model and effort; every other Codex failure
blocks, and no path proposes a downgrade.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any


SCHEMA_VERSION = 1

CODEX_MODEL = "gpt-5.6-sol"
CODEX_EFFORT = "ultra"
MIN_CODEX_VERSION = (0, 144, 0)
CODEX_MAX_ATTEMPTS = 2

CLAUDE_MODEL = "claude-fable-5"
CLAUDE_MODEL_ALIAS = "fable"
CLAUDE_EFFORT = "max"
MIN_CLAUDE_VERSION = (2, 1, 170)
CLAUDE_READ_ONLY_ALLOWED_TOOLS = ("Read", "Glob", "Grep")
CLAUDE_READ_ONLY_DENIED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash",
    "WebFetch",
    "WebSearch",
    "Agent",
    "Task",
)
CLAUDE_READ_ONLY_ENV_UNSET = (
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_PERMISSION_MODE",
)

_SEMVER = re.compile(
    r"(?<!\d)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
)

_CODEX_BLOCKING_FAILURES = {
    "entitlement_denied": (
        "entitlement_denied",
        "GPT-5.6 Sol entitlement was denied by the first real invocation",
        "request_access",
    ),
    "quota_exhausted": (
        "quota_exhausted",
        "GPT-5.6 Sol usage quota is exhausted",
        "wait_for_quota_reset_or_change_access",
    ),
    "model_unavailable": (
        "model_unavailable",
        "GPT-5.6 Sol was unavailable to the first real invocation",
        "request_access",
    ),
    "authentication_error": (
        "authentication_error",
        "Codex authentication failed during the first real invocation",
        "repair_authentication",
    ),
    "error": (
        "invocation_error",
        "The first real GPT-5.6 Sol invocation failed",
        "inspect_error_and_block",
    ),
}

_CODEX_RETRYABLE_FAILURES = {"timeout", "transport_error"}

_CLAUDE_ACCESS_FAILURES = {
    False: (
        "fable_unavailable",
        "Claude Fable 5 is unavailable",
    ),
    "unavailable": (
        "fable_unavailable",
        "Claude Fable 5 is unavailable",
    ),
    "entitlement_denied": (
        "fable_entitlement_denied",
        "Claude Fable 5 entitlement was denied",
    ),
    "provider_policy_denied": (
        "fable_provider_policy_denied",
        "Provider policy does not permit Claude Fable 5",
    ),
    "unknown": (
        "fable_access_unverified",
        "Claude Fable 5 access has not been verified",
    ),
}

_CLAUDE_ZDR_FAILURES = {
    False: (
        "zdr_incompatible",
        "Claude Fable 5 does not satisfy the required zero-data-retention policy",
    ),
    "incompatible": (
        "zdr_incompatible",
        "Claude Fable 5 does not satisfy the required zero-data-retention policy",
    ),
    "denied": (
        "zdr_incompatible",
        "Claude Fable 5 does not satisfy the required zero-data-retention policy",
    ),
    "unknown": (
        "zdr_unverified",
        "Claude Fable 5 zero-data-retention compatibility is unverified",
    ),
}


def _semver(value: Any) -> tuple[tuple[int, int, int], bool] | None:
    """Return the numeric core and whether the observed version is prerelease."""

    if not isinstance(value, str):
        return None
    match = _SEMVER.search(value)
    if match is None:
        return None
    core = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    return core, match.group("prerelease") is not None


def _version_at_least(value: Any, minimum: tuple[int, int, int]) -> bool:
    parsed = _semver(value)
    if parsed is None:
        return False
    core, is_prerelease = parsed
    if core != minimum:
        return core > minimum
    return not is_prerelease


def _codex_base(version: Any) -> dict[str, Any]:
    return {
        "state": "blocked",
        "reason_code": None,
        "reason": None,
        "model": CODEX_MODEL,
        "effort": CODEX_EFFORT,
        "observed_version": version if isinstance(version, str) else None,
        "live_catalog_verified": False,
        "execution_path": "codex_exec",
        "arguments": [
            "-m",
            CODEX_MODEL,
            "-c",
            'model_reasoning_effort="ultra"',
        ],
        "next_action": None,
        "retry": {
            "attempts": 0,
            "max_attempts": CODEX_MAX_ATTEMPTS,
            "remaining": 0,
        },
        "downgrade_allowed": False,
        "fallback_model": None,
    }


def _block_codex(
    decision: dict[str, Any], reason_code: str, reason: str, next_action: str
) -> dict[str, Any]:
    retry = decision["retry"]
    retry["remaining"] = 0
    decision.update(
        {
            "state": "blocked",
            "reason_code": reason_code,
            "reason": reason,
            "next_action": next_action,
        }
    )
    return decision


def _catalog_has_sol_ultra(catalog: Any) -> bool:
    if not isinstance(catalog, dict):
        return False
    models = catalog.get("models")
    if not isinstance(models, list):
        return False

    for model in models:
        if not isinstance(model, dict) or model.get("slug") != CODEX_MODEL:
            continue
        levels = model.get("supported_reasoning_levels")
        if not isinstance(levels, list):
            continue
        if any(
            isinstance(level, dict) and level.get("effort") == CODEX_EFFORT
            for level in levels
        ):
            return True
    return False


def evaluate_codex(raw: Any) -> dict[str, Any]:
    """Evaluate Codex preflight and authoritative invocation observations."""

    config = raw if isinstance(raw, dict) else {}
    version = config.get("version")
    decision = _codex_base(version)

    if config.get("installed") is not True:
        return _block_codex(
            decision,
            "cli_missing",
            "Codex CLI is not installed",
            "install_codex_cli",
        )

    if _semver(version) is None:
        return _block_codex(
            decision,
            "version_unparseable",
            "Codex CLI version could not be parsed as semantic versioning",
            "inspect_codex_installation",
        )
    if not _version_at_least(version, MIN_CODEX_VERSION):
        return _block_codex(
            decision,
            "cli_too_old",
            "Codex CLI must be at least 0.144.0",
            "upgrade_codex_cli",
        )

    if not _catalog_has_sol_ultra(config.get("live_catalog")):
        return _block_codex(
            decision,
            "live_catalog_missing_capability",
            "The live Codex catalog lacks GPT-5.6 Sol with ultra reasoning",
            "request_access_or_refresh_live_catalog",
        )
    decision["live_catalog_verified"] = True

    invocation = config.get("first_real_invocation", {})
    if not isinstance(invocation, dict):
        return _block_codex(
            decision,
            "invalid_invocation_observation",
            "first_real_invocation must be an object",
            "correct_observation_input",
        )
    status = invocation.get("status", "not_run")
    if not isinstance(status, str):
        return _block_codex(
            decision,
            "invalid_invocation_status",
            "Invocation status must be a string",
            "correct_observation_input",
        )
    default_attempts = 0 if status == "not_run" else 1
    attempts = invocation.get("attempts", default_attempts)
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
        or attempts > CODEX_MAX_ATTEMPTS
        or (status == "not_run" and attempts != 0)
        or (status != "not_run" and attempts < 1)
    ):
        return _block_codex(
            decision,
            "invalid_invocation_attempts",
            "Invocation attempts must be zero before the probe and between one and two afterward",
            "correct_observation_input",
        )
    decision["retry"] = {
        "attempts": attempts,
        "max_attempts": CODEX_MAX_ATTEMPTS,
        "remaining": max(0, CODEX_MAX_ATTEMPTS - attempts),
    }

    if status == "not_run":
        decision.update(
            {
                "state": "probe_required",
                "reason_code": "first_real_invocation_required",
                "reason": (
                    "Run the first real Phase 2 invocation as the authoritative "
                    "entitlement and quota test"
                ),
                "next_action": "run_first_real_invocation",
            }
        )
        return decision

    if status == "success":
        decision.update(
            {
                "state": "ready",
                "reason_code": "authoritative_invocation_succeeded",
                "reason": "The first real GPT-5.6 Sol invocation succeeded",
                "next_action": "continue",
            }
        )
        return decision

    if status in _CODEX_RETRYABLE_FAILURES:
        if attempts < CODEX_MAX_ATTEMPTS:
            decision.update(
                {
                    "state": "retry",
                    "reason_code": status,
                    "reason": (
                        "Transient Codex failure; retry once with the exact same "
                        "GPT-5.6 Sol/ultra configuration"
                    ),
                    "next_action": "retry_same_invocation_once",
                }
            )
            return decision
        return _block_codex(
            decision,
            f"{status}_retry_exhausted",
            "The one permitted Codex retry also failed",
            "block_and_report_failure",
        )

    blocking = _CODEX_BLOCKING_FAILURES.get(status)
    if blocking is not None:
        return _block_codex(decision, *blocking)
    return _block_codex(
        decision,
        "unknown_invocation_status",
        f"Unknown Codex invocation status: {status!r}",
        "correct_observation_input",
    )


def _claude_base(version: Any) -> dict[str, Any]:
    return {
        "state": "blocked",
        "reason_code": None,
        "reason": None,
        "model": CLAUDE_MODEL,
        "effort": CLAUDE_EFFORT,
        "observed_version": version if isinstance(version, str) else None,
        "execution_path": None,
        "arguments": [],
        "environment_unset": [],
        "read_only": {
            "required": True,
            "permission_mode": "plan",
            "allowed_tools": list(CLAUDE_READ_ONLY_ALLOWED_TOOLS),
            "denied_tools": list(CLAUDE_READ_ONLY_DENIED_TOOLS),
        },
        "subagent_model_override": None,
        "next_action": None,
        "waiver_required": False,
        "waiver_granted": False,
        "downgrade_allowed": False,
        "fallback_model": None,
    }


def _waive_or_block_claude(
    decision: dict[str, Any],
    config: dict[str, Any],
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    waiver = config.get("explicit_waiver", False)
    if not isinstance(waiver, bool):
        decision.update(
            {
                "reason_code": "invalid_waiver_value",
                "reason": "explicit_waiver must be a JSON boolean",
                "next_action": "correct_observation_input",
            }
        )
        return decision
    if waiver:
        fallback = config.get("waiver_fallback")
        if not isinstance(fallback, dict):
            return _block_claude_input(
                decision,
                "named_fallback_required",
                "An explicit waiver requires an observed named Opus fallback",
            )
        fallback_model = fallback.get("model")
        observed_models = config.get("observed_models")
        if (
            fallback.get("available") is not True
            or fallback.get("explicitly_authorized") is not True
            or not isinstance(fallback_model, str)
            or re.fullmatch(r"claude-opus-[0-9]+(?:-[0-9]+)+", fallback_model) is None
            or not isinstance(observed_models, list)
            or not all(isinstance(model, str) for model in observed_models)
            or fallback_model not in observed_models
            or fallback.get("effort") != CLAUDE_EFFORT
            or fallback.get("execution_path") != "explicit_cli"
            or config.get("installed") is not True
        ):
            return _block_claude_input(
                decision,
                "invalid_named_fallback",
                "The waived fallback must be an available, explicitly authorized, versioned Claude Opus model at max effort",
            )
        decision.update(
            {
                "state": "waived",
                "reason_code": reason_code,
                "reason": reason,
                "model": fallback_model,
                "effort": CLAUDE_EFFORT,
                "execution_path": "explicit_cli",
                "arguments": [
                    "-p",
                    "--model",
                    fallback_model,
                    "--effort",
                    CLAUDE_EFFORT,
                    "--permission-mode",
                    "plan",
                    "--allowedTools",
                    ",".join(CLAUDE_READ_ONLY_ALLOWED_TOOLS),
                    "--disallowedTools",
                    ",".join(CLAUDE_READ_ONLY_DENIED_TOOLS),
                    "--disable-slash-commands",
                    "--no-session-persistence",
                    "--no-chrome",
                ],
                "environment_unset": list(CLAUDE_READ_ONLY_ENV_UNSET),
                "next_action": "invoke_explicit_named_fallback",
                "waiver_granted": True,
                "downgrade_allowed": True,
                "fallback_model": fallback_model,
            }
        )
        return decision
    decision.update(
        {
            "state": "blocked",
            "reason_code": reason_code,
            "reason": reason,
            "next_action": "request_explicit_waiver_or_restore_fable_access",
            "waiver_required": True,
        }
    )
    return decision


def _block_claude_input(
    decision: dict[str, Any], reason_code: str, reason: str
) -> dict[str, Any]:
    """Block malformed observations; a waiver cannot legitimize invalid input."""

    decision.update(
        {
            "state": "blocked",
            "reason_code": reason_code,
            "reason": reason,
            "next_action": "correct_observation_input",
        }
    )
    return decision


def evaluate_claude(raw: Any) -> dict[str, Any]:
    """Evaluate the Fable/max gate and choose Agent or explicit CLI execution."""

    config = raw if isinstance(raw, dict) else {}
    version = config.get("version")
    decision = _claude_base(version)

    installed = config.get("installed")
    if not isinstance(installed, bool):
        return _block_claude_input(
            decision,
            "invalid_installed_status",
            "installed must be a JSON boolean",
        )
    if installed is not True:
        return _waive_or_block_claude(
            decision,
            config,
            "cli_missing",
            "Claude Code is not installed",
        )
    if not isinstance(version, str):
        return _block_claude_input(
            decision,
            "invalid_version_value",
            "Claude Code version must be a string",
        )
    if _semver(version) is None:
        return _waive_or_block_claude(
            decision,
            config,
            "version_unparseable",
            "Claude Code version could not be parsed as semantic versioning",
        )
    if not _version_at_least(version, MIN_CLAUDE_VERSION):
        return _waive_or_block_claude(
            decision,
            config,
            "cli_too_old",
            "Claude Code must be at least 2.1.170",
        )

    access = config.get("fable_access", "unknown")
    if access is not True and access != "available":
        if access is False:
            code, reason = _CLAUDE_ACCESS_FAILURES[False]
        elif isinstance(access, str) and access in _CLAUDE_ACCESS_FAILURES:
            code, reason = _CLAUDE_ACCESS_FAILURES[access]
        else:
            return _block_claude_input(
                decision,
                "invalid_fable_access",
                "fable_access must be available, unavailable, entitlement_denied, "
                "provider_policy_denied, or unknown",
            )
        return _waive_or_block_claude(decision, config, code, reason)

    zdr = config.get("zero_data_retention", "unknown")
    if zdr is not True and zdr != "compatible":
        if zdr is False:
            code, reason = _CLAUDE_ZDR_FAILURES[False]
        elif isinstance(zdr, str) and zdr in _CLAUDE_ZDR_FAILURES:
            code, reason = _CLAUDE_ZDR_FAILURES[zdr]
        else:
            return _block_claude_input(
                decision,
                "invalid_zdr_status",
                "zero_data_retention must be compatible, incompatible, denied, or unknown",
            )
        return _waive_or_block_claude(decision, config, code, reason)

    waiver = config.get("explicit_waiver", False)
    if not isinstance(waiver, bool):
        return _waive_or_block_claude(
            decision,
            config,
            "invalid_waiver_value",
            "explicit_waiver must be a JSON boolean",
        )

    environment = config.get("environment", {})
    if not isinstance(environment, dict):
        return _block_claude_input(
            decision,
            "invalid_environment",
            "environment must be an object",
        )
    override = environment.get("CLAUDE_CODE_SUBAGENT_MODEL")
    if override is not None and not isinstance(override, str):
        return _block_claude_input(
            decision,
            "invalid_subagent_override",
            "CLAUDE_CODE_SUBAGENT_MODEL must be a string or null",
        )
    effort_override = environment.get("CLAUDE_CODE_EFFORT_LEVEL")
    if effort_override is not None and not isinstance(effort_override, str):
        return _block_claude_input(
            decision,
            "invalid_effort_override",
            "CLAUDE_CODE_EFFORT_LEVEL must be a string or null",
        )
    host_capabilities = config.get("host_capabilities", {})
    if not isinstance(host_capabilities, dict):
        return _block_claude_input(
            decision,
            "invalid_host_capabilities",
            "host_capabilities must be an object",
        )
    exact_override = override if isinstance(override, str) else ""
    compatible_overrides = {"", CLAUDE_MODEL_ALIAS, CLAUDE_MODEL}
    model_conflict = exact_override not in compatible_overrides
    effort_conflict = effort_override not in {None, CLAUDE_EFFORT}
    agent_selection_verified = (
        host_capabilities.get("agent_model_selection") is True
        and host_capabilities.get("agent_effort_selection") is True
        and host_capabilities.get("agent_read_only_enforced") is True
    )
    conflict = model_conflict or effort_conflict or not agent_selection_verified

    if conflict:
        execution_path = "explicit_cli"
        arguments = [
            "-p",
            "--model",
            CLAUDE_MODEL_ALIAS,
            "--effort",
            CLAUDE_EFFORT,
            "--permission-mode",
            "plan",
            "--allowedTools",
            ",".join(CLAUDE_READ_ONLY_ALLOWED_TOOLS),
            "--disallowedTools",
            ",".join(CLAUDE_READ_ONLY_DENIED_TOOLS),
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
        ]
        next_action = "invoke_explicit_claude_cli"
        environment_unset = list(CLAUDE_READ_ONLY_ENV_UNSET)
    else:
        execution_path = "agent_tool"
        arguments = ["model=fable", "effort=max"]
        next_action = "invoke_fable_agent"
        environment_unset = []

    decision.update(
        {
            "state": "ready",
            "reason_code": ("explicit_cli_required" if conflict else "fable_ready"),
            "reason": (
                "Unverified model/effort/read-only agent selection or a conflicting override requires the explicit read-only Fable CLI path"
                if conflict
                else "Claude Fable 5 with max effort is available"
            ),
            "execution_path": execution_path,
            "arguments": arguments,
            "environment_unset": environment_unset,
            "subagent_model_override": override,
            "next_action": next_action,
        }
    )
    return decision


def evaluate_model_policy(request: Any) -> dict[str, Any]:
    """Return deterministic Codex and Claude decisions for observed facts."""

    if not isinstance(request, dict):
        return {
            "version": SCHEMA_VERSION,
            "state": "blocked",
            "codex": None,
            "claude": None,
            "errors": ["input must be a JSON object"],
        }

    codex = evaluate_codex(request.get("codex"))
    claude = evaluate_claude(request.get("claude"))
    states = {codex["state"], claude["state"]}
    if "blocked" in states:
        state = "blocked"
    elif "retry" in states:
        state = "retry"
    elif "probe_required" in states:
        state = "probe_required"
    elif "waived" in states:
        state = "waived"
    else:
        state = "ready"

    return {
        "version": SCHEMA_VERSION,
        "state": state,
        "codex": codex,
        "claude": claude,
        "errors": [],
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        result = {
            "version": SCHEMA_VERSION,
            "state": "blocked",
            "codex": None,
            "claude": None,
            "errors": [f"input must be valid JSON: {error}"],
        }
    else:
        result = evaluate_model_policy(request)

    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 2 if result["state"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
