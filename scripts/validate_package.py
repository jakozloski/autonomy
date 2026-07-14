#!/usr/bin/env python3
"""Deterministically validate the autonomy skill package.

The validator intentionally uses only the Python standard library so it can run
from Codex, Claude Code, CI, or a freshly cloned repository without installing
package-specific dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence


ALLOWED_FRONTMATTER_KEYS = frozenset({"name", "description"})
REQUIRED_FRONTMATTER_KEYS = ALLOWED_FRONTMATTER_KEYS
MAX_SKILL_LINES_EXCLUSIVE = 500
MAX_REFERENCE_LINES_EXCLUSIVE = 500

CODEX_FLOOR_MODEL = "gpt-5.6-sol"
EXEC_MODEL_FLAGS = "-m <selected> -c 'model_reasoning_effort=\"xhigh\"'"
REVIEW_MODEL_FLAGS = "-c 'model=\"<selected>\"' -c 'model_reasoning_effort=\"xhigh\"'"

REQUIRED_REFERENCE_FILES = (
    "references/project-and-entry.md",
    "references/phases-1-5.md",
    "references/monitor-ci-feedback.md",
    "references/monitor-exit-handoffs.md",
    "references/state-and-safety.md",
)
REQUIRED_SCRIPT_FILES = (
    "scripts/handoff_decision.py",
    "scripts/model_policy.py",
    "scripts/state_schema.py",
    "scripts/validate_package.py",
    "scripts/test_handoff_decision.py",
    "scripts/test_model_policy.py",
    "scripts/test_state_schema.py",
    "scripts/test_validate_package.py",
)

# Evidence-gate and state-hardening content contracts.  Each marker is an
# exact substring the named file must contain; renaming the prose label in a
# reference must update this inventory in the same commit (same contract as
# the heading manifest).
REQUIRED_GATE_MARKERS = {
    "SKILL.md": (
        "state_schema.py",
        "red/green regression evidence",
        "evaluated_head_sha",
    ),
    "references/phases-1-5.md": (
        "Red/green regression evidence (mandatory when",
        "Variant analysis (mandatory when",
        "Diff-triggered review focus lines",
        "regression_evidence.status",
    ),
    "references/state-and-safety.md": (
        "Resume trust model",
        "regression_evidence:",
        "variant_analysis:",
        "state_schema_version",
        "analyzed_head_sha",
        "audit-only",
        "defect_evidence_mode",
    ),
    "references/monitor-exit-handoffs.md": (
        "diff-triggered review focus lines",
    ),
    "references/project-and-entry.md": (
        "red/green + variant evidence gate",
        "defect_evidence_mode",
    ),
}

REQUIRED_REDACTION_PATTERNS = {
    "aws_access_or_session_key": (
        r"(AKIA|ASIA)[0-9A-Z]{16}",
        ("AKIA" + "1234567890ABCDEF", "ASIA" + "1234567890ABCDEF"),
    ),
    "aws_secret_access_key": (
        r"""(?i)AWS_SECRET_ACCESS_KEY["']?\s*[:=]\s*["']?[A-Za-z0-9/+=]{40}["']?""",
        (
            "AWS_SECRET_ACCESS_KEY=" + "A" * 40,
            'AWS_SECRET_ACCESS_KEY": "' + "B" * 40 + '"',
        ),
    ),
    "aws_session_token": (
        r"""(?i)AWS_SESSION_TOKEN["']?\s*[:=]\s*["']?[A-Za-z0-9/+=]{16,4096}["']?""",
        (
            "AWS_" + "SESSION_TOKEN=" + "C" * 32,
            "AWS_" + 'SESSION_TOKEN": "' + "D" * 32 + '"',
        ),
    ),
    "github_user_or_oauth_token": (
        r"gh[pour]_[A-Za-z0-9]{20,255}",
        (
            "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890",
            "gh" + "o_abcdefghijklmnopqrstuvwxyz1234567890",
            "gh" + "u_abcdefghijklmnopqrstuvwxyz1234567890",
            "gh" + "r_abcdefghijklmnopqrstuvwxyz1234567890",
        ),
    ),
    "github_server_token": (
        r"ghs_([A-Za-z0-9]{20,255}|[A-Za-z0-9]+_[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})",
        (
            "gh" + "s_abcdefghijklmnopqrstuvwxyz1234567890",
            "gh"
            + "s_12345_"
            + ".".join(
                (
                    "eyJhbGciOiJSUzI1NiJ9",
                    "eyJpc3MiOiIxMjM0NSJ9",
                    "c2lnbmF0dXJlX3BhcnQ",
                )
            ),
        ),
    ),
    "github_fine_grained_pat": (
        r"github_pat_[A-Za-z0-9_]{20,255}",
        ("github_" + "pat_abcdefghijklmnopqrstuvwxyz_1234567890",),
    ),
    "linear_api_key": (
        r"lin_api_[A-Za-z0-9_]{40,}",
        ("lin_" + "api_abcdefghijklmnopqrstuvwxyz_1234567890_ABCDE",),
    ),
    "openai_key": (
        r"sk-((proj|svcacct)-)?[A-Za-z0-9_-]{20,}",
        (
            "sk-" + "proj-abcDEF0123456789_-abcDEF",
            "sk-" + "svcacct-abcDEF0123456789_-abcDEF",
        ),
    ),
    "anthropic_key": (
        r"sk-ant-[A-Za-z0-9_-]{40,}",
        ("sk-" + "ant-abcdefghijklmnopqrstuvwxyz_1234567890-ABCDE",),
    ),
    "jwt_base64url": (
        r"eyJ[A-Za-z0-9_\-=]{10,}\.eyJ[A-Za-z0-9_\-=]{10,}\.[A-Za-z0-9_\-=]+",
        ("eyJ" + "abcde-fghijk.eyJlmno_pqrstuv.signature-part",),
    ),
}
HEADING_MANIFEST_PATH = "references/heading-manifest.md"

# This is the complete heading inventory moved out of the former monolithic
# SKILL.md. Exact headings are deliberate: renaming one is a navigation change
# and must update this inventory (or an explicitly supplied JSON manifest).
BUILTIN_EXPECTED_HEADINGS: Mapping[str, tuple[str, ...]] = {
    "SKILL.md": (
        "# Full Autonomy Workflow",
        "## Loading Contract",
        "## Non-Negotiable Invariants",
        "## Mandatory Model Policy",
        "### Claude voices: Fable 5 at max",
        "### Codex voices: GPT-5.6 Sol at xhigh",
        "## Authorization and Entry Routing",
        "## Project Profile and State",
        "## Phase State Machine",
        "## Feedback Identity and Human Roundtrips",
        "## Ownership Transfer Rules",
        "## Validation Before Push",
        "## Completion Semantics",
        "## Final Rules",
    ),
    "references/project-and-entry.md": (
        "## Resolved Project Profile",
        "### Discovery Order",
        "### `BASE_BRANCH`",
        "### `QUALITY_CHECK_STEPS`",
        "### `DEV_SERVER_FRONTEND` / `DEV_SERVER_BACKEND`",
        "### `PROTECTED_BRANCHES`",
        "### `ISSUE_TRACKER`",
        "## Entry Points",
        "### Entry A: Solve an Issue",
        "### Entry B: Take Over a PR",
        "## Scope Analysis & Skill Selection",
        "### Step 1: Check gstack Availability",
        "### Step 2: Classify Scope from Diff",
        "### Step 3: Classify Change Type",
        "### Step 4: Select Skills via Capability-Gated Matrix",
        "### Step 5: Persist to State",
        "### Adapter Architecture",
        "### Security Model (Autonomous Mode)",
    ),
    "references/phases-1-5.md": (
        "## Phase 1: Plan",
        "## Phase 2: Review the Plan",
        "## Phase 3: Implement",
        "## Phase 4: Self-Review",
        "## Phase 4a: Security Gate",
        "## Runtime Verification (Advisory — Human QA Downstream)",
        "### `skill_only` Exemption (auto-waived)",
        "### Opt-In Frontend Verification (when user asks)",
        "### Opt-In Backend Verification (when user asks)",
        "### Phase 6 Re-Verification",
        "## Phase 5: Create / Update PR",
        "### PR Body Template (MANDATORY)",
        "### Issue Tracker Enforcement (Conditional on `ISSUE_TRACKER.type`)",
        "### If no PR exists yet:",
        "### If PR already exists (takeover):",
    ),
    "references/monitor-ci-feedback.md": (
        "## Phase 6: Monitor Loop",
        "### Step 1: Check CI / Check Runs",
        "### Step 2: Check Review Feedback",
        "#### Detect unaddressed human inline threads:",
        "#### Compute unreplied inline comment sets (canonical):",
        "#### Check top-level bot comments:",
        "#### Check bot review summaries:",
        "### Step 3: Check Branch Status",
        "#### Merge Conflict Resolution (Step 3, conflicts branch)",
    ),
    "references/monitor-exit-handoffs.md": (
        "### Step 4: Evaluate Loop Exit",
        "#### MANDATORY VERIFICATION GATE",
        "#### Exit conditions",
        "#### QA handoff (repo-conditional — conditions (a) and (d))",
        "#### Review-roundtrip handoff (condition (c), human feedback only)",
        "#### Draft-PR gate (flip draft → ready on the first clean pass after the grace window)",
        "#### Stable-poll gate (prevents exiting right as Bugbot posts a new comment)",
        "### PHASE_6_SELF_REVIEW (Diff-Scoped Post-Fix Review)",
    ),
    "references/state-and-safety.md": (
        "## State Tracking",
        "## Aborting Mid-Workflow",
        "## Timeout Heuristics",
        "## Secret/Token Redaction",
        "## Completion Signals",
        "## Rules",
    ),
}

# The human-readable heading inventory must retain an explicit old-to-new record
# for headings renamed during or after the package split. Unchanged former
# headings are already covered by BUILTIN_EXPECTED_HEADINGS.
RENAMED_FORMER_HEADINGS = (
    "## Philosophy",
    "## Model Configuration (Mandatory)",
    "### Codex voices: GPT-5.6 Sol at ultra",
    "### Step 2: Check for Bot Feedback",
    "#### QA handoff (repo-conditional — runs inside terminal-success exits (a) and (d))",
    "#### Review-roundtrip handoff (conditional — runs inside condition (c)'s CHANGES_REQUESTED / unresolved-human-threads exit)",
)

_GPT_55_PATTERN = re.compile(r"\bgpt(?:-|\s)?5\.5\b", re.IGNORECASE)
_SUFFIX_BOT_PATTERNS = (
    re.compile(
        r"\b(?:end|ends|ending)\s+(?:in|with)\s+[`'\"]?\[bot\]",
        re.IGNORECASE,
    ),
    re.compile(r"\.ends_?with\(\s*['\"]\[bot\]['\"]\s*\)", re.IGNORECASE),
    re.compile(r"\.endsWith\(\s*['\"]\[bot\]['\"]\s*\)"),
)
_SUFFIX_EXPLANATION_PATTERN = re.compile(
    r"\b(?:even\s+if|whether\s+or\s+not|regardless\s+of|"
    r"do\s+not\s+classify|don't\s+classify|never\s+classify|"
    r"must\s+not\s+classify)\b",
    re.IGNORECASE,
)
_ULTRACODE_EXPLICIT_PATTERNS = (
    re.compile(r"--effort(?:=|\s+)ultracode\b", re.IGNORECASE),
    re.compile(r"\+\s*(?:the\s+)?ultracode\b", re.IGNORECASE),
)
_ULTRACODE_POSITIVE_PATTERNS = (
    re.compile(r"\bultracode\s+(?:orchestration|opt[- ]?in)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:use|run|enable|invoke|select|adopt|require)\b[^.\n]{0,80}"
        r"\bultracode\b",
        re.IGNORECASE,
    ),
)
_NEGATION_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|never|forbid(?:den)?|must\s+not|should\s+not|"
    r"cannot|can't|without)\b",
    re.IGNORECASE,
)


def _display_path(path: Path, package_dir: Path) -> str:
    try:
        return path.relative_to(package_dir).as_posix()
    except ValueError:
        return str(path)


def _parse_frontmatter(skill_text: str) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    lines = skill_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, ["SKILL.md must begin with a YAML frontmatter delimiter (---)"]

    try:
        end_index = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return {}, ["SKILL.md frontmatter is missing its closing --- delimiter"]

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines[1:end_index], start=2):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)\s*:\s*(.*)", raw_line)
        if not match:
            errors.append(
                f"SKILL.md:{line_number}: unsupported frontmatter syntax; "
                "use one scalar key per line"
            )
            continue
        key, value = match.groups()
        if key in values:
            errors.append(f"SKILL.md:{line_number}: duplicate frontmatter key {key!r}")
            continue
        scalar = value.strip()
        if scalar and scalar[0] in "'\"":
            if len(scalar) < 2 or scalar[-1] != scalar[0]:
                errors.append(
                    f"SKILL.md:{line_number}: frontmatter key {key!r} has an "
                    "unterminated quoted scalar"
                )
                continue
            scalar = scalar[1:-1]
        values[key] = scalar

    unknown = sorted(set(values) - ALLOWED_FRONTMATTER_KEYS)
    if unknown:
        errors.append(
            "SKILL.md frontmatter has non-portable key(s): " + ", ".join(unknown)
        )
    missing = sorted(REQUIRED_FRONTMATTER_KEYS - set(values))
    if missing:
        errors.append(
            "SKILL.md frontmatter is missing required key(s): " + ", ".join(missing)
        )
    for key in sorted(REQUIRED_FRONTMATTER_KEYS & set(values)):
        if not values[key]:
            errors.append(f"SKILL.md frontmatter key {key!r} must not be empty")
    if values.get("name") and values["name"] != "autonomy":
        errors.append("SKILL.md frontmatter name must be 'autonomy'")
    return values, errors


def _load_heading_manifest(path: Path) -> Mapping[str, tuple[str, ...]]:
    """Load a JSON mapping of relative file paths to exact Markdown headings."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to read heading manifest {path}: {error}") from error

    if not isinstance(payload, dict):
        raise ValueError("heading manifest must be a JSON object")

    normalized: dict[str, tuple[str, ...]] = {}
    for raw_file, raw_headings in payload.items():
        if not isinstance(raw_file, str) or not raw_file:
            raise ValueError("heading manifest file names must be non-empty strings")
        if not isinstance(raw_headings, list) or not raw_headings:
            raise ValueError(
                f"heading manifest entry {raw_file!r} must be a non-empty list"
            )
        if not all(isinstance(heading, str) and heading for heading in raw_headings):
            raise ValueError(
                f"heading manifest entry {raw_file!r} contains an invalid heading"
            )
        normalized[raw_file] = tuple(raw_headings)
    return normalized


def _markdown_headings(text: str) -> set[str]:
    headings: set[str] = set()
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence_match:
            marker = fence_match.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if not in_fence and re.fullmatch(r"#{1,6}\s+.+", line):
            headings.add(line.rstrip())
    return headings


def _package_policy_files(package_dir: Path) -> list[Path]:
    files = [package_dir / "SKILL.md"]
    files.extend(sorted((package_dir / "references").glob("**/*.md")))
    files.extend(sorted((package_dir / "agents").glob("**/*.yaml")))
    files.extend(sorted((package_dir / "agents").glob("**/*.yml")))
    return [path for path in files if path.is_file()]


def _has_positive_ultracode_policy(line: str) -> bool:
    if "ultracode" not in line.lower():
        return False
    if any(pattern.search(line) for pattern in _ULTRACODE_EXPLICIT_PATTERNS):
        return True
    if _NEGATION_PATTERN.search(line):
        return False
    return any(pattern.search(line) for pattern in _ULTRACODE_POSITIVE_PATTERNS)


def _has_suffix_only_bot_classification(line: str) -> bool:
    if not any(pattern.search(line) for pattern in _SUFFIX_BOT_PATTERNS):
        return False
    return _SUFFIX_EXPLANATION_PATTERN.search(line) is None


def _extract_yaml_scalar(text: str, key: str) -> list[str]:
    """Extract simple or block YAML scalar values without a YAML dependency."""

    lines = text.splitlines()
    values: list[str] = []
    key_pattern = re.compile(rf"^(\s*){re.escape(key)}\s*:\s*(.*)$")
    for index, line in enumerate(lines):
        match = key_pattern.match(line)
        if not match:
            continue
        indentation, raw_value = match.groups()
        value = raw_value.strip()
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block_lines: list[str] = []
            for continuation in lines[index + 1 :]:
                if not continuation.strip():
                    block_lines.append("")
                    continue
                continuation_indent = len(continuation) - len(continuation.lstrip())
                if continuation_indent <= len(indentation):
                    break
                block_lines.append(continuation.strip())
            value = "\n".join(block_lines).strip()
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values.append(value)
    return values


def _extract_direct_interface_scalar(text: str, key: str) -> list[str]:
    """Extract only direct two-space children of the root interface mapping."""

    values: list[str] = []
    lines = text.splitlines()
    pattern = re.compile(rf"^  {re.escape(key)}\s*:\s*(.*)$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match is None:
            continue
        value = match.group(1).strip()
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block_lines: list[str] = []
            for continuation in lines[index + 1 :]:
                if not continuation.strip():
                    block_lines.append("")
                    continue
                indentation = len(continuation) - len(continuation.lstrip())
                if indentation <= 2:
                    break
                block_lines.append(continuation.strip())
            value = "\n".join(block_lines).strip()
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values.append(value)
    return values


def _direct_interface_quote_errors(text: str, keys: Iterable[str]) -> list[str]:
    """Reject direct interface scalars that start a quote but never close it."""

    errors: list[str] = []
    for key in keys:
        pattern = re.compile(rf"^  {re.escape(key)}\s*:\s*(.*)$")
        for line in text.splitlines():
            match = pattern.match(line)
            if match is None:
                continue
            value = match.group(1).strip()
            if (
                value
                and value[0] in "'\""
                and (len(value) < 2 or value[-1] != value[0])
            ):
                errors.append(
                    f"agents/openai.yaml interface.{key} has an unterminated "
                    "quoted scalar"
                )
    return errors


def _validate_references(
    package_dir: Path,
    expected_headings: Mapping[str, Sequence[str]],
) -> list[str]:
    errors: list[str] = []
    package_root = package_dir.resolve()

    for required_file in REQUIRED_REFERENCE_FILES:
        required_path = package_dir / required_file
        if not required_path.is_file():
            errors.append(f"missing required reference file: {required_file}")
            continue
        line_count = len(required_path.read_text(encoding="utf-8").splitlines())
        if line_count >= MAX_REFERENCE_LINES_EXCLUSIVE:
            errors.append(
                f"{required_file} has {line_count} lines; required phase references "
                f"must stay below {MAX_REFERENCE_LINES_EXCLUSIVE}"
            )

    for relative_path, headings in sorted(expected_headings.items()):
        candidate = (package_dir / relative_path).resolve()
        try:
            candidate.relative_to(package_root)
        except ValueError:
            errors.append(
                f"heading manifest path escapes the skill package: {relative_path}"
            )
            continue
        if not candidate.is_file():
            if relative_path not in REQUIRED_REFERENCE_FILES:
                errors.append(f"missing heading target file: {relative_path}")
            continue
        actual_headings = _markdown_headings(candidate.read_text(encoding="utf-8"))
        expected_heading_set = set(headings)
        for heading in headings:
            if heading not in actual_headings:
                errors.append(f"{relative_path}: missing exact heading {heading!r}")
        for heading in sorted(actual_headings - expected_heading_set):
            errors.append(
                f"{relative_path}: unexpected heading {heading!r}; "
                "add it to the heading manifest"
            )

    inventory_path = package_dir / HEADING_MANIFEST_PATH
    if not inventory_path.is_file():
        errors.append(f"missing required heading inventory: {HEADING_MANIFEST_PATH}")
        return errors

    inventory_text = inventory_path.read_text(encoding="utf-8")
    # The human-readable table uses inline-code cells. Nested code spans cannot
    # represent headings that themselves contain backticks, so compare a
    # normalized semantic form and accept destination basenames.
    normalized_inventory_text = inventory_text.replace("`", "")
    for relative_path, headings in sorted(expected_headings.items()):
        if Path(relative_path).name not in normalized_inventory_text:
            errors.append(f"{HEADING_MANIFEST_PATH}: does not name {relative_path!r}")
        for heading in headings:
            if heading.replace("`", "") not in normalized_inventory_text:
                errors.append(
                    f"{HEADING_MANIFEST_PATH}: does not enumerate heading {heading!r}"
                )
    if expected_headings is BUILTIN_EXPECTED_HEADINGS:
        for former_heading in RENAMED_FORMER_HEADINGS:
            if former_heading.replace("`", "") not in normalized_inventory_text:
                errors.append(
                    f"{HEADING_MANIFEST_PATH}: does not preserve renamed former "
                    f"heading {former_heading!r}"
                )
    return errors


def _validate_gate_markers(package_dir: Path) -> list[str]:
    """Require every evidence-gate/state-hardening marker in its named file."""
    errors: list[str] = []
    for relative_path, markers in sorted(REQUIRED_GATE_MARKERS.items()):
        candidate = package_dir / relative_path
        if not candidate.is_file():
            # Missing reference/skill files are reported by their own checks;
            # do not duplicate that error here.
            continue
        text = candidate.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                errors.append(
                    f"{relative_path}: missing required gate marker {marker!r}"
                )
    return errors


def _validate_policy_text(package_dir: Path) -> list[str]:
    errors: list[str] = []
    combined_parts: list[str] = []
    for path in _package_policy_files(package_dir):
        text = path.read_text(encoding="utf-8")
        combined_parts.append(text)
        display_path = _display_path(path, package_dir)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if _GPT_55_PATTERN.search(line):
                errors.append(
                    f"{display_path}:{line_number}: obsolete GPT-5.5 policy remains"
                )
            if _has_positive_ultracode_policy(line):
                errors.append(
                    f"{display_path}:{line_number}: positive ultracode policy remains"
                )
            if _has_suffix_only_bot_classification(line):
                errors.append(
                    f"{display_path}:{line_number}: suffix-only [bot] classification remains"
                )

    combined = "\n".join(combined_parts)
    if EXEC_MODEL_FLAGS not in combined:
        errors.append("missing exact codex exec flags: " + EXEC_MODEL_FLAGS)
    if REVIEW_MODEL_FLAGS not in combined:
        errors.append("missing exact codex review flags: " + REVIEW_MODEL_FLAGS)
    if CODEX_FLOOR_MODEL not in combined:
        errors.append("missing documented codex floor model: " + CODEX_FLOOR_MODEL)
    state_path = package_dir / "references" / "state-and-safety.md"
    if state_path.is_file():
        state_text = state_path.read_text(encoding="utf-8")
        for kind, (pattern, samples) in REQUIRED_REDACTION_PATTERNS.items():
            if f"`{pattern}`" not in state_text:
                errors.append(
                    f"missing current redaction pattern for {kind}: {pattern}"
                )
                continue
            compiled = re.compile(pattern)
            for sample in samples:
                if compiled.fullmatch(sample) is None:
                    errors.append(
                        f"redaction pattern for {kind} does not match its fixture"
                    )
                    break
    return errors


def _validate_openai_yaml(package_dir: Path) -> list[str]:
    path = package_dir / "agents" / "openai.yaml"
    if not path.is_file():
        return ["missing required agents/openai.yaml"]

    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    lines = text.splitlines()
    root_entries = [
        (index, line.strip())
        for index, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace()
    ]
    interface_entries = [
        index for index, value in root_entries if value == "interface:"
    ]
    if len(interface_entries) != 1 or len(root_entries) != 1:
        errors.append(
            "agents/openai.yaml must contain exactly one root interface mapping"
        )

    errors.extend(
        _direct_interface_quote_errors(
            text, ("display_name", "short_description", "default_prompt")
        )
    )

    display_names = _extract_direct_interface_scalar(text, "display_name")
    short_descriptions = _extract_direct_interface_scalar(text, "short_description")
    default_prompts = _extract_direct_interface_scalar(text, "default_prompt")

    if len(display_names) != 1 or not display_names[0].strip():
        errors.append(
            "agents/openai.yaml must contain exactly one non-empty interface.display_name"
        )

    if len(short_descriptions) != 1 or not short_descriptions[0].strip():
        errors.append(
            "agents/openai.yaml must contain exactly one non-empty interface.short_description"
        )
    if len(default_prompts) != 1 or not default_prompts[0].strip():
        errors.append(
            "agents/openai.yaml must contain exactly one non-empty interface.default_prompt"
        )
    elif "$autonomy" not in default_prompts[0]:
        errors.append(
            "agents/openai.yaml default_prompt must mention $autonomy"
        )
    return errors


def validate_package(
    package_dir: Path,
    heading_manifest: Path | None = None,
) -> list[str]:
    """Return every validation error for ``package_dir`` in stable order."""

    package_dir = package_dir.resolve()
    skill_path = package_dir / "SKILL.md"
    if not skill_path.is_file():
        return [f"missing SKILL.md in {package_dir}"]

    if heading_manifest is None:
        expected_headings = BUILTIN_EXPECTED_HEADINGS
    else:
        expected_headings = _load_heading_manifest(heading_manifest.resolve())

    skill_text = skill_path.read_text(encoding="utf-8")
    errors: list[str] = []
    _, frontmatter_errors = _parse_frontmatter(skill_text)
    errors.extend(frontmatter_errors)

    line_count = len(skill_text.splitlines())
    if line_count >= MAX_SKILL_LINES_EXCLUSIVE:
        errors.append(
            f"SKILL.md has {line_count} lines; it must stay below "
            f"{MAX_SKILL_LINES_EXCLUSIVE}"
        )

    errors.extend(_validate_references(package_dir, expected_headings))
    for required_file in REQUIRED_SCRIPT_FILES:
        if not (package_dir / required_file).is_file():
            errors.append(f"missing required script file: {required_file}")
    errors.extend(_validate_policy_text(package_dir))
    errors.extend(_validate_gate_markers(package_dir))
    errors.extend(_validate_openai_yaml(package_dir))
    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the autonomy skill package."
    )
    parser.add_argument(
        "package_dir",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="skill package directory (defaults to the parent of this script directory)",
    )
    parser.add_argument(
        "--heading-manifest",
        type=Path,
        help=(
            "optional JSON object mapping package-relative file paths to lists of "
            "exact Markdown headings; defaults to the built-in inventory"
        ),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        errors = validate_package(args.package_dir, args.heading_manifest)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if errors:
        print(f"autonomy package validation failed ({len(errors)} error(s)):")
        for error in errors:
            print(f"- {error}")
        return 1

    print("autonomy package validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
