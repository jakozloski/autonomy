from __future__ import annotations

import os
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
    REQUIRED_PY_BINDINGS,
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
            f"Use `codex exec {EXEC_MODEL_FLAGS} resume <session-id>` to resume.",
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
        # admin#1495 r15 F12: every valid package carries the MIT notice.
        (self.root / "LICENSE").write_text(
            "MIT License\n\nCopyright (c) 2026 Jake Kozloski\n\n"
            "Permission is hereby granted, free of charge, to any person"
            " obtaining a copy...\n",
            encoding="utf-8",
        )

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
            # _validate_py_bindings (R7.2 codex #9) requires each cross-module
            # constant binding as an OPERATIVE source line, not a comment, so
            # bake the statements in verbatim or test_valid_package_passes
            # fails on the very invariant this fixture is supposed to satisfy.
            for statement in REQUIRED_PY_BINDINGS.get(relative_path, ()):
                script_lines.append(statement)
            # _validate_test_collection (CR 3761135481) requires every test
            # module to define a collectable test* method — the fixture must
            # satisfy the invariant it exists to defend, like the marker and
            # binding loops above.
            # _validate_size_boundary_parity (algo#1216 r16 F5 + admin#1495
            # r12 F8) reads the paired assignments textually — bake matching
            # literals so the fixture satisfies the invariants it defends.
            if relative_path == "scripts/monitor_runner.py":
                script_lines.append("MAX_CANDIDATE_BYTES = 8 * 1_048_576")
                script_lines.append("MAX_WORK_ITERATIONS = 50")
            if relative_path == "scripts/state_schema.py":
                script_lines.append(
                    "STATE_READ_CEILING_BYTES = 8 * 1_048_576"
                )
                script_lines.append("MAX_WORK_ITERATIONS = 50")
            name = relative_path.rsplit("/", 1)[-1]
            if name.startswith("test_") and name.endswith(".py"):
                script_lines.extend(
                    (
                        "import unittest",
                        "",
                        "",
                        "class FixtureSmokeTest(unittest.TestCase):",
                        "    def test_fixture_collects(self) -> None:",
                        "        self.assertTrue(True)",
                    )
                )
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

    def test_size_boundary_parity_detects_drift(self) -> None:
        # algo#1216 r16 F5: the runner's candidate cap and the schema
        # CLI's read ceiling are independent literals with a mirror
        # comment but no guard — a drifted pair must fail validation.
        runner = self.package.root / "scripts" / "monitor_runner.py"
        runner.write_text(
            runner.read_text(encoding="utf-8").replace(
                "MAX_CANDIDATE_BYTES = 8 * 1_048_576",
                "MAX_CANDIDATE_BYTES = 9 * 1_048_576",
            ),
            encoding="utf-8",
        )
        errors = validate_package(self.package.root)
        self.assertTrue(
            any("size-boundary parity" in error for error in errors), errors
        )

    def test_claims_audit_anchor_rejects_fenced_decoy(self) -> None:
        # algo#1216 r16 F12: the operative Check 4 claims-audit instruction
        # is anchored — relocating it into a fenced block turns it into
        # display content, and the anchored-marker machinery must fail
        # validation instead of counting the decoy.
        path = self.package.root / "references" / "merge-readiness.md"
        text = path.read_text(encoding="utf-8")
        anchor_line = next(
            line
            for line in text.splitlines()
            if "2. For each claim, verify against the actual code path"
            in line
        )
        path.write_text(
            text.replace(
                anchor_line, "```text\n" + anchor_line + "\n```"
            ),
            encoding="utf-8",
        )
        errors = validate_package(self.package.root)
        self.assertTrue(
            any(
                "expected exactly one operative line" in error
                and "For each claim" in error
                for error in errors
            ),
            errors,
        )

    def test_missing_exec_resume_shape_is_reported(self) -> None:
        # admin#1495 r12 F10: the ordered resume shape (flags BEFORE the
        # resume subcommand) is pinned — a package documenting only
        # trailing flags would let a delegated resume drop the sandbox pin.
        (self.package.root / "SKILL.md").write_text(
            _valid_skill_text().replace(
                f"codex exec {EXEC_MODEL_FLAGS} resume", "codex exec resume"
            ),
            encoding="utf-8",
        )
        errors = validate_package(self.package.root)
        self.assertTrue(
            any("exec-resume shape" in error for error in errors), errors
        )

    def test_work_cap_parity_detects_drift(self) -> None:
        # admin#1495 r12 F8: the immutable work cap is restated in the
        # schema for the validator's own check — a drifted pair must fail.
        schema = self.package.root / "scripts" / "state_schema.py"
        schema.write_text(
            schema.read_text(encoding="utf-8").replace(
                "MAX_WORK_ITERATIONS = 50", "MAX_WORK_ITERATIONS = 49"
            ),
            encoding="utf-8",
        )
        errors = validate_package(self.package.root)
        self.assertTrue(
            any("work-cap parity" in error for error in errors), errors
        )

    def test_required_test_module_with_zero_tests_is_rejected(self) -> None:
        # CR 3761135481: unittest discover exits 0 after collecting zero
        # tests, so existence checks alone cannot prove the CI gate runs
        # anything. A required test module with no test* method must fail
        # validation.
        target = self.package.root / "scripts" / "test_cli_fail_closed.py"
        target.write_text(
            "import unittest\n\n\nclass Empty(unittest.TestCase):\n    pass\n",
            encoding="utf-8",
        )
        errors = validate_package(self.package.root)
        self.assertTrue(
            any("defines no test* method" in error for error in errors),
            errors,
        )


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
        # CR 3760684079: a unittest assertion, not a bare assert — the bare
        # form strips under -O and a zero-match would pass vacuously.
        self.assertEqual(len(matches), 1, (payload, matches))
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
                        lines.append(
                            # CR 3760684085: strip ONE bullet prefix, not a
                            # character set (lstrip eats every leading dash).
                            "Parked copy: " + relocated.removeprefix("- ")
                        )
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

    def test_anchored_marker_rejects_inline_comment_hidden_clause(self) -> None:
        # R6-F16: the required clause is still ON the anchor line as raw
        # bytes but wrapped in an inline HTML comment, so it renders as
        # nothing — a raw-line substring search passes while the displayed
        # condition is gone. The display-text check must reject it.
        for relative_path, anchor_specs in REQUIRED_ANCHORED_MARKERS.items():
            for anchor, fenced, required in anchor_specs:
                if fenced:
                    continue  # inside a code fence, comment syntax is literal
                for substring in required:
                    with self.subTest(
                        file=relative_path, anchor=anchor, substring=substring
                    ):
                        target = self.package.root / relative_path
                        original = target.read_text(encoding="utf-8")
                        lines = original.splitlines()
                        index = self._operative_index(lines, anchor, required)
                        head, sep, tail = lines[index].rpartition(substring)
                        # CR 3787358750: unittest assertion, not a bare
                        # assert — python -O would remove the guard and the
                        # case would pass vacuously on a missing substring.
                        self.assertTrue(
                            sep, "operative line lost its required text"
                        )
                        lines[index] = f"{head}<!-- {substring} -->{tail}"
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
            "double-quoted scalar",
            errors,
        )

    def test_frontmatter_rejects_quoted_scalars_the_yaml_loader_refuses(self) -> None:
        # R7 codex #14: outer-quote matching alone accepts scalars a real YAML
        # loader rejects; the pinned skill scanner then omits the unparseable
        # package and still exits zero, shipping an unscanned skill green. Each
        # case is invalid YAML per PyYAML 6.0.2 flow-scalar rules and must be
        # caught here so the validator gate agrees with the loader.
        rejected = {
            '"bad\\q"': "an unknown escape character",  # unknown double-quote escape
            '"a"b"': "content after the closing double quote",  # stray interior quote
            '"a\\xZZ"': "an invalid \\x hex escape",  # non-hex \x digits
            "'a'b'": "content after the closing single quote",  # single-quote reopen
            # R7.2 codex #4: the scanner's parser (frontmatter.loads, which uses
            # libyaml's CSafeLoader when present) raises on BOTH a \U past
            # U+10FFFF and a lone surrogate, skipping the package UNSCANNED, so
            # the gate must reject them too. Bare yaml.safe_load (pure-Python)
            # instead LOADS the surrogate, so audit with frontmatter.loads. The
            # runtime differential is pinned in the scanner-parity test below.
            '"\\U00110000"': "outside the Unicode scalar range",  # astral past U+10FFFF
            '"\\ud800"': "outside the Unicode scalar range",  # lone surrogate
        }
        for scalar, fragment in rejected.items():
            with self.subTest(scalar=scalar):
                skill_path = self.package.root / "SKILL.md"
                skill_path.write_text(
                    _valid_skill_text().replace(
                        "description: Run the complete autonomous engineering "
                        "workflow.",
                        f"description: {scalar}",
                    ),
                    encoding="utf-8",
                )
                errors = validate_package(self.package.root)
                self.assertTrue(
                    any(
                        "frontmatter key 'description' has" in error
                        and fragment in error
                        for error in errors
                    ),
                    f"{scalar!r} -> {errors}",
                )

    def test_frontmatter_accepts_valid_quoted_scalars(self) -> None:
        # The pass-through side of the #14 gate: legal quoted scalars — a valid
        # escape, single-quote literal backslash, doubled single quote, and a
        # colon-bearing double-quoted string (the real SKILL.md shape) — must
        # NOT be rejected, or the stricter check would false-fail real skills.
        for scalar in (
            '"line\\tbreak"',  # valid \t escape
            "'literal\\q backslash'",  # single-quoted: backslash is literal
            "'it''s fine'",  # single-quoted doubled quote
            '"Full workflow: take over this PR, solve this issue."',
        ):
            with self.subTest(scalar=scalar):
                skill_path = self.package.root / "SKILL.md"
                skill_path.write_text(
                    _valid_skill_text().replace(
                        "description: Run the complete autonomous engineering "
                        "workflow.",
                        f"description: {scalar}",
                    ),
                    encoding="utf-8",
                )
                errors = validate_package(self.package.root)
                self.assertFalse(
                    any("quoted scalar" in error for error in errors),
                    f"{scalar!r} -> {errors}",
                )

    def test_frontmatter_accepts_slash_escape_max_codepoint_and_trailing_comment(
        self,
    ) -> None:
        # R7.2 O4/O10 pass-through, confirmed against frontmatter.loads in the
        # differential oracle: PyYAML 6.0.2's double-quote escape set INCLUDES
        # '/', so "a\/b" is valid (loads to "a/b"); \U0010FFFF is the top of the
        # Unicode scalar range (in-range, unlike \U00110000); and a trailing '#'
        # comment after a closing quote is stripped by the loader. Each must
        # pass, or the stricter gate would false-reject a real skill the scanner
        # accepts. Keyed on the reject wrapper ("frontmatter key 'description'
        # has ...") so a regression that starts rejecting any of them fails
        # here. (Both reviewers flagged \/ as invalid; the oracle proved
        # otherwise, so this pins the accept.)
        for scalar in (
            '"a\\/b"',  # O4: '/' is a valid double-quote escape
            '"\\U0010FFFF"',  # top of the Unicode scalar range (in-range)
            '"ok" # trailing note',  # O10: trailing comment after a quoted scalar
        ):
            with self.subTest(scalar=scalar):
                skill_path = self.package.root / "SKILL.md"
                skill_path.write_text(
                    _valid_skill_text().replace(
                        "description: Run the complete autonomous engineering "
                        "workflow.",
                        f"description: {scalar}",
                    ),
                    encoding="utf-8",
                )
                errors = validate_package(self.package.root)
                self.assertFalse(
                    any(
                        "frontmatter key 'description' has" in error
                        for error in errors
                    ),
                    f"{scalar!r} -> {errors}",
                )

    def test_surrogate_and_astral_gate_matches_scanner_parser(self) -> None:
        # R7.2 codex #4, executable differential: makes the load-bearing one-way
        # invariant real instead of a prose claim. The scanner's OWN parser is
        # python-frontmatter, which aliases SafeLoader to libyaml's CSafeLoader
        # when the C ext is present; that loader RAISES on a lone surrogate,
        # whereas bare yaml.safe_load (pure-Python) LOADS it. So we compute the
        # scanner-parser verdict at runtime and assert: any escape the loader
        # raises on (unloadable -> skipped UNSCANNED) MUST be rejected by the
        # gate; the top in-range codepoint the loader accepts must pass.
        # \U00110000 raises in BOTH loaders, so it exercises the invariant even
        # where libyaml is absent and the surrogate case goes quiet.
        try:
            import frontmatter  # the scanner's real frontmatter parser
        except ImportError:
            if os.environ.get("SKILL_CHECKS_REQUIRE_SCANNER") == "1":
                self.fail(
                    "python-frontmatter absent but SKILL_CHECKS_REQUIRE_SCANNER=1"
                    " — CI must install the scanner deps rather than skip"
                    " load-bearing parity tests (algo#1216 R2 finding 3787189766)"
                )
            self.skipTest("python-frontmatter absent; scanner-parity uncheckable")
        cases = {
            '"\\ud800"': True,  # lone surrogate: reject (loader raises under libyaml)
            '"\\U00110000"': True,  # astral past U+10FFFF: reject (both loaders raise)
            '"\\U0010FFFF"': False,  # top in-range: accept (both loaders load)
        }
        for scalar, must_reject in cases.items():
            with self.subTest(scalar=scalar):
                doc = f"---\ndescription: {scalar}\nname: autonomy\n---\nbody\n"
                try:
                    frontmatter.loads(doc)
                    scanner_raises = False
                except Exception:
                    scanner_raises = True
                skill_path = self.package.root / "SKILL.md"
                skill_path.write_text(
                    _valid_skill_text().replace(
                        "description: Run the complete autonomous engineering "
                        "workflow.",
                        f"description: {scalar}",
                    ),
                    encoding="utf-8",
                )
                errors = validate_package(self.package.root)
                rejected = any(
                    "outside the Unicode scalar range" in error for error in errors
                )
                if scanner_raises:
                    # THE load-bearing direction: parser-raise => gate must reject,
                    # else an unloadable skill ships UNSCANNED-green.
                    self.assertTrue(
                        rejected,
                        f"scanner parser raises on {scalar!r} but the gate accepted "
                        f"it -> unscanned skill would ship green: {errors}",
                    )
                if not must_reject:
                    # In-range codepoint the scanner loads: the gate must not
                    # false-reject a skill the scanner would scan clean.
                    self.assertFalse(
                        rejected,
                        f"{scalar!r} is in-range (scanner loads it) yet the gate "
                        f"rejected it: {errors}",
                    )

    def test_splitlines_boundary_control_chars_match_scanner_parser(self) -> None:
        # Pass-4 opus F1, executable differential: str.splitlines() consumes
        # U+000B/000C/001C/001D/001E as line boundaries, so a per-splitlines-
        # line control scan can never see them - while the scanner's parser
        # raises ReaderError on every one and the package would ship
        # UNSCANNED-green. The \n-physical-line scan must reject each one.
        try:
            import frontmatter  # the scanner's real frontmatter parser
        except ImportError:
            if os.environ.get("SKILL_CHECKS_REQUIRE_SCANNER") == "1":
                self.fail(
                    "python-frontmatter absent but SKILL_CHECKS_REQUIRE_SCANNER=1"
                    " — CI must install the scanner deps rather than skip"
                    " load-bearing parity tests (algo#1216 R2 finding 3787189766)"
                )
            self.skipTest("python-frontmatter absent; scanner-parity uncheckable")
        for code in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E):
            with self.subTest(codepoint=f"U+{code:04X}"):
                char = chr(code)
                doc = _valid_skill_text().replace(
                    "name: autonomy", f"name: autonomy{char}injected: x"
                )
                try:
                    frontmatter.loads(doc)
                    scanner_raises = False
                except Exception:
                    scanner_raises = True
                self.assertTrue(
                    scanner_raises,
                    f"premise drift: frontmatter.loads now accepts U+{code:04X};"
                    " re-derive the boundary battery against the new parser",
                )
                (self.package.root / "SKILL.md").write_text(doc, encoding="utf-8")
                errors = validate_package(self.package.root)
                self.assertTrue(
                    any(
                        f"non-printable character (U+{code:04X})" in error
                        for error in errors
                    ),
                    f"scanner parser raises on U+{code:04X} but the gate accepted"
                    f" -> unscanned skill would ship green: {errors}",
                )

    def test_non_separation_whitespace_matches_scanner_parser(self) -> None:
        # Pass-4 codex F5, executable differential: YAML separation whitespace
        # (s-white) is exactly SPACE and TAB. Every other char Python's
        # str.isspace()/re \s accepts (NBSP, the Zs space separators, NEL,
        # LS/PS) is CONTENT to the YAML reader, not separation. Dropped into
        # the "name: " separator position each one makes the scanner's parser
        # raise (skill skipped UNSCANNED), while a Python-native strip/split
        # would silently absorb it and ACCEPT. The physical-line whitespace
        # guard must reject each so no unscanned skill ships green.
        try:
            import frontmatter  # the scanner's real frontmatter parser
        except ImportError:
            if os.environ.get("SKILL_CHECKS_REQUIRE_SCANNER") == "1":
                self.fail(
                    "python-frontmatter absent but SKILL_CHECKS_REQUIRE_SCANNER=1"
                    " — CI must install the scanner deps rather than skip"
                    " load-bearing parity tests (algo#1216 R2 finding 3787189766)"
                )
            self.skipTest("python-frontmatter absent; scanner-parity uncheckable")
        # Printable (YAML c-printable), non-C0-control whitespace: the class
        # the whitespace guard uniquely owns (C0 controls are the opus-F1
        # non-printable check, exercised by the battery above).
        battery = (
            0x00A0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
            0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000,
            0x0085, 0x2028, 0x2029,
        )
        for code in battery:
            with self.subTest(codepoint=f"U+{code:04X}"):
                char = chr(code)
                # Replace the "name: " separator space with the non-separation
                # char: a structural position where the reader must raise.
                doc = _valid_skill_text().replace(
                    "name: autonomy", f"name:{char}autonomy"
                )
                try:
                    frontmatter.loads(doc)
                    scanner_raises = False
                except Exception:
                    scanner_raises = True
                self.assertTrue(
                    scanner_raises,
                    f"premise drift: frontmatter.loads now accepts U+{code:04X}"
                    " in the separator position; re-derive the battery",
                )
                (self.package.root / "SKILL.md").write_text(doc, encoding="utf-8")
                errors = validate_package(self.package.root)
                self.assertTrue(
                    any(
                        "non-separation Unicode whitespace character "
                        f"(U+{code:04X})" in error
                        for error in errors
                    ),
                    f"scanner parser raises on U+{code:04X} but the gate accepted"
                    f" -> unscanned skill would ship green: {errors}",
                )

    def test_non_separation_whitespace_is_rejected_fail_closed(self) -> None:
        # The guard flags on PRESENCE (fail-closed, over-rejection-safe), not
        # only in reader-raising structural positions: a trailing NBSP on a
        # plain scalar is CONTENT the reader ACCEPTS, yet the guard still
        # rejects it. Pins the fail-closed design so a future narrowing to
        # "only structural positions" (which would reopen the under-rejection
        # gap for positions not enumerated) fails here.
        char = "\u00a0"  # NBSP trailing a plain scalar
        doc = _valid_skill_text().replace(
            "name: autonomy", f"name: autonomy{char}"
        )
        try:
            import frontmatter

            frontmatter.loads(doc)  # must NOT raise: the reader accepts it
        except ImportError:
            pass  # validator-only property; scanner premise is optional here
        except Exception:  # pragma: no cover - premise drift
            self.skipTest("premise drift: reader now raises on trailing NBSP")
        (self.package.root / "SKILL.md").write_text(doc, encoding="utf-8")
        errors = validate_package(self.package.root)
        self.assertTrue(
            any(
                "non-separation Unicode whitespace character (U+00A0)" in error
                for error in errors
            ),
            "fail-closed guard must reject NBSP even where the reader accepts"
            f" it: {errors}",
        )

    def test_py_binding_parked_in_a_comment_is_rejected(self) -> None:
        # R7.2 codex #9: the cross-module constant binding must be an OPERATIVE
        # source line. _validate_py_bindings anchors on ^[ \t]*<stmt>...$
        # (MULTILINE), so a naive `stmt in text` substring check would accept
        # the binding parked in a `#` comment while the module reverts the
        # constant to an independent literal — exactly the drift the pin exists
        # to catch. Comment out the fixture's operative binding and the gate
        # must still fail it.
        target = "scripts/handoff_decision.py"
        statement = REQUIRED_PY_BINDINGS[target][0]
        path = self.package.root / target
        original = path.read_text(encoding="utf-8")
        self.assertIn(
            statement + "\n", original, "fixture must write the binding operative"
        )
        path.write_text(
            original.replace(statement + "\n", f"# {statement}\n"),
            encoding="utf-8",
        )
        errors = validate_package(self.package.root)
        self.assertTrue(
            any(
                statement in error and "operative source line" in error
                for error in errors
            ),
            errors,
        )

    def test_openai_yaml_rejects_quoted_scalars_the_yaml_loader_refuses(self) -> None:
        # The openai.yaml interface scalars share the SKILL.md quoted-scalar
        # rule via `_quoted_scalar_error`; pin that the sibling gate also
        # rejects an unknown escape, not only an unterminated quote.
        (self.package.root / "agents" / "openai.yaml").write_text(
            "interface:\n"
            '  display_name: "bad\\q"\n'
            "  short_description: Autonomous workflow\n"
            "  default_prompt: Use $autonomy.\n",
            encoding="utf-8",
        )

        errors = validate_package(self.package.root)

        self.assertTrue(
            any(
                "agents/openai.yaml interface.display_name has an unknown "
                "escape character" in error
                for error in errors
            ),
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

    def test_password_and_cookie_patterns_cover_r2_probes(self) -> None:
        # R2 round-3 finding 3774515260 (PR #3551) reconciled with algo#1216
        # finding 3779532276: label/header-anchored password and cookie
        # forms are in scope. The exact probes from both findings must
        # redact, including DB_PASSWORD-style prefixed labels the
        # \b-anchored form missed.
        self.assertIn("password_assignment", REQUIRED_REDACTION_PATTERNS)
        self.assertIn("cookie_header_value", REQUIRED_REDACTION_PATTERNS)
        password = re.compile(
            REQUIRED_REDACTION_PATTERNS["password_assignment"][0]
        )
        cookie = re.compile(REQUIRED_REDACTION_PATTERNS["cookie_header_value"][0])
        self.assertIsNotNone(password.search("Password: " + "S3cretpass!"))
        self.assertIsNotNone(password.search("DB_PASSWORD=" + "prodsecret99"))
        # r13 F12 reversed the old minimum-length carve-out: EVERY
        # non-empty anchored value redacts — the probes leaked short
        # passwords ("password: x") and short quoted forms.
        self.assertIsNotNone(password.search("password: short"))
        self.assertIsNotNone(password.search("password: x"))
        self.assertIsNotNone(password.search('DB_PASSWORD="a"'))
        # The label anchor still bounds scope: prose without an
        # assignment operator never matches.
        self.assertIsNone(password.search("the password field is required"))
        # Pass-3 codex F1: an escaped quote inside a quoted value must not
        # terminate the match early - the WHOLE quoted secret redacts.
        escaped = password.search(
            'PASSWORD="' + 'correct \\"horse\\" battery staple"'
        )
        self.assertIsNotNone(escaped)
        assert escaped is not None
        self.assertTrue(escaped.group(0).endswith('staple"'))
        self.assertIsNotNone(
            cookie.search("Cookie: sessionid=" + "abcdef1234567890")
        )
        # Whole-remainder semantics (CR 3787358691/3779091168, converging
        # with this PR's pass-2 findings): once a Cookie header carries 8+
        # chars the ENTIRE remainder redacts - a short first pair cannot
        # expose a later credential, quoted values sit inside the span,
        # and valueless attribute tails ride along.
        behind_short = cookie.search(
            "Cookie: consent=true; sessionid=" + "abcdef1234567890"
        )
        self.assertIsNotNone(behind_short)
        assert behind_short is not None
        self.assertTrue(behind_short.group(0).endswith("abcdef1234567890"))
        self.assertIsNotNone(
            cookie.search('Cookie: sessionid="' + 'abcdef1234567890"')
        )
        attr_tail = cookie.search(
            "Set-Cookie: sid=" + "0123456789abcdef" + "; Path=/; HttpOnly; Secure"
        )
        self.assertIsNotNone(attr_tail)
        assert attr_tail is not None
        self.assertTrue(attr_tail.group(0).endswith("HttpOnly; Secure"))
        # Post-merge pass-2 codex F2, still binding under the simpler form:
        # the span never crosses a line ending - the following line stays
        # outside the redaction.
        crossing = cookie.search(
            "Cookie: consent=true;\nnotcookie=abcdefgh12345678"
        )
        self.assertIsNotNone(crossing)
        assert crossing is not None
        self.assertNotIn("notcookie", crossing.group(0))
        self.assertNotIn("\n", crossing.group(0))
        # Sub-threshold remainders stay unredacted.
        # r13 F12: every non-empty Cookie value redacts — the short-value
        # carve-out leaked probe cookies.
        self.assertIsNotNone(cookie.search("Cookie: a=b"))
        self.assertIsNone(cookie.search("Cookie:"))

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
            "agents/openai.yaml interface.display_name has an unterminated "
            "double-quoted scalar",
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
            any(
                "frontmatter key 'description' has an unquoted ': ' that must be quoted"
                in error
                for error in errors
            ),
            errors,
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
                    any(
                        "an unquoted ': ' that must be quoted" in error
                        for error in errors
                    ),
                    errors,
                )

    def test_frontmatter_rejects_every_plain_scalar_indicator(self) -> None:
        # R7.2 codex #4 (completeness pin): a skill description must be a plain
        # or quoted STRING scalar. An unquoted value opening with a YAML
        # indicator is not one, and _plain_scalar_error refuses the WHOLE
        # _PLAIN_SCALAR_INDICATORS table for two reasons that resolve the same
        # way (mirroring its docstring): the loader REJECTS some (@ [ * ! | > …),
        # so the pinned scanner skips the package UNSCANNED while scan-all still
        # exits 0; it ACCEPTS others as a non-string node (& anchor, bare block),
        # out of contract for a description. Both must reject. Iterate the table
        # itself so a NEW indicator added without a gate case fails here (partial
        # coverage of a set is its own defect); deleting the lookup in
        # _plain_scalar_error turns every subcase red. The assertion pins the
        # literal reject phrase and the indicator character, not the map's own
        # descriptive text, so it cannot go tautological with the map.
        from validate_package import _PLAIN_SCALAR_INDICATORS

        for indicator in _PLAIN_SCALAR_INDICATORS:
            with self.subTest(indicator=indicator):
                skill_path = self.package.root / "SKILL.md"
                skill_path.write_text(
                    _valid_skill_text().replace(
                        "description: Run the complete autonomous engineering "
                        "workflow.",
                        f"description: {indicator}safe",
                    ),
                    encoding="utf-8",
                )
                errors = validate_package(self.package.root)
                self.assertTrue(
                    any(
                        "frontmatter key 'description' has an unquoted value "
                        "opening with YAML " in error
                        and f"'{indicator}'" in error
                        for error in errors
                    ),
                    f"{indicator!r} -> {errors}",
                )

    def test_frontmatter_rejects_block_openers_and_control_char(self) -> None:
        # R7.2 codex #4, the non-table plain-scalar rejections: '-' and '?' open
        # a block/mapping node when a space follows (the loader reads '- x' as a
        # sequence, not a string), and a raw C0 control char anywhere in the
        # fence is a ReaderError. Both make the scanner skip the package
        # UNSCANNED, so the gate must catch them alongside the indicator table
        # above. Deleting the '-?:' branch reddens the openers; deleting the
        # _forbidden_control_char guard reddens the NUL case.
        openers = {
            "- x": "an unquoted value opening with the '-' indicator",
            "? x": "an unquoted value opening with the '?' indicator",
        }
        for value, fragment in openers.items():
            with self.subTest(value=value):
                skill_path = self.package.root / "SKILL.md"
                skill_path.write_text(
                    _valid_skill_text().replace(
                        "description: Run the complete autonomous engineering "
                        "workflow.",
                        f"description: {value}",
                    ),
                    encoding="utf-8",
                )
                errors = validate_package(self.package.root)
                self.assertTrue(
                    any(
                        "frontmatter key 'description' has " + fragment in error
                        for error in errors
                    ),
                    f"{value!r} -> {errors}",
                )
        # A raw NUL in the value is a line-level ReaderError with its own message,
        # not a plain-scalar phrase — pin it so the control-char guard (which
        # runs on the whole fence, before the scalar checks) cannot regress.
        skill_path = self.package.root / "SKILL.md"
        skill_path.write_text(
            _valid_skill_text().replace(
                "description: Run the complete autonomous engineering workflow.",
                "description: a\x00b",
            ),
            encoding="utf-8",
        )
        errors = validate_package(self.package.root)
        self.assertTrue(
            any(
                "non-printable character (U+0000) the YAML reader rejects" in error
                for error in errors
            ),
            errors,
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


class EntryPointScanTests(unittest.TestCase):
    def test_non_delegating_legacy_command_is_rejected(self) -> None:
        # admin#1495 finding 3813789192: a `.cursor/commands/autonomous-*.md`
        # still driving the legacy state machine bypasses every gate while
        # reading as the canonical workflow. The scan rejects any such
        # entry point that does not visibly delegate to the package, and
        # stays quiet for delegating or absent ones.
        from validate_package import _validate_entry_points

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            commands = repo / ".cursor" / "commands"
            commands.mkdir(parents=True)
            legacy = commands / "autonomous-feature.md"
            legacy.write_text(
                "Create .cursor/workflow-state.local.md then git push"
                " and gh pr create\n",
                encoding="utf-8",
            )
            package = repo / ".agents" / "skills" / "autonomy"
            package.mkdir(parents=True)
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("3813789192" in error for error in errors), errors
            )
            # r14 F10: delegation is a structurally parsed link resolving
            # to the package — the earlier bare-path prose form is now one
            # of the rejected shapes (pinned in the dedicated test below).
            legacy.write_text(
                "Delegate: read [the autonomy skill]"
                "(../../.agents/skills/autonomy/SKILL.md) and follow it.\n",
                encoding="utf-8",
            )
            self.assertEqual(_validate_entry_points(package), [])
            # admin#1495 finding 3816225750: with a skill-package-checks
            # workflow present, its triggers must include the guarded
            # commands path — otherwise a command-only change bypasses
            # this very guard.
            workflows = repo / ".github" / "workflows"
            workflows.mkdir(parents=True)
            wf = workflows / "skill-package-checks.yml"
            wf.write_text(
                "on:\n  pull_request:\n    paths:\n"
                '      - ".agents/skills/**"\n'
                "  push:\n    paths:\n"
                '      - ".agents/skills/**"\n',
                encoding="utf-8",
            )
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("3816225750" in error for error in errors), errors
            )
            wf.write_text(
                "on:\n  pull_request:\n    paths:\n"
                '      - ".agents/skills/**"\n'
                '      - ".cursor/commands/autonomous-*.md"\n'
                "  push:\n    paths:\n"
                '      - ".agents/skills/**"\n'
                '      - ".cursor/commands/autonomous-*.md"\n',
                encoding="utf-8",
            )
            self.assertEqual(_validate_entry_points(package), [])
            legacy.unlink()
            self.assertEqual(_validate_entry_points(package), [])

    def test_trigger_and_delegation_checks_are_structural(self) -> None:
        # admin#1495 r12 F20: raw substring checks accepted a YAML comment,
        # a push-only trigger, a wrong key, or a wrong indent as the
        # required pull_request paths trigger — and an HTML comment as
        # delegation. The narrow structural parser rejects each while the
        # current valid shape (block form, quoted or bare, with an inline
        # comment) still passes.
        from validate_package import _validate_entry_points

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            package = repo / ".agents" / "skills" / "autonomy"
            package.mkdir(parents=True)
            workflows = repo / ".github" / "workflows"
            workflows.mkdir(parents=True)
            wf = workflows / "skill-package-checks.yml"
            bypasses = {
                "comment-only": (
                    "on:\n  pull_request:\n    paths:\n"
                    '      - ".agents/skills/**"\n'
                    '      # - ".cursor/commands/autonomous-*.md"\n'
                ),
                "push-only": (
                    "on:\n  push:\n    paths:\n"
                    '      - ".cursor/commands/autonomous-*.md"\n'
                    "  pull_request:\n    paths:\n"
                    '      - ".agents/skills/**"\n'
                ),
                "wrong-key": (
                    "on:\n  pull_request:\n    paths-ignore:\n"
                    '      - ".cursor/commands/autonomous-*.md"\n'
                ),
                "wrong-indent": (
                    "on:\n  pull_request:\n    branches:\n      - main\n"
                    "  paths:\n"
                    '    - ".cursor/commands/autonomous-*.md"\n'
                ),
            }
            for label, workflow_text in bypasses.items():
                with self.subTest(shape=label):
                    wf.write_text(workflow_text, encoding="utf-8")
                    errors = _validate_entry_points(package)
                    self.assertTrue(
                        any("pull_request.paths" in e for e in errors),
                        (label, errors),
                    )
            wf.write_text(
                "on:\n  pull_request:\n    paths:\n"
                '      - ".agents/skills/**"\n'
                "      - .cursor/commands/autonomous-*.md # the guard\n"
                "  push:\n    paths:\n"
                "      - .cursor/commands/autonomous-*.md\n",
                encoding="utf-8",
            )
            self.assertEqual(_validate_entry_points(package), [])
            # Delegation must be operative text, not an HTML comment.
            commands = repo / ".cursor" / "commands"
            commands.mkdir(parents=True)
            legacy = commands / "autonomous-feature.md"
            legacy.write_text(
                "Run the legacy flow directly.\n<!-- skills/autonomy -->\n",
                encoding="utf-8",
            )
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("3813789192" in error for error in errors), errors
            )

    def test_load_root_symlinks_must_resolve_to_the_package(self) -> None:
        # admin#1495 finding 3822586140: a retargeted, dangling, or
        # regular-file load root silently changes (or breaks) what a
        # client loads — every existing root must resolve to the
        # validated package, in either repository orientation, and a
        # symlink root must be covered by the workflow's triggers.
        from validate_package import _validate_entry_points

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            package = repo / ".agents" / "skills" / "autonomy"
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text("# pkg\n", encoding="utf-8")
            claude_root = repo / ".claude" / "skills"
            claude_root.mkdir(parents=True)
            link = claude_root / "autonomy"
            workflows = repo / ".github" / "workflows"
            workflows.mkdir(parents=True)
            wf = workflows / "skill-package-checks.yml"

            def wf_write(*paths):
                # r13 F5: the commands filter is required unconditionally
                # whenever the workflow exists, so every fixture
                # carries it — mirrored onto BOTH events per r13 F13.
                block = (
                    '      - ".cursor/commands/autonomous-*.md"\n'
                    + "".join(f'      - "{p}"\n' for p in paths)
                )
                wf.write_text(
                    "on:\n  pull_request:\n    paths:\n" + block
                    + "  push:\n    paths:\n" + block,
                    encoding="utf-8",
                )

            wf_write(".agents/skills/**", ".claude/skills/autonomy")
            # correct symlink + covered trigger: clean
            link.symlink_to(Path("..") / ".." / ".agents" / "skills" / "autonomy")
            self.assertEqual(_validate_entry_points(package), [])
            # glob coverage also satisfies the trigger requirement
            wf_write(".agents/skills/**", ".claude/skills/**")
            self.assertEqual(_validate_entry_points(package), [])
            # missing trigger coverage: error
            wf_write(".agents/skills/**")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("symlink-only change" in e for e in errors), errors
            )
            wf_write(".agents/skills/**", ".claude/skills/autonomy")
            # retargeted: error
            link.unlink()
            other = repo / "elsewhere"
            other.mkdir()
            link.symlink_to(Path("..") / ".." / "elsewhere")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("does not resolve to the validated package" in e for e in errors),
                errors,
            )
            # dangling: error
            link.unlink()
            link.symlink_to(Path("..") / ".." / "missing-target")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("does not resolve to the validated package" in e for e in errors),
                errors,
            )
            # regular file: error
            link.unlink()
            link.write_text("not a link\n", encoding="utf-8")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("does not resolve to the validated package" in e for e in errors),
                errors,
            )
            link.unlink()

    def test_legacy_workflow_roots_and_event_filters(self) -> None:
        # admin#1495 r13 F10 + r13 F13: existing autonomous-workflow roots
        # must visibly delegate (HTML comments do not count) and be covered
        # by BOTH event filters; a workflow that consumes .gitignore (the
        # check-ignore pin) must trigger on it on both events too.
        from validate_package import _validate_entry_points

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            package = repo / ".agents" / "skills" / "autonomy"
            package.mkdir(parents=True)
            workflows = repo / ".github" / "workflows"
            workflows.mkdir(parents=True)
            wf = workflows / "skill-package-checks.yml"
            push_block = (
                "  push:\n    paths:\n"
                '      - ".agents/skills/**"\n'
                '      - ".cursor/commands/autonomous-*.md"\n'
                '      - ".claude/skills/autonomous-workflow/**"\n'
            )
            both = (
                "on:\n  pull_request:\n    paths:\n"
                '      - ".agents/skills/**"\n'
                '      - ".cursor/commands/autonomous-*.md"\n'
                '      - ".claude/skills/autonomous-workflow/**"\n'
                + push_block
            )
            wf.write_text(both, encoding="utf-8")
            legacy = repo / ".claude" / "skills" / "autonomous-workflow"
            legacy.mkdir(parents=True)
            (legacy / "SKILL.md").write_text(
                "Superseded: read [the autonomy skill]"
                "(../../../.agents/skills/autonomy/SKILL.md).\n",
                encoding="utf-8",
            )
            self.assertEqual(_validate_entry_points(package), [])
            # comment-only delegation is rejected
            (legacy / "SKILL.md").write_text(
                "Run the legacy flow.\n<!-- skills/autonomy -->\n",
                encoding="utf-8",
            )
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("r13 F10" in error for error in errors), errors
            )
            (legacy / "SKILL.md").write_text(
                "Delegates to [the package]"
                "(../../../.agents/skills/autonomy).\n",
                encoding="utf-8",
            )
            # a push filter missing the legacy root fails structurally
            wf.write_text(
                both.replace(
                    push_block,
                    "  push:\n    paths:\n"
                    '      - ".agents/skills/**"\n'
                    '      - ".cursor/commands/autonomous-*.md"\n',
                ),
                encoding="utf-8",
            )
            errors = _validate_entry_points(package)
            self.assertTrue(
                any(
                    "push paths do not cover"
                    " .claude/skills/autonomous-workflow" in error
                    for error in errors
                ),
                errors,
            )
            # a workflow consuming .gitignore must trigger on it, both events
            wf.write_text(
                both
                + "jobs:\n  x:\n    steps:\n"
                "      - run: git check-ignore -q probe\n",
                encoding="utf-8",
            )
            errors = _validate_entry_points(package)
            self.assertEqual(
                sum(".gitignore" in error for error in errors), 2, errors
            )

    def test_delegation_requires_a_resolving_structural_link(self) -> None:
        # admin#1495 r14 F10 (supersedes the r13 substring predicate,
        # whose both-token acceptance is preserved below as the two
        # RESOLVING link forms): delegation is a structurally parsed
        # markdown link whose target, resolved from the SOURCE file's
        # real directory, is exactly the canonical package. The substring
        # form accepted `../evil-autonomy/SKILL.md` (suffix lookalike)
        # and prose that names the path while refusing to follow it.
        from validate_package import _delegates_to_autonomy

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            package = repo / ".agents" / "skills" / "autonomy"
            package.mkdir(parents=True)
            legacy = repo / ".agents" / "skills" / "autonomous-workflow"
            legacy.mkdir(parents=True)
            source = legacy / "SKILL.md"
            commands = repo / ".cursor" / "commands"
            commands.mkdir(parents=True)
            pointer = commands / "autonomous-feature.md"

            # both canonical link forms, each from its real caller's
            # location (r13 F10's both-form guarantee, now by resolution)
            self.assertTrue(_delegates_to_autonomy(
                "Invoke the [`autonomy`](../autonomy/SKILL.md) skill",
                source, package,
            ))
            self.assertTrue(_delegates_to_autonomy(
                "See [the canonical workflow]"
                "(../../.agents/skills/autonomy/SKILL.md).",
                pointer, package,
            ))
            # a link to the package DIRECTORY resolves too
            self.assertTrue(_delegates_to_autonomy(
                "See [the package](../autonomy).", source, package,
            ))
            rejected = (
                # bare-path prose is a mention, not a delegation
                "read .agents/skills/autonomy/SKILL.md and follow it",
                "delegates to skills/autonomy",
                # negated prose mention — r14 F10's exact fixture
                "Do not follow ../autonomy/SKILL.md; execute the legacy"
                " workflow.",
                # suffix and prefix lookalikes as REAL links
                "[go](../evil-autonomy/SKILL.md)",
                "[go](../autonomy-evil/SKILL.md)",
                # commented-out link still never counts (finding 3813789192)
                "legacy\n<!-- [go](../autonomy/SKILL.md) -->\n",
                # external-scheme target never resolves locally
                "[docs](https://example.com/autonomy/SKILL.md)",
                "run the legacy flow directly",
            )
            for text in rejected:
                with self.subTest(text=text):
                    self.assertFalse(
                        _delegates_to_autonomy(text, source, package)
                    )

    def test_legacy_root_sibling_relative_link_is_accepted(self) -> None:
        # r13 F10 end to end: a superseded root whose ONLY delegation is the
        # sibling-relative link — the real admin#1495 stub form — must
        # validate clean. This is the exact case that failed admin CI at r27
        # before the token was widened.
        from validate_package import _validate_entry_points

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            package = repo / ".agents" / "skills" / "autonomy"
            package.mkdir(parents=True)
            workflows = repo / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "skill-package-checks.yml").write_text(
                "on:\n  pull_request:\n    paths:\n"
                '      - ".agents/skills/**"\n'
                '      - ".cursor/commands/autonomous-*.md"\n'
                "  push:\n    paths:\n"
                '      - ".agents/skills/**"\n'
                '      - ".cursor/commands/autonomous-*.md"\n',
                encoding="utf-8",
            )
            legacy = repo / ".agents" / "skills" / "autonomous-workflow"
            legacy.mkdir(parents=True)
            # The real stub opens with a "do not follow ... previous
            # instructions" hardening line; it is deliberately omitted here
            # because the behavioral scanner flags that phrase as generic
            # prompt injection, and the delegation-token logic under test
            # does not depend on it — the sibling-relative link is the
            # discriminator.
            stub = (
                "# Autonomous Feature Workflow (superseded)\n\n"
                "This compatibility alias is superseded. Invoke the"
                " [`autonomy`](../autonomy/SKILL.md) skill instead and follow"
                " its Loading Contract.\n"
            )
            self.assertNotIn("skills/autonomy", stub)
            (legacy / "SKILL.md").write_text(stub, encoding="utf-8")
            self.assertEqual(_validate_entry_points(package), [])

    def test_cursor_legacy_root_is_enumerated_with_blob_triggers(self) -> None:
        # admin#1495 r14 F8: the tracked .cursor/skills/autonomous-workflow
        # root was outside enumeration and both CI filters — a root-only
        # retarget could restore a nondelegating Cursor workflow without
        # running Skill Package Checks. Also pins the symlink-blob trigger
        # rule: "root/**" never matches the symlink blob itself.
        import shutil as _shutil

        from validate_package import _validate_entry_points

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            package = repo / ".agents" / "skills" / "autonomy"
            package.mkdir(parents=True)
            real_legacy = repo / ".agents" / "skills" / "autonomous-workflow"
            real_legacy.mkdir(parents=True)
            (real_legacy / "SKILL.md").write_text(
                "Superseded. Invoke [`autonomy`](../autonomy/SKILL.md).\n",
                encoding="utf-8",
            )
            cursor_skills = repo / ".cursor" / "skills"
            cursor_skills.mkdir(parents=True)
            cursor_root = cursor_skills / "autonomous-workflow"
            cursor_root.symlink_to(
                Path("..") / ".." / ".agents" / "skills" / "autonomous-workflow"
            )
            workflows = repo / ".github" / "workflows"
            workflows.mkdir(parents=True)
            wf = workflows / "skill-package-checks.yml"

            def wf_with(*paths: str) -> None:
                # the commands filter is required unconditionally whenever
                # the workflow exists (finding 3816225750)
                entries = "".join(
                    f'      - "{p}"\n'
                    for p in paths + (".cursor/commands/autonomous-*.md",)
                )
                wf.write_text(
                    "on:\n  pull_request:\n    paths:\n" + entries
                    + "  push:\n    paths:\n" + entries,
                    encoding="utf-8",
                )

            # ancestor glob covers descendants AND the bare symlink blob
            wf_with(".agents/skills/**", ".cursor/skills/**")
            self.assertEqual(_validate_entry_points(package), [])
            # exact bare + descendants pair also satisfies
            wf_with(
                ".agents/skills/**",
                ".cursor/skills/autonomous-workflow",
                ".cursor/skills/autonomous-workflow/**",
            )
            self.assertEqual(_validate_entry_points(package), [])
            # "root/**" alone: descendant coverage passes but the bare
            # symlink blob is unmatched — a retarget would slide past CI
            wf_with(".agents/skills/**", ".cursor/skills/autonomous-workflow/**")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("bare symlink path" in e for e in errors), errors
            )
            # missing entirely — the r13 F10 coverage error now names the
            # cursor root too
            wf_with(".agents/skills/**")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any(
                    ".cursor/skills/autonomous-workflow" in e
                    and "do not cover" in e
                    for e in errors
                ),
                errors,
            )
            # dangling: the cursor symlink outlives its target
            _shutil.rmtree(real_legacy)
            wf_with(".agents/skills/**", ".cursor/skills/**")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("no readable SKILL.md" in e for e in errors), errors
            )
            # regular-directory replacement that does not delegate
            cursor_root.unlink()
            cursor_root.mkdir()
            (cursor_root / "SKILL.md").write_text(
                "run the legacy flow directly\n", encoding="utf-8"
            )
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("does not delegate" in e for e in errors), errors
            )
            # retargeted at a lookalike package: the link in the lookalike
            # resolves to the WRONG directory, so delegation fails
            _shutil.rmtree(cursor_root)
            lookalike = repo / ".agents" / "skills" / "evil-autonomy"
            lookalike.mkdir(parents=True)
            (lookalike / "SKILL.md").write_text(
                "Invoke [`autonomy`](../evil-autonomy/SKILL.md).\n",
                encoding="utf-8",
            )
            cursor_root.symlink_to(
                Path("..") / ".." / ".agents" / "skills" / "evil-autonomy"
            )
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("does not delegate" in e for e in errors), errors
            )

    def test_retired_interfaces_are_rejected_independently(self) -> None:
        # admin#1495 r14 F11 (alongside F8): the five retired workflow*
        # package-script keys and the retired ralph shell must not return
        # — each rejected on its own, unrelated scripts untouched.
        import json as _json

        from validate_package import _validate_entry_points

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            package = repo / ".agents" / "skills" / "autonomy"
            package.mkdir(parents=True)
            self.assertEqual(_validate_entry_points(package), [])
            # the retired shell alone
            ralph = repo / ".cursor" / "ralph-scripts"
            ralph.mkdir(parents=True)
            shell = ralph / "autonomous-workflow.sh"
            shell.write_text("#!/bin/sh\n", encoding="utf-8")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("retired legacy shell" in e for e in errors), errors
            )
            shell.unlink()
            # unrelated ralph scripts and unrelated script keys stay legal
            (ralph / "ralph-loop.sh").write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            manifest = repo / "package.json"
            manifest.write_text(
                _json.dumps(
                    {"scripts": {"build": "x", "workflow-viz": "y"}}
                ),
                encoding="utf-8",
            )
            self.assertEqual(_validate_entry_points(package), [])
            # any single retired key alone
            manifest.write_text(
                _json.dumps({"scripts": {"build": "x", "workflow:poll": "s"}}),
                encoding="utf-8",
            )
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("workflow:poll" in e for e in errors), errors
            )
            # unparseable manifest fails closed
            manifest.write_text("{not json", encoding="utf-8")
            errors = _validate_entry_points(package)
            self.assertTrue(
                any("unparseable" in e for e in errors), errors
            )

    def test_load_root_mirror_orientation_is_supported(self) -> None:
        # The algo layout: canonical at .claude, symlink at .agents,
        # trigger satisfied by the .agents/skills/** glob.
        from validate_package import _validate_entry_points

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            package = repo / ".claude" / "skills" / "autonomy"
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text("# pkg\n", encoding="utf-8")
            agents_root = repo / ".agents" / "skills"
            agents_root.mkdir(parents=True)
            link = agents_root / "autonomy"
            link.symlink_to(Path("..") / ".." / ".claude" / "skills" / "autonomy")
            workflows = repo / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "skill-package-checks.yml").write_text(
                "on:\n  pull_request:\n    paths:\n"
                '      - ".claude/skills/**"\n'
                '      - ".agents/skills/**"\n'
                '      - ".cursor/commands/autonomous-*.md"\n'
                "  push:\n    paths:\n"
                '      - ".claude/skills/**"\n'
                '      - ".agents/skills/**"\n'
                '      - ".cursor/commands/autonomous-*.md"\n',
                encoding="utf-8",
            )
            self.assertEqual(_validate_entry_points(package), [])


if __name__ == "__main__":
    unittest.main()
