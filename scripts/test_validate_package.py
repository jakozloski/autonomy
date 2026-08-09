from __future__ import annotations

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
    REQUIRED_ANCHORED_MARKERS,
    REQUIRED_GATE_MARKERS,
    REQUIRED_REDACTION_PATTERNS,
    REQUIRED_SCRIPT_FILES,
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
            "Conductor owns orchestration; do not substitute the separate ultracode mode.",
            *(
                f"Gate marker: {marker}"
                for marker in REQUIRED_GATE_MARKERS.get("SKILL.md", ())
            ),
            "",
        )
    )


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

        for relative_path, markers in REQUIRED_GATE_MARKERS.items():
            if relative_path == "SKILL.md":
                continue  # markers are embedded by _valid_skill_text()
            with (self.root / relative_path).open("a", encoding="utf-8") as handle:
                for marker in markers:
                    handle.write(f"Gate marker: {marker}\n")

        # Anchored markers require the real condition-line shape: one
        # operative line per anchor carrying every required substring, in
        # the anchor's declared Markdown context (prose list item vs a line
        # inside a fenced block, as the monitor pseudocode is).
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            with (self.root / relative_path).open("a", encoding="utf-8") as handle:
                for anchor, fenced, required in anchor_specs:
                    payload = f"{anchor} {' '.join(required)}"
                    if fenced:
                        handle.write(f"```text\n{payload}\n```\n")
                    else:
                        handle.write(f"- {payload}\n")

        (self.root / "agents" / "openai.yaml").write_text(
            "\n".join(
                (
                    "interface:",
                    '  display_name: "Conductor Autonomy"',
                    '  short_description: "Run a full autonomous workflow"',
                    '  default_prompt: "Use $autonomy to finish this task."',
                    "",
                )
            ),
            encoding="utf-8",
        )
        for relative_path in REQUIRED_SCRIPT_FILES:
            # Script fixtures are written AFTER the marker-append loop above,
            # so any gate markers targeting a script path must be baked in
            # here or the overwrite silently drops them (R3 round-8 finding).
            script_lines = ["# package fixture"]
            for marker in REQUIRED_GATE_MARKERS.get(relative_path, ()):
                script_lines.append(marker)
            (self.root / relative_path).write_text(
                "\n".join(script_lines) + "\n", encoding="utf-8"
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

    def test_missing_state_schema_helper_and_tests_fail(self) -> None:
        for relative_path in ("scripts/state_schema.py", "scripts/test_state_schema.py"):
            with self.subTest(script=relative_path):
                target = self.package.root / relative_path
                target.unlink()
                try:
                    errors = validate_package(self.package.root)
                    self.assertIn(
                        f"missing required script file: {relative_path}", errors
                    )
                finally:
                    target.write_text("# package fixture\n", encoding="utf-8")

    def test_missing_gate_marker_is_rejected_per_file_and_marker(self) -> None:
        for relative_path, markers in REQUIRED_GATE_MARKERS.items():
            for marker in markers:
                with self.subTest(file=relative_path, marker=marker):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    stripped = "\n".join(
                        line
                        for line in original.splitlines()
                        if marker not in line
                    ) + "\n"
                    assert marker not in stripped
                    target.write_text(stripped, encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: missing required gate marker {marker!r}",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def _operative_index(self, lines: list[str], anchor: str, required) -> int:
        payload = f"{anchor} {' '.join(required)}"
        matches = [
            index for index, line in enumerate(lines) if payload in line
        ]
        assert len(matches) == 1
        return matches[0]

    def test_anchored_marker_rejects_relocation_attack(self) -> None:
        # The pass-2 adversarial scenario: strip a required substring from the
        # anchored condition line while parking the FULL original line text
        # elsewhere in the file. Presence markers stay satisfied; only a
        # placement-aware check can catch the revert.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                for substring in required:
                    with self.subTest(
                        file=relative_path, anchor=anchor, substring=substring
                    ):
                        target = self.package.root / relative_path
                        original = target.read_text(encoding="utf-8")
                        lines = original.splitlines()
                        index = self._operative_index(lines, anchor, required)
                        relocated = lines[index]
                        # Revert the anchored line (drop the substring) and
                        # relocate the intact text as a NON-anchored paragraph.
                        lines[index] = lines[index].replace(substring, "")
                        lines.append(f"Parked copy: {relocated.lstrip('- ')}")
                        target.write_text(
                            "\n".join(lines) + "\n", encoding="utf-8"
                        )
                        try:
                            errors = validate_package(self.package.root)
                            self.assertIn(
                                f"{relative_path}: anchored line {anchor!r} is"
                                f" missing required text {substring!r}",
                                errors,
                            )
                        finally:
                            target.write_text(original, encoding="utf-8")

    def test_anchored_marker_rejects_fenced_decoy_and_bullet_swap(self) -> None:
        # The pass-3 EXECUTED bypass: swap the operative prose line's list
        # marker for a renderer-equivalent one and revert its text, then park
        # the intact original line inside a fenced code block. The old
        # line-literal matcher accepted the fenced decoy as its single match
        # (validator returned zero errors); the context-aware matcher must
        # keep matching the bullet-swapped operative line and ignore the
        # fenced decoy, reporting the reverted text.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue  # prose-context anchors only
                substring = required[0]
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    reverted = decoy.replace(substring, "").replace(
                        "- ", "* ", 1
                    )
                    lines[index] = reverted
                    lines.extend(("```text", decoy, "```"))
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: anchored line {anchor!r} is"
                            f" missing required text {substring!r}",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_rejects_unfenced_decoy_for_fenced_anchor(
        self,
    ) -> None:
        # Inverse of the fenced-decoy attack: a fenced-context anchor (the
        # monitor pseudocode) must not accept a decoy parked OUTSIDE the
        # fence while the in-fence operative line is reverted.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if not fenced:
                    continue
                substring = required[0]
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    lines[index] = lines[index].replace(substring, "")
                    lines.append(decoy)  # outside any fence
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: anchored line {anchor!r} is"
                            f" missing required text {substring!r}",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_rejects_tilde_conflated_fence_decoy(self) -> None:
        # Pass-4 latent gap: a shared ```/~~~ toggle desynchronizes on a
        # mixed-family construction — under the old tracker, a tilde fence
        # around a backtick-fenced decoy flipped the state so the decoy
        # counted as operative. Family-aware tracking must keep the decoy
        # fenced: with the operative line deleted, the count is zero.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    del lines[index]
                    lines.extend(("~~~", "```text", decoy, "```", "~~~"))
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_ignores_html_comment_decoy(self) -> None:
        # Pass-4 latent gap: a multi-line HTML comment renders as nothing,
        # but each of its inner lines was previously evaluated as operative
        # prose — an invisible decoy could stand in for a deleted condition.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    del lines[index]
                    lines.extend(("<!--", decoy, "-->"))
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_ignores_indented_code_decoy(self) -> None:
        # A decoy indented as Markdown code (4+ spaces) is display content:
        # with the operative prose line deleted it must count zero matches,
        # not one.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = "        " + lines[index]
                    del lines[index]
                    lines.append(decoy)
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_ignores_tab_indented_code_decoy(self) -> None:
        # Pass-5 adversarial gap: a leading tab is one raw character but
        # advances to a four-column stop in CommonMark, so a tab-indented
        # decoy is indented code. Raw-length indent measurement read it as
        # prose; column-width measurement must count zero matches with the
        # operative line deleted.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = "\t" + lines[index]
                    del lines[index]
                    lines.append(decoy)
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_ignores_decoy_in_four_backtick_fence(
        self,
    ) -> None:
        # Pass-5 adversarial gap: the tracker kept only the fence family,
        # so an inner triple-backtick line closed a four-backtick fence and
        # a decoy parked after it read as prose. CommonMark closes a fence
        # only on a run at least as long as its opener — the decoy stays
        # fenced, counting zero with the operative line deleted.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    del lines[index]
                    lines.extend(("````", "```", decoy, "````"))
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_ignores_decoy_after_info_string_closer(
        self,
    ) -> None:
        # Pass-6 adversarial gap: CommonMark closing fences carry only
        # whitespace after the run, so a same-family run with an info
        # string ("````text" inside a four-backtick fence) is content —
        # the length-only rule closed on it and read the parked decoy as
        # prose. With the operative line deleted the decoy must count
        # zero.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    del lines[index]
                    lines.extend(("````", "````text", decoy, "````"))
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_ignores_decoy_after_over_indented_closer(
        self,
    ) -> None:
        # Pass-6 sibling gap: a closing run indented more than three
        # columns past its opener is content per CommonMark, not a
        # closer — closing on it read the parked decoy as prose.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    del lines[index]
                    lines.extend(("```text", "      ```", decoy, "```"))
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_fenced_anchor_rejects_decoy_after_backtick_info_opener(
        self,
    ) -> None:
        # Pass-6 sibling gap, fenced-anchor direction: a backtick fence
        # whose info string contains a backtick is not a fence line at
        # all (CommonMark 4.5) — treating it as an opener classified the
        # following decoy as fenced, satisfying the pseudocode anchor
        # while the decoy renders as visible prose.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if not fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    del lines[index]
                    lines.extend(("```a`b", decoy))
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_anchored_marker_ignores_decoy_after_relative_indent_closer(
        self,
    ) -> None:
        # Pass-7 gap, empirically demonstrated: with an opener indented
        # 1-3 columns, a "closer" indented past three columns satisfied
        # the relative opener+3 rule, but CommonMark's top-level closer
        # rule is absolute (<= 3 columns) — the tracker closed a fence a
        # renderer leaves open, and the parked decoy read as prose. The
        # closer rule must be absolute whenever the opener sits at three
        # columns or fewer.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    lines = original.splitlines()
                    index = self._operative_index(lines, anchor, required)
                    decoy = lines[index]
                    del lines[index]
                    lines.extend(("  ```", "     ```", decoy, "  ```"))
                    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 0",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

    def test_unclosed_fence_at_eof_is_rejected(self) -> None:
        # A file ending inside an open fence is the signature of a
        # truncated regeneration; it must fail validation instead of
        # silently reclassifying the tail of the file.
        for relative_path in REQUIRED_ANCHORED_MARKERS:
            with self.subTest(file=relative_path):
                target = self.package.root / relative_path
                original = target.read_text(encoding="utf-8")
                target.write_text(original + "```text\n", encoding="utf-8")
                try:
                    errors = validate_package(self.package.root)
                    self.assertIn(
                        f"{relative_path}: unclosed code fence at end of file",
                        errors,
                    )
                finally:
                    target.write_text(original, encoding="utf-8")

    def test_duplicate_anchor_line_is_rejected(self) -> None:
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                with self.subTest(file=relative_path, anchor=anchor):
                    target = self.package.root / relative_path
                    original = target.read_text(encoding="utf-8")
                    payload = f"{anchor} {' '.join(required)}"
                    duplicate = (
                        f"```text\n{payload}\n```\n"
                        if fenced
                        else f"- {payload}\n"
                    )
                    target.write_text(original + duplicate, encoding="utf-8")
                    try:
                        errors = validate_package(self.package.root)
                        self.assertIn(
                            f"{relative_path}: expected exactly one operative"
                            f" line anchored by {anchor!r}, found 2",
                            errors,
                        )
                    finally:
                        target.write_text(original, encoding="utf-8")

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
            f"{unexpected_heading!r}; "
            "add it to BUILTIN_EXPECTED_HEADINGS in scripts/validate_package.py",
            errors,
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
            "display_name: Conductor\n"
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
            "    display_name: Conductor\n"
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

    def test_frontmatter_rejects_unquoted_colon_space_scalar(self) -> None:
        skill_path = self.package.root / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        skill_path.write_text(
            text.replace(
                "description: Run the complete autonomous engineering workflow.",
                "description: bad: value",
            ),
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertTrue(
            any("must quote scalars containing" in error for error in errors)
        )

    def test_frontmatter_rejects_colon_tab_and_trailing_colon_scalars(self) -> None:
        for bad_value in ("bad:\tvalue", "bad:"):
            with self.subTest(value=bad_value):
                skill_path = self.package.root / "SKILL.md"
                skill_path.write_text(
                    _valid_skill_text().replace(
                        "description: Run the complete autonomous engineering workflow.",
                        f"description: {bad_value}",
                    ),
                    encoding="utf-8",
                )
                errors = validate_package(self.package.root)
                self.assertTrue(
                    any("must quote scalars containing" in error for error in errors)
                )

    def test_openai_yaml_accepts_document_start_marker(self) -> None:
        (self.package.root / "agents" / "openai.yaml").write_text(
            "---\n"
            "interface:\n"
            '  display_name: "Conductor Autonomy"\n'
            '  short_description: "Run a full autonomous workflow"\n'
            '  default_prompt: "Use $autonomy to finish this task."\n',
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertNotIn(
            "agents/openai.yaml must contain exactly one root interface mapping",
            errors,
        )

    def test_cli_validates_the_default_package_directory(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("package validation passed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
