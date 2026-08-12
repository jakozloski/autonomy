"""Hermetic e2e tests for scripts/monitor_runner.py.

Runs the runner as a real subprocess against a fake ``claude`` binary, per
the package's structural rule (module docstring of test_cli_fail_closed.py):
this file uses ``subprocess`` and therefore imports NOTHING from the package
under test — the state fixture is an embedded literal, self-verified in
setUp through the state_schema CLI, and every assertion reads runner output
or on-disk state, never package internals.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
RUNNER = SCRIPTS / "monitor_runner.py"
SCHEMA = SCRIPTS / "state_schema.py"

STATE_FIXTURE = """---
state_schema_version: 1
workflow_id: "wf-full-125"
description: "Full workflow"
branch: "feat/thing"
base_branch: "main"
pre_takeover_branch: null
current_phase: "monitor"
pr_number: 7
stash_ref: null
resolved_conventions:
  quality_check_steps: []
  model_runtime:
    codex:
      model: "gpt-5.6-sol"
      gate_status: "ready"
    claude:
      model: "claude-fable-5"
      gate_status: "ready"
      host_agent_selection_verified: true
    claude_reviewer:
      model: "claude-opus-5"
      gate_status: "ready"
validated_ticket:
  tracker_type: null
  identifier: null
  provider_id: null
  validated_at: null
  source_fingerprint: null
regression_evidence:
  status: "not_applicable"
  root_cause: null
  test_paths: []
  red_evidence: null
  red_exemption_reason: null
  green_evidence: null
  evaluated_head_sha: null
  exemption_reason: null
variant_analysis:
  status: "skipped"
  search_patterns: []
  matches_inspected: 0
  analyzed_head_sha: null
  variants_fixed: []
  variants_reported: []
  skipped_reason: "change_type feature: no defect to search for"
last_processed_comments: {}
last_processed_reviews: {}
last_processed_threads: {}
authenticated_actor: "octocat"
thread_reply_timestamps: {}
acknowledged_top_level_comments: {}
acknowledged_top_level_reviews: {}
acknowledged_human_top_level_comments: {}
acknowledged_human_top_level_reviews: {}
exhausted_feedback: {}
manual_unknown_feedback: {}
manual_branch_protection_blockers: {}
human_roundtrip:
  reviewers: {}
handoffs:
  qa:
    scenario: null
    status: "idle"
    repository_name_with_owner: null
    targets:
      github_assignees: []
      tracker_assignee_id: null
      tracker_assignee_name: null
    operations: []
    operation_results: {}
  review_roundtrip:
    scenario: null
    status: "idle"
    targets:
      reviewers: []
      github_assignees: []
    operations: []
    operation_results: {}
last_check_status: "pending"
monitor_iterations: 0
monitor_poll_ticks: 0
monitor_self_review_call_count: 0
post_push_until: null
last_observed_head_sha: null
clean_poll_timestamps: []
attempt_log: {}
gstack_integration:
  available: false
  gstack_dir: null
  selected_skills: []
  scope_frontend: false
  scope_backend: false
  scope_tests_only: false
  scope_skill_only: false
  change_type: "feature"
  defect_evidence_mode: "none"
  review:
    status: "pending"
    tier: null
    notes: []
finding_ledger:
  next_seq_id: 1
  entries: []
  convergence: {}
decision_audit_trail: []
phases:
  plan: "complete"
  plan_review: "complete"
  implementation: "complete"
  self_review: "complete"
  runtime_verification:
    status: "waived"
    reason: "skill_only: no runtime code changed"
  pr: "complete"
  monitor: "in_progress"
---

# Workflow State

- entry: initialized.
"""

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Fake claude CLI for monitor_runner e2e tests.

Behavior is selected by FAKE_MODE; argv is recorded to FAKE_ARGV_LOG so
tests can assert pinning and resume flags without trusting the runner.
"""
import json, os, re, subprocess, sys, time

if "--version" in sys.argv:
    print("2.1.220 (fake)")
    sys.exit(0)

mode = os.environ.get("FAKE_MODE", "ok")
argv_log = os.environ.get("FAKE_ARGV_LOG")
if argv_log:
    with open(argv_log, "a", encoding="utf-8") as h:
        h.write(json.dumps(sys.argv[1:]) + "\n")

prompt = sys.argv[-1]
state_match = re.search(r"state file at (\S+) —", prompt)
candidate_match = re.search(r"updated state to (\S+) and NEVER", prompt)
skill_match = re.search(r"package is at (\S+)\.", prompt)
state_path = state_match.group(1) if state_match else ""
candidate_path = candidate_match.group(1) if candidate_match else ""
skill_dir = skill_match.group(1) if skill_match else ""
attempt_match = re.search(r'"attempt_id": "([0-9a-f]+)"', prompt)
ordinal_match = re.search(r'"tick_ordinal": (\d+)', prompt)
attempt_id = attempt_match.group(1) if attempt_match else "missing"
tick_ordinal = int(ordinal_match.group(1)) if ordinal_match else 0

model = "claude-fable-5" if mode == "wrong_model" else os.environ.get(
    "FAKE_MODEL", "claude-opus-5"
)
if mode != "no_init":
    init = {"type": "system", "subtype": "init", "model": model}
    if os.environ.get("FAKE_OMIT_SID") != "1":
        init["session_id"] = os.environ.get("FAKE_SID", "fake-sid-1")
    print(json.dumps(init), flush=True)

if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_SLEEP", "30")))
    sys.exit(1)

side_effect = os.environ.get("FAKE_SIDE_EFFECT_FILE")
if side_effect and mode == "die_after_side_effect":
    # Model the skill's own idempotency: check-before-post.
    if not os.path.exists(side_effect):
        with open(side_effect, "w", encoding="utf-8") as h:
            h.write("posted")
    sys.exit(1)

text = open(state_path, encoding="utf-8").read()
ticks = re.search(r"monitor_poll_ticks: (\d+)", text)
count = int(ticks.group(1))
if mode != "counter_noop":
    text = text.replace(
        f"monitor_poll_ticks: {count}", f"monitor_poll_ticks: {count + 1}", 1
    )
outcome_env = os.environ.get("FAKE_OUTCOME", "continue")
if outcome_env == "terminal" and os.environ.get("FAKE_SKIP_STATUS_FLIP") != "1":
    text = text.replace('monitor: "in_progress"', 'monitor: "paused"', 1)
if os.environ.get("FAKE_SET_PENDING_HANDOFF") == "1":
    text = text.replace('    status: "idle"', '    status: "pending"', 1)
if os.environ.get("FAKE_SET_FAILED_HANDOFF") == "1":
    old_qa = "\n".join([
        "  qa:",
        "    scenario: null",
        '    status: "idle"',
        "    repository_name_with_owner: null",
        "    targets:",
        "      github_assignees: []",
        "      tracker_assignee_id: null",
        "      tracker_assignee_name: null",
        "    operations: []",
        "    operation_results: {}",
    ])
    new_qa = "\n".join([
        "  qa:",
        '    scenario: "clean_unapproved"',
        '    status: "failed"',
        '    repository_name_with_owner: "Keeper-Dating/matchmaking"',
        "    targets:",
        '      github_assignees: ["tjkeeper"]',
        "      tracker_assignee_id: null",
        "      tracker_assignee_name: null",
        '    operations: ["qa.github.replace_assignees:gtest"]',
        "    operation_results:",
        '      "qa.github.replace_assignees:gtest":',
        '        status: "failed"',
        "        attempts: 1",
        '        started_at: "2026-08-08T00:00:00Z"',
        '        verified_at: "2026-08-08T00:00:01Z"',
        '        error: "GitHub rejected the assignee"',
    ])
    assert old_qa in text
    text = text.replace(old_qa, new_qa, 1)
corrupt_target = os.environ.get("FAKE_CORRUPT_FILE")
if corrupt_target:
    with open(corrupt_target, "w", encoding="utf-8") as h:
        h.write("raise SystemExit(1)\n")
if mode in ("mutate_canonical", "mutate_then_die"):
    with open(state_path, "w", encoding="utf-8") as h:
        h.write(text)
    if mode == "mutate_then_die":
        sys.exit(1)
if mode == "tamper_block":
    text = text.replace("child_session_id: null", 'child_session_id: "hacked"', 1)
with open(candidate_path, "w", encoding="utf-8") as h:
    h.write(text)

digest_raw = subprocess.run(
    [sys.executable, os.path.join(skill_dir, "scripts", "state_schema.py"),
     "--monitor-digest", candidate_path],
    capture_output=True, text=True).stdout
digest = json.loads(digest_raw).get("digest")
if mode == "wrong_digest":
    digest = "deadbeef" * 8
verdict_attempt = "0" * 32 if mode == "verdict_mismatch" else attempt_id
verdict = {"schema_version": 1, "attempt_id": verdict_attempt,
           "tick_ordinal": tick_ordinal, "outcome": os.environ.get("FAKE_OUTCOME", "continue"),
           "post_workflow_digest": digest}
if mode == "no_verdict":
    print(json.dumps({"type": "result", "result": "not json"}), flush=True)
else:
    print(json.dumps({"type": "result", "result": json.dumps(verdict)}), flush=True)
sys.exit(0)
'''


class MonitorRunnerE2ETests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="monitor-runner-"))
        # CR 3760684042: reclaim the fixture directory even on failure.
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.state = self.dir / "workflow-state.local.md"
        self.state.write_text(STATE_FIXTURE, encoding="utf-8")
        self.fake = self.dir / "fake-claude.py"
        self.fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.fake.chmod(0o755)
        self.argv_log = self.dir / "argv.jsonl"
        verdict = subprocess.run(
            [sys.executable, str(SCHEMA), str(self.state)],
            capture_output=True,
            text=True,
        )
        payload = json.loads(verdict.stdout)
        self.assertEqual(payload["state"], "valid", payload["errors"])

    def _run(
        self,
        mode: str = "ok",
        budget: str = "365",
        env_extra: dict[str, str] | None = None,
        timeout: int = 90,
        wait_scale: str = "1.0",
        max_ticks: str | None = None,
        schema_cli: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["FAKE_MODE"] = mode
        env["FAKE_ARGV_LOG"] = str(self.argv_log)
        env.update(env_extra or {})
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                str(self.state),
                "--slice-budget",
                budget,
                "--skill-dir",
                str(SCRIPTS.parent),
                "--claude-bin",
                str(self.fake),
                "--schema-cli",
                schema_cli if schema_cli is not None else str(SCHEMA),
                "--wait-scale",
                wait_scale,
            ]
            + (["--max-ticks", max_ticks] if max_ticks is not None else []),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )

    def _summary(self, completed: subprocess.CompletedProcess[str]) -> dict:
        lines = [l for l in completed.stdout.strip().splitlines() if l.startswith("{")]
        self.assertTrue(lines, completed.stdout + completed.stderr)
        return json.loads(lines[-1])

    def _extract(self) -> dict:
        raw = subprocess.run(
            [sys.executable, str(SCHEMA), "--monitor-extract", str(self.state)],
            capture_output=True,
            text=True,
        ).stdout
        return json.loads(raw)

    def _argv_calls(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.argv_log.read_text().splitlines()
            if line.strip()
        ]

    def test_ok_tick_commits_and_records_session(self) -> None:
        completed = self._run()
        summary = self._summary(completed)
        self.assertEqual(summary["runner_outcome"], "slice_exhausted", completed.stderr)
        self.assertEqual(summary["ticks_completed"], 1)
        self.assertEqual(summary["child_session_id"], "fake-sid-1")
        extract = self._extract()
        self.assertEqual(extract["state"], "valid", extract["errors"])
        self.assertEqual(extract["counters"]["monitor_poll_ticks"], 1)
        block = extract["monitor_cli"]
        self.assertEqual(block["child_session_id"], "fake-sid-1")
        self.assertIsNone(block["in_flight"])
        self.assertEqual(block["last_completed_attempt_id"] is None, False)
        calls = self._argv_calls()
        self.assertEqual(len(calls), 1)
        first = calls[0]
        self.assertIn("--model", first)
        self.assertEqual(first[first.index("--model") + 1], "claude-opus-5")
        self.assertNotIn("--resume", first)
        self.assertNotIn("--permission-mode", first)

    def test_second_slice_resumes_child_session(self) -> None:
        self._run()
        completed = self._run()
        self._summary(completed)
        calls = self._argv_calls()
        self.assertEqual(len(calls), 2)
        second = calls[1]
        self.assertIn("--resume", second)
        self.assertEqual(second[second.index("--resume") + 1], "fake-sid-1")
        extract = self._extract()
        self.assertEqual(extract["counters"]["monitor_poll_ticks"], 2)

    def test_terminal_outcome_exits_zero_and_stops(self) -> None:
        completed = self._run(env_extra={"FAKE_OUTCOME": "terminal"}, budget="2000")
        summary = self._summary(completed)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(summary["runner_outcome"], "terminal")
        self.assertEqual(summary["ticks_completed"], 1)

    def test_terminal_with_failed_handoff_aggregate_commits(self) -> None:
        # R2 #1328 finding 3767068772, reproduced red-first: the QA contract
        # records a failed handoff operation as a non-blocking warning and
        # the exit still pauses, but the transition check rejected any
        # terminal candidate whose ledger aggregate was "failed" — the valid
        # candidate was discarded and monitoring strand-blocked instead of
        # pausing.
        completed = self._run(
            budget="2000",
            env_extra={"FAKE_OUTCOME": "terminal", "FAKE_SET_FAILED_HANDOFF": "1"},
        )
        summary = self._summary(completed)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(summary["runner_outcome"], "terminal")
        extract = self._extract()
        self.assertEqual(extract["state"], "valid", extract["errors"])
        self.assertEqual(extract["monitor_status"], "paused")
        self.assertIn("failed", extract["handoff_statuses"])

    def test_corrupted_schema_source_cannot_forge_validation(self) -> None:
        # R2 #1328 finding 3767068783: the schema CLI executed from the
        # writable package after the write-capable child had run — replacing
        # the helper mid-run could forge validation. The runner now
        # snapshots the helper before any launch; a child that rewrites the
        # source it was pointed at changes nothing.
        schema_copy = self.dir / "schema-copy.py"
        shutil.copyfile(SCHEMA, schema_copy)
        completed = self._run(
            schema_cli=str(schema_copy),
            env_extra={"FAKE_CORRUPT_FILE": str(schema_copy)},
        )
        summary = self._summary(completed)
        self.assertEqual(summary["runner_outcome"], "slice_exhausted", completed.stderr)
        self.assertEqual(summary["ticks_completed"], 1)
        self.assertEqual(
            schema_copy.read_text(encoding="utf-8"), "raise SystemExit(1)\n"
        )
        extract = self._extract()
        self.assertEqual(extract["state"], "valid", extract["errors"])
        self.assertEqual(extract["counters"]["monitor_poll_ticks"], 1)

    def test_wrong_served_model_blocks_immediately(self) -> None:
        completed = self._run(mode="wrong_model")
        self.assertEqual(completed.returncode, 5, completed.stdout + completed.stderr)
        summary = self._summary(completed)
        self.assertEqual(summary["runner_outcome"], "blocked")
        extract = self._extract()
        self.assertEqual(extract["counters"]["monitor_poll_ticks"], 0)

    def test_verdict_mismatch_blocks_after_three_strikes(self) -> None:
        completed = self._run(mode="verdict_mismatch", budget="900", timeout=90, wait_scale="0.02")
        self.assertEqual(completed.returncode, 5, completed.stdout + completed.stderr)
        extract = self._extract()
        failures = extract["monitor_cli"]["child_failures"]
        signatures = [f["signature"] for f in failures]
        self.assertEqual(
            signatures.count("monitor-child:verdict_mismatch"), 3, signatures
        )
        self.assertEqual(extract["counters"]["monitor_poll_ticks"], 0)
        self.assertEqual(extract["state"], "valid", extract["errors"])

    def test_tampered_control_block_is_rejected(self) -> None:
        completed = self._run(mode="tamper_block", budget="900", timeout=90, wait_scale="0.02")
        self.assertEqual(completed.returncode, 5)
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:transition_rejected", signatures)
        self.assertEqual(extract["counters"]["monitor_poll_ticks"], 0)

    def test_wrong_digest_is_rejected(self) -> None:
        completed = self._run(mode="wrong_digest", budget="900", timeout=90, wait_scale="0.02")
        self.assertEqual(completed.returncode, 5)
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:transition_rejected", signatures)

    def test_counter_noop_candidate_is_rejected(self) -> None:
        completed = self._run(mode="counter_noop", budget="900", timeout=90, wait_scale="0.02")
        self.assertEqual(completed.returncode, 5)
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:transition_rejected", signatures)

    def test_no_verdict_charges_failure_and_discards_candidate(self) -> None:
        completed = self._run(mode="no_verdict", budget="900", timeout=90, wait_scale="0.02")
        self.assertEqual(completed.returncode, 5)
        strays = list(self.dir.glob("workflow-state.local.md.attempt-*"))
        self.assertEqual(strays, [])
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:no_verdict", signatures)

    def test_side_effect_before_death_is_not_duplicated(self) -> None:
        marker = self.dir / "reply-posted"
        completed = self._run(
            mode="die_after_side_effect",
            budget="900",
            env_extra={"FAKE_SIDE_EFFECT_FILE": str(marker)},
            timeout=90,
            wait_scale="0.02",
        )
        self.assertEqual(completed.returncode, 5)
        self.assertEqual(marker.read_text(), "posted")
        extract = self._extract()
        self.assertEqual(extract["state"], "valid", extract["errors"])

    def test_second_runner_bounces_off_the_lock(self) -> None:
        env = dict(os.environ)
        env.update(
            {
                "FAKE_MODE": "sleep",
                "FAKE_SLEEP": "20",
                "FAKE_ARGV_LOG": str(self.argv_log),
            }
        )
        first = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                str(self.state),
                "--slice-budget",
                "600",
                "--skill-dir",
                str(SCRIPTS.parent),
                "--claude-bin",
                str(self.fake),
                "--schema-cli",
                str(SCHEMA),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        try:
            # CR 3760684053: poll the observable (the first runner's child
            # launch hits the argv log) instead of a fixed sleep — bounded,
            # and immune to slow-host flake.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.argv_log.exists() and self.argv_log.read_text(
                    encoding="utf-8"
                ).strip():
                    break
                time.sleep(0.1)
            else:
                self.fail("first runner never launched its child")
            second = self._run(budget="365", timeout=30)
            self.assertEqual(second.returncode, 3, second.stdout + second.stderr)
            summary = self._summary(second)
            self.assertEqual(summary["runner_outcome"], "lock_held")
        finally:
            first.send_signal(signal.SIGKILL)
            first.wait(timeout=30)

    def test_crash_recovery_blocks_on_live_child_then_reconciles(self) -> None:
        # R5-2 (final): the runner has NO kill authority. A live recorded
        # child blocks with exact manual instructions; once the child is
        # gone (proven extinct), the next runner reconciles and proceeds.
        env = dict(os.environ)
        env.update(
            {
                "FAKE_MODE": "sleep",
                "FAKE_SLEEP": "60",
                "FAKE_ARGV_LOG": str(self.argv_log),
            }
        )
        first = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                str(self.state),
                "--slice-budget",
                "600",
                "--skill-dir",
                str(SCRIPTS.parent),
                "--claude-bin",
                str(self.fake),
                "--schema-cli",
                str(SCHEMA),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        deadline = time.time() + 30
        in_flight = None
        while time.time() < deadline:
            extract = self._extract()
            block = extract.get("monitor_cli")
            if isinstance(block, dict) and block.get("in_flight"):
                in_flight = block["in_flight"]
                break
            time.sleep(0.5)
        self.assertIsNotNone(in_flight, "runner never registered in_flight")
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=30)
        pgid = in_flight["child_pgid"]
        # Phase 1: live child => block with instructions, nothing signaled.
        blocked = self._run(budget="365", timeout=60)
        self.assertEqual(blocked.returncode, 5, blocked.stdout + blocked.stderr)
        summary = self._summary(blocked)
        self.assertIn("no kill authority", summary["reason"])
        self.assertIn(str(in_flight["child_pid"]), summary["reason"])
        # Phase 2: the human terminates it; the next runner reconciles.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.time() + 15
        while time.time() < deadline:
            probe = subprocess.run(
                ["ps", "-o", "pid=,stat=", "-g", str(pgid)],
                capture_output=True, text=True,
            ).stdout.strip()
            if not probe or all(
                line.split()[1].startswith("Z") for line in probe.splitlines()
            ):
                break
            time.sleep(0.3)
        completed = self._run(budget="365", timeout=60)
        summary = self._summary(completed)
        self.assertEqual(summary["ticks_completed"], 1, completed.stdout)
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:unknown_outcome", signatures)
        self.assertIsNone(extract["monitor_cli"]["in_flight"])

    def test_tiny_slice_budget_never_launches(self) -> None:
        completed = self._run(budget="30", timeout=30)
        summary = self._summary(completed)
        self.assertEqual(summary["runner_outcome"], "slice_exhausted")
        self.assertEqual(summary["ticks_completed"], 0)
        self.assertEqual(self._argv_calls(), [])

    def test_terminal_claim_without_status_flip_is_rejected(self) -> None:
        # F2 postcondition: a "terminal" verdict over a still-in-progress
        # monitor is a false completion and must not commit.
        completed = self._run(
            env_extra={"FAKE_OUTCOME": "terminal", "FAKE_SKIP_STATUS_FLIP": "1"},
            budget="900",
            timeout=90,
            wait_scale="0.02",
        )
        self.assertEqual(completed.returncode, 5)
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:transition_rejected", signatures)
        self.assertEqual(extract["monitor_status"], "in_progress")

    def test_missing_identity_metadata_fails_closed(self) -> None:
        completed = self._run(mode="no_init", budget="900", timeout=90, wait_scale="0.02")
        self.assertEqual(completed.returncode, 5)
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:identity_unreported", signatures)

    def test_non_monitor_phase_never_launches_a_child(self) -> None:
        text = self.state.read_text()
        text = text.replace('current_phase: "monitor"', 'current_phase: "pr"')
        text = text.replace('  pr: "complete"', '  pr: "in_progress"')
        text = text.replace('  monitor: "in_progress"', '  monitor: "pending"')
        self.state.write_text(text)
        verdict = subprocess.run(
            [sys.executable, str(SCHEMA), str(self.state)],
            capture_output=True, text=True,
        )
        payload = json.loads(verdict.stdout)
        self.assertEqual(payload["state"], "valid", payload["errors"])
        completed = self._run(budget="900", timeout=60)
        self.assertEqual(completed.returncode, 5, completed.stdout + completed.stderr)
        self.assertEqual(self._argv_calls(), [])

    def test_streak_survives_slice_boundaries(self) -> None:
        # R2-6: two failures in slice one plus one more in slice two must
        # trigger the three-strike block — the streak is persisted state,
        # not runner-process memory.
        first = self._run(
            mode="verdict_mismatch", budget="900", timeout=90,
            wait_scale="0.02", max_ticks="2",
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertEqual(signatures.count("monitor-child:verdict_mismatch"), 2)
        second = self._run(
            mode="verdict_mismatch", budget="900", timeout=90,
            wait_scale="0.02", max_ticks="2",
        )
        self.assertEqual(second.returncode, 5, second.stdout + second.stderr)

    def test_success_resets_the_persisted_streak(self) -> None:
        self._run(
            mode="verdict_mismatch", budget="900", timeout=90,
            wait_scale="0.02", max_ticks="2",
        )
        ok = self._run(budget="365", timeout=90)
        self.assertEqual(self._summary(ok)["ticks_completed"], 1)
        third = self._run(
            mode="verdict_mismatch", budget="900", timeout=90,
            wait_scale="0.02", max_ticks="2",
        )
        # Two more failures after a success: streak is 2, not 4 — no block.
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)

    def test_resumed_tick_without_session_id_fails_closed(self) -> None:
        self._run(budget="365")  # establishes fake-sid-1
        completed = self._run(
            budget="900", timeout=90, wait_scale="0.02",
            env_extra={"FAKE_OMIT_SID": "1"}, max_ticks="3",
        )
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:session_unreported", signatures)
        self.assertEqual(extract["counters"]["monitor_poll_ticks"], 1)

    def test_canonical_mutation_fails_closed_as_suspect(self) -> None:
        # R3-1/R3-2: canonical drift under the held lock is an UNKNOWN
        # writer — the runner neither restores nor retries; it stops as
        # suspect state and leaves the evidence (and in_flight) in place.
        before = self._extract()["counters"]["monitor_poll_ticks"]
        completed = self._run(
            mode="mutate_canonical", budget="900", timeout=90,
            wait_scale="0.02", max_ticks="3",
        )
        self.assertEqual(completed.returncode, 4, completed.stdout + completed.stderr)
        summary = self._summary(completed)
        self.assertEqual(summary["runner_outcome"], "suspect_state")
        extract = self._extract()
        # The mutation is preserved as evidence, never clobbered:
        self.assertEqual(extract["counters"]["monitor_poll_ticks"], before + 1)
        # in_flight stays recorded for the next runner's recovery path.
        self.assertIsNotNone(extract["monitor_cli"]["in_flight"])
        calls = self._argv_calls()
        self.assertEqual(len(calls), 1, "no retry on mutated input")

    def test_mutation_with_early_child_failure_also_fails_closed(self) -> None:
        # R3-1: the mutation check runs on EVERY post-child path, including
        # a child that dies without a verdict.
        completed = self._run(
            mode="mutate_then_die", budget="900", timeout=90,
            wait_scale="0.02", max_ticks="3",
        )
        self.assertEqual(completed.returncode, 4, completed.stdout + completed.stderr)
        self.assertEqual(self._summary(completed)["runner_outcome"], "suspect_state")
        self.assertEqual(len(self._argv_calls()), 1, "no retry on mutated input")

    def _spawn_group_leader(self) -> "subprocess.Popen[str]":
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
            text=True,
        )

    def _fingerprint(self, pid: int) -> str:
        return subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()

    def _write_in_flight_block(
        self, pid: int, pgid: int, fingerprint: str
    ) -> None:
        block = (
            "monitor_cli:\n"
            "  schema_version: 1\n"
            '  child_session_id: "fake-sid-1"\n'
            '  owner_model: "claude-opus-5"\n'
            "  last_completed_attempt_id: null\n"
            "  child_failures: []\n"
            "  in_flight:\n"
            '    attempt_id: "deadbeefdeadbeefdeadbeefdeadbeef"\n'
            "    tick_ordinal: 1\n"
            '    started_at: "2026-08-06T12:00:00+00:00"\n'
            '    deadline_at: "2026-08-06T12:45:00+00:00"\n'
            f"    child_pid: {pid}\n"
            f"    child_pgid: {pgid}\n"
            f'    child_started_fingerprint: {json.dumps(fingerprint)}\n'
            '    base_workflow_digest: "abababababababababababababababababababababababababababababababab"'
        )
        text = self.state.read_text(encoding="utf-8")
        lines = text.split("\n")
        fence = [i for i, l in enumerate(lines) if l.strip() == "---"][1]
        lines[fence:fence] = block.split("\n")
        self.state.write_text("\n".join(lines), encoding="utf-8")

    def test_suspect_state_blocks_without_signaling_the_recorded_child(self) -> None:
        # R4-2: untrusted (suspect) state never authorizes a signal — a
        # forged record must not become a kill primitive. The runner blocks
        # loudly, NAMES the recorded child, and leaves it untouched.
        leader = self._spawn_group_leader()
        try:
            self._write_in_flight_block(
                leader.pid, leader.pid, self._fingerprint(leader.pid)
            )
            text = self.state.read_text(encoding="utf-8")
            self.state.write_text(
                text.replace("post_push_until: null", "post_push_until: null\nzzz_unknown: 1"),
                encoding="utf-8",
            )
            completed = self._run(budget="365", timeout=60)
            self.assertEqual(completed.returncode, 4, completed.stdout + completed.stderr)
            summary = self._summary(completed)
            self.assertEqual(summary["runner_outcome"], "suspect_state")
            self.assertIn(str(leader.pid), summary["reason"])
            self.assertIn("NOT signaled", summary["reason"])
            self.assertIsNone(leader.poll(), "suspect state must not signal")
            self.assertEqual(self._argv_calls(), [], "suspect state must not launch")
        finally:
            if leader.poll() is None:
                leader.kill()
            leader.wait(timeout=30)

    def test_dead_leader_with_live_group_blocks(self) -> None:
        # R4-3: a dead leader whose group still has live members is
        # unprovable ownership — BLOCK, never kill the survivors. The
        # leader here satisfies pid == pgid (schema-valid record), spawns a
        # same-group survivor, and exits.
        leader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(120)'])",
            ],
            start_new_session=True,
            text=True,
        )
        fingerprint = self._fingerprint(leader.pid)
        pgid = leader.pid
        leader.wait(timeout=30)  # leader exits; survivor keeps the group alive
        try:
            self._write_in_flight_block(pgid, pgid, fingerprint or "gone")
            completed = self._run(budget="365", timeout=60)
            self.assertEqual(completed.returncode, 5, completed.stdout + completed.stderr)
            summary = self._summary(completed)
            self.assertEqual(summary["runner_outcome"], "blocked")
            self.assertIn("no kill authority", summary["reason"])
            survivors = subprocess.run(
                ["ps", "-o", "pid=,stat=", "-g", str(pgid)],
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertTrue(survivors, "survivor should still be alive (not killed)")
        finally:
            import signal as _signal
            try:
                os.killpg(pgid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def test_forged_non_leader_record_is_schema_suspect(self) -> None:
        # R4-3 schema half: pid != pgid can never reach the kill path — the
        # validity gate rejects the record first, and nothing is signaled.
        survivor = self._spawn_group_leader()
        try:
            self._write_in_flight_block(
                survivor.pid + 1 if survivor.pid + 1 != survivor.pid else survivor.pid + 2,
                survivor.pid,
                "forged",
            )
            completed = self._run(budget="365", timeout=60)
            self.assertEqual(completed.returncode, 4, completed.stdout + completed.stderr)
            self.assertEqual(self._summary(completed)["runner_outcome"], "suspect_state")
            self.assertIsNone(survivor.poll(), "forged record must not signal")
            self.assertEqual(self._argv_calls(), [])
        finally:
            if survivor.poll() is None:
                survivor.kill()
            survivor.wait(timeout=30)

    def test_terminal_with_pending_handoff_is_rejected(self) -> None:
        completed = self._run(
            budget="900", timeout=90, wait_scale="0.02", max_ticks="3",
            env_extra={"FAKE_OUTCOME": "terminal", "FAKE_SET_PENDING_HANDOFF": "1"},
        )
        self.assertEqual(completed.returncode, 5)
        extract = self._extract()
        signatures = [f["signature"] for f in extract["monitor_cli"]["child_failures"]]
        self.assertIn("monitor-child:transition_rejected", signatures)
        self.assertEqual(extract["monitor_status"], "in_progress")

    def test_ambiguous_ps_answer_blocks_instead_of_proving_extinction(self) -> None:
        # R6: rc=1 from ps is a trusted no-match only when SILENT — the
        # same code with stderr is a platform error and must block, never
        # read as extinction (which would launch a writer beside a
        # possibly-live orphan).
        fake_ps_dir = self.dir / "fakebin"
        fake_ps_dir.mkdir()
        fake_ps = fake_ps_dir / "ps"
        fake_ps.write_text(
            "#!/bin/sh\n"
            'if [ -n "$FAKE_PS_FAIL" ]; then case "$*" in *-g*)'
            ' echo "ps: internal error" >&2; exit 1;; esac; fi\n'
            'exec /bin/ps "$@"\n',
            encoding="utf-8",
        )
        fake_ps.chmod(0o755)
        dead = subprocess.Popen([sys.executable, "-c", "pass"], text=True)
        dead.wait(timeout=30)
        self._write_in_flight_block(dead.pid, dead.pid, "gone")
        completed = self._run(
            budget="365",
            timeout=60,
            env_extra={
                "PATH": f"{fake_ps_dir}:{os.environ['PATH']}",
                "FAKE_PS_FAIL": "1",
            },
        )
        self.assertEqual(completed.returncode, 5, completed.stdout + completed.stderr)
        summary = self._summary(completed)
        self.assertIn("ambiguous", summary["reason"])

    def test_wrapper_eof_never_executes_the_model(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "monitor_child_wrapper.py"),
                "--",
                str(self.fake),
                "prompt",
            ],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self._argv_calls(), [])


if __name__ == "__main__":
    unittest.main()
