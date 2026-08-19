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
import fnmatch
import os
import pathlib
import shutil
import tempfile
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import monitor_runner
from model_policy import REVIEWER_EFFORT
from monitor_runner import (
    Runner,
    atomic_write,
    durable_replace,
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
        claude_bin="/opt/homebrew/bin/claude",
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
        helper_runner.cleanup_wrapper_stage()


class LaunchSnapshotClearingTests(unittest.TestCase):
    """admin#1495 finding 3790049904 / algo#1216 finding 3788363456.

    ``charge_failure`` and ``_clear_in_flight`` commit canonical state with
    ``in_flight`` cleared; the launch snapshot MUST be dropped in the same
    step. Pre-fix it was retained, so the next PRE-launch charge (a
    ``spawn_failed`` before any new launch commit) compared moved-on
    canonical state against the stale snapshot and raised a false
    ``suspect_state`` instead of following bounded retry accounting. The
    reproduced two-failure sequence is pinned here in-process (no child,
    no subprocess: the schema CLI is stubbed; every other seam is real)."""

    class _StubSchema:
        """Stands in for the schema CLI only. ``extract`` is consulted
        exclusively by the pre-commit canonical recheck, so the call count
        doubles as the assertion that the recheck ran (or was skipped)."""

        def __init__(self) -> None:
            self.queued: list[dict] = []
            self.calls = 0

        def extract(self, path: object) -> dict:
            self.calls += 1
            return self.queued.pop(0)

    def _state_runner(self) -> Runner:
        tmp = Path(tempfile.mkdtemp(prefix="unit-launch-snapshot-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state = tmp / "state.md"
        state.write_text(
            "---\nstate_schema_version: 1\n---\n\nbody\n", encoding="utf-8"
        )
        args = argparse.Namespace(
            state_file=str(state),
            skill_dir=str(SCRIPTS.parent),
            claude_bin="/opt/homebrew/bin/claude",
            schema_cli=str(SCRIPTS / "state_schema.py"),
            slice_budget=100.0,
            wait_scale=1.0,
            acknowledge_taint=None,
        )
        runner = Runner(args)
        _HELPER_RUNNERS.append(runner)
        runner.owner_model = "claude-opus-5"
        runner.owner_effort = None
        return runner

    def _launched(self, runner: Runner) -> tuple[dict, "LaunchSnapshotClearingTests._StubSchema"]:
        launch_block = runner.current_block({})
        launch_block["in_flight"] = {"attempt_id": "a" * 32}
        stub = self._StubSchema()
        runner.schema = stub
        # As the launch commit records it, and as the healthy mid-tick
        # recheck will re-read it: canonical still byte-true to the snapshot.
        runner.launch_block = dict(launch_block)
        runner.launch_base_digest = "digest-1"
        stub.queued.append(
            {"monitor_cli": dict(launch_block), "digest": "digest-1"}
        )
        return launch_block, stub

    def test_charged_failure_drops_snapshot_and_pre_launch_charge_retries(
        self,
    ) -> None:
        runner = self._state_runner()
        launch_block, stub = self._launched(runner)
        runner.charge_failure(
            {"monitor_cli": dict(launch_block)}, "monitor-child:die_late"
        )
        # THE mechanism: the snapshot dies with the in_flight clear.
        self.assertIsNone(runner.launch_block)
        self.assertIsNone(runner.launch_base_digest)
        self.assertEqual(stub.calls, 1)
        # Next tick, spawn fails BEFORE any launch commit. Canonical has
        # legitimately moved past the old snapshot (in_flight nulled); the
        # queued entry is what a stale-snapshot compare would have read —
        # pre-fix this call raised RunnerExit(4, "suspect_state").
        post_commit = dict(launch_block)
        post_commit["in_flight"] = None
        stub.queued.append({"monitor_cli": post_commit, "digest": "digest-2"})
        runner.charge_failure(
            {"monitor_cli": dict(post_commit)}, "monitor-child:spawn_failed"
        )
        self.assertEqual(stub.calls, 1)  # no snapshot -> recheck skipped
        self.assertEqual(
            runner.consecutive_signature, "monitor-child:spawn_failed"
        )
        self.assertEqual(runner.consecutive_count, 1)
        self.assertEqual(
            [entry["signature"] for entry in runner.failures],
            ["monitor-child:die_late", "monitor-child:spawn_failed"],
        )
        # Both charges committed real state writes through the splice path.
        self.assertIn("monitor_cli:", runner.read_text())

    def test_clear_in_flight_drops_snapshot_too(self) -> None:
        runner = self._state_runner()
        launch_block, stub = self._launched(runner)
        runner._clear_in_flight({"monitor_cli": dict(launch_block)})
        self.assertIsNone(runner.launch_block)
        self.assertIsNone(runner.launch_base_digest)
        self.assertEqual(stub.calls, 1)




class DurableReplaceTests(unittest.TestCase):
    def test_replaces_and_removes_source(self) -> None:
        # admin#1495 R2 finding 3791925163: every commit-path namespace
        # update routes through one helper that also fsyncs the parent.
        tmp = Path(tempfile.mkdtemp(prefix="unit-durable-replace-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        source = tmp / "candidate.md"
        target = tmp / "state.md"
        source.write_text("new", encoding="utf-8")
        target.write_text("old", encoding="utf-8")
        durable_replace(source, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertFalse(source.exists())


class SidecarGateTests(unittest.TestCase):
    """admin#1495 R2 finding 3791925160: per-launch reconciliation with
    terminal compaction and bounded retention, driven in-process with the
    schema CLI stubbed (each queued extract is consumed per sidecar in
    sorted-glob order; every other seam is real)."""

    class _StubSchema:
        def __init__(self) -> None:
            self.queued: list[dict] = []

        def extract(self, path: object) -> dict:
            return self.queued.pop(0)

    def _runner_with_state(self) -> Runner:
        tmp = Path(tempfile.mkdtemp(prefix="unit-sidecar-gate-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state = tmp / "state.md"
        state.write_text(
            "---\nstate_schema_version: 1\n---\n\nbody\n", encoding="utf-8"
        )
        args = argparse.Namespace(
            state_file=str(state),
            skill_dir=str(SCRIPTS.parent),
            claude_bin="/opt/homebrew/bin/claude",
            schema_cli=str(SCRIPTS / "state_schema.py"),
            slice_budget=100.0,
            wait_scale=1.0,
            acknowledge_taint=None,
        )
        runner = Runner(args)
        _HELPER_RUNNERS.append(runner)
        return runner

    def _sidecar(self, runner: Runner, marker: str) -> Path:
        sidecar = runner.state_path.with_suffix(
            f".failed-candidate-{marker}.md"
        )
        sidecar.write_text("sidecar", encoding="utf-8")
        return sidecar

    def test_redundant_terminal_sidecar_is_compacted(self) -> None:
        # R2 follow-up 3793041749: compaction now requires the FULL record
        # to match (canonical-JSON digest), not just the status.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        sidecar = self._sidecar(runner, "aa")
        stub.queued.append(
            {
                "state": "valid",
                "handoff_results": {"qa": {"op-1": "complete"}},
                "handoff_result_digests": {"qa": {"op-1": "d" * 64}},
            }
        )
        canonical = {
            "handoff_results": {"qa": {"op-1": "complete"}},
            "handoff_result_digests": {"qa": {"op-1": "d" * 64}},
        }
        runner._gate_sidecars(canonical)  # no raise
        self.assertFalse(
            sidecar.exists(), "redundant terminal evidence must compact"
        )

    def test_same_status_differing_record_blocks_as_conflict(self) -> None:
        # R2 follow-up 3793041749: matching status with differing
        # attempts/evidence history is CONFLICTING evidence, never
        # redundant — the sidecar must survive and the gate must block.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        sidecar = self._sidecar(runner, "ee")
        stub.queued.append(
            {
                "state": "valid",
                "handoff_results": {"qa": {"op-7": "complete"}},
                "handoff_result_digests": {"qa": {"op-7": "a" * 64}},
            }
        )
        canonical = {
            "handoff_results": {"qa": {"op-7": "complete"}},
            "handoff_result_digests": {"qa": {"op-7": "b" * 64}},
        }
        with self.assertRaises(RunnerExit) as caught:
            runner._gate_sidecars(canonical)
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("CONFLICTS", caught.exception.reason)
        self.assertTrue(sidecar.exists())

    def test_unmerged_terminal_sidecar_blocks(self) -> None:
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        sidecar = self._sidecar(runner, "bb")
        stub.queued.append(
            {
                "state": "valid",
                "handoff_results": {"qa": {"op-2": "failed"}},
            }
        )
        canonical = {"handoff_results": {}}
        with self.assertRaises(RunnerExit) as caught:
            runner._gate_sidecars(canonical)
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("TERMINAL operation evidence", caught.exception.reason)
        self.assertTrue(sidecar.exists(), "never delete unmerged evidence")

    def test_no_status_valid_sidecar_is_compacted(self) -> None:
        # admin#1495 finding 3793025403: a valid sidecar with zero operation
        # results records no external intents — it must compact instead of
        # accumulating toward the retention block.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        sidecar = self._sidecar(runner, "ff")
        stub.queued.append(
            {"state": "valid", "handoff_results": {"qa": {}}}
        )
        # Entry passes compact_no_status=True; mid-slice gates never compact
        # (the attempt-scoped preservation contract keeps the current
        # streak's evidence).
        runner._gate_sidecars(
            {"handoff_results": {}}, compact_no_status=True
        )  # no raise
        self.assertFalse(sidecar.exists())
        stub.queued.append(
            {"state": "valid", "handoff_results": {"qa": {}}}
        )
        survivor = self._sidecar(runner, "gg")
        runner._gate_sidecars({"handoff_results": {}})  # mid-slice default
        self.assertTrue(survivor.exists())

    def test_attempt_stray_is_gated_like_a_sidecar(self) -> None:
        # algo#1216 finding 3792942215 (residue): a failed _preserve_failed
        # rename leaves the raw .attempt-* candidate behind — the gate must
        # classify it exactly like a failed-candidate sidecar.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        stray = runner.state_path.parent / (
            runner.state_path.name + ".attempt-" + "e" * 32 + ".md"
        )
        stray.write_text("---\nstate_schema_version: 1\n---\n", encoding="utf-8")
        self.addCleanup(lambda: stray.unlink(missing_ok=True))
        stub.queued.append(
            {
                "state": "valid",
                "handoff_results": {"qa": {"op-8": "pending"}},
            }
        )
        with self.assertRaises(RunnerExit) as caught:
            runner._gate_sidecars({"handoff_results": {}})
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("pending external intents", caught.exception.reason)
        self.assertTrue(stray.exists())

    def test_retention_limit_blocks_before_any_parse(self) -> None:
        # R2 re-reply 3792845972: the count ceiling is enforced BEFORE any
        # sidecar is schema-extracted — the untouched stub queue proves it.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        count = runner.SIDECAR_RETENTION_LIMIT + 1
        for index in range(count):
            self._sidecar(runner, f"{index:02d}")
            stub.queued.append({"state": "valid", "handoff_results": {}})
        with self.assertRaises(RunnerExit) as caught:
            runner._gate_sidecars({"handoff_results": {}})
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("retention limit", caught.exception.reason)
        self.assertEqual(
            len(stub.queued), count, "no sidecar may be parsed past the cap"
        )

    def test_conflicting_terminal_evidence_blocks_never_compacts(self) -> None:
        # R2 re-reply 3792845972: key-only matching deleted a sidecar
        # recording "complete" while canonical said "failed" — conflicting
        # histories are exactly what a human must reconcile.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        sidecar = self._sidecar(runner, "cc")
        stub.queued.append(
            {
                "state": "valid",
                "handoff_results": {"qa": {"op-9": "complete"}},
            }
        )
        canonical = {"handoff_results": {"qa": {"op-9": "failed"}}}
        with self.assertRaises(RunnerExit) as caught:
            runner._gate_sidecars(canonical)
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("CONFLICTS", caught.exception.reason)
        self.assertTrue(
            sidecar.exists(), "conflicting evidence must never be deleted"
        )

    def test_oversized_sidecar_blocks_without_parsing(self) -> None:
        # R2 re-reply 3792845972: byte ceiling enforced from stat metadata
        # BEFORE parsing — the empty stub queue proves no extract ran.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        runner.max_candidate_bytes = 64
        sidecar = self._sidecar(runner, "dd")
        sidecar.write_text("x" * 200, encoding="utf-8")
        with self.assertRaises(RunnerExit) as caught:
            runner._gate_sidecars({"handoff_results": {}})
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("failed validation", caught.exception.reason)
        self.assertTrue(sidecar.exists())


class ChildSkillSnapshotTests(unittest.TestCase):
    """admin#1495 R2 finding 3722356278 (follow-up 3777166503): the
    write-capable monitor child re-reads package prose and runs package
    scripts, and the live skill_dir can sit inside the mutable PR checkout.
    Those reads must hit a launch-time snapshot outside the package, pinned
    at __init__ and re-verified before every launch."""

    def _skill_fixture(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="unit-skill-fixture-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
        (tmp / "references").mkdir()
        (tmp / "references" / "guide.md").write_text("ref\n", encoding="utf-8")
        (tmp / "scripts").mkdir()
        (tmp / "scripts" / "state_schema.py").write_text(
            "# schema\n", encoding="utf-8"
        )
        return tmp

    def _snapshot_runner(self, skill_dir: Path) -> Runner:
        args = argparse.Namespace(
            state_file=str(skill_dir / "state.md"),
            skill_dir=str(skill_dir),
            claude_bin="/opt/homebrew/bin/claude",
            schema_cli=str(SCRIPTS / "state_schema.py"),
            slice_budget=100.0,
            wait_scale=1.0,
            acknowledge_taint=None,
        )
        runner = Runner(args)
        _HELPER_RUNNERS.append(runner)
        return runner

    def test_snapshot_copies_surface_and_pins_digests(self) -> None:
        skill = self._skill_fixture()
        runner = self._snapshot_runner(skill)
        snapshot = runner.child_skill_dir
        self.assertNotEqual(snapshot, skill)
        self.assertEqual(
            (snapshot / "SKILL.md").read_text(encoding="utf-8"), "# Skill\n"
        )
        self.assertEqual(
            (snapshot / "references" / "guide.md").read_text(encoding="utf-8"),
            "ref\n",
        )
        self.assertEqual(
            (snapshot / "scripts" / "state_schema.py").read_text(
                encoding="utf-8"
            ),
            "# schema\n",
        )
        self.assertEqual(
            set(runner.skill_manifest),
            {"SKILL.md", "references/guide.md", "scripts/state_schema.py"},
        )

    def test_post_init_mutation_of_the_live_package_never_reaches_it(
        self,
    ) -> None:
        skill = self._skill_fixture()
        runner = self._snapshot_runner(skill)
        # The takeover shape: a checkout swaps the live package content
        # AFTER the runner started.
        (skill / "SKILL.md").write_text(
            "# swapped by a checkout\n", encoding="utf-8"
        )
        (skill / "references" / "guide.md").write_text(
            "attacker prose\n", encoding="utf-8"
        )
        self.assertEqual(
            (runner.child_skill_dir / "SKILL.md").read_text(encoding="utf-8"),
            "# Skill\n",
        )
        # Live-package mutation must NOT trip the staging check either —
        # the snapshot is the pinned surface, the live dir is expected to
        # move under a takeover.
        runner._verify_skill_snapshot()

    def test_tampered_snapshot_fails_closed_before_launch(self) -> None:
        skill = self._skill_fixture()
        runner = self._snapshot_runner(skill)
        (runner.child_skill_dir / "SKILL.md").write_text(
            "tampered\n", encoding="utf-8"
        )
        with self.assertRaises(RunnerExit) as caught:
            runner._verify_skill_snapshot()
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("skill snapshot drifted", caught.exception.reason)

    def test_cleanup_removes_the_snapshot_directory(self) -> None:
        skill = self._skill_fixture()
        runner = self._snapshot_runner(skill)
        snapshot = runner.child_skill_dir
        self.assertTrue(snapshot.is_dir())
        runner.cleanup_wrapper_stage()
        self.assertFalse(snapshot.exists())



class DurableReplaceTests(unittest.TestCase):
    def test_fsync_failure_raises_instead_of_reporting_success(self) -> None:
        # admin#1495 finding 3793025395: injected EIO was swallowed and
        # durable_replace reported success on an unproven commit.
        import monitor_runner as mr
        tmp = Path(tempfile.mkdtemp(prefix="unit-durable-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        source = tmp / "a"
        target = tmp / "b"
        source.write_text("x", encoding="utf-8")
        real_fsync = os.fsync

        def failing_fsync(fd: int) -> None:
            import errno
            raise OSError(errno.EIO, "injected")

        with mock.patch.object(mr.os, "fsync", failing_fsync):
            with self.assertRaises(RunnerExit) as caught:
                mr.durable_replace(source, target)
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("fsync failed", caught.exception.reason)
        # Best-effort class (ENOTSUP) still succeeds silently.
        source.write_text("y", encoding="utf-8")

        def unsupported_fsync(fd: int) -> None:
            import errno
            raise OSError(errno.ENOTSUP, "unsupported")

        with mock.patch.object(mr.os, "fsync", unsupported_fsync):
            mr.durable_replace(source, target)  # no raise
        self.assertEqual(target.read_text(encoding="utf-8"), "y")


class SnapshotIntegrityTests(unittest.TestCase):
    def _fixture_runner(self):
        tmp = Path(tempfile.mkdtemp(prefix="unit-snap-fixture-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
        (tmp / "references").mkdir()
        (tmp / "references" / "guide.md").write_text("ref\n", encoding="utf-8")
        (tmp / "scripts").mkdir()
        (tmp / "scripts" / "state_schema.py").write_text(
            "# schema\n", encoding="utf-8"
        )
        args = argparse.Namespace(
            state_file=str(tmp / "state.md"),
            skill_dir=str(tmp),
            claude_bin="/opt/homebrew/bin/claude",
            schema_cli=str(SCRIPTS / "state_schema.py"),
            slice_budget=100.0,
            wait_scale=1.0,
            acknowledge_taint=None,
        )
        runner = Runner(args)
        _HELPER_RUNNERS.append(runner)
        return runner

    def test_planted_extra_file_fails_snapshot_verification(self) -> None:
        # admin#1495 finding 3793025406: hashing manifest entries alone
        # accepted ADDITIONS — a planted scripts/hashlib.py shadows stdlib
        # for the child's plain script invocations.
        runner = self._fixture_runner()
        (runner.child_skill_dir / "scripts" / "hashlib.py").write_text(
            "EVIL = True\n", encoding="utf-8"
        )
        with self.assertRaises(RunnerExit) as caught:
            runner._verify_skill_snapshot()
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("does not equal the manifest", caught.exception.reason)

    def test_symlink_in_snapshot_fails_verification(self) -> None:
        runner = self._fixture_runner()
        staged = runner.child_skill_dir / "SKILL.md"
        staged.unlink()
        staged.symlink_to("/etc/hosts")
        with self.assertRaises(RunnerExit) as caught:
            runner._verify_skill_snapshot()
        self.assertEqual(caught.exception.code, 4)

    def test_fifo_read_fails_closed(self) -> None:
        # algo#1216 finding 3792942225: a planted FIFO blocked the runner
        # past its slice deadline; _read_regular_file refuses special files
        # without blocking.
        import monitor_runner as mr
        tmp = Path(tempfile.mkdtemp(prefix="unit-fifo-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fifo = tmp / "candidate.md"
        os.mkfifo(fifo)
        with self.assertRaises(RunnerExit) as caught:
            mr._read_regular_file(fifo, 1024)
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("not a regular file", caught.exception.reason)


class ChildEnvAllowlistTests(unittest.TestCase):
    def test_ambient_sentinel_never_reaches_the_child_env(self) -> None:
        # algo#1216 finding 3792942221 (in-package half): the child env is
        # allowlist-built — an unrelated ambient variable provably reached
        # the child under the old denylist.
        runner = _runner("claude-opus-5", None)
        with mock.patch.dict(os.environ, {
            "AMBIENT_SENTINEL_XYZ": "leak",
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "CLAUDE_CONFIG_DIR": "/keep/me",
            "FAKE_MODE": "ok",
        }):
            captured = {}

            class _Proc:
                pid = 4242
                stdin = None
                stdout = None
                stderr = None

            def fake_popen(argv, **kwargs):
                captured.update(kwargs.get("env") or {})
                raise OSError("stop before real spawn")

            with mock.patch.object(
                mr_module().subprocess, "Popen", fake_popen
            ):
                with self.assertRaises(RunnerExit):
                    runner.launch_child("prompt", None, 60)
        self.assertNotIn("AMBIENT_SENTINEL_XYZ", captured)
        self.assertIn("PATH", captured)
        self.assertIn("CLAUDE_CONFIG_DIR", captured)
        # claude_bin is the real name "claude" here, so FAKE_* is stripped.
        self.assertNotIn("FAKE_MODE", captured)

    def test_claude_prefixed_secrets_never_reach_the_child(self) -> None:
        # mm#3551 finding 3806719670 + algo#1216 finding 3807740755: the
        # ACCOUNT-token bundle must never reach the child, while the
        # child's OWN session auth (CLAUDE_CODE_OAUTH_TOKEN — Keeper VMs
        # run an OAuth-only contract) must — stripping it left the child
        # unauthenticated. Exact names either way.
        runner = _runner("claude-opus-5", None)
        with mock.patch.dict(os.environ, {
            "CLAUDE_CODE_OAUTH_TOKEN": "secret-token",
            "CLAUDE_ACCOUNT_TOKENS_JSON": "{\"bundle\": true}",
            "CLAUDE_CONFIG_DIR": "/keep/me",
            "PATH": os.environ.get("PATH", "/usr/bin"),
        }):
            captured = {}

            def fake_popen(argv, **kwargs):
                captured.update(kwargs.get("env") or {})
                raise OSError("stop before real spawn")

            with mock.patch.object(
                mr_module().subprocess, "Popen", fake_popen
            ):
                with self.assertRaises(RunnerExit):
                    runner.launch_child("prompt", None, 60)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", captured)
        self.assertNotIn("CLAUDE_ACCOUNT_TOKENS_JSON", captured)
        self.assertIn("CLAUDE_CONFIG_DIR", captured)


def mr_module():
    import monitor_runner
    return monitor_runner


class WrapperStagingTests(unittest.TestCase):
    """algo#1216 R2 finding 3779532260 composed with the in-memory trust base
    (pass-4 codex C-F1): the barrier wrapper's SOURCE bytes are pinned in the
    runner's heap at init — before any child has run — and the staged launch
    file lives outside the child-writable package with an unpredictable name.
    cleanup_wrapper_stage (main's finally) reclaims it. Lives HERE, not in the
    e2e file: this file holds the no-child-process direct-construction tests
    (scanner structural rule keeps the e2e file's subprocess calls away from
    module-loading machinery)."""

    def test_runner_pins_wrapper_bytes_and_stages_outside_the_worktree(self) -> None:
        runner = _runner("claude-opus-5", "max")
        source = SCRIPTS / "monitor_child_wrapper.py"
        self.assertEqual(runner.wrapper_source, source.read_bytes())
        self.assertTrue(runner.wrapper_stage_path.exists())
        self.assertNotEqual(
            runner.wrapper_stage_path.resolve().parent, SCRIPTS.resolve()
        )
        runner.cleanup_wrapper_stage()
        self.assertFalse(runner.wrapper_stage_path.exists())


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
        # admin#1495 finding 3807823238 / mm#3551 finding 3808151945: hosts
        # legitimately resolve the binary to an absolute path — pin the
        # BASENAME, not the literal bare name.
        self.assertEqual(Path(command[0]).name, "claude")


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
        self.assertEqual(argv[3], str(runner.wrapper_stage_path))
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
        self.assertEqual(Path(argv[5]).name, "claude")
        self.assertEqual(argv[-1], "the-prompt")


class _ScriptedDatetime:
    """Stand-in for monitor_runner.datetime with a scripted now() sequence
    (the last entry repeats). strptime delegates to the real class so the
    method under test parses exactly as production does."""

    script: list[datetime] = []

    @staticmethod
    def strptime(raw: str, fmt: str) -> datetime:
        return datetime.strptime(raw, fmt)

    @classmethod
    def now(cls, tz: timezone | None = None) -> datetime:
        return cls.script.pop(0) if len(cls.script) > 1 else cls.script[0]


class ResumeLivenessWaitTests(unittest.TestCase):
    """algo#1216 finding 3807740769 (admin 3807823260 / mm 3808151933):
    resume restored the ladder rung but never consumed the persisted
    next_retry_at, so a new slice launched immediately with backoff time
    remaining. _resume_liveness_wait must sleep out the remainder
    (budget-bounded) before the loop may launch."""

    T0 = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

    def _extract_with_deadline(self, deadline: datetime) -> dict:
        return {
            "monitor_cli": {
                "liveness": {
                    "rung": 1,
                    "next_retry_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            }
        }

    def _wait_with(self, extract: dict, now_script: list, remaining: float):
        runner = _runner("claude-opus-5", None)
        runner.remaining = lambda: remaining
        sleeps: list[float] = []
        _ScriptedDatetime.script = list(now_script)
        with mock.patch.object(monitor_runner, "datetime", _ScriptedDatetime):
            with mock.patch("time.sleep", side_effect=sleeps.append):
                runner._resume_liveness_wait(extract)
        return sleeps

    def test_future_deadline_is_slept_out_before_any_launch(self) -> None:
        deadline = self.T0 + timedelta(seconds=4)
        sleeps = self._wait_with(
            self._extract_with_deadline(deadline),
            [self.T0, self.T0 + timedelta(seconds=5)],
            remaining=10_000.0,
        )
        self.assertEqual(sleeps, [4.0], "the persisted remainder must be slept")

    def test_elapsed_deadline_passes_straight_through(self) -> None:
        deadline = self.T0 + timedelta(seconds=4)
        sleeps = self._wait_with(
            self._extract_with_deadline(deadline),
            [self.T0 + timedelta(seconds=10)],
            remaining=10_000.0,
        )
        self.assertEqual(sleeps, [], "an elapsed deadline must not wait")

    def test_exhausted_budget_returns_without_sleeping(self) -> None:
        # The loop's own budget check then ends the slice — the wait must
        # not eat into cleanup margin.
        deadline = self.T0 + timedelta(seconds=4)
        sleeps = self._wait_with(
            self._extract_with_deadline(deadline),
            [self.T0],
            remaining=0.0,
        )
        self.assertEqual(sleeps, [], "no budget means no wait, not a launch")


class RestageWrapperTests(unittest.TestCase):
    def test_restage_never_writes_through_a_planted_symlink(self) -> None:
        # mm#3551 finding 3808151918: Path.write_bytes followed a
        # child-planted symlink at the stage path, redirecting the trusted
        # rewrite into an arbitrary same-UID target. _restage_wrapper
        # unlinks the planted NAME and recreates with O_EXCL|O_NOFOLLOW;
        # revert it to write_bytes and the victim assertion fails.
        runner = _runner("claude-opus-5", None)
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim-state.md"
            victim.write_bytes(b"KEEP")
            stage = runner.wrapper_stage_path
            stage.unlink()
            os.symlink(victim, stage)
            runner._restage_wrapper()
            self.assertEqual(victim.read_bytes(), b"KEEP")
            self.assertFalse(stage.is_symlink())
            self.assertEqual(stage.read_bytes(), runner.wrapper_source)


# The glob set state-and-safety.md tells consuming repos to .gitignore —
# stated as literals on purpose (the shapes ARE the contract under test).
GITIGNORE_ARTIFACT_GLOBS = (
    "workflow-state.local.md.monitor.lock",
    "workflow-state.local.md.attempt-*",
    "workflow-state.local.md.tmp-*",
    "workflow-state.local.failed-candidate-*.md",
)


class GitignoreArtifactShapeTests(unittest.TestCase):
    """admin#1495 finding 3807823247: two documented globs matched nothing
    the runner writes — ``with_suffix`` REPLACES ``.md`` on the
    failed-candidate sidecar, and the atomic-write temp was undocumented.
    Each artifact name here comes from the runner's REAL constructor
    (never a re-derived expression), so a constructor shape change or a
    glob drift fails this test instead of surfacing as untracked dirt in
    a consuming repository."""

    def _covered(self, name: str) -> bool:
        return any(
            fnmatch.fnmatch(name, glob) for glob in GITIGNORE_ARTIFACT_GLOBS
        )

    def test_runner_artifact_names_match_the_documented_globs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "workflow-state.local.md"

            args = argparse.Namespace(
                state_file=str(state),
                skill_dir=str(SCRIPTS.parent),
                claude_bin="/opt/homebrew/bin/claude",
                schema_cli=str(SCRIPTS / "state_schema.py"),
                slice_budget=100.0,
                wait_scale=1.0,
                acknowledge_taint=None,
            )
            runner = Runner(args)
            _HELPER_RUNNERS.append(runner)
            self.assertEqual(
                runner.lock_path.name, "workflow-state.local.md.monitor.lock"
            )
            self.assertTrue(self._covered(runner.lock_path.name))

            # atomic_write's temp: capture the real source of the final
            # replace instead of re-deriving the with_suffix expression.
            replaced: list[Path] = []
            with mock.patch(
                "monitor_runner.durable_replace",
                side_effect=lambda source, target: replaced.append(source),
            ):
                atomic_write(state, "content\n")
            self.assertEqual(len(replaced), 1)
            tmp_name = replaced[0].name
            self.assertTrue(
                tmp_name.startswith("workflow-state.local.md.tmp-"), tmp_name
            )
            self.assertTrue(self._covered(tmp_name))

            # Attempt candidate + failed-candidate sidecar, round-tripped
            # through _preserve_failed's own parser: if run_tick's candidate
            # shape drifted from this synthesis, the parsed attempt id would
            # come back "unknown" and the equality below would fail.
            attempt_id = "0123456789abcdef"
            candidate_name = state.name + f".attempt-{attempt_id}.md"
            self.assertTrue(self._covered(candidate_name))
            preserved: list[Path] = []
            stub = types.SimpleNamespace(state_path=state)
            with mock.patch(
                "monitor_runner.durable_replace",
                side_effect=lambda source, target: preserved.append(target),
            ):
                Runner._preserve_failed(stub, Path(tmp) / candidate_name)
            self.assertEqual(len(preserved), 1)
            sidecar_name = preserved[0].name
            self.assertEqual(
                sidecar_name,
                f"workflow-state.local.failed-candidate-{attempt_id}.md",
            )
            self.assertTrue(self._covered(sidecar_name))
            # The r15 glob this finding corrected must NOT match the real
            # sidecar — with_suffix replaced the .md rather than appending.
            self.assertFalse(
                fnmatch.fnmatch(
                    sidecar_name, "workflow-state.local.md.failed-candidate-*"
                )
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
