#!/usr/bin/env python3
"""Owner-pinned Phase 6 monitor runner — deterministic slice loop.

The parent session (any model) makes ONE foreground invocation per slice;
this runner then drives owner-pinned ``claude -p`` child ticks under a real
kernel lock, so the monitor's cache lineage lives on the OWNER model no
matter which model the parent runs. No LLM reasoning happens here: the
runner is deterministic control flow — lock, launch, supervise, verify,
commit, wait — and the parent wakes once per slice, not once per tick
(waking the parent per tick would re-send its giant context and keep the
dominant cache-write cost this design exists to remove).

Structural rule (same as test_cli_fail_closed.py): this file uses
``subprocess``, so it never imports the package evaluators — state parsing
and validation go through the ``state_schema.py`` CLI (``--monitor-extract``
/ ``--monitor-digest``), keeping the eval entry-point names out of this
file entirely.

Write protocol (plan-gate converged):
- The child NEVER touches canonical state: it writes a full updated copy to
  a per-attempt CANDIDATE path; the runner is the sole canonical committer.
- Finalization is ONE atomic write: the runner splices the runner-owned
  ``monitor_cli`` block (session id, cleared ``in_flight``, failure ledger)
  into the VALIDATED candidate and ``os.replace``s it onto canonical. There
  is no post-commit acknowledgement write to lose.
- EVERY post-child path first verifies canonical (digest and control
  block) against the launch snapshot. Drift under the held lock is an
  unknown writer: the candidate is discarded and the runner stops as
  suspect state — never a restore, never a retry on mutated input.
- The launch barrier closes the spawn-registration crash window: the child
  is spawned as a wrapper (own process group) that waits for a GO token;
  the runner records pid/pgid/start-fingerprint in ``in_flight`` FIRST,
  then releases the token. A runner death before release leaves a wrapper
  that exits on EOF without ever executing the model.
- Recovery has NO kill authority: no local artifact proves a record's
  provenance against a write-capable child, so a recovery record is never
  a signal authorization — extinct-or-block, with the record named.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from model_policy import (  # noqa: E402
    MIN_CLAUDE_VERSION,
    MONITOR_CHILD_IDLE_TIMEOUT_SECONDS,
    MONITOR_CHILD_MIN_VIABLE_SECONDS,
    MONITOR_SLICE_BUDGET_SECONDS,
    MONITOR_SLICE_CLEANUP_MARGIN_SECONDS,
    PER_ATTEMPT_CEILING_SECONDS,
    LIVENESS_BACKOFF_LADDER_SECONDS,
    _has_auth_signature,
    _version_at_least,
    monitor_child_arguments,
    monitor_child_prompt,
    monitor_orchestrator_binding,
)

WRAPPER_EXEC_FAILED_MARKER = "MONITOR-WRAPPER-EXEC-FAILED"
_RESUME_NOT_FOUND_HINTS = ("no conversation found", "session not found", "unknown session")
DIAGNOSTIC_LINE_CAP = 50
PIPE_BUFFER_CAP = 1_048_576

MONITOR_CHILD_FAILURE_LIMIT = 3  # mirrors state_schema.MONITOR_CHILD_FAILURE_LIMIT
WAIT_CHUNK_SECONDS = 60


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _heartbeat(message: str) -> None:
    print(f"monitor-runner: {message}", flush=True)


class RunnerExit(Exception):
    def __init__(self, code: int, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.outcome = outcome
        self.reason = reason


class SchemaCli:
    """All state parsing/validation via the trusted CLI — never imported."""

    def __init__(self, schema_path: Path) -> None:
        self._path = schema_path

    def _run(self, mode: list[str], target: Path) -> dict[str, Any]:
        completed = subprocess.run(
            [sys.executable, str(self._path), *mode, str(target)],
            capture_output=True,
            text=True,
        )
        try:
            return json.loads(completed.stdout)
        except (json.JSONDecodeError, ValueError):
            return {"state": "suspect", "errors": ["schema CLI produced no JSON"]}

    def extract(self, target: Path) -> dict[str, Any]:
        return self._run(["--monitor-extract"], target)

    def digest(self, target: Path) -> str | None:
        result = self._run(["--monitor-digest"], target)
        digest = result.get("digest")
        return digest if isinstance(digest, str) and digest else None

    def validate_text_via_file(self, text: str, scratch: Path) -> dict[str, Any]:
        scratch.write_text(text, encoding="utf-8")
        try:
            return self._run([], scratch)
        finally:
            try:
                scratch.unlink()
            except OSError:
                pass


def _render_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def render_monitor_cli_block(block: dict[str, Any]) -> str:
    """Fixed rendering of the runner-owned block (restricted-parser-safe:
    block maps only, no inline maps, quoted strings)."""

    lines = ["monitor_cli:"]
    lines.append(f"  schema_version: {block['schema_version']}")
    lines.append(f"  child_session_id: {_render_scalar(block['child_session_id'])}")
    lines.append(f"  owner_model: {_render_scalar(block['owner_model'])}")
    lines.append(
        "  last_completed_attempt_id:"
        f" {_render_scalar(block['last_completed_attempt_id'])}"
    )
    failures = block.get("child_failures") or []
    if not failures:
        lines.append("  child_failures: []")
    else:
        lines.append("  child_failures:")
        for record in failures:
            lines.append(f"    - signature: {_render_scalar(record['signature'])}")
            lines.append(f"      at: {_render_scalar(record['at'])}")
    in_flight = block.get("in_flight")
    if in_flight is None:
        lines.append("  in_flight: null")
    else:
        lines.append("  in_flight:")
        for key in (
            "attempt_id",
            "tick_ordinal",
            "started_at",
            "deadline_at",
            "child_pid",
            "child_pgid",
            "child_started_fingerprint",
            "base_workflow_digest",
        ):
            lines.append(f"    {key}: {_render_scalar(in_flight[key])}")
    return "\n".join(lines)


def splice_monitor_cli(text: str, block: dict[str, Any]) -> str:
    """Replace (or insert before the closing fence) the monitor_cli block.

    The block is runner-rendered on every write, so its span is always the
    fixed shape above; child candidates carry it value-identically and the
    runner re-renders at finalization regardless.
    """

    lines = text.split("\n")
    fence_indexes = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(fence_indexes) < 2:
        raise RunnerExit(4, "suspect_state", "state file has no frontmatter fence")
    close = fence_indexes[1]
    start = None
    for i in range(fence_indexes[0] + 1, close):
        if lines[i].startswith("monitor_cli:"):
            start = i
            break
    rendered = render_monitor_cli_block(block).split("\n")
    if start is None:
        new_lines = lines[:close] + rendered + lines[close:]
    else:
        end = start + 1
        while end < close and (lines[end].startswith(" ") or not lines[end].strip()):
            end += 1
        new_lines = lines[:start] + rendered + lines[end:]
    return "\n".join(new_lines)


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def process_fingerprint(pid: int) -> str | None:
    completed = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True
    )
    value = completed.stdout.strip()
    return value or None


def _live_group_members(pgid: int) -> list[str]:
    """R5-4: fail-closed, bounded process-table inspection.

    ``ps -g`` exits 0 with rows when members exist and 1 with no rows when
    none do — BOTH are trusted answers. Anything else (timeout, launch
    failure, unparseable output, other exit codes) is an inspection
    failure and blocks: an unprovable answer must never read as
    extinction. Zombies (stat Z…) cannot execute or write and do not
    count as alive.
    """

    try:
        completed = subprocess.run(
            ["ps", "-o", "pid=,stat=", "-g", str(pgid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunnerExit(
            5,
            "blocked",
            "process-table inspection failed"
            f" ({error.__class__.__name__}) — cannot prove the recorded"
            " group extinct; needs a human",
        )
    if completed.returncode == 1:
        # rc 1 is ps's no-match answer ONLY when it is silent — the same
        # code with stderr (or stray stdout) is an invocation/platform
        # error, and an unprovable answer must never read as extinction.
        if completed.stdout.strip() or completed.stderr.strip():
            raise RunnerExit(
                5,
                "blocked",
                "process-table inspection returned an ambiguous no-match"
                " (rc 1 with output) — cannot prove the recorded group"
                " extinct; needs a human",
            )
        return []
    if completed.returncode != 0:
        raise RunnerExit(
            5,
            "blocked",
            f"process-table inspection exited {completed.returncode} —"
            " cannot prove the recorded group extinct; needs a human",
        )
    members: list[str] = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 2 or not parts[0].isdigit():
            raise RunnerExit(
                5,
                "blocked",
                "process-table inspection produced unparseable output —"
                " cannot prove the recorded group extinct; needs a human",
            )
        if not parts[1].startswith("Z"):
            members.append(parts[0])
    return members


def _bounded_reap(proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    """R3-5: reap a killed child under a bounded, heartbeating deadline.

    A SIGKILLed process stuck in uninterruptible I/O must not silence the
    runner past every ceiling — after the bound the child is reported
    unreaped, a blocked-class outcome: a possibly-live writer is a human
    problem, not a retry.
    """

    deadline = time.monotonic() + timeout
    last_beat = time.monotonic()
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        if time.monotonic() - last_beat >= 10:
            _heartbeat("awaiting kill reap")
            last_beat = time.monotonic()
        time.sleep(0.2)
    return proc.poll() is not None


def _drain_child(
    proc: subprocess.Popen,
    idle_timeout: float,
    deadline: float,
) -> dict[str, Any]:
    """Bounded protocol drain: parses stream-json INCREMENTALLY under idle +
    absolute-deadline bounds, retaining only the protocol facts (session id,
    served model, final result payload) plus capped diagnostics — memory is
    bounded no matter how chatty or newline-free the child is.

    Deliberately NOT model_policy.supervise_stream: that boundary returns
    bounded excerpts for gate calls, while the runner must read the protocol
    content. Bounds match the Timeout Heuristics values. Emits a parent
    heartbeat every 60s so the SUPERVISING session's own idle guard sees
    activity while a healthy child streams for minutes. After pipe EOF the
    wait is deadline-bounded — a child that closed its pipes but lives past
    the ceiling is killed as runaway, never waited on unboundedly.
    """

    import selectors

    selector = selectors.DefaultSelector()
    protocol: dict[str, Any] = {"session_id": None, "served_model": None, "result_text": None}
    recent_lines: list[str] = []
    stderr_tail: list[str] = []
    buffers = {proc.stdout: b"", proc.stderr: b""}
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
    last_activity = time.monotonic()
    last_heartbeat = time.monotonic()
    outcome = "clean"

    def _consume(decoded: str, from_stdout: bool) -> None:
        if from_stdout:
            recent_lines.append(decoded[:2000])
            del recent_lines[:-DIAGNOSTIC_LINE_CAP]
            try:
                event = json.loads(decoded)
            except json.JSONDecodeError:
                return
            if not isinstance(event, dict):
                return
            if event.get("type") == "system" and event.get("subtype") == "init":
                sid = event.get("session_id")
                model = event.get("model")
                if isinstance(sid, str) and sid:
                    protocol["session_id"] = sid
                if isinstance(model, str) and model:
                    protocol["served_model"] = model
            elif event.get("type") == "result" and isinstance(event.get("result"), str):
                protocol["result_text"] = event["result"]
        else:
            stderr_tail.append(decoded[:400])
            del stderr_tail[:-20]

    while selector.get_map():
        now = time.monotonic()
        if now >= deadline:
            outcome = "runaway"
            break
        if now - last_activity >= idle_timeout:
            outcome = "timeout"
            break
        if now - last_heartbeat >= 60:
            _heartbeat(f"supervising child (remaining ceiling {int(deadline - now)}s)")
            last_heartbeat = now
        for key, _ in selector.select(timeout=1.0):
            pipe = key.fileobj
            try:
                chunk = os.read(pipe.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(pipe)
                continue
            last_activity = time.monotonic()
            buffers[pipe] += chunk
            if len(buffers[pipe]) > PIPE_BUFFER_CAP:
                buffers[pipe] = buffers[pipe][-PIPE_BUFFER_CAP:]
            while b"\n" in buffers[pipe]:
                line, buffers[pipe] = buffers[pipe].split(b"\n", 1)
                _consume(line.decode("utf-8", "replace"), pipe is proc.stdout)
    selector.close()
    # R2-7: a final line without a trailing newline still carries protocol
    # facts — consume the remainders before deciding anything.
    for pipe, remainder in buffers.items():
        if remainder:
            _consume(remainder.decode("utf-8", "replace"), pipe is proc.stdout)
    if outcome != "clean":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        if not _bounded_reap(proc):
            outcome = "unreaped"
    else:
        # Deadline-bounded post-EOF wait that KEEPS heartbeating — a child
        # that closed its pipes but lives on must not silence the runner
        # past the parent's own idle guard.
        while proc.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                outcome = "runaway"
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                if not _bounded_reap(proc):
                    outcome = "unreaped"
                break
            if now - last_heartbeat >= 60:
                _heartbeat(
                    f"child pipes closed; awaiting exit (remaining {int(deadline - now)}s)"
                )
                last_heartbeat = now
            time.sleep(0.5)
    return {
        "outcome": outcome,
        "exit_code": proc.returncode,
        "protocol": protocol,
        "recent_lines": recent_lines,
        "stderr_tail": stderr_tail,
    }


def parse_verdict(result_text: str | None) -> dict[str, Any] | None:
    """The verdict comes only from the final result payload — never from
    intermediate model text."""

    if result_text is None:
        return None
    stripped = result_text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`\n ")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def classify_child_failure(
    exit_code: int,
    stderr_tail: list[str],
    resumed: bool,
) -> tuple[str, str]:
    """Phase-aware classification (F4): ('block'|'ladder'|'charge'|'fresh_session', signature).

    - Deterministic setup failures (exec-failed wrapper marker, auth
      signatures) BLOCK with an actionable reason.
    - A resume-target-not-found error clears the stale session and retries
      fresh — not a failure at all.
    - Rate/overload noise is liveness-class: ladder wait, no budget charge.
    - Everything else is an unknown-outcome charge.
    """

    joined = "\n".join(stderr_tail)
    lowered = joined.lower()
    if WRAPPER_EXEC_FAILED_MARKER in joined:
        return ("block", "claude CLI binary could not be executed — install or fix PATH")
    if _has_auth_signature(joined):
        return ("block", "claude CLI authentication failure — re-authenticate the owner route")
    if resumed and any(hint in lowered for hint in _RESUME_NOT_FOUND_HINTS):
        return ("fresh_session", "monitor-child:resume_not_found")
    if "429" in lowered or "rate limit" in lowered or "overloaded" in lowered:
        return ("ladder", "monitor-child:rate_limited")
    return ("charge", f"monitor-child:exit_{exit_code}")


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.state_path = Path(args.state_file).resolve()
        self.skill_dir = Path(args.skill_dir).resolve()
        self.claude_bin = args.claude_bin
        # R2 #1328 finding 3767068783: the schema CLI must not execute from
        # the writable skill package after a write-capable child has run — a
        # compromised child could rewrite the validator and forge a valid
        # verdict. Snapshot the helper to a private directory OUTSIDE the
        # worktree BEFORE any child launch, pin its digest, and re-verify
        # identity immediately before every canonical commit.
        source_cli = Path(args.schema_cli).resolve()
        snapshot_dir = Path(
            tempfile.mkdtemp(prefix="autonomy-schema-snapshot-")
        )
        self.schema_snapshot_path = snapshot_dir / source_cli.name
        shutil.copyfile(source_cli, self.schema_snapshot_path)
        self.schema_snapshot_sha256 = hashlib.sha256(
            self.schema_snapshot_path.read_bytes()
        ).hexdigest()
        self.schema = SchemaCli(self.schema_snapshot_path)
        self.slice_deadline = time.monotonic() + args.slice_budget
        # Testability seam (same class as --claude-bin): scales ladder and
        # poll waits so hermetic failure-path tests finish in seconds. The
        # default 1.0 is the production contract; the runner clamps upward
        # of 0 and never above 1.
        self.wait_scale = min(1.0, max(0.001, args.wait_scale))
        # Testability seam: bounds tick ATTEMPTS in one slice so multi-slice
        # protocol tests are deterministic. None (production) = unbounded.
        self.max_ticks = getattr(args, "max_ticks", None)
        self.tick_attempts = 0
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".monitor.lock")
        self._lock_handle: IO[bytes] | None = None
        self.ticks_completed = 0
        self.child_session_id: str | None = None
        self.owner_model: str | None = None
        # F1: verification evidence lives in RUNNER MEMORY, never re-derived
        # from post-child state (an untrusted child could rewrite canonical
        # evidence). Loaded once at start; appended only by the runner.
        self.failures: list[dict[str, Any]] = []
        self.consecutive_signature: str | None = None
        self.consecutive_count = 0
        self.launch_block: dict[str, Any] | None = None
        self.launch_base_digest: str | None = None

    # -- lock ------------------------------------------------------------
    def acquire_lock(self) -> None:
        handle = open(self.lock_path, "ab")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RunnerExit(3, "lock_held", "another monitor runner is active")
        self._lock_handle = handle

    # -- state helpers ---------------------------------------------------
    def read_text(self) -> str:
        return self.state_path.read_text(encoding="utf-8")

    def current_block(self, extract: dict[str, Any]) -> dict[str, Any]:
        block = extract.get("monitor_cli")
        base = dict(block) if isinstance(block, dict) else {
            "schema_version": 1,
            "child_session_id": None,
            "owner_model": self.owner_model,
            "last_completed_attempt_id": None,
            "in_flight": None,
        }
        # The failure ledger is runner-memory truth (bounded diagnostic
        # history in state) — a child that erased it changes nothing.
        base["child_failures"] = list(self.failures[-10:])
        return base

    def commit_block(self, block: dict[str, Any]) -> None:
        spliced = splice_monitor_cli(self.read_text(), block)
        atomic_write(self.state_path, spliced)

    # -- failure ledger --------------------------------------------------
    def charge_failure(self, extract: dict[str, Any], signature: str) -> None:
        # R2 #1495 finding 3777668741 (second half): this failure commit is a
        # read-modify-write of canonical state — re-verify canonical against
        # the launch snapshot IMMEDIATELY before it, so drift written after
        # the post-drain verification stops as suspect instead of being
        # silently absorbed. Only meaningful once a launch snapshot exists;
        # pre-launch charges have no child window to guard.
        if self.launch_block is not None and self.launch_base_digest is not None:
            self._require_unmutated_canonical(
                self.schema.extract(self.state_path), None
            )
        self.failures.append({"signature": signature, "at": _utcnow_iso()})
        # Streak, not lifetime (F8): the 3-strike rule fires on CONSECUTIVE
        # same-signature failures; a successful tick or a different
        # signature resets the streak, and the persisted list is bounded
        # diagnostic history only.
        if signature == self.consecutive_signature:
            self.consecutive_count += 1
        else:
            self.consecutive_signature = signature
            self.consecutive_count = 1
        block = self.current_block(extract)
        block["in_flight"] = None
        self.commit_block(block)
        if self.consecutive_count >= MONITOR_CHILD_FAILURE_LIMIT:
            raise RunnerExit(
                5,
                "blocked",
                f"{signature}: {MONITOR_CHILD_FAILURE_LIMIT} consecutive"
                " same-signature child failures — needs a human (never silent"
                " inline fallback)",
            )

    # -- recovery --------------------------------------------------------
    def _reconcile_recorded_orphan(self, in_flight: dict[str, Any]) -> None:
        """Recovery WITHOUT kill authority (R5-2, final form).

        No local artifact can prove that THIS workflow created a recovery
        record — a write-capable same-user child can forge any file the
        runner can read, so automatic signaling from persisted state is a
        kill primitive no matter how the record is validated. The runner
        therefore NEVER signals on recovery: it either PROVES the recorded
        group extinct (hardened process-table inspection, fail-closed) and
        reconciles, or it BLOCKS with the full record and exact manual
        instructions. Orphaned children are one-shot ``claude -p``
        processes that end on their own, so the block case is the rare
        persistent tail, not the norm.
        """

        pid = in_flight.get("child_pid")
        pgid = in_flight.get("child_pgid")
        recorded = in_flight.get("child_started_fingerprint")
        if not (isinstance(pid, int) and isinstance(pgid, int)):
            return
        if _live_group_members(pgid):
            raise RunnerExit(
                5,
                "blocked",
                "a previously recorded monitor child may still be running"
                f" (pid {pid}, pgid {pgid}, started {recorded!r}) — the"
                " runner has no kill authority (record provenance cannot be"
                " proven locally); verify and terminate it manually, then"
                " resume (reset clears the record)",
            )

    def recover_in_flight(self, extract: dict[str, Any]) -> None:
        """State-writing half of recovery: discard candidates and charge the
        unknown-outcome budget. Runs only on a VALID state (writes go through
        the same splice/commit path as everything else); the no-signal
        extinction check already ran before the validity gate."""

        block = extract.get("monitor_cli")
        if not isinstance(block, dict):
            return
        in_flight = block.get("in_flight")
        if not isinstance(in_flight, dict):
            return
        for stray in self.state_path.parent.glob(self.state_path.name + ".attempt-*"):
            try:
                stray.unlink()
            except OSError:
                pass
        _heartbeat("recovery: unknown prior attempt reconciled (candidate discarded)")
        self.charge_failure(extract, "monitor-child:unknown_outcome")

    # -- tick ------------------------------------------------------------
    def remaining(self) -> float:
        return self.slice_deadline - time.monotonic()

    def launch_child(
        self, prompt: str, resume_id: str | None, ceiling: float
    ) -> dict[str, Any]:
        argv = [self.claude_bin] + monitor_child_arguments(
            self.owner_model, resume_id=resume_id
        )
        argv.append(prompt)
        # The barrier wrapper lives in its own exec-only file (scanner
        # structural rule: exec and subprocess never share a file).
        wrapper = [
            sys.executable,
            str(SCRIPTS_DIR / "monitor_child_wrapper.py"),
            "--",
        ] + argv
        proc = subprocess.Popen(
            wrapper,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            cwd=str(self.state_path.parent),
        )
        return {"proc": proc}

    def run_tick(self, extract: dict[str, Any]) -> str:
        from datetime import timedelta

        tick_ordinal = self.ticks_completed + 1
        attempt_id = uuid.uuid4().hex
        candidate = self.state_path.parent / (
            self.state_path.name + f".attempt-{attempt_id}.md"
        )
        base_digest = extract.get("digest")
        if not isinstance(base_digest, str):
            raise RunnerExit(4, "suspect_state", "canonical digest unavailable")
        tainted = extract.get("tainted") or []
        if tainted:
            # R2 #1495 finding 3776596739: structural validity alone must not
            # launch a write-capable child — instruction-like content in the
            # prompt ledger or feedback maps reaches the model with no tool
            # clamp. Fail closed BEFORE the launch; clearing the taint is a
            # human judgment, never the runner's.
            raise RunnerExit(
                4,
                "suspect_state",
                f"canonical state carries {len(tainted)} instruction-like"
                " taint record(s) — a write-capable monitor child must not"
                " launch on untrusted content; review the tainted paths in"
                " the validator output, clean or explicitly rewrite them,"
                " then resume",
            )
        prompt = monitor_child_prompt(
            str(self.skill_dir),
            str(self.state_path),
            str(candidate),
            attempt_id,
            tick_ordinal,
        )
        ceiling = min(
            PER_ATTEMPT_CEILING_SECONDS,
            self.remaining() - MONITOR_SLICE_CLEANUP_MARGIN_SECONDS,
        )
        resumed = self.child_session_id is not None
        self.tick_attempts += 1
        launched = self.launch_child(prompt, self.child_session_id, ceiling)
        proc: subprocess.Popen = launched["proc"]
        fingerprint = process_fingerprint(proc.pid)
        if fingerprint is None:
            # R4-4: no GO was sent — EOF makes the wrapper exit without
            # executing the model; the reap is bounded and heartbeating like
            # every other kill path.
            try:
                proc.stdin.close()
            except OSError:
                pass
            if not _bounded_reap(proc):
                raise RunnerExit(
                    5,
                    "blocked",
                    "unregistered launch wrapper could not be reaped — a"
                    " possibly-live process needs a human",
                )
            self.charge_failure(extract, "monitor-child:spawn_failed")
            return "retry"
        block = self.current_block(extract)
        block["owner_model"] = self.owner_model
        block["in_flight"] = {
            "attempt_id": attempt_id,
            "tick_ordinal": tick_ordinal,
            "started_at": _utcnow_iso(),
            "deadline_at": (
                datetime.now(timezone.utc) + timedelta(seconds=max(0, ceiling))
            ).isoformat(),
            "child_pid": proc.pid,
            # CR 3760683988: start_new_session=True makes the child a session
            # leader, so pgid == pid BY CONSTRUCTION — and unlike
            # os.getpgid(), recording the pid cannot raise if the child
            # exits in the window between spawn and this line.
            "child_pgid": proc.pid,
            "child_started_fingerprint": fingerprint,
            "base_workflow_digest": base_digest,
        }
        # F1: the verification evidence is THIS in-memory snapshot — never
        # re-derived from state the child may have rewritten.
        self.commit_block(block)
        committed = self.schema.extract(self.state_path)
        # R4-1: the launch baseline must be a VALIDATED snapshot whose
        # workflow digest still equals the pre-commit base — a concurrent
        # mutation in this window must never become the accepted baseline,
        # and a failed extraction must never silently disable the mutation
        # checks. The GO token has not been sent yet, so aborting here
        # leaves the wrapper to exit on EOF without executing the model.
        if (
            committed.get("state") != "valid"
            or committed.get("digest") != base_digest
            # R5-1: EXACT equality with the block just committed — a
            # schema-valid control mutation in this window must not become
            # the accepted baseline (the workflow digest excludes it).
            or committed.get("monitor_cli") != block
        ):
            try:
                proc.stdin.close()
            except OSError:
                pass
            if not _bounded_reap(proc):
                raise RunnerExit(
                    5,
                    "blocked",
                    "aborted launch wrapper could not be reaped — a"
                    " possibly-live process needs a human",
                )
            raise RunnerExit(
                4,
                "suspect_state",
                "canonical state failed validation between launch commit and"
                " GO — unknown writer; reconcile per the Resume trust model",
            )
        self.launch_block = committed["monitor_cli"]
        self.launch_base_digest = committed["digest"]
        assert proc.stdin is not None
        proc.stdin.write(b"GO\n")
        proc.stdin.flush()
        proc.stdin.close()
        drained = _drain_child(
            proc,
            idle_timeout=MONITOR_CHILD_IDLE_TIMEOUT_SECONDS,
            deadline=time.monotonic() + ceiling,
        )
        fresh = self.schema.extract(self.state_path)
        self._require_unmutated_canonical(fresh, candidate)
        if drained["outcome"] == "unreaped":
            self._discard(candidate)
            raise RunnerExit(
                5,
                "blocked",
                "killed monitor child could not be reaped within the bounded"
                " window — a possibly-live writer needs a human",
            )
        # R2 #1495 findings 3776596760 + 3777668741: the leader's exit —
        # clean OR failed — says nothing about descendants, and the failure
        # path clears the only survivor record (in_flight) when it commits.
        # Prove the whole process group extinct for EVERY drained outcome
        # before any state is cleared or committed; survivors are killed and
        # boundedly rechecked, an unkillable survivor blocks for a human,
        # and a clean tick that needed the kill is charged and retried.
        survivors = _live_group_members(proc.pid)
        if survivors:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and _live_group_members(
                proc.pid
            ):
                time.sleep(0.2)
            if _live_group_members(proc.pid):
                self._discard(candidate)
                raise RunnerExit(
                    5,
                    "blocked",
                    "monitor child left live process-group members that"
                    " survived SIGKILL — a possibly-live writer needs a"
                    " human",
                )
            if drained["outcome"] == "clean" and drained["exit_code"] == 0:
                self._discard(candidate)
                self.charge_failure(fresh, "monitor-child:group_survivors")
                return "retry"
        if drained["outcome"] != "clean" or drained["exit_code"] != 0:
            self._discard(candidate)
            if drained["outcome"] != "clean":
                self.charge_failure(fresh, f"monitor-child:{drained['outcome']}")
                return "retry"
            action, detail = classify_child_failure(
                drained["exit_code"], drained["stderr_tail"], resumed
            )
            if action == "block":
                self._clear_in_flight(fresh)
                raise RunnerExit(5, "blocked", detail)
            if action == "fresh_session":
                _heartbeat("resume target gone — clearing session for a fresh owner child")
                self.child_session_id = None
                self._clear_in_flight(fresh)
                return "retry_now"
            if action == "ladder":
                self._clear_in_flight(fresh)
                return "retry"
            self.charge_failure(fresh, detail)
            return "retry"
        protocol = drained["protocol"]
        verdict = parse_verdict(protocol.get("result_text"))
        served = protocol.get("served_model")
        session_id = protocol.get("session_id")
        # F3: identity and session continuity fail CLOSED.
        if not isinstance(served, str) or not served:
            self._discard(candidate)
            self.charge_failure(fresh, "monitor-child:identity_unreported")
            return "retry"
        if served != self.owner_model:
            self._discard(candidate)
            self._clear_in_flight(fresh)
            raise RunnerExit(
                5,
                "blocked",
                f"served model {served!r} is not the bound owner"
                f" {self.owner_model!r} — identity is the contract",
            )
        if resumed:
            if not isinstance(session_id, str) or session_id != self.child_session_id:
                self._discard(candidate)
                self.charge_failure(
                    fresh,
                    "monitor-child:session_mismatch"
                    if session_id
                    else "monitor-child:session_unreported",
                )
                return "retry"
        elif not isinstance(session_id, str) or not session_id:
            self._discard(candidate)
            self.charge_failure(fresh, "monitor-child:no_session_id")
            return "retry"
        if verdict is None:
            self._discard(candidate)
            self.charge_failure(fresh, "monitor-child:no_verdict")
            return "retry"
        return self._verify_and_commit(
            fresh, candidate, attempt_id, tick_ordinal, verdict, protocol
        )

    def _clear_in_flight(self, extract: dict[str, Any]) -> None:
        # Same pre-commit canonical recheck as charge_failure (finding
        # 3777668741): this path also clears in_flight via read-modify-write.
        if self.launch_block is not None and self.launch_base_digest is not None:
            self._require_unmutated_canonical(
                self.schema.extract(self.state_path), None
            )
        block = self.current_block(extract)
        block["in_flight"] = None
        # R2-5: memory is truth for session identity too — a cleared resume
        # target must be cleared in CANONICAL state, or the next slice
        # reloads the stale id and repeats the failed resume.
        block["child_session_id"] = self.child_session_id
        self.commit_block(block)


    def _require_unmutated_canonical(
        self, fresh: dict[str, Any], candidate: Path | None
    ) -> None:
        """R3-1/R3-2: every post-child path verifies canonical is byte-true
        to the launch snapshot BEFORE any state write or retry decision.

        A drifted digest or control block under the held lock means an
        unknown writer touched canonical state mid-tick. The runner cannot
        prove whether that was the child, a human, or another tool — so it
        neither restores nor retries on it: it stops with suspect state and
        leaves the evidence in place for the Resume trust model.
        """

        if self.launch_block is None or self.launch_base_digest is None:
            raise RunnerExit(
                4,
                "suspect_state",
                "post-child verification has no launch snapshot — internal"
                " ordering defect; never continue unchecked",
            )
        if (
            fresh.get("monitor_cli") == self.launch_block
            and fresh.get("digest") == self.launch_base_digest
        ):
            return
        if candidate is not None:
            self._discard(candidate)
        raise RunnerExit(
            4,
            "suspect_state",
            "canonical state changed under the monitor lock (digest or"
            " control drift vs the launch snapshot) — unknown writer;"
            " reconcile per the Resume trust model before resuming"
            " monitoring",
        )

    def _discard(self, candidate: Path) -> None:
        try:
            candidate.unlink()
        except OSError:
            pass

    def _verify_and_commit(
        self,
        fresh: dict[str, Any],
        candidate: Path,
        attempt_id: str,
        tick_ordinal: int,
        verdict: dict[str, Any],
        protocol: dict[str, Any],
    ) -> str:
        outcome = verdict.get("outcome")
        checks_failed = (
            verdict.get("schema_version") != 1
            or verdict.get("attempt_id") != attempt_id
            or verdict.get("tick_ordinal") != tick_ordinal
            or outcome not in ("continue", "terminal", "blocked")
        )
        if checks_failed or not candidate.exists():
            self._discard(candidate)
            self.charge_failure(fresh, "monitor-child:verdict_mismatch")
            return "retry"
        snapshot = self.launch_block
        base_digest = self.launch_base_digest
        if snapshot is None or base_digest is None:
            self._discard(candidate)
            raise RunnerExit(4, "suspect_state", "launch snapshot missing — runner defect")
        # F1: canonical control must still equal the runner-memory snapshot
        # (a child that edited canonical state or its control block is a
        # protocol violation, whatever it wrote).
        self._require_unmutated_canonical(fresh, candidate)
        candidate_extract = self.schema.extract(candidate)
        candidate_digest = self.schema.digest(candidate)
        counters_before = fresh.get("counters") or {}
        counters_after = candidate_extract.get("counters") or {}
        deltas = tuple(
            counters_after.get(name, 0) - counters_before.get(name, 0)
            for name in ("monitor_iterations", "monitor_poll_ticks")
        )
        # F2: the verdict's outcome must agree with the candidate's own
        # monitor lifecycle — a "terminal" claim over a still-in-progress
        # monitor is exactly the false-completion the audit exists to stop.
        monitor_status = candidate_extract.get("monitor_status")
        handoff_statuses = candidate_extract.get("handoff_statuses") or []
        outcome_consistent = (
            (outcome == "continue" and monitor_status == "in_progress")
            or (
                outcome == "terminal"
                and monitor_status in ("complete", "paused")
                # R2-2 as amended by R2 #1328 finding 3767068772: a terminal
                # claim must carry a terminal-consistent ledger — no handoff
                # may be MID-FLIGHT (pending). Terminal `failed` aggregates
                # are legitimate: the QA contract records a failed operation
                # as a non-blocking warning and the exit still pauses or
                # completes. The SESSION still owns the exit flow (terminal
                # audit, exit actions) on receipt; this is the schema-visible
                # floor.
                and all(
                    status in ("idle", "complete", "failed")
                    for status in handoff_statuses
                )
            )
            or (
                outcome == "blocked"
                and monitor_status == "blocked"
                # A blocked claim needs recorded blocker evidence, not a
                # bare status flip.
                and candidate_extract.get("blocked_evidence_present") is True
            )
        )
        valid = (
            candidate_extract.get("state") == "valid"
            and not (candidate_extract.get("tainted") or [])
            and candidate_digest is not None
            and candidate_digest == verdict.get("post_workflow_digest")
            and deltas in ((1, 0), (0, 1))
            and candidate_extract.get("monitor_cli") == snapshot
            and outcome_consistent
        )
        if not valid:
            self._discard(candidate)
            self.charge_failure(fresh, "monitor-child:transition_rejected")
            return "retry"
        # R2-6: the success marker must be part of the SAME single finalize
        # write (appending after the commit would never persist it, and a
        # separate post-commit write would reopen the acknowledgement gap).
        self.failures.append(
            {"signature": "monitor-child:success", "at": _utcnow_iso()}
        )
        block = self.current_block(fresh)
        session_id = protocol.get("session_id")
        if self.child_session_id is None and isinstance(session_id, str):
            self.child_session_id = session_id
        block["child_session_id"] = self.child_session_id
        block["owner_model"] = self.owner_model
        block["last_completed_attempt_id"] = attempt_id
        block["in_flight"] = None
        finalized = splice_monitor_cli(
            candidate.read_text(encoding="utf-8"), block
        )
        scratch = candidate.with_suffix(candidate.suffix + ".check")
        verdict_check = self.schema.validate_text_via_file(finalized, scratch)
        if verdict_check.get("state") != "valid":
            self._discard(candidate)
            self.failures.pop()  # the un-committed success marker
            self.charge_failure(fresh, "monitor-child:finalize_invalid")
            return "retry"
        # R2 #1328 finding 3767068783 (commit-time identity recheck): the
        # snapshot lives outside the worktree, but verifying it is cheap and
        # closes the remaining tamper window before the verdict it produced
        # is acted on.
        current_cli_sha = hashlib.sha256(
            self.schema_snapshot_path.read_bytes()
        ).hexdigest()
        if current_cli_sha != self.schema_snapshot_sha256:
            self._discard(candidate)
            self.failures.pop()
            raise RunnerExit(
                4,
                "suspect_state",
                "schema-CLI snapshot digest changed since runner init —"
                " validation authority is no longer trustworthy; stop and"
                " reconcile per the Resume trust model",
            )
        # R2 #1328 finding 3767068789: the canonical check at candidate
        # verification time leaves the extraction/digest/finalize-validation
        # window unguarded — a writer that ignores the kernel lock could land
        # between it and this replace and be silently erased. Re-read
        # canonical NOW, immediately before the atomic replacement; any drift
        # is an unknown writer and stops the runner as suspect state, never a
        # clobber (identical semantics to the post-child check above).
        last_look = self.schema.extract(self.state_path)
        self._require_unmutated_canonical(last_look, candidate)
        atomic_write(candidate, finalized)
        os.replace(candidate, self.state_path)
        self.ticks_completed += 1
        # F8: a successful commit resets the failure streak (the persisted
        # success marker above is the cross-slice reconstruction boundary).
        self.consecutive_signature = None
        self.consecutive_count = 0
        self.launch_block = None
        self.launch_base_digest = None
        _heartbeat(
            f"tick {tick_ordinal} committed (outcome={outcome},"
            f" session={self.child_session_id})"
        )
        return str(outcome)

    # -- waits -----------------------------------------------------------
    def wait_between_ticks(self, ladder_rung: int = 0) -> bool:
        base = WAIT_CHUNK_SECONDS
        if ladder_rung:
            index = min(ladder_rung - 1, len(LIVENESS_BACKOFF_LADDER_SECONDS) - 1)
            base = LIVENESS_BACKOFF_LADDER_SECONDS[index]
        base *= self.wait_scale
        budget = self.remaining() - MONITOR_SLICE_CLEANUP_MARGIN_SECONDS
        if budget <= MONITOR_CHILD_MIN_VIABLE_SECONDS:
            return False
        wait = min(base, budget - MONITOR_CHILD_MIN_VIABLE_SECONDS)
        end = time.monotonic() + max(0, wait)
        while time.monotonic() < end:
            chunk = min(WAIT_CHUNK_SECONDS, end - time.monotonic())
            time.sleep(max(0.05, chunk))
            _heartbeat(f"waiting (remaining slice {int(self.remaining())}s)")
        return True

    # -- main loop -------------------------------------------------------
    def _check_cli_floor(self) -> None:
        try:
            completed = subprocess.run(
                [self.claude_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RunnerExit(
                5,
                "blocked",
                f"claude CLI version probe failed ({error.__class__.__name__})"
                f" for {self.claude_bin!r} — install or fix the binary before"
                " owner-pinned monitoring",
            )
        if completed.returncode != 0:
            raise RunnerExit(
                5,
                "blocked",
                f"claude CLI at {self.claude_bin!r} is missing or failing —"
                " install it before owner-pinned monitoring",
            )
        version = completed.stdout.strip().split()[0] if completed.stdout.strip() else ""
        if not _version_at_least(version, tuple(MIN_CLAUDE_VERSION) + (0,) * (3 - len(MIN_CLAUDE_VERSION))):
            raise RunnerExit(
                5,
                "blocked",
                f"claude CLI {version or 'unknown'} is below the policy floor"
                f" {'.'.join(str(part) for part in MIN_CLAUDE_VERSION)}",
            )

    def run(self) -> dict[str, Any]:
        self.acquire_lock()
        # R3-3 → R4-2: recovery ordering vs kill authority. A suspect state
        # must not strand a live child, but PID/PGID/fingerprint prove only
        # WHICH process a record names, never that THIS workflow created the
        # record — and there is no local registry a write-capable child
        # could not also forge. So untrusted (suspect) state NEVER
        # authorizes a signal: it blocks loudly, naming the recorded child
        # so the human resolves both. Recovery NEVER signals
        # (no kill authority): extinct-or-block, from a schema-VALID
        # record only.
        extract = self.schema.extract(self.state_path)
        prior = extract.get("monitor_cli")
        prior_in_flight = (
            prior.get("in_flight") if isinstance(prior, dict) else None
        )
        if extract.get("state") != "valid":
            orphan_note = ""
            if isinstance(prior_in_flight, dict):
                orphan_note = (
                    " NOTE: the state records an in-flight child"
                    f" (pid {prior_in_flight.get('child_pid')!r}) that was NOT"
                    " signaled — verify and resolve it manually"
                )
            raise RunnerExit(
                4,
                "suspect_state",
                "; ".join(extract.get("errors") or ["suspect"]) + orphan_note,
            )
        if isinstance(prior_in_flight, dict):
            self._reconcile_recorded_orphan(prior_in_flight)
        if isinstance(prior, dict):
            recorded_failures = prior.get("child_failures")
            if isinstance(recorded_failures, list):
                self.failures = [
                    dict(record) for record in recorded_failures if isinstance(record, dict)
                ]
                # R2-6: reconstruct the consecutive streak from the persisted
                # tail — success markers are the recorded reset boundaries.
                for record in reversed(self.failures):
                    signature = record.get("signature")
                    if signature == "monitor-child:success":
                        break
                    if self.consecutive_signature is None:
                        self.consecutive_signature = signature
                        self.consecutive_count = 1
                    elif signature == self.consecutive_signature:
                        self.consecutive_count += 1
                    else:
                        break
            sid = prior.get("child_session_id")
            if isinstance(sid, str) and sid:
                self.child_session_id = sid
        # Ledger half of recovery (state-writing) — only on a valid state,
        # after the no-signal extinction check above proved extinction.
        if isinstance(prior_in_flight, dict):
            self.recover_in_flight(extract)
            extract = self.schema.extract(self.state_path)
        self._check_cli_floor()
        runtime = extract.get("model_runtime")
        binding = monitor_orchestrator_binding(runtime)
        if binding.get("state") != "bound":
            raise RunnerExit(
                4, "suspect_state", "; ".join(binding.get("errors") or ["unbound"])
            )
        self.owner_model = binding["model"]
        if isinstance(prior, dict):
            recorded_owner = prior.get("owner_model")
            if isinstance(recorded_owner, str) and recorded_owner != self.owner_model:
                raise RunnerExit(
                    5,
                    "blocked",
                    f"recorded owner {recorded_owner!r} no longer matches the"
                    f" recomputed binding {self.owner_model!r} — re-run the"
                    " model gate",
                )
        # F9: this is the PHASE 6 runner — a write-capable owner child must
        # never launch against a workflow that is not actually monitoring.
        if extract.get("current_phase") != "monitor" or extract.get(
            "monitor_status"
        ) != "in_progress":
            raise RunnerExit(
                5,
                "blocked",
                "workflow is not at an in-progress monitor phase — the"
                " owner-pinned runner only executes Phase 6",
            )
        retries = 0
        # Bounded like every loop in this package (scanner rule + doctrine):
        # the slice deadline is the real bound and always fires first; the
        # iteration cap is an unreachable fail-closed backstop, never a
        # behavior change.
        for _tick_round in range(100_000):
            if self.max_ticks is not None and self.tick_attempts >= self.max_ticks:
                return {
                    "runner_outcome": "slice_exhausted",
                    "ticks_completed": self.ticks_completed,
                    "child_session_id": self.child_session_id,
                }
            if self.remaining() <= (
                MONITOR_CHILD_MIN_VIABLE_SECONDS + MONITOR_SLICE_CLEANUP_MARGIN_SECONDS
            ):
                return {
                    "runner_outcome": "slice_exhausted",
                    "ticks_completed": self.ticks_completed,
                    "child_session_id": self.child_session_id,
                }
            extract = self.schema.extract(self.state_path)
            if extract.get("state") != "valid":
                raise RunnerExit(
                    4, "suspect_state", "; ".join(extract.get("errors") or ["suspect"])
                )
            result = self.run_tick(extract)
            if result == "terminal" or result == "blocked":
                return {
                    "runner_outcome": result,
                    "ticks_completed": self.ticks_completed,
                    "child_session_id": self.child_session_id,
                }
            if result == "retry_now":
                continue
            if result == "retry":
                retries += 1
                if not self.wait_between_ticks(ladder_rung=retries):
                    return {
                        "runner_outcome": "slice_exhausted",
                        "ticks_completed": self.ticks_completed,
                        "child_session_id": self.child_session_id,
                    }
                continue
            retries = 0
            if not self.wait_between_ticks():
                return {
                    "runner_outcome": "slice_exhausted",
                    "ticks_completed": self.ticks_completed,
                    "child_session_id": self.child_session_id,
                }
        raise RunnerExit(
            5,
            "blocked",
            "tick-round backstop exhausted without a terminal outcome —"
            " impossible under the slice deadline; needs a human",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_file")
    parser.add_argument("--slice-budget", type=float, default=MONITOR_SLICE_BUDGET_SECONDS)
    parser.add_argument("--skill-dir", default=str(SCRIPTS_DIR.parent))
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--wait-scale", type=float, default=1.0)
    parser.add_argument("--max-ticks", type=int, default=None)
    parser.add_argument("--schema-cli", default=str(SCRIPTS_DIR / "state_schema.py"))
    args = parser.parse_args()
    runner = Runner(args)
    try:
        summary = runner.run()
    except RunnerExit as exit_info:
        _emit(
            {
                "runner_outcome": exit_info.outcome,
                "reason": exit_info.reason,
                "ticks_completed": runner.ticks_completed,
                "child_session_id": runner.child_session_id,
            }
        )
        return exit_info.code
    _emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
