"""In-process unit tests for scripts/monitor_runner.py internals.

Kept separate from test_monitor_runner.py on purpose. That file drives the
runner as a real child process and, per the package's structural rule,
imports NOTHING from the package under test. These tests instead import
``Runner`` and a few module helpers directly to pin seams no black-box
integration test can reach:

  * ``Runner._child_command`` effort/resume/model threading (the CONSUMER
    half of the effort seam) and ``Runner._bind_owner`` model+effort adoption
    (the PRODUCER half). No integration test can distinguish "threaded from
    the binding" from "fell back to the module default" while the base and
    reviewer legs share the same ``max`` effort, so the guarantee is only
    pinnable by injecting a distinct effort/binding in-process.
  * ``_read_ceiling`` (the ``--max-candidate-bytes`` argparse type) and
    ``_scratch_refusal`` (the operator-facing fail-closed message), pure
    functions with no child to observe.

This file spawns no child process and names no dynamic-dispatch call, so it
does not trip the skill scanner and needs no split.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import unittest
from pathlib import Path
from unittest import mock

from model_policy import REVIEWER_EFFORT
from monitor_runner import (
    Runner,
    RunnerExit,
    SchemaCli,
    _read_ceiling,
    _scratch_refusal,
)

SCRIPTS = Path(__file__).resolve().parent


def _runner(owner_model: str, owner_effort: str | None) -> Runner:
    """A Runner built for argv inspection only. ``__init__`` resolves paths
    and reads the schema-CLI source bytes into memory (so ``schema_cli`` must
    point at a real file - the state file still need not exist); the owner
    fields the binding would set at monitor entry are assigned directly here."""

    args = argparse.Namespace(
        state_file=str(SCRIPTS / "unit-nonexistent-state.md"),
        skill_dir=str(SCRIPTS.parent),
        claude_bin="claude",
        schema_cli=str(SCRIPTS / "state_schema.py"),
        slice_budget=100.0,
        wait_scale=1.0,
        acknowledge_taint=None,
    )
    runner = Runner(args)
    # __init__ stages the wrapper exec file (a real mkstemp); reclaim every
    # helper-built runner's file when the module finishes so unit runs leave
    # no tmp litter — the production reclaim lives in main()'s finally.
    _HELPER_RUNNERS.append(runner)
    runner.owner_model = owner_model
    runner.owner_effort = owner_effort
    return runner


_HELPER_RUNNERS: list[Runner] = []


def tearDownModule() -> None:  # noqa: N802 — unittest hook name
    for helper_runner in _HELPER_RUNNERS:
        helper_runner.cleanup_wrapper_exec()


class ChildCommandThreadingTests(unittest.TestCase):
    """Pin the effort/resume/model threading in Runner._child_command."""

    def test_bound_effort_is_threaded_not_the_module_default(self) -> None:
        # R7 codex #17: base and reviewer effort are both "max" today, so an
        # integration assertion of `--effort max` still passes when the runner
        # stops threading the binding's effort (the argv default is also the
        # reviewer effort). Inject an effort distinct from every policy value
        # and from the default; the child command must carry THAT value, or
        # the threading has been dropped.
        sentinel = "unit-sentinel-effort"
        self.assertNotEqual(sentinel, REVIEWER_EFFORT)
        command = _runner("claude-opus-5", sentinel)._child_command(None)
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-opus-5")
        self.assertIn("--effort", command)
        self.assertEqual(command[command.index("--effort") + 1], sentinel)

    def test_absent_owner_effort_falls_back_to_the_reviewer_default(self) -> None:
        # The documented fallback: when the binding recorded no effort, the
        # child uses the module default. Pins that the else-arm exists and is
        # the reviewer floor, not an empty or omitted flag.
        command = _runner("claude-fable-5", None)._child_command(None)
        self.assertIn("--effort", command)
        self.assertEqual(command[command.index("--effort") + 1], REVIEWER_EFFORT)

    def test_resume_id_is_threaded_into_the_child_command(self) -> None:
        command = _runner("claude-opus-5", "max")._child_command("sid-42")
        self.assertIn("--resume", command)
        self.assertEqual(command[command.index("--resume") + 1], "sid-42")

    def test_absent_resume_id_omits_the_resume_flag(self) -> None:
        command = _runner("claude-opus-5", "max")._child_command(None)
        self.assertNotIn("--resume", command)

    def test_child_command_starts_with_the_pinned_binary(self) -> None:
        command = _runner("claude-opus-5", "max")._child_command(None)
        self.assertEqual(command[0], "claude")


class BindOwnerProducerTests(unittest.TestCase):
    """Pin the PRODUCER half of the effort seam: Runner._bind_owner adopts the
    binding's model AND effort, and fails closed on an unbound binding or owner
    drift (pass-3 opus #3 / codex #6). Deleting the effort adoption fails the
    first test even while every policy effort is 'max'."""

    @staticmethod
    def _binding(**overrides):
        base = {"state": "bound", "model": "claude-fable-5", "effort": "max"}
        base.update(overrides)
        return lambda runtime: base

    def test_adopts_binding_model_and_effort_over_the_decoys(self) -> None:
        sentinel = "unit-sentinel-effort"
        self.assertNotEqual(sentinel, REVIEWER_EFFORT)
        runner = _runner("decoy-model", "decoy-effort")
        runner._bind_owner(
            {},
            None,
            binding_provider=self._binding(model="claude-opus-5", effort=sentinel),
        )
        # Both fields come from the binding, overwriting the constructor decoys.
        self.assertEqual(runner.owner_model, "claude-opus-5")
        self.assertEqual(runner.owner_effort, sentinel)

    def test_unbound_binding_exits_suspect_state(self) -> None:
        runner = _runner("decoy-model", "decoy-effort")
        with self.assertRaises(RunnerExit) as caught:
            runner._bind_owner(
                {},
                None,
                binding_provider=self._binding(state="unbound", errors=["no route"]),
            )
        self.assertEqual(caught.exception.code, 4)

    def test_recorded_owner_drift_blocks(self) -> None:
        runner = _runner("decoy-model", "decoy-effort")
        with self.assertRaises(RunnerExit) as caught:
            runner._bind_owner(
                {},
                {"owner_model": "claude-sonnet-5"},
                binding_provider=self._binding(model="claude-fable-5"),
            )
        self.assertEqual(caught.exception.code, 5)

    def test_persisted_ownership_drift_blocks(self) -> None:
        runner = _runner("decoy-model", "decoy-effort")
        with self.assertRaises(RunnerExit) as caught:
            runner._bind_owner(
                {"monitor_ownership": {"model": "claude-sonnet-5"}},
                None,
                binding_provider=self._binding(model="claude-fable-5"),
            )
        self.assertEqual(caught.exception.code, 5)


class ReadCeilingValidatorTests(unittest.TestCase):
    """Pin the --max-candidate-bytes argparse type (pass-3 codex #12): a sub-1
    ceiling makes the candidate read ``read(0)`` (a no-op that rejects every
    candidate) or ``read(-1)`` (an unbounded whole-file slurp), so it must be
    rejected at parse time."""

    def test_rejects_zero_and_negative(self) -> None:
        for bad in ("0", "-1", "-2"):
            with self.subTest(value=bad):
                with self.assertRaises(argparse.ArgumentTypeError):
                    _read_ceiling(bad)

    def test_accepts_one_and_above(self) -> None:
        self.assertEqual(_read_ceiling("1"), 1)
        self.assertEqual(_read_ceiling("512"), 512)

    def test_non_integer_raises_for_argparse(self) -> None:
        with self.assertRaises(ValueError):
            _read_ceiling("not-a-number")


class ScratchRefusalMessageTests(unittest.TestCase):
    """Pin the operator-facing scratch-refusal message (pass-3 opus #7): it is
    the operator's only handle on the failed-closed guard, so it must name the
    path, the error type, BOTH triggering states (stale leftover / planted
    link), and that the candidate itself was untouched."""

    def test_message_names_path_error_both_states_and_untouched_candidate(
        self,
    ) -> None:
        message = _scratch_refusal(
            Path("/run/agent/state.snap"), FileExistsError("exists")
        )
        self.assertIn("/run/agent/state.snap", message)
        self.assertIn("FileExistsError", message)
        self.assertIn("stale leftover", message)
        self.assertIn("child-planted link", message)
        self.assertIn("NOT read or modified", message)


class SchemaCliInvocationTests(unittest.TestCase):
    """Pass-5 codex F1/F3: pin the memory-pin MECHANISM positively, not just
    the absence of one snapshot-dir prefix. A regression that reintroduced an
    on-disk snapshot (under ANY name) or dropped interpreter isolation would
    put a file path in argv or lose -I/-S here, so this fails on it."""

    def test_run_streams_pinned_bytes_through_an_isolated_stdin_interpreter(
        self,
    ) -> None:
        source = b"# pinned validator bytes\nprint('{}')\n"
        completed = mock.Mock(
            returncode=0, stdout=b'{"state": "valid"}', stderr=b""
        )
        with mock.patch(
            "monitor_runner.subprocess.run", return_value=completed
        ) as run:
            result = SchemaCli(source)._run(
                ["--monitor-extract"], Path("/tmp/state.md")
            )
        self.assertEqual(result, {"state": "valid"})
        run.assert_called_once()
        argv = run.call_args.args[0]
        # the validator SOURCE is streamed as bytes over stdin, never handed
        # to the child as a path - argv is exactly the isolated interpreter,
        # the stdin marker, the mode, and the TARGET being validated:
        self.assertEqual(
            argv,
            [
                sys.executable,
                "-I",
                "-S",
                "-",
                "--monitor-extract",
                "/tmp/state.md",
            ],
        )
        self.assertEqual(run.call_args.kwargs["input"], source)
        # no schema-source path of any kind appears in argv (the whole point
        # of the heap pin); the only path present is the validation target.
        self.assertNotIn("state_schema", " ".join(str(a) for a in argv))


class LaunchChildIsolationTests(unittest.TestCase):
    """Pass-10 codex: the owner-pinned wrapper child must boot under the SAME
    isolated interpreter as the validator child (-I -S), so a same-UID
    sitecustomize / .pth cannot run in the wrapper's Python before it launches
    the model. Drop -I -S from launch_child's wrapper argv and this fails - the
    runner's startup would then be only ONE of two unisolated boots, breaking
    the surface-(b) claim in the comment above _run. Popen is mocked, so no
    child spawns and the file's no-child-process property holds."""

    def test_wrapper_child_boots_under_the_isolated_interpreter(self) -> None:
        runner = _runner("claude-opus-5", "max")
        with mock.patch("monitor_runner.subprocess.Popen") as popen:
            popen.return_value = mock.Mock()
            runner.launch_child("the-prompt", None, 100.0)
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        # the isolated interpreter and its flags, then the wrapper + separator.
        # The wrapper execs from the runner's heap-pinned bytes staged into a
        # 0600 unpredictable-name file OUTSIDE the package (algo#1216 R2
        # finding 3779532260) — never from the child-writable package path.
        self.assertEqual(argv[:3], [sys.executable, "-I", "-S"])
        self.assertEqual(argv[3], str(runner.wrapper_exec_path))
        self.assertNotEqual(
            pathlib.Path(argv[3]).resolve().parent, SCRIPTS.resolve()
        )
        self.assertEqual(
            pathlib.Path(argv[3]).read_bytes(),
            (SCRIPTS / "monitor_child_wrapper.py").read_bytes(),
        )
        self.assertEqual(argv[4], "--")
        # the owner-pinned model argv follows the separator (binary first,
        # prompt last); the runner's -I -S gate only the wrapper's own boot and
        # are gone by the time the wrapper launches THIS:
        self.assertEqual(argv[5], "claude")
        self.assertEqual(argv[-1], "the-prompt")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
