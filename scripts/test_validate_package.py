from __future__ import annotations

import json
import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from validate_package import (
    BUILTIN_EXPECTED_HEADINGS,
    CODEX_FLOOR_MODEL,
    EXEC_MODEL_FLAGS,
    REQUIRED_REDACTION_PATTERNS,
    REQUIRED_SCRIPT_FILES,
    RENAMED_FORMER_HEADINGS,
    REVIEW_MODEL_FLAGS,
    main,
    validate_package,
)


def _valid_skill_text() -> str:
    return "\n".join(
        (
            "---",
            "name: autonomy",
            "description: Run the complete autonomous engineering workflow.",
            "---",
            "",
            *BUILTIN_EXPECTED_HEADINGS["SKILL.md"],
            "",
            f"Use `codex exec {EXEC_MODEL_FLAGS}` for Codex execution.",
            f"Use `codex review {REVIEW_MODEL_FLAGS}` for Codex review.",
            f"The codex floor model is {CODEX_FLOOR_MODEL}; newer eligible models auto-forward.",
            "This skill owns orchestration; do not substitute the separate ultracode mode.",
            "",
        )
    )


def _heading_inventory(
    expected_headings: dict[str, tuple[str, ...]] | None = None,
) -> str:
    expected = expected_headings or dict(BUILTIN_EXPECTED_HEADINGS)
    sections = ["# Autonomy Heading Manifest", ""]
    for relative_path, headings in expected.items():
        sections.extend(
            (
                Path(relative_path).name,
                "",
                "```text",
                *(heading.replace("`", "") for heading in headings),
                "```",
                "",
            )
        )
    if expected_headings is None:
        sections.extend(
            (
                "Renamed former headings",
                "",
                "```text",
                *RENAMED_FORMER_HEADINGS,
                "```",
                "",
            )
        )
    return "\n".join(sections)


class PackageFixture:
    def __init__(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        (self.root / "references").mkdir()
        (self.root / "agents").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "SKILL.md").write_text(_valid_skill_text(), encoding="utf-8")

        for relative_path, headings in BUILTIN_EXPECTED_HEADINGS.items():
            if relative_path == "SKILL.md":
                continue
            path = self.root / relative_path
            path.write_text(
                "\n\n".join((*headings, "Known bot: `coderabbitai[bot]`.")) + "\n",
                encoding="utf-8",
            )

        state_path = self.root / "references" / "state-and-safety.md"
        with state_path.open("a", encoding="utf-8") as state_file:
            for pattern, _samples in REQUIRED_REDACTION_PATTERNS.values():
                state_file.write(f"Required pattern: `{pattern}`\n")

        (self.root / "references" / "heading-manifest.md").write_text(
            _heading_inventory(), encoding="utf-8"
        )
        (self.root / "agents" / "openai.yaml").write_text(
            "\n".join(
                (
                    "interface:",
                    '  display_name: "Autonomy"',
                    '  short_description: "Run a full autonomous workflow"',
                    '  default_prompt: "Use $autonomy to finish this task."',
                    "",
                )
            ),
            encoding="utf-8",
        )
        for relative_path in REQUIRED_SCRIPT_FILES:
            (self.root / relative_path).write_text(
                "# package fixture\n", encoding="utf-8"
            )

    def close(self) -> None:
        self._temporary_directory.cleanup()


class ValidatePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = PackageFixture()

    def tearDown(self) -> None:
        self.package.close()

    def test_valid_package_passes(self) -> None:
        self.assertEqual(validate_package(self.package.root), [])

    def test_missing_runtime_helper_fails(self) -> None:
        (self.package.root / "scripts" / "model_policy.py").unlink()

        errors = validate_package(self.package.root)

        self.assertIn("missing required script file: scripts/model_policy.py", errors)

    def test_frontmatter_rejects_non_portable_keys(self) -> None:
        skill_path = self.package.root / "SKILL.md"
        skill_path.write_text(
            _valid_skill_text().replace(
                "description: Run the complete autonomous engineering workflow.\n",
                "description: Run the complete autonomous engineering workflow.\n"
                "license: MIT\n"
                "user_invocable: true\n",
            ),
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertTrue(
            any(
                "non-portable key(s): license, user_invocable" in error
                for error in errors
            ),
            errors,
        )

    def test_frontmatter_rejects_unterminated_quoted_scalar(self) -> None:
        skill_path = self.package.root / "SKILL.md"
        skill_path.write_text(
            _valid_skill_text().replace(
                "description: Run the complete autonomous engineering workflow.",
                'description: "unterminated',
            ),
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            "SKILL.md:3: frontmatter key 'description' has an unterminated "
            "quoted scalar",
            errors,
        )

    def test_skill_line_limit_is_strictly_below_500(self) -> None:
        skill_path = self.package.root / "SKILL.md"
        lines = _valid_skill_text().splitlines()
        lines.extend("filler" for _ in range(500 - len(lines)))
        self.assertEqual(len(lines), 500)
        skill_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        errors = validate_package(self.package.root)

        self.assertIn("SKILL.md has 500 lines; it must stay below 500", errors)

    def test_phase_reference_line_limit_is_strictly_below_500(self) -> None:
        reference_path = self.package.root / "references" / "project-and-entry.md"
        lines = reference_path.read_text(encoding="utf-8").splitlines()
        lines.extend("filler" for _ in range(500 - len(lines)))
        self.assertEqual(len(lines), 500)
        reference_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        errors = validate_package(self.package.root)

        self.assertIn(
            "references/project-and-entry.md has 500 lines; required phase "
            "references must stay below 500",
            errors,
        )

    def test_heading_manifest_is_excluded_from_reference_line_limit(self) -> None:
        inventory_path = self.package.root / "references" / "heading-manifest.md"
        lines = inventory_path.read_text(encoding="utf-8").splitlines()
        lines.extend("inventory note" for _ in range(500 - len(lines)))
        self.assertEqual(len(lines), 500)
        inventory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.assertEqual(validate_package(self.package.root), [])

    def test_missing_reference_and_heading_are_reported(self) -> None:
        missing_path = self.package.root / "references" / "state-and-safety.md"
        missing_path.unlink()
        phases_path = self.package.root / "references" / "phases-1-5.md"
        phases_path.write_text(
            phases_path.read_text(encoding="utf-8").replace(
                "## Phase 4a: Security Gate", "## Renamed Security Gate"
            ),
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            "missing required reference file: references/state-and-safety.md", errors
        )
        self.assertIn(
            "references/phases-1-5.md: missing exact heading "
            "'## Phase 4a: Security Gate'",
            errors,
        )

    def test_heading_inventory_must_enumerate_every_expected_heading(self) -> None:
        inventory_path = self.package.root / "references" / "heading-manifest.md"
        inventory_path.write_text(
            inventory_path.read_text(encoding="utf-8").replace(
                "### Security Model (Autonomous Mode)", ""
            ),
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            "references/heading-manifest.md: does not enumerate heading "
            "'### Security Model (Autonomous Mode)'",
            errors,
        )

    def test_missing_skill_heading_is_reported(self) -> None:
        skill_path = self.package.root / "SKILL.md"
        missing_heading = "## Mandatory Model Policy"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8").replace(missing_heading, ""),
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            f"SKILL.md: missing exact heading {missing_heading!r}",
            errors,
        )

    def test_unlisted_phase_heading_is_reported(self) -> None:
        phases_path = self.package.root / "references" / "phases-1-5.md"
        unexpected_heading = "### Phase 3b: Undocumented Gate"
        with phases_path.open("a", encoding="utf-8") as phases_file:
            phases_file.write(f"\n{unexpected_heading}\n")

        errors = validate_package(self.package.root)

        self.assertIn(
            "references/phases-1-5.md: unexpected heading "
            f"{unexpected_heading!r}; add it to the heading manifest",
            errors,
        )

    def test_heading_inventory_preserves_every_renamed_former_heading(self) -> None:
        inventory_path = self.package.root / "references" / "heading-manifest.md"
        original_inventory = inventory_path.read_text(encoding="utf-8")

        for former_heading in RENAMED_FORMER_HEADINGS:
            with self.subTest(former_heading=former_heading):
                inventory_path.write_text(
                    original_inventory.replace(former_heading, "", 1),
                    encoding="utf-8",
                )

                errors = validate_package(self.package.root)

                self.assertIn(
                    "references/heading-manifest.md: does not preserve renamed "
                    f"former heading {former_heading!r}",
                    errors,
                )

    def test_obsolete_model_and_positive_ultracode_policy_are_rejected(self) -> None:
        reference_path = self.package.root / "references" / "project-and-entry.md"
        with reference_path.open("a", encoding="utf-8") as reference:
            reference.write("Use GPT-5.5 for the compatibility pass.\n")
            reference.write("Enable ultracode orchestration for every phase.\n")

        errors = validate_package(self.package.root)

        self.assertTrue(
            any("obsolete GPT-5.5 policy remains" in error for error in errors)
        )
        self.assertTrue(
            any("positive ultracode policy remains" in error for error in errors)
        )

    def test_current_redaction_patterns_match_credential_fixtures(self) -> None:
        for kind, (pattern, samples) in REQUIRED_REDACTION_PATTERNS.items():
            with self.subTest(kind=kind):
                compiled = re.compile(pattern)
                for sample in samples:
                    self.assertIsNotNone(compiled.fullmatch(sample))

    def test_missing_current_redaction_pattern_is_rejected(self) -> None:
        state_path = self.package.root / "references" / "state-and-safety.md"
        pattern, _samples = REQUIRED_REDACTION_PATTERNS["github_server_token"]
        state_path.write_text(
            state_path.read_text(encoding="utf-8").replace(f"`{pattern}`", ""),
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            f"missing current redaction pattern for github_server_token: {pattern}",
            errors,
        )

    def test_negative_ultracode_prohibition_is_allowed(self) -> None:
        errors = validate_package(self.package.root)

        self.assertFalse(
            any("positive ultracode policy remains" in error for error in errors),
            errors,
        )

    def test_both_exact_codex_flag_forms_are_required(self) -> None:
        skill_path = self.package.root / "SKILL.md"
        skill_path.write_text(
            _valid_skill_text().replace(EXEC_MODEL_FLAGS, "-m gpt-5.6-sol"),
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn("missing exact codex exec flags: " + EXEC_MODEL_FLAGS, errors)
        self.assertNotIn(
            "missing exact codex review flags: " + REVIEW_MODEL_FLAGS, errors
        )

    def test_suffix_only_bot_classification_is_rejected(self) -> None:
        reference_path = self.package.root / "references" / "monitor-ci-feedback.md"
        with reference_path.open("a", encoding="utf-8") as reference:
            reference.write("Any username ending in `[bot]` is a bot.\n")

        errors = validate_package(self.package.root)

        self.assertTrue(
            any(
                "suffix-only [bot] classification remains" in error for error in errors
            ),
            errors,
        )

    def test_suffix_mention_in_an_explanation_is_allowed(self) -> None:
        reference_path = self.package.root / "references" / "monitor-ci-feedback.md"
        with reference_path.open("a", encoding="utf-8") as reference:
            reference.write(
                "The authenticated actor still counts even if its login happens to "
                "end in `[bot]`.\n"
            )

        errors = validate_package(self.package.root)

        self.assertFalse(
            any(
                "suffix-only [bot] classification remains" in error for error in errors
            ),
            errors,
        )

    def test_openai_yaml_requires_both_interface_fields(self) -> None:
        (self.package.root / "agents" / "openai.yaml").write_text(
            "interface:\n  default_prompt: 'Run the autonomy workflow.'\n",
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            "agents/openai.yaml must contain exactly one non-empty interface.short_description",
            errors,
        )
        self.assertIn(
            "agents/openai.yaml must contain exactly one non-empty interface.display_name",
            errors,
        )
        self.assertIn(
            "agents/openai.yaml default_prompt must mention $autonomy", errors
        )

    def test_openai_yaml_rejects_root_level_interface_fields(self) -> None:
        (self.package.root / "agents" / "openai.yaml").write_text(
            "display_name: Autonomy\n"
            "short_description: Autonomous workflow\n"
            "default_prompt: Use $autonomy.\n",
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            "agents/openai.yaml must contain exactly one root interface mapping",
            errors,
        )

    def test_openai_yaml_rejects_nested_interface_fields(self) -> None:
        (self.package.root / "agents" / "openai.yaml").write_text(
            "interface:\n"
            "  nested:\n"
            "    display_name: Autonomy\n"
            "    short_description: Autonomous workflow\n"
            "    default_prompt: Use $autonomy.\n",
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            "agents/openai.yaml must contain exactly one non-empty interface.display_name",
            errors,
        )

    def test_openai_yaml_rejects_unterminated_quoted_scalar(self) -> None:
        (self.package.root / "agents" / "openai.yaml").write_text(
            "interface:\n"
            '  display_name: "unterminated\n'
            "  short_description: Autonomous workflow\n"
            "  default_prompt: Use $autonomy.\n",
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertIn(
            "agents/openai.yaml interface.display_name has an unterminated quoted scalar",
            errors,
        )

    def test_custom_json_heading_manifest_is_supported(self) -> None:
        custom_headings = {
            "references/project-and-entry.md": list(
                BUILTIN_EXPECTED_HEADINGS["references/project-and-entry.md"]
            )
        }
        custom_manifest_path = self.package.root / "custom-headings.json"
        custom_manifest_path.write_text(json.dumps(custom_headings), encoding="utf-8")

        errors = validate_package(self.package.root, custom_manifest_path)

        self.assertEqual(errors, [])

    def test_cli_accepts_custom_heading_manifest_argument(self) -> None:
        custom_manifest_path = self.package.root / "custom-headings.json"
        custom_manifest_path.write_text(
            json.dumps(
                {
                    "references/project-and-entry.md": list(
                        BUILTIN_EXPECTED_HEADINGS["references/project-and-entry.md"]
                    )
                }
            ),
            encoding="utf-8",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    str(self.package.root),
                    "--heading-manifest",
                    str(custom_manifest_path),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("package validation passed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
