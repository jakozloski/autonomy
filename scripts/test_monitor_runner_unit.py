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
import re
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

    @staticmethod
    def _survives(sidecar: Path) -> bool:
        """r14 F17: kept sidecars live on under their quarantine name
        (original + .q<pid>) — evidence survival is checked across both."""
        if sidecar.exists():
            return True
        return any(sidecar.parent.glob(sidecar.name + ".q*"))

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
            self._survives(sidecar), "redundant terminal evidence must compact"
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
        self.assertTrue(self._survives(sidecar))

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
        self.assertTrue(self._survives(sidecar), "never delete unmerged evidence")

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
        self.assertFalse(self._survives(sidecar))
        stub.queued.append(
            {"state": "valid", "handoff_results": {"qa": {}}}
        )
        survivor = self._sidecar(runner, "gg")
        runner._gate_sidecars({"handoff_results": {}})  # mid-slice default
        self.assertTrue(self._survives(survivor))

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
        self.assertTrue(self._survives(stray))

    def test_replacement_during_parse_survives_compaction(self) -> None:
        # r14 F17's exact race: a file substituted at the ORIGINAL name
        # while the gate parses must never be deleted on the strength of
        # the benign content that was parsed. The quarantine rename binds
        # the compaction unlink to the parsed inode, so the substitute
        # survives.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        sidecar = self._sidecar(runner, "race")
        substitute_holder: dict[str, Path] = {}

        original_extract = stub.extract

        def racing_extract(path):
            # the child strikes between parse and compaction: a NEW file
            # appears at the original name
            substitute = sidecar
            substitute.write_text("substituted evidence", encoding="utf-8")
            substitute_holder["path"] = substitute
            return original_extract(path)

        stub.extract = racing_extract
        stub.queued.append({"state": "valid", "handoff_results": {}})
        runner._gate_sidecars({"handoff_results": {}}, compact_no_status=True)
        self.assertTrue(
            substitute_holder["path"].exists(),
            "the substituted file at the original name must survive"
            " compaction of the parsed inode",
        )
        self.assertEqual(
            substitute_holder["path"].read_text(encoding="utf-8"),
            "substituted evidence",
        )

    def test_retention_limit_blocks_after_bounded_batch(self) -> None:
        # R2 re-reply 3792845972, amended by admin#1495 r12 F16: over-limit
        # no longer blocks before compaction — the BOUNDED batch (limit + 1
        # entries, never the full pile) classifies first so compactable
        # sidecars can shed; a mid-slice gate (compact_no_status=False)
        # leaves no-intent sidecars in place, so the rescan still exceeds
        # and the block fires with the batch-compaction message. Startup
        # work stays bounded: exactly the enumerated batch is parsed.
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
        self.assertIn("bounded batch compaction", caught.exception.reason)
        self.assertEqual(
            len(stub.queued), 0, "exactly the bounded batch is parsed"
        )

    def _remaining_sidecars(self, runner: Runner) -> list[str]:
        return sorted(
            f.name
            for f in runner.state_path.parent.iterdir()
            if f.is_file() and ".failed-candidate-" in f.name
        )

    def test_over_limit_no_intent_sidecars_compact_at_entry(self) -> None:
        # admin#1495 r12 F16 (the fix): at ENTRY (compact_no_status=True)
        # the same over-limit pile of valid no-intent sidecars compacts
        # inside the bounded batch and the gate proceeds without a block.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        count = runner.SIDECAR_RETENTION_LIMIT + 1
        for index in range(count):
            self._sidecar(runner, f"{index:02d}")
            stub.queued.append({"state": "valid", "handoff_results": {}})
        runner._gate_sidecars({"handoff_results": {}}, compact_no_status=True)
        self.assertEqual(self._remaining_sidecars(runner), [])

    def test_over_limit_batches_persist_progress_across_gates(self) -> None:
        # 45 no-intent sidecars: the first gate compacts one bounded batch
        # (21), rescans, and blocks on the 24 that remain; the second gate
        # compacts 21 more and proceeds with 3 under the limit — each
        # deletion is durable progress across invocations.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        for index in range(45):
            self._sidecar(runner, f"{index:03d}")
        for _ in range(runner.SIDECAR_RETENTION_LIMIT + 1):
            stub.queued.append({"state": "valid", "handoff_results": {}})
        with self.assertRaises(RunnerExit) as caught:
            runner._gate_sidecars(
                {"handoff_results": {}}, compact_no_status=True
            )
        self.assertIn("bounded batch compaction", caught.exception.reason)
        self.assertEqual(len(self._remaining_sidecars(runner)), 24)
        for _ in range(runner.SIDECAR_RETENTION_LIMIT + 1):
            stub.queued.append({"state": "valid", "handoff_results": {}})
        runner._gate_sidecars({"handoff_results": {}}, compact_no_status=True)
        self.assertEqual(len(self._remaining_sidecars(runner)), 3)

    def test_quarantine_never_clobbers_preexisting_evidence(self) -> None:
        # r14 F17 re-eval: a recycled pid colliding with older quarantined
        # evidence must not destroy it — the move is no-clobber (os.link
        # EEXIST + counter fallback), so both survive.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        sidecar = self._sidecar(runner, "clash")
        older = sidecar.with_name(sidecar.name + f".q{os.getpid()}")
        older.write_text("older-quarantined-evidence", encoding="utf-8")
        # Both files are scanned as sidecars; invalid extracts retain both
        # as unreadable, which isolates the property under test: the MOVE
        # itself must not replace the pre-existing quarantine target.
        stub.queued.append({"state": "invalid", "errors": ["x"]})
        stub.queued.append({"state": "invalid", "errors": ["x"]})
        with self.assertRaises(RunnerExit):
            runner._gate_sidecars(
                {"handoff_results": {}}, compact_no_status=True
            )
        # Both files may themselves be (re-)quarantined by the pass; the
        # property is CONTENT SURVIVAL — the older evidence must exist
        # somewhere, byte-identical, never replaced by the colliding move.
        contents = [
            f.read_text(encoding="utf-8")
            for f in runner.state_path.parent.iterdir()
            if f.is_file() and ".failed-candidate-" in f.name
        ]
        self.assertIn(
            "older-quarantined-evidence", contents,
            "pre-existing quarantined evidence must survive the collision",
        )
        self.assertIn(
            "sidecar", contents,
            "the newly quarantined sidecar must survive too",
        )

    def test_retention_scan_stops_at_first_over_limit_match(self) -> None:
        # admin#1495 finding 3813789211: the enumeration itself is bounded —
        # glob() materialized all 128 matches under the monitor lock in R2's
        # repro. The scan streams os.scandir entries and stops at limit + 1,
        # so the yield count stays far below the accumulated pile.
        runner = self._runner_with_state()
        stub = self._StubSchema()
        runner.schema = stub
        limit = runner.SIDECAR_RETENTION_LIMIT
        total = limit + 50
        for index in range(total):
            self._sidecar(runner, f"{index:03d}")
        # admin#1495 r12 F16: the bounded batch is parsed before the block
        # now, so the stub queue holds exactly one batch; the mid-slice
        # gate keeps the no-intent sidecars, and the rescan (a second
        # bounded scan) still exceeds.
        for _ in range(limit + 1):
            stub.queued.append({"state": "valid", "handoff_results": {}})
        real_scandir = os.scandir
        counted: list[str] = []

        class _CountingScandir:
            def __init__(self, path: object) -> None:
                self._inner = real_scandir(path)

            def __enter__(self) -> "_CountingScandir":
                return self

            def __exit__(self, *exc: object) -> bool:
                self._inner.close()
                return False

            def __iter__(self) -> "_CountingScandir":
                return self

            def __next__(self):
                entry = next(self._inner)
                counted.append(entry.name)
                return entry

        with mock.patch.object(monitor_runner.os, "scandir", _CountingScandir):
            with self.assertRaises(RunnerExit) as caught:
                runner._gate_sidecars({"handoff_results": {}})
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("more than", caught.exception.reason)
        matching = sum(
            1
            for name in counted
            if ".failed-candidate" in name or ".attempt-" in name
        )
        self.assertEqual(
            matching, 2 * (limit + 1),
            "each scan (batch + rescan) must stop at the first over-limit"
            " match",
        )
        self.assertLess(
            len(counted), total,
            "an over-limit pile must never be fully enumerated",
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
            self._survives(sidecar), "conflicting evidence must never be deleted"
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
        self.assertTrue(self._survives(sidecar))


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



class DurableReplaceFsyncTests(unittest.TestCase):
    # NOTE: formerly a second `DurableReplaceTests` — a duplicate class name
    # in one module SHADOWS the first binding, so the basic replace tests
    # above silently stopped running under discovery. Distinct name keeps
    # both suites live.
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
        # r11 finding 3825265246 REVERSED the old best-effort pin: an
        # ENOTSUP/EACCES-class directory fsync is an UNPROVEN commit on
        # the path holding the only record of possibly-fired external
        # mutations — it fails closed like any other fsync error.
        source.write_text("y", encoding="utf-8")

        def unsupported_fsync(fd: int) -> None:
            import errno
            raise OSError(errno.ENOTSUP, "unsupported")

        with mock.patch.object(mr.os, "fsync", unsupported_fsync):
            with self.assertRaises(RunnerExit) as caught:
                mr.durable_replace(source, target)
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("fsync failed", caught.exception.reason)


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

    @classmethod
    def fromisoformat(cls, value: str) -> datetime:
        # r15 F11: _parse_retry_deadline normalizes via fromisoformat —
        # the scripted clock stubs `now`, never parsing.
        return datetime.fromisoformat(value)


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


class MappedRepositoryParityTests(unittest.TestCase):
    def test_manifest_matches_the_qa_owner_map(self) -> None:
        # algo#1216 finding 3813491661: the runner is import-free of
        # package modules, so its mapped-repository set restates the key
        # set of handoff_decision.QA_OWNER_BY_REPOSITORY — that map is
        # canonical, and this regression fails on any drift in either
        # direction.
        import handoff_decision

        self.assertEqual(
            monitor_runner.MAPPED_QA_REPOSITORIES,
            {
                key.casefold()
                for key in handoff_decision.QA_OWNER_BY_REPOSITORY
            },
        )

    def test_origin_url_parsing_covers_the_three_git_shapes(self) -> None:
        for url, expected in (
            ("git@github.com:Keeper-Dating/matchmaking.git", "Keeper-Dating/matchmaking"),
            ("https://github.com/Keeper-Dating/algo", "Keeper-Dating/algo"),
            ("ssh://git@github.com/Keeper-Dating/admin-portal.git", "Keeper-Dating/admin-portal"),
            ("https://github.com/Keeper-Dating/matchmaking.git/", "Keeper-Dating/matchmaking"),
            ("not a url", None),
            ("", None),
        ):
            self.assertEqual(
                monitor_runner._repo_name_with_owner(url), expected, url
            )


class TerminalPlannedQaTests(unittest.TestCase):
    def test_terminal_missing_planned_qa_flags_only_mapped_idle(self) -> None:
        # algo#1216 finding 3813491661, pinned at the predicate the manifest
        # rule calls. The r17 F9 containment gate now preempts that branch end
        # to end on a non-delegating host (a mapped launch blocks BEFORE any
        # child produces a terminal candidate), so the rule is verified here
        # directly rather than through the now-gated e2e path. A mapped
        # repository whose QA aggregate is idle or absent is a missing-handoff
        # terminal; every other combination stays valid.
        mapped = "keeper-dating/matchmaking"
        self.assertIn(mapped, monitor_runner.MAPPED_QA_REPOSITORIES)
        for bound_repo, qa_status, expected in (
            (mapped, "idle", True),
            (mapped, None, True),
            ("Keeper-Dating/Matchmaking", "idle", True),  # case-insensitive
            (mapped, "failed", False),
            (mapped, "complete", False),
            ("someone/unmapped-repo", "idle", False),
            ("someone/unmapped-repo", None, False),
            (None, "idle", False),  # a non-str bound repo is never mapped
        ):
            self.assertIs(
                monitor_runner._terminal_missing_planned_qa(
                    bound_repo, qa_status
                ),
                expected,
                (bound_repo, qa_status),
            )


# CapabilityFamilyResolutionTests (r17) was REPLACED by
# CapabilityGrammarTests + the capability-probe e2e set (algo#1216 r18 F3
# / admin#1495 r14 F9): the helpers it pinned resolved families from name
# substrings, granted them from mcpServers configuration presence, and
# accepted any listing line naming a family — each shape R2 demonstrated
# as a live false-grant. Its still-valid concerns carry forward: the
# allow-union and github-only-incomplete pins live in
# test_mutation_grant_matrix + the github-only e2e block test; deny
# precedence (explicit and catch-all) lives in
# test_denied_families_shapes + the deny-all e2e; malformed-token
# tolerance lives in test_mutation_grant_matrix and unknown-shape
# fail-closed in test_denied_families_shapes.

class SidecarQuarantineRenameTests(unittest.TestCase):
    # admin#1495 F5: quarantine must be ONE atomic no-replace move, never
    # link+unlink. These pin the properties that close the same-UID
    # source-replacement race the old link+unlink left open.

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _sidecar(self, name: str, body: str = "x") -> Path:
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_move_is_atomic_source_and_target_never_coexist(self) -> None:
        # link+unlink left BOTH names bound to one inode mid-operation; the
        # atomic rename never does — after it, exactly one name exists.
        src = self._sidecar("state.md.sidecar-1234", "EVIDENCE")
        quarantined = monitor_runner._quarantine_sidecar(src)
        self.assertIsNotNone(quarantined)
        self.assertFalse(src.exists())
        self.assertTrue(quarantined.exists())
        self.assertEqual(quarantined.read_text(encoding="utf-8"), "EVIDENCE")

    def test_no_replace_preserves_an_existing_target(self) -> None:
        # RENAME_NOREPLACE / RENAME_EXCL: colliding onto an existing name
        # raises FileExistsError and leaves BOTH files intact — a plain
        # rename would have destroyed the older quarantined evidence.
        src = self._sidecar("s", "new")
        dst = self._sidecar("t", "older-quarantined")
        with self.assertRaises(FileExistsError):
            monitor_runner._rename_noreplace(src, dst)
        self.assertEqual(src.read_text(encoding="utf-8"), "new")
        self.assertEqual(dst.read_text(encoding="utf-8"), "older-quarantined")

    def test_collision_advances_the_counter_suffix(self) -> None:
        src = self._sidecar("state.md.sidecar-5678", "B")
        collide = self.dir / f"state.md.sidecar-5678.q{os.getpid()}"
        collide.write_text("OLD", encoding="utf-8")
        quarantined = monitor_runner._quarantine_sidecar(src)
        self.assertIsNotNone(quarantined)
        self.assertTrue(quarantined.name.endswith(f".q{os.getpid()}-1"))
        self.assertEqual(collide.read_text(encoding="utf-8"), "OLD")
        self.assertFalse(src.exists())

    def test_quarantine_name_keeps_the_sidecar_prefix(self) -> None:
        # crash-after-rename recovery: the retention scan and resume
        # discovery must still find the parked file, so its name must retain
        # the original sidecar name as a prefix.
        for name in ("state.md.sidecar-9", "state.md.attempt-9"):
            src = self._sidecar(name)
            quarantined = monitor_runner._quarantine_sidecar(src)
            self.assertIsNotNone(quarantined, name)
            self.assertTrue(quarantined.name.startswith(name), quarantined.name)

    def test_unsupported_platform_fails_closed_source_untouched(self) -> None:
        # No atomic primitive: return None (caller records unreadable) and
        # leave the source EXACTLY as found — never degrade to link+unlink.
        src = self._sidecar("state.md.sidecar-3", "C")
        with mock.patch.object(monitor_runner.sys, "platform", "sunos5"):
            self.assertIsNone(monitor_runner._quarantine_sidecar(src))
        self.assertEqual(src.read_text(encoding="utf-8"), "C")

    def test_rename_syscall_failure_fails_closed(self) -> None:
        # A hard OSError from the primitive (not EEXIST/unsupported) leaves
        # the source and returns None — nothing bound, nothing deleted.
        src = self._sidecar("state.md.sidecar-4", "D")
        with mock.patch.object(
            monitor_runner,
            "_rename_noreplace",
            side_effect=OSError(monitor_runner.errno.EIO, "io"),
        ):
            self.assertIsNone(monitor_runner._quarantine_sidecar(src))
        self.assertEqual(src.read_text(encoding="utf-8"), "D")

    def test_parent_fsync_failure_still_quarantines(self) -> None:
        # Unlike the canonical commit, a quarantine an fsync-failure crash
        # reverts is safe (re-scan is idempotent), so the move still lands.
        src = self._sidecar("state.md.sidecar-7", "E")
        with mock.patch.object(
            monitor_runner, "_fsync_parent", return_value=False
        ):
            quarantined = monitor_runner._quarantine_sidecar(src)
        self.assertIsNotNone(quarantined)
        self.assertFalse(src.exists())
        self.assertEqual(quarantined.read_text(encoding="utf-8"), "E")

    def test_darwin_excl_flag_is_not_the_linux_bit(self) -> None:
        # Regression on the verified constant: Darwin RENAME_EXCL is 0x4;
        # 0x2 is RENAME_SWAP (needs both names -> ENOENT). A silent revert to
        # the Linux 0x1 would make every quarantine ENOENT-fail on macOS.
        self.assertEqual(monitor_runner._DARWIN_RENAME_EXCL, 0x4)
        self.assertEqual(monitor_runner._RENAME_NOREPLACE, 1)


class DescendantIdentityTests(unittest.TestCase):
    """algo#1216 finding 3816160128: PID reuse must never make liveness or
    cleanup treat an unrelated same-UID process as a recorded descendant."""

    RECORDED = "Tue Aug 12 10:00:00 2026"
    RECYCLED = "Tue Aug 19 09:00:00 2026"

    def _fake_ps(self, tmp: Path, rows: str) -> Path:
        script = tmp / "ps"
        script.write_text(
            "#!/bin/sh\n" f"cat <<'ROWS'\n{rows}\nROWS\n", encoding="utf-8"
        )
        script.chmod(0o755)
        return script

    def test_recycled_pid_is_not_live_and_matching_pid_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            fake = self._fake_ps(
                tmp,
                f"12345 S {self.RECYCLED}\n67890 S {self.RECORDED}",
            )
            snapshot = {
                12345: {"pgid": 12345, "lstart": self.RECORDED},
                67890: {"pgid": 12345, "lstart": self.RECORDED},
            }
            with mock.patch.dict(
                os.environ, {"MONITOR_RUNNER_BIN_PS": str(fake)}
            ):
                live = monitor_runner._live_snapshot_pids(snapshot)
        self.assertEqual(
            live, [67890],
            "a pid with a different lstart is a recycled pid, not the"
            " recorded descendant",
        )

    def test_kill_targets_require_pid_and_leader_identity(self) -> None:
        snapshot = {
            # leader 100 recycled: its group must NOT be signaled
            100: {"pgid": 100, "lstart": self.RECORDED},
            101: {"pgid": 100, "lstart": self.RECORDED},
            # leader 200 identity-valid: group signaled; member too
            200: {"pgid": 200, "lstart": self.RECORDED},
            201: {"pgid": 200, "lstart": self.RECORDED},
            # 300: no fingerprint recorded (legacy shape) — never signaled
            300: 300,
        }
        identities = {
            100: ("S", self.RECYCLED),
            101: ("S", self.RECORDED),
            200: ("S", self.RECORDED),
            201: ("S", self.RECORDED),
            300: ("S", self.RECORDED),
        }
        pgids, pids = monitor_runner._validated_kill_targets(
            snapshot, [101, 201, 300], identities
        )
        self.assertEqual(pgids, [200], "killpg only with a validated leader")
        self.assertEqual(
            pids, [101, 201],
            "identity-matched pids are signaled; the unfingerprinted one"
            " is left to the fail-closed recheck",
        )

    def test_scan_failure_blocks_instead_of_bypassing_the_gate(self) -> None:
        # admin#1495 finding 3816225740: an unenumerable state directory
        # must block — ([], False) read as "no sidecars" and launched a
        # write-capable child over possibly-fired operations.
        tmp = Path(tempfile.mkdtemp(prefix="unit-scan-fail-"))
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

        def _boom(_path):
            raise OSError("EIO")

        with mock.patch.object(monitor_runner.os, "scandir", _boom):
            with self.assertRaises(RunnerExit) as caught:
                runner._scan_sidecars(20)
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("sidecar scan failed", caught.exception.reason)


class AttemptContainmentTests(unittest.TestCase):
    """r13 F8: the cgroup containment mechanism, exercised against a fake
    cgroupfs directory (the MONITOR_RUNNER_CGROUP_ROOT seam covers the
    create() branches; instance methods are driven directly). The
    real-cgroupfs create() success line is environment-gated — covered by
    the delegation-gated integration test below when the host provides
    cgroup v2 delegation, and disclosed otherwise."""

    def _fake_cgroup(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="unit-cgroup-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "cgroup.procs").write_text("")
        return tmp

    def test_create_degrades_without_a_real_cgroupfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"MONITOR_RUNNER_CGROUP_ROOT": tmp}
            ):
                self.assertIsNone(
                    monitor_runner.AttemptContainment.create("a" * 32)
                )
            # the probe directory is cleaned up after the failed check
            self.assertEqual(list(Path(tmp).iterdir()), [])
        with mock.patch.dict(
            os.environ, {"MONITOR_RUNNER_CGROUP_ROOT": "/nonexistent-root"}
        ):
            self.assertIsNone(
                monitor_runner.AttemptContainment.create("a" * 32)
            )

    def test_membership_is_the_extinction_proof_and_kill_authority(self) -> None:
        cg = self._fake_cgroup()
        containment = monitor_runner.AttemptContainment(cg)
        self.assertTrue(containment.adopt(4242))
        (cg / "cgroup.procs").write_text("123\n456\n")
        self.assertEqual(containment.live_pids(), [123, 456])
        # cgroup.kill preferred when the host provides it
        (cg / "cgroup.kill").write_text("")
        containment.kill()
        self.assertEqual((cg / "cgroup.kill").read_text(), "1\n")
        # per-pid fallback goes through membership only — no fingerprints,
        # because membership IS identity inside the boundary
        (cg / "cgroup.kill").unlink()
        killed: list[int] = []
        with mock.patch.object(
            monitor_runner.os, "kill", lambda pid, sig: killed.append(pid)
        ):
            containment.kill()
        self.assertEqual(killed, [123, 456])
        # an unreadable boundary fails closed
        (cg / "cgroup.procs").unlink()
        with self.assertRaises(RunnerExit) as caught:
            containment.live_pids()
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("containment boundary unreadable", caught.exception.reason)

    @unittest.skipUnless(
        Path("/sys/fs/cgroup").is_dir()
        and os.access("/sys/fs/cgroup", os.W_OK),
        "requires cgroup v2 delegation",
    )
    def test_between_snapshot_escape_cannot_leave_the_boundary(self) -> None:
        # r13 F8's exact escape: a double-forked, re-sessioned descendant
        # orphaned between snapshots still appears in cgroup.procs and
        # dies on kill.
        containment = monitor_runner.AttemptContainment.create("f" * 32)
        if containment is None:
            self.skipTest("cgroup delegation unavailable")
        self.addCleanup(containment.remove)
        import subprocess as sp

        proc = sp.Popen(
            [
                sys.executable,
                "-c",
                "import os,time\n"
                "if os.fork() == 0:\n"
                "    os.setsid()\n"
                "    time.sleep(60)\n"
                "else:\n"
                "    time.sleep(60)\n",
            ],
            start_new_session=True,
        )
        self.addCleanup(lambda: proc.poll() or proc.kill())
        self.assertTrue(containment.adopt(proc.pid))
        import time as _time

        _time.sleep(1.0)
        members = containment.live_pids()
        self.assertGreaterEqual(
            len(members), 2, "the re-sessioned orphan must stay a member"
        )
        containment.kill()
        deadline = _time.monotonic() + 10
        while _time.monotonic() < deadline and containment.live_pids():
            _time.sleep(0.2)
        self.assertEqual(containment.live_pids(), [])

    def test_adopt_fails_closed_on_an_unwritable_membership_file(self) -> None:
        # algo#1216 r18 F5 (adoption-failure leg): a target whose
        # cgroup.procs cannot be written reports False — the runner then
        # holds containment=None with the degraded:cgroup-adopt-failed
        # record and the universal gate refuses the launch. (On a plain
        # filesystem write_text would CREATE the file, so the fixture
        # pre-creates it read-only; the real-kernel rejection is covered
        # by the delegation-gated dead-pid test below.)
        tmp = Path(tempfile.mkdtemp(prefix="unit-cgroup-roprocs-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        procs = tmp / "cgroup.procs"
        procs.write_text("")
        procs.chmod(0o444)
        self.addCleanup(procs.chmod, 0o644)
        containment = monitor_runner.AttemptContainment(tmp)
        self.assertFalse(containment.adopt(os.getpid()))

    @unittest.skipUnless(
        Path("/sys/fs/cgroup").is_dir()
        and os.access("/sys/fs/cgroup", os.W_OK),
        "requires cgroup v2 delegation",
    )
    def test_real_adoption_of_a_dead_pid_fails_closed(self) -> None:
        # r18 F5: the kernel rejects adopting a reaped pid (ESRCH) — the
        # REAL adoption-failure path, not the fake-fs proxy above.
        containment = monitor_runner.AttemptContainment.create("d" * 32)
        if containment is None:
            self.skipTest("cgroup delegation unavailable")
        self.addCleanup(containment.remove)
        import subprocess as sp

        proc = sp.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=10)  # fully reaped: the pid no longer exists
        self.assertFalse(containment.adopt(proc.pid))

    @unittest.skipUnless(
        Path("/sys/fs/cgroup").is_dir()
        and os.access("/sys/fs/cgroup", os.W_OK),
        "requires cgroup v2 delegation",
    )
    def test_concurrent_attempts_are_isolated_and_cleaned(self) -> None:
        # r18 F5 / admin#1495 r14 F2 (package half of the host smoke):
        # two attempts hold disjoint memberships; killing one leaves the
        # other's member alive; remove() leaves no directory behind.
        import subprocess as sp
        import time as _time

        first = monitor_runner.AttemptContainment.create("a1" * 16)
        second = monitor_runner.AttemptContainment.create("b2" * 16)
        if first is None or second is None:
            if first is not None:
                first.remove()
            if second is not None:
                second.remove()
            self.skipTest("cgroup delegation unavailable")
        procs = [
            sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            for _ in range(2)
        ]
        try:
            self.assertTrue(first.adopt(procs[0].pid))
            self.assertTrue(second.adopt(procs[1].pid))
            self.assertEqual(first.live_pids(), [procs[0].pid])
            self.assertEqual(second.live_pids(), [procs[1].pid])
            first.kill()
            deadline = _time.monotonic() + 10
            while _time.monotonic() < deadline and first.live_pids():
                _time.sleep(0.2)
            self.assertEqual(first.live_pids(), [])
            self.assertEqual(
                second.live_pids(), [procs[1].pid],
                "killing one attempt must not touch its sibling",
            )
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
            for containment in (first, second):
                containment.kill()
                containment.remove()
        self.assertFalse(first.path.exists(), "remove() must clean the dir")
        self.assertFalse(second.path.exists(), "remove() must clean the dir")


class CapabilityGrammarTests(unittest.TestCase):
    """algo#1216 r18 F3 / admin#1495 r14 F9: exact mutation-operation
    grammar and per-row connected-status parsing — the substring matcher
    granted families from read-only tokens, unrelated names, and
    failed/pending rows."""

    def test_mutation_grant_matrix(self) -> None:
        cases = {
            "Bash(gh *)": ("github", "bash"),
            "Bash(gh:*)": ("github", "bash"),
            "Bash(gh api:*)": ("github", "bash"),
            "Bash(gh pr *)": ("github", "bash"),
            "mcp__github__*": ("github", "mcp"),
            "mcp__github__update_pull_request": ("github", "mcp"),
            "mcp__plugin_github_github__issue_write": ("github", "mcp"),
            "mcp__linear__*": ("linear", "mcp"),
            "mcp__linear__update_issue": ("linear", "mcp"),
            "mcp__plugin_linear_linear__*": ("linear", "mcp"),
            # read-only, literal, lookalike, and unrelated shapes grant nothing
            "Bash(gh pr view:*)": None,
            "Bash(gh pr edit --add-assignee x)": None,
            "Bash(ghq *)": None,
            "mcp__github__pull_request_read": None,
            "mcp__linear__get_issue": None,
            "mcp__github_evil__*": None,
            "Read(~/github-notes/**)": None,
            "linear": None,
            "WebFetch(domain:github.com)": None,
        }
        for token, want in cases.items():
            with self.subTest(token=token):
                self.assertEqual(monitor_runner._mutation_grant(token), want)

    def test_denied_families_shapes(self) -> None:
        every = set(monitor_runner.REQUIRED_CHILD_CAPABILITIES)
        self.assertEqual(monitor_runner._denied_families(None), set())
        # unknown shapes deny everything (fail closed)
        self.assertEqual(monitor_runner._denied_families("nope"), every)
        self.assertEqual(monitor_runner._denied_families([1]), every)
        self.assertEqual(monitor_runner._denied_families(["*"]), every)
        self.assertEqual(
            monitor_runner._denied_families(["mcp__linear__*"]), {"linear"}
        )
        # denying ANY family tool conservatively denies the family
        self.assertEqual(
            monitor_runner._denied_families(["mcp__github__get_me"]),
            {"github"},
        )
        self.assertEqual(
            monitor_runner._denied_families(["Bash(rm *)"]), set()
        )

    def test_mcp_row_health_matrix(self) -> None:
        listing = "\n".join(
            (
                "github: gh-mcp - ✓ Connected",
                "linear: npx @linear/mcp - ✗ Failed to connect",
                "auth-one: srv - authentication required",
                "pending-one: srv - pending",
                "dropped: srv - disconnected",
                "erroring: srv - error while connecting",
                "notyet: srv - not connected",
                "wordy: srv - connected",
                "unknown-status: srv - warming up",
                "malformed line with no separator",
                "tricky: fails yet says connected",
            )
        )
        rows = monitor_runner._parse_mcp_list_rows(listing)
        self.assertTrue(rows["github"])
        self.assertTrue(rows["wordy"])
        for name in (
            "linear",
            "auth-one",
            "pending-one",
            "dropped",
            "erroring",
            "notyet",
            "unknown-status",
            "tricky",
        ):
            self.assertFalse(rows[name], name)
        self.assertNotIn("malformed line with no separator", rows)

    def test_probe_timeout_blocks_fail_closed(self) -> None:
        # admin#1495 r14 F9's "timeout" output: a hung `mcp list` proves
        # nothing — no family is granted and the probe blocks.
        runner = _runner("claude-opus-5", None)
        runner.repository_hint = "keeper-dating/matchmaking"
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"MONITOR_RUNNER_USER_SETTINGS": str(settings)}
            ), mock.patch.object(
                monitor_runner.subprocess,
                "run",
                side_effect=monitor_runner.subprocess.TimeoutExpired(
                    cmd="mcp list", timeout=30
                ),
            ):
                with self.assertRaises(RunnerExit) as caught:
                    runner._child_capability_probe({"monitor_cli": {}})
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("no CONNECTED MCP row", caught.exception.reason)


class R15RunnerCorrectnessTests(unittest.TestCase):
    """admin#1495 r15 F6/F7/F11: shared resume-loss detection, plugin
    row parsing, and the normalized/bounded liveness deadline."""

    def test_resume_loss_offset_matrix(self) -> None:
        cases = {
            "No conversation found with id": 0,
            "prefix Session not found tail": 7,
            "warn: unknown session 123": 6,
            "no conversation found ... session not found": 0,
            "all healthy": None,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    monitor_runner.resume_loss_offset(text), want
                )

    def test_signature_excerpt_anchors_late_resume_markers(self) -> None:
        for marker in ("session not found", "unknown session"):
            with self.subTest(marker=marker):
                line = "x" * 3000 + f"error: {marker} mid-run" + "y" * 100
                excerpt = monitor_runner._signature_excerpt(line)
                self.assertIn(marker, excerpt)

    def test_plugin_qualified_row_parsing(self) -> None:
        listing = "\n".join(
            (
                "plugin:linear:linear: npx @linear/mcp - \u2713 Connected",
                "plugin:github:github: srv - \u2717 Failed to connect",
                "plugin: mystery - \u2713 Connected",
                "pluginx:linear:linear: srv - \u2713 Connected",
                "linear: npx - \u2713 Connected",
            )
        )
        rows = monitor_runner._parse_mcp_list_rows(listing)
        self.assertTrue(rows["plugin:linear:linear"])
        self.assertFalse(rows["plugin:github:github"])
        # unknown servers keep first-colon parsing and never grant
        self.assertTrue(rows["plugin"])
        self.assertIn("pluginx", rows)
        self.assertTrue(rows["linear"])

    def test_retry_deadline_normalization(self) -> None:
        parse = monitor_runner._parse_retry_deadline
        zulu = parse("2026-08-25T10:00:00Z")
        offset = parse("2026-08-25T12:00:00+02:00")
        self.assertIsNotNone(zulu)
        self.assertEqual(zulu, offset)  # same instant, offset form honored
        self.assertIsNone(parse("2026-08-25T10:00:00"))  # naive rejected
        self.assertIsNone(parse("not a time"))
        self.assertIsNone(parse(None))

    def test_resume_wait_honors_offset_form_and_ceiling(self) -> None:
        import time as _time
        from datetime import datetime, timedelta, timezone

        runner = _runner("claude-opus-5", None)
        runner.wait_scale = 0.001
        runner.remaining = lambda: 100000.0
        # far-future OFFSET-form deadline: pre-fix the offset form was
        # silently ignored (instant return) and a Z-form far future was
        # unbounded; post-fix the wait is honored AND clamped to the
        # scaled ladder ceiling (~2.1s at this scale).
        far = (
            datetime.now(timezone.utc) + timedelta(days=365000)
        ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        started = _time.monotonic()
        runner._resume_liveness_wait(
            {"monitor_cli": {"liveness": {"next_retry_at": far}}}
        )
        elapsed = _time.monotonic() - started
        ceiling = (
            monitor_runner.LIVENESS_BACKOFF_LADDER_SECONDS[-1] + 300.0
        ) * runner.wait_scale
        self.assertGreaterEqual(
            elapsed, min(ceiling, 0.5), "the offset-form wait must be honored"
        )
        self.assertLess(elapsed, 30.0, "the far-future wait must be clamped")


class ContainmentRefusalDecisionTests(unittest.TestCase):
    """algo#1216 r18 F5: the universal-gate decision matrix, pinned at the
    decision layer. The e2e block tests pin the process path (stdin closed
    before GO, wrapper reaped, RunnerExit 5); this pins WHO may proceed:
    nothing uncontained, except an operator-attested hermetic test child
    on a repository that is not Keeper-bound. Both degraded records —
    creation failure and adoption failure — refuse identically."""

    _CREATE_FAILED = "degraded:no-cgroup-v2-delegation"
    _ADOPT_FAILED = "degraded:cgroup-adopt-failed"

    def _decide(
        self, record: str, repository: str | None, attested: bool
    ) -> str | None:
        runner = _runner("claude-opus-5", None)
        # repository=None must mean TRULY unbound: _bound_repository falls
        # back to the live origin hint, which on a Keeper CI checkout
        # resolves to the Keeper repository under test and (correctly)
        # refuses the attestation — the fallback is the gate working, not
        # the case this matrix isolates.
        runner.repository_hint = None
        extract = {"monitor_cli": {"repository": repository}}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MONITOR_RUNNER_UNCONTAINED_TEST_CHILD", None)
            if attested:
                os.environ["MONITOR_RUNNER_UNCONTAINED_TEST_CHILD"] = "1"
            return runner._containment_refusal(record, extract)

    def test_keeper_bound_refuses_even_when_attested(self) -> None:
        # The floor is the OWNER, not the QA map — r18 F5's repro was
        # exact-Algo, which the QA map excludes.
        for repository in (
            "Keeper-Dating/matchmaking",
            "Keeper-Dating/algo",
            "keeper-dating/ALGO",
        ):
            for record in (self._CREATE_FAILED, self._ADOPT_FAILED):
                with self.subTest(repository=repository, record=record):
                    refusal = self._decide(record, repository, attested=True)
                    self.assertIsNotNone(refusal)
                    self.assertIn(record, refusal)
                    self.assertIn("EVERY repository", refusal)

    def test_unattested_refuses_for_every_repository(self) -> None:
        for repository in (
            "Keeper-Dating/matchmaking",
            "someone-else/sandbox",
            None,
        ):
            for record in (self._CREATE_FAILED, self._ADOPT_FAILED):
                with self.subTest(repository=repository, record=record):
                    refusal = self._decide(record, repository, attested=False)
                    self.assertIsNotNone(refusal)
                    self.assertIn(record, refusal)
                    self.assertIn(
                        "MONITOR_RUNNER_CGROUP_ROOT", refusal,
                        "the refusal must name the host remediation",
                    )

    def test_attested_non_keeper_proceeds(self) -> None:
        for repository in ("someone-else/sandbox", None):
            for record in (self._CREATE_FAILED, self._ADOPT_FAILED):
                with self.subTest(repository=repository, record=record):
                    self.assertIsNone(
                        self._decide(record, repository, attested=True)
                    )


class OriginTrustTests(unittest.TestCase):
    """r14 F5: only GitHub's own URL shapes bind a repository, and a
    resolvable live origin disagreeing with the persisted binding fails
    closed instead of being papered over."""

    def test_parser_is_a_github_allowlist(self) -> None:
        cases = (
            ("git@github.com:Keeper-Dating/matchmaking.git", "Keeper-Dating/matchmaking"),
            ("https://github.com/Keeper-Dating/algo", "Keeper-Dating/algo"),
            ("ssh://git@github.com/Keeper-Dating/admin-portal.git", "Keeper-Dating/admin-portal"),
            ("https://github.com/Keeper-Dating/matchmaking.git/", "Keeper-Dating/matchmaking"),
            ("https://user@github.com/Keeper-Dating/algo", "Keeper-Dating/algo"),
            ("https://evil.example/Keeper-Dating/matchmaking.git", None),
            ("git@gitlab.com:Keeper-Dating/matchmaking.git", None),
            ("https://github.com.evil.example/Keeper-Dating/matchmaking", None),
            ("not a url", None),
        )
        for url, want in cases:
            with self.subTest(url=url):
                self.assertEqual(
                    monitor_runner._repo_name_with_owner(url), want
                )

    def test_persisted_vs_live_mismatch_fails_closed(self) -> None:
        class FakeRunner:
            repository_hint = "keeper-dating/other"
            repository_probe = "resolved"
            owner_model = "claude-opus-5"
            failures: list = []

        block = {
            "schema_version": 1,
            "repository": "Keeper-Dating/matchmaking",
            "child_session_id": None,
            "owner_model": "x",
            "last_completed_attempt_id": None,
            "in_flight": None,
            "liveness": None,
        }
        with self.assertRaises(RunnerExit) as caught:
            monitor_runner.Runner.current_block(
                FakeRunner(), {"monitor_cli": block}
            )
        self.assertIn("disagrees with the live origin", caught.exception.reason)
        # same-repo (case-insensitive) and probe-unavailable stay sticky
        FakeRunner.repository_hint = "keeper-dating/MATCHMAKING"
        out = monitor_runner.Runner.current_block(
            FakeRunner(), {"monitor_cli": dict(block)}
        )
        self.assertEqual(out["repository"], "Keeper-Dating/matchmaking")
        FakeRunner.repository_hint = None
        FakeRunner.repository_probe = "unavailable"
        out = monitor_runner.Runner.current_block(
            FakeRunner(), {"monitor_cli": dict(block)}
        )
        self.assertEqual(out["repository"], "Keeper-Dating/matchmaking")
        # r14 F5 re-eval: a SUCCESSFUL probe resolving to an untrusted
        # (foreign/unparseable) origin is a trusted answer, not
        # unavailability — it must block against the persisted binding,
        # never stay sticky.
        FakeRunner.repository_hint = None
        FakeRunner.repository_probe = "foreign"
        with self.assertRaises(RunnerExit) as caught:
            monitor_runner.Runner.current_block(
                FakeRunner(), {"monitor_cli": dict(block)}
            )
        self.assertIn("untrusted remote", caught.exception.reason)


class RateLimitExcerptTests(unittest.TestCase):
    """r14 F7 re-eval: the sticky excerpt must PRESERVE a late rate-limit
    marker — R2's 3000-pad probe found the marker at offset 3001 and then
    lost it to the head-anchored excerpt."""

    def test_late_marker_survives_excerpting(self) -> None:
        probe = "x" * 3000 + " HTTP/2 429 Too Many Requests"
        excerpt = monitor_runner._signature_excerpt(probe)
        self.assertIsNotNone(
            monitor_runner.rate_limit_offset(excerpt),
            "the retained excerpt must still classify as rate-limit",
        )

    def test_mixed_auth_and_rate_prefers_deterministic_auth(self) -> None:
        probe = (
            "x" * 3000
            + " authentication_error: invalid api key; also HTTP/2 429"
        )
        excerpt = monitor_runner._signature_excerpt(probe)
        self.assertTrue(
            monitor_runner._has_auth_signature(excerpt),
            "auth anchoring must win so the deterministic block survives",
        )


class RateLimitMatcherTests(unittest.TestCase):
    """r14 F7: one contextual matcher — incidental numbers never enter
    the no-charge ladder; real HTTP/status/provider forms do."""

    def test_contextual_forms(self) -> None:
        for text, hit in (
            ("elapsed 429ms in setup", False),
            ("request id 4290 done", False),
            ("HTTP/1.1 429 Too Many Requests", True),
            ("status=429", True),
            ("status code: 429", True),
            ("error 429 from provider", True),
            ("You are being rate limited", True),
            ("rate-limit exceeded", True),
            ("server overloaded, retry later", True),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    monitor_runner.rate_limit_offset(text) is not None, hit
                )
        # the offset anchors excerpts on the marker, not the line head
        self.assertEqual(
            monitor_runner.rate_limit_offset("x" * 3000 + " http 429"), 3001
        )

    def test_classifier_uses_the_contextual_matcher(self) -> None:
        action, _ = monitor_runner.classify_child_failure(
            1, ["step took 429ms overall"], False
        )
        self.assertEqual(action, "charge")
        action, _ = monitor_runner.classify_child_failure(
            1, ["HTTP/2 429 too many requests"], False
        )
        self.assertEqual(action, "ladder")

    def test_structured_result_error_joins_classification(self) -> None:
        # r14 F12: an in-band error verdict with EMPTY stderr classifies
        # from the result text instead of decaying to a generic charge.
        action, _ = monitor_runner.classify_child_failure(
            1,
            [],
            False,
            result_text="Request failed: rate limit reached, retry later",
            result_is_error=True,
        )
        self.assertEqual(action, "ladder")
        # is_error=false result text is NOT classification input
        action, _ = monitor_runner.classify_child_failure(
            1,
            [],
            False,
            result_text="analysis mentions rate limit history",
            result_is_error=False,
        )
        self.assertEqual(action, "charge")


class MissingCandidateRecoveryTests(unittest.TestCase):
    """admin#1495 r11 finding 3825265254: an in-flight attempt whose
    candidate never became durable is an UNKNOWABLE remote outcome —
    recovery must block for explicit reconciliation, never clear
    in_flight and hand the next child a blank slate to replay."""

    def _runner(self) -> Runner:
        tmp = Path(tempfile.mkdtemp(prefix="unit-recover-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state = tmp / "workflow-state.local.md"
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

    def test_missing_candidate_blocks_for_reconciliation(self) -> None:
        runner = self._runner()
        attempt = "ab" * 16
        extract = {
            "monitor_cli": {"in_flight": {"attempt_id": attempt}},
        }
        with self.assertRaises(RunnerExit) as caught:
            runner.recover_in_flight(extract)
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("NO candidate", caught.exception.reason)
        self.assertIn(attempt, caught.exception.reason)

    def test_previously_preserved_sidecar_proceeds(self) -> None:
        runner = self._runner()
        attempt = "cd" * 16
        sidecar = runner.state_path.with_suffix(
            f".failed-candidate-{attempt}.md"
        )
        sidecar.write_text("evidence", encoding="utf-8")
        charged: list[str] = []
        runner.charge_failure = lambda extract, signature: charged.append(
            signature
        )
        runner.recover_in_flight(
            {"monitor_cli": {"in_flight": {"attempt_id": attempt}}}
        )
        self.assertEqual(charged, ["monitor-child:unknown_outcome"])


class PostGoBackstopTests(unittest.TestCase):
    """r14 F4 re-eval: the r22 head CALLED _post_go_backstop without
    defining it — no test exercised the escape path, so 727 tests stayed
    green over a phantom method. These pin (a) the mechanism itself with a
    real child, and (b) the SOURCE SHAPE: the boundary and its handler
    must exist together."""

    def _runner(self) -> Runner:
        tmp = Path(tempfile.mkdtemp(prefix="unit-backstop-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state = tmp / "workflow-state.local.md"
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

    def test_backstop_terminates_preserves_and_raises_structured(self) -> None:
        import subprocess as sp
        import sys as _sys

        runner = self._runner()
        child = sp.Popen(
            [_sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        candidate = runner.state_path.with_suffix(".attempt-test.md")
        candidate.write_text("write-ahead evidence", encoding="utf-8")
        with self.assertRaises(RunnerExit) as caught:
            runner._post_go_backstop(
                child, child.pid, None, candidate, RuntimeError("boom")
            )
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("post-GO supervision failed", caught.exception.reason)
        self.assertIn("RuntimeError", caught.exception.reason)
        self.assertIsNotNone(child.returncode, "child must be reaped")
        preserved = list(
            runner.state_path.parent.glob("*.failed-candidate-*")
        )
        self.assertTrue(preserved, "candidate must be preserved as evidence")

    def test_run_tick_boundary_and_handler_exist_together(self) -> None:
        # The phantom catcher: a call site without a definition (or a
        # definition without the boundary) fails here, independent of any
        # runtime path reaching the escape.
        import inspect

        self.assertTrue(hasattr(Runner, "_post_go_backstop"))
        self.assertTrue(callable(getattr(Runner, "_post_go_backstop")))
        source = inspect.getsource(Runner.run_tick)
        self.assertIn("except BaseException as error:", source)
        self.assertIn("self._post_go_backstop(", source)
        # admin#1495 F4: the structured-exit arm binds the exception (it
        # rethrows the ORIGINAL) and runs the SAME identity-safe descendant
        # extinction the normal path and the backstop use — a bare
        # `except RunnerExit:` that only swept an empty containment was the
        # bug. Pin both the binding and the extinction call so a regression
        # to the sweep-only shape fails here.
        self.assertIn("except RunnerExit as exc:", source)
        self.assertIn("self._extinguish_child_descendants(", source)
        self.assertTrue(
            hasattr(Runner, "_extinguish_containment")
            and hasattr(Runner, "_extinguish_child_descendants"),
            "the shared extinction helpers must exist",
        )

    def test_assert_free_paths_survive_optimized_python(self) -> None:
        # r14 F10 re-eval: prove the two former assert sites raise
        # structured errors under python3 -O (asserts stripped).
        import subprocess as sp
        import sys as _sys

        probe = (
            "import sys; sys.path.insert(0, %r); "
            "import monitor_runner, handoff_decision; "
            "print('OPTIMIZED-IMPORT-OK')"
        ) % str(SCRIPTS)
        completed = sp.run(
            [_sys.executable, "-O", "-c", probe],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OPTIMIZED-IMPORT-OK", completed.stdout)
        source_mr = Path(SCRIPTS / "monitor_runner.py").read_text(
            encoding="utf-8"
        )
        source_hd = Path(SCRIPTS / "handoff_decision.py").read_text(
            encoding="utf-8"
        )
        for src, name in ((source_mr, "monitor_runner"), (source_hd, "handoff_decision")):
            for line in src.splitlines():
                stripped = line.strip()
                self.assertFalse(
                    stripped.startswith("assert ") and "# nosec" not in stripped,
                    f"bare assert in {name}: {stripped[:80]}",
                )


class AtomicWriteCleanupTests(unittest.TestCase):
    """admin#1495 r12 F19: temp cleanup is phase-aware — a pre-rename
    replace failure removes the temp this call created; a post-rename
    fsync failure has no temp left and must never touch the committed
    target."""

    def test_pre_rename_replace_failure_removes_the_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.md"
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(
                monitor_runner.os, "replace", side_effect=OSError("boom")
            ):
                with self.assertRaises(OSError):
                    monitor_runner.atomic_write(target, "new")
            self.assertEqual(list(Path(tmp).glob("*.tmp-*")), [])
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_post_rename_fsync_failure_keeps_the_committed_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.md"
            target.write_text("old", encoding="utf-8")
            with mock.patch.object(
                monitor_runner, "_fsync_parent", return_value=False
            ):
                with self.assertRaises(monitor_runner.RunnerExit):
                    monitor_runner.atomic_write(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(list(Path(tmp).glob("*.tmp-*")), [])


class ResultVariantClassificationTests(unittest.TestCase):
    """algo#1216 r17 F7: error-union results carry diagnostics in
    errors[] with no result text — auth blocks, rate limits ladder, and
    the generic case charges the SUBTYPE signature, for both exits."""

    def test_error_variant_matrix(self) -> None:
        cases = (
            (0, ["execution died"], "charge",
             "monitor-child:result_error_during_execution"),
            (1, ["execution died"], "charge",
             "monitor-child:result_error_during_execution"),
            (0, ["authentication_error: token has expired"], "block", None),
            (1, ["authentication_error: token has expired"], "block", None),
            (0, ["Rate limit reached for the model. Please retry later."],
             "ladder", "monitor-child:rate_limited"),
            (1, ["Rate limit reached for the model. Please retry later."],
             "ladder", "monitor-child:rate_limited"),
        )
        for exit_code, errors, want_action, want_signature in cases:
            with self.subTest(exit_code=exit_code, action=want_action):
                action, detail = monitor_runner.classify_child_failure(
                    exit_code,
                    [],
                    False,
                    result_is_error=True,
                    result_subtype="error_during_execution",
                    result_errors=errors,
                )
                self.assertEqual(action, want_action, detail)
                if want_signature is not None:
                    self.assertEqual(detail, want_signature)


class WrapperStageWriteTests(unittest.TestCase):
    """algo#1216 r17 F1 (sibling site): a short os.write on the wrapper
    restage would exec a PREFIX of the trusted wrapper source."""

    def test_wrapper_restage_survives_short_writes(self) -> None:
        runner = _runner("claude-opus-5", "max")
        real_write = os.write
        with mock.patch.object(
            monitor_runner.os,
            "write",
            side_effect=lambda fd, data: real_write(fd, bytes(data)[:1]),
        ):
            runner._restage_wrapper()
        self.assertEqual(
            runner.wrapper_stage_path.read_bytes(), runner.wrapper_source
        )


class ConstructionGuardTests(unittest.TestCase):
    """admin#1495 r12 F18: __init__ creates the wrapper stage and the
    skill snapshot before main() ever holds the instance — a constructor
    failure after either resource exists must remove exactly this
    construction's resources (never a sibling runner's), through the same
    cleanup main()'s finally uses."""

    def _args(self, tmp: Path, skill_dir: Path) -> argparse.Namespace:
        return argparse.Namespace(
            state_file=str(tmp / "state.md"),
            skill_dir=str(skill_dir),
            claude_bin="/opt/homebrew/bin/claude",
            schema_cli=str(SCRIPTS / "state_schema.py"),
            slice_budget=100.0,
            wait_scale=1.0,
            acknowledge_taint=None,
        )

    def test_snapshot_read_failure_removes_both_resources(self) -> None:
        # The snapshot loop fails (no SKILL.md in the skill dir) AFTER the
        # wrapper stage and snapshot dir both exist.
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            fake_tmproot = tmp / "tmproot"
            fake_tmproot.mkdir()
            empty_skill = tmp / "skill"
            empty_skill.mkdir()
            with mock.patch.object(tempfile, "tempdir", str(fake_tmproot)):
                with self.assertRaises(FileNotFoundError):
                    Runner(self._args(tmp, empty_skill))
            self.assertEqual(list(fake_tmproot.iterdir()), [])

    def test_git_resolution_failure_removes_both_resources(self) -> None:
        # _resolve_system_binary fails closed (missing git host) AFTER the
        # full snapshot succeeded.
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            fake_tmproot = tmp / "tmproot"
            fake_tmproot.mkdir()
            with mock.patch.object(
                monitor_runner,
                "_resolve_system_binary",
                side_effect=monitor_runner.RunnerExit(5, "blocked", "no git"),
            ):
                with mock.patch.object(
                    tempfile, "tempdir", str(fake_tmproot)
                ):
                    with self.assertRaises(monitor_runner.RunnerExit):
                        Runner(self._args(tmp, SCRIPTS.parent))
            self.assertEqual(list(fake_tmproot.iterdir()), [])

    def test_snapshot_dir_creation_failure_removes_the_stage(self) -> None:
        # mkdtemp itself fails: only the wrapper stage exists — the guard
        # must tolerate the partially constructed instance.
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            fake_tmproot = tmp / "tmproot"
            fake_tmproot.mkdir()
            with mock.patch.object(
                tempfile, "mkdtemp", side_effect=OSError("boom")
            ):
                with mock.patch.object(
                    tempfile, "tempdir", str(fake_tmproot)
                ):
                    with self.assertRaises(OSError):
                        Runner(self._args(tmp, SCRIPTS.parent))
            self.assertEqual(list(fake_tmproot.iterdir()), [])


class WorkCapDocParityTests(unittest.TestCase):
    def test_reference_cap_matches_the_runner_constant(self) -> None:
        # algo#1216 finding 3813491642: the cap the trusted runner enforces
        # and the MAX_ITERATIONS literal the child-facing reference
        # documents must be the same number — a doc edit that moves one
        # without the other silently splits the contract.
        doc = (
            SCRIPTS.parent / "references" / "monitor-ci-feedback.md"
        ).read_text(encoding="utf-8")
        match = re.search(r"MAX_ITERATIONS\s*=\s*(\d+)", doc)
        self.assertIsNotNone(
            match, "monitor-ci-feedback.md no longer states MAX_ITERATIONS"
        )
        self.assertEqual(
            int(match.group(1)), monitor_runner.MAX_WORK_ITERATIONS
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TrustedControlDriftTests(unittest.TestCase):
    """admin#1495 r15 F1/F10/F19: the launch-vs-candidate trusted-control
    comparison — trail prefix + sensitive-append ban, frozen AC capture
    and ticket, frozen model-binding identity with append-only histories."""

    def _launch(self) -> dict:
        return {
            "decision_audit_trail": ["branch-established:x@" + "a" * 40, "note:one"],
            "acceptance_criteria_capture": {"digest": "d" * 16, "source_revision": "r1"},
            "validated_ticket": {"identifier": "ADM-953", "provider_id": "p1"},
            "model_runtime": {
                "codex": {"model": "gpt-5.6-sol", "gate_status": "ready",
                          "post_invocation": [{"at": "t1"}]},
                "claude": {"model": "claude-fable-5", "gate_status": "ready",
                           "post_invocation": []},
                "claude_reviewer": {"model": "claude-opus-5", "gate_status": "ready",
                                    "post_invocation": []},
                "escalation_invocations": [],
            },
        }

    def _drift(self, mutate) -> str | None:
        import copy
        launch = self._launch()
        candidate = copy.deepcopy(launch)
        mutate(candidate)
        return monitor_runner._trusted_control_drift(launch, candidate)

    def test_benign_appends_and_work_pass(self) -> None:
        def mutate(c):
            c["decision_audit_trail"].append("ref-read:state-and-safety:abc")
            c["model_runtime"]["codex"]["post_invocation"].append({"at": "t2"})
            c["model_runtime"]["escalation_invocations"].append({"at": "t3"})
        self.assertIsNone(self._drift(mutate))

    def test_trail_deletion_rewrite_reorder_reject(self) -> None:
        for label, mutate in (
            ("deletion", lambda c: c["decision_audit_trail"].pop(0)),
            ("rewrite", lambda c: c["decision_audit_trail"].__setitem__(0, "r2-gate:waived")),
            ("reorder", lambda c: c["decision_audit_trail"].reverse()),
        ):
            with self.subTest(label=label):
                self.assertIsNotNone(self._drift(mutate))

    def test_forged_sensitive_append_rejects(self) -> None:
        for forged in ("r2-gate:waived", "branch-established:y@" + "b" * 40,
                       "package-validated:def@2026", "validation-before-push:" + "c" * 40):
            with self.subTest(forged=forged):
                self.assertIsNotNone(
                    self._drift(lambda c, f=forged: c["decision_audit_trail"].append(f))
                )

    def test_capture_and_ticket_frozen(self) -> None:
        for label, mutate in (
            ("capture-digest", lambda c: c["acceptance_criteria_capture"].__setitem__("digest", "e" * 16)),
            ("capture-removed", lambda c: c.__setitem__("acceptance_criteria_capture", None)),
            ("ticket", lambda c: c["validated_ticket"].__setitem__("provider_id", "p2")),
        ):
            with self.subTest(label=label):
                self.assertIsNotNone(self._drift(mutate))

    def test_binding_identity_frozen_history_prefix(self) -> None:
        for label, mutate in (
            ("gate-flip", lambda c: c["model_runtime"]["claude_reviewer"].__setitem__("gate_status", "blocked")),
            ("model-swap", lambda c: c["model_runtime"]["codex"].__setitem__("model", "gpt-6")),
            ("history-rewrite", lambda c: c["model_runtime"]["codex"].__setitem__("post_invocation", [{"at": "tX"}])),
            ("leg-removed", lambda c: c["model_runtime"].pop("claude")),
        ):
            with self.subTest(label=label):
                self.assertIsNotNone(self._drift(mutate))
        # clearing escalations only matters once the launch recorded some —
        # an empty launch list is a prefix of anything, including empty.
        launch = self._launch()
        launch["model_runtime"]["escalation_invocations"] = [{"at": "e1"}]
        import copy
        candidate = copy.deepcopy(launch)
        candidate["model_runtime"]["escalation_invocations"] = []
        self.assertIsNotNone(monitor_runner._trusted_control_drift(launch, candidate))


class QaManifestViolationTests(unittest.TestCase):
    """admin#1495 r15 F17: the canonical mapped-repository QA manifest."""

    def _extract(self, ops, results=None, status="pending"):
        return {
            "handoff_status_by_kind": {"qa": status},
            "handoff_operations": {"qa": ops},
            "handoff_results": {"qa": results if results is not None else {o: "complete" for o in ops}},
        }

    def _violation(self, ops, **kw):
        runner = _runner("claude-opus-5", None)
        return runner._qa_manifest_violation(
            "Keeper-Dating/matchmaking", self._extract(ops, **kw)
        )

    FULL = [
        "qa.github.replace_assignees:gaaaaaaaaaaaa",
        "qa.github.verify_assignees:gaaaaaaaaaaaa",
        "qa.linear.verify_ticket_binding:gaaaaaaaaaaaa",
        "qa.linear.assign_ticket:gaaaaaaaaaaaa",
        "qa.linear.verify_ticket_assignee:gaaaaaaaaaaaa",
        "qa.linear.set_ticket_state:gaaaaaaaaaaaa",
        "qa.linear.verify_ticket_state:gaaaaaaaaaaaa",
    ]

    def test_complete_manifest_passes(self) -> None:
        self.assertIsNone(self._violation(self.FULL))

    def test_outage_shapes_pass(self) -> None:
        self.assertIsNone(self._violation([
            "qa.github.replace_assignees:gbbbbbbbbbbbb",
            "qa.github.verify_assignees:gbbbbbbbbbbbb",
            "qa.linear.record_unavailable:gbbbbbbbbbbbb",
        ]))

    def test_subset_omission_rejects(self) -> None:
        # github-only (the exact F17 shape) and assign-without-state
        self.assertIsNotNone(self._violation(self.FULL[:2]))
        self.assertIsNotNone(self._violation(self.FULL[:5]))

    def test_mixed_generations_reject(self) -> None:
        ops = list(self.FULL)
        ops[0] = "qa.github.replace_assignees:gcccccccccccc"
        self.assertIsNotNone(self._violation(ops))

    def test_missing_results_reject(self) -> None:
        self.assertIsNotNone(self._violation(self.FULL, results={}))

    def test_unmapped_and_idle_skip(self) -> None:
        runner = _runner("claude-opus-5", None)
        self.assertIsNone(runner._qa_manifest_violation(
            "someone-else/sandbox", self._extract(self.FULL[:1])))
        self.assertIsNone(self._violation([], status="idle"))

    def test_manifest_table_matches_the_planner(self) -> None:
        # single-source parity: the runner's family table must mint the
        # SAME families as a real full plan from handoff_decision.
        import handoff_decision as hd
        req = {
            "scenario": "clean_unapproved",
            "repository": {"nameWithOwner": "Keeper-Dating/matchmaking"},
            "pull_request_number": 7,
            "authenticated_actor": "jakozloski",
            "existing_assignees": ["jakozloski"],
            "code_reviewers": [],
            "issue_tracker": {
                "ticket_identifier": "ADM-953",
                "ticket_provider_id": "abc",
                "write_path": "local_api",
                "qa_assignee": {"provider_id": "qa-1"},
                "qa_state": {"provider_id": "st-1"},
            },
        }
        plan = hd.plan_handoff(req)
        self.assertEqual(plan["state"], "pending", plan["errors"])
        families = {
            op["id"].rpartition(":")[0].split(":", 1)[0]
            for op in plan["operations"]
        }
        github = {f for f in families if f.startswith("qa.github.")}
        linear = {f for f in families if f.startswith("qa.linear.")}
        self.assertTrue(monitor_runner._QA_REQUIRED_GITHUB_FAMILIES <= github)
        if linear:
            self.assertIn(
                frozenset(linear), monitor_runner._QA_LINEAR_LEG_SHAPES
            )


class RecoveryContainmentRecordTests(unittest.TestCase):
    """algo#1216 r19 F11: a persisted cgroup containment record makes
    recovery unresolved whatever the group probe says; degraded records
    keep the group-proof path; malformed records fail closed."""

    def _reconcile(self, containment) -> None:
        import subprocess as sp
        import sys as _sys

        proc = sp.Popen([_sys.executable, "-c", "pass"], start_new_session=True)
        pgid = os.getpgid(proc.pid)
        proc.wait(timeout=10)  # dead, reaped: the group id is gone
        runner = _runner("claude-opus-5", None)
        in_flight = {
            "child_pid": proc.pid,
            "child_pgid": pgid,
            "child_started_fingerprint": "gone",
        }
        if containment is not None:
            in_flight["containment"] = containment
        runner._reconcile_recorded_orphan(in_flight)

    def test_cgroup_record_blocks_despite_dead_group(self) -> None:
        with self.assertRaises(RunnerExit) as caught:
            self._reconcile("cgroup:/sys/fs/cgroup/autonomy-monitor-x")
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("does not prove the BOUNDARY extinct", caught.exception.reason)

    def test_malformed_record_fails_closed(self) -> None:
        with self.assertRaises(RunnerExit):
            self._reconcile("surprise-shape")

    def test_degraded_record_keeps_the_group_proof_path(self) -> None:
        # dead group + degraded record: no raise from the orphan check
        # (the caller's no-candidate reconciliation owns what follows)
        self._reconcile("degraded:no-cgroup-v2-delegation")
        self._reconcile(None)


class ContainmentRemovalObservabilityTests(unittest.TestCase):
    """algo#1216 r19 F12: removal success is observable and the pointer
    clears only after confirmed removal; the post-GO final read
    propagates while the pre-GO path retains and discloses."""

    def _containment_dir(self):
        tmp = Path(tempfile.mkdtemp(prefix="unit-qd17-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "cgroup.procs").write_text("")
        return tmp

    def test_remove_reports_failure(self) -> None:
        cg = self._containment_dir()
        containment = monitor_runner.AttemptContainment(cg)
        # nonempty dir: rmdir fails -> False, dir retained
        self.assertFalse(containment.remove())
        self.assertTrue(cg.exists())
        (cg / "cgroup.procs").unlink()
        self.assertTrue(containment.remove())
        self.assertFalse(cg.exists())

    def test_extinguish_propagates_unreadable_final_read(self) -> None:
        cg = self._containment_dir()
        runner = _runner("claude-opus-5", None)
        runner.attempt_containment = monitor_runner.AttemptContainment(cg)
        (cg / "cgroup.procs").unlink()  # unreadable membership
        with self.assertRaises(RunnerExit):
            runner._extinguish_containment()
        # the pointer survives for the human
        self.assertIsNotNone(runner.attempt_containment)

    def test_extinguish_retains_pointer_on_failed_removal(self) -> None:
        cg = self._containment_dir()
        (cg / "cgroup.procs").write_text("")
        (cg / "extra-file").write_text("x")  # rmdir will fail
        runner = _runner("claude-opus-5", None)
        runner.attempt_containment = monitor_runner.AttemptContainment(cg)
        self.assertTrue(runner._extinguish_containment())
        self.assertIsNotNone(
            runner.attempt_containment,
            "the pointer must survive a failed removal",
        )
        # a real cgroup dir rmdirs clean once empty (cgroup.procs is a
        # kernel-virtual file); the plain-fs proxy cannot be both readable
        # and rmdir-able, so the confirmed-removal leg pins the WIRING.
        with mock.patch.object(
            monitor_runner.AttemptContainment, "remove", return_value=True
        ):
            self.assertTrue(runner._extinguish_containment())
        self.assertIsNone(runner.attempt_containment)
