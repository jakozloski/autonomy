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
import re
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
    CLAUDE_READ_ONLY_ENV_UNSET,
    MIN_CLAUDE_VERSION,
    MONITOR_CHILD_FAILURE_LIMIT,
    MONITOR_CHILD_IDLE_TIMEOUT_SECONDS,
    MONITOR_CHILD_MIN_VIABLE_SECONDS,
    MONITOR_SLICE_BUDGET_SECONDS,
    MONITOR_SLICE_CLEANUP_MARGIN_SECONDS,
    PER_ATTEMPT_CEILING_SECONDS,
    LIVENESS_BACKOFF_LADDER_SECONDS,
    _has_auth_signature,
    auth_signature_offset,
    _version_at_least,
    monitor_child_arguments,
    monitor_child_prompt,
    monitor_orchestrator_binding,
)

WRAPPER_EXEC_FAILED_MARKER = "MONITOR-WRAPPER-EXEC-FAILED"
_RESUME_NOT_FOUND_HINTS = ("no conversation found", "session not found", "unknown session")
DIAGNOSTIC_LINE_CAP = 50
PIPE_BUFFER_CAP = 1_048_576
# R7 codex #11: the child writes the candidate; a bounded read refuses an
# adversarial multi-GB write before it can OOM the runner. Generous vs any
# real workflow-state file (hundreds of KB), a hard ceiling vs a hostile one.
MAX_CANDIDATE_BYTES = 8 * 1_048_576

WAIT_CHUNK_SECONDS = 60


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def taint_digest(tainted: list[Any]) -> str:
    """Deterministic digest over a taint finding set (paths+digests only).

    The user-confirmed override (``--acknowledge-taint``) is keyed on this
    value, so an acknowledgment covers exactly the flagged set it was issued
    for — any new or changed finding produces a new digest and re-blocks.
    """

    rows = sorted(
        f"{record.get('path')}:{record.get('digest')}"
        for record in tainted
        if isinstance(record, dict)
    )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _heartbeat(message: str) -> None:
    print(f"monitor-runner: {message}", flush=True)


class RunnerExit(Exception):
    """A structured, nonzero runner exit — ``code`` becomes the process exit
    status the supervising parent classifies the slice by.

    Runner exit-code contract (opus pass-3 #11). The parent keys its loop on
    the emitted ``runner_outcome``; the process code mirrors it for the
    runner's own hard stops and is 0 for any slice that emitted a summary:

      0  normal summary from run(): ``slice_exhausted`` (budget/ticks spent,
         work remaining — invoke the runner again), ``terminal`` (child
         reported a terminal verdict — run the terminal exit flow), or a
         child-reported ``blocked`` verdict (run the blocked exit flow).
      3  ``lock_held`` — another runner holds the kernel lock; exit at once,
         never monitor inline as a fallback.
      4  ``suspect_state`` — fail-closed stop on untrusted or drifted input
         (missing frontmatter fence, canonical digest unavailable, launch
         snapshot missing, served-model mismatch, unbound/mismatched owner
         binding); OR ``internal_failure`` — main()'s last-resort handler for
         an unhandled exception, emitted as a structured summary (never a raw
         traceback) so the supervisor can act. Both share code 4: "do not
         blindly re-invoke on the same input."
      5  ``blocked`` — the runner's OWN deterministic block: three
         same-signature child failures, or owner/ownership drift detected
         under the held lock. Distinct from a code-0 child-reported
         ``blocked`` verdict; both drive the same parent blocked exit flow.

    Codes 3/4/5 are always raised through this exception; 0 is a normal
    return. There is no code 1/2 — an unclassifiable crash is converted to a
    structured code-4 ``internal_failure`` rather than escaping raw.
    """

    def __init__(self, code: int, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.outcome = outcome
        self.reason = reason


class SchemaCli:
    """All state parsing/validation via the trusted CLI — never imported."""

    def __init__(self, schema_source: bytes) -> None:
        # C-F1: the validator's SOURCE is pinned in the RUNNER's heap and
        # fed to the interpreter over stdin on every call, so there is no
        # on-disk copy a same-UID child can swap during a validation window.
        # Pass-5 codex F1: the heap copy is unreachable to a NON-DESCENDANT
        # same-UID process wherever the platform restricts cross-process
        # memory access (macOS task-port defaults; Linux yama ptrace_scope>=1).
        # Where it does not (Linux ptrace_scope=0), a sibling that can
        # ptrace-WRITE this process already owns it outright - it would poke
        # the commit call, not the pinned bytes - so no in-process pin helps
        # there, and this stays strictly no worse than the prior on-disk
        # snapshot while removing that snapshot's swap race. (The boot-time
        # load chain is a SEPARATE, lower-bar residual than ptrace - no
        # cross-process write needed - addressed at the _run boundary below;
        # the ptrace framing here concerns only reaching these pinned bytes.)
        self._source = schema_source

    def _run(self, mode: list[str], target: Path) -> dict[str, Any]:
        # R6-F8: guarded like the package's other subprocess sites - a
        # managed host denying the interpreter (or a wedged filesystem)
        # must surface as a structured suspect verdict, not a raw traceback
        # that kills the runner mid-slice. C-F1: the validator SOURCE is fed
        # over stdin (python3 -) from runner-pinned bytes, so no on-disk copy
        # exists for a same-UID child to swap mid-validation. Pass-5 codex F1:
        # -I -S run the child ISOLATED - no cwd/'' on sys.path, no PYTHON* env,
        # no site/sitecustomize - else a same-UID sibling could plant a
        # sitecustomize.py (or a shadowing hashlib.py) on an EXTRA import path
        # and forge the verdict without any file-swap race (state_schema imports
        # only stdlib, so -S strips nothing it needs). Pass-6 codex F1 /
        # pass-7 codex+opus (residual -I -S does NOT close): the flags cannot
        # drop the interpreter's OWN stdlib prefix (it needs its stdlib) nor
        # protect a replaced sys.executable, so a same-UID-WRITABLE
        # interpreter/stdlib (e.g. a user pyenv prefix) forges this verdict,
        # which the candidate `valid` gate consumes. Pass-8 codex+opus: that
        # SAME class extends to the runner's OWN per-slice startup, and
        # interpreter+stdlib immutability alone is NOT sufficient. The runner is
        # re-exec'd per slice (main -> sys.exit) and its boot loads two DISTINCT
        # UID-writable surfaces before the acceptance gate runs:
        #   (a) this package's source - monitor_runner.py is EXECUTED, directly
        #       imports model_policy, which transitively imports state_schema;
        #       state_schema.py is ADDITIONALLY re-read from disk as the pinned
        #       validator bytes (the schema_source read), and the launch child
        #       monitor_child_wrapper.py is spawned;
        #   (b) the runner's own UNISOLATED interpreter startup - both children
        #       it spawns boot isolated (validator at [sys.executable, -I, -S,
        #       ...] below; owner-pinned wrapper via launch_child, -I -S too),
        #       but the runner ITSELF is launched as plain
        #       `python3 monitor_runner.py` (see monitor-exit-handoffs.md): its
        #       path init consumes PYTHONPATH, then site.py adds the
        #       interpreter-prefix AND user site-packages dirs, their .pth files,
        #       and a sitecustomize / usercustomize import - all in effect BEFORE
        #       this file's first line.
        # A same-UID slice-N writer of ANY (a)-file OR any (b)-surface (e.g.
        # ~/.local/.../usercustomize.py) plants code the slice-N+1 boot loads and
        # owns the gate outright. An in-process guard (primed validator, integrity
        # pin) closes only the WITHIN-slice window and is re-loaded from the same
        # mutable surfaces next boot. Surface (b) alone IS closable in-language,
        # but ONLY by isolating the runner's boot the way its children already
        # are - -I -S: -S turns site.py off, -I additionally drops PYTHONPATH +
        # user site, and neither flag alone suffices. Clearing PYTHONPATH +
        # PYTHONNOUSERSITE is merely PARTIAL - it closes the env and user-site
        # sub-surfaces but NOT the interpreter-prefix site-packages .pth /
        # sitecustomize (only -S does), and under the common UID-writable
        # user-pyenv prefix that prefix site-packages is itself same-UID-writable.
        # Surface (a), the package files imported by path regardless of
        # isolation flags, is NOT, and closes only ENVIRONMENTALLY: the whole
        # boot chain (interpreter + stdlib + this package's source, plus the
        # runner's startup surface unless isolated away) immutable to whatever
        # UID the monitored work can write as - a read-only rootfs/mount that
        # ALSO covers $HOME/user-site, or a privilege-separated child/launcher
        # whose UID cannot write the chain. A read-only package/stdlib mount
        # that leaves $HOME writable is INSUFFICIENT - surface (b) survives it.
        # (A boot-time refusal on a UID-writable boot chain fails CLOSED - loud,
        # not silent forgery - but still does not CLOSE the window; left a
        # documented deployment requirement, not code, as the common user-pyenv
        # + working-copy setup is UID-writable by design.)
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-", *mode, str(target)],
                input=self._source,
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "state": "suspect",
                "errors": [
                    f"schema CLI invocation failed ({error.__class__.__name__})"
                ],
            }
        try:
            return json.loads(completed.stdout)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return {"state": "suspect", "errors": ["schema CLI produced no JSON"]}

    def extract(self, target: Path) -> dict[str, Any]:
        return self._run(["--monitor-extract"], target)

    # (the digest-only CLI mode is still used by children; the runner itself
    # now reads digests from extracts, so no digest() wrapper remains)

    def extract_text_via_file(self, text: str, scratch: Path) -> dict[str, Any]:
        """R6-F6: extract from an in-memory SNAPSHOT, not the live candidate.

        Finalization is single-read — the runner reads the candidate bytes
        once, then validates, digests, and commits exactly that snapshot
        through runner-owned scratch files, so nothing can mutate the
        candidate between validation and commit.

        R7 codex #4: the scratch name is derived from the candidate path the
        write-capable child was handed, so the child can pre-plant a
        symlink/hardlink there; `_write_exclusive` (O_EXCL, no-follow) refuses
        any pre-existing path so the runner never writes THROUGH a planted
        link with its own authority — a collision lands a `suspect` verdict
        (rejected transition), never a write-through.
        """

        try:
            _write_exclusive(scratch, text)
        except OSError as error:
            return {
                "state": "suspect",
                "errors": [_scratch_refusal(scratch, error)],
            }
        try:
            return self._run(["--monitor-extract"], scratch)
        finally:
            try:
                scratch.unlink()
            except OSError:
                pass

    def validate_text_via_file(self, text: str, scratch: Path) -> dict[str, Any]:
        try:
            _write_exclusive(scratch, text)
        except OSError as error:
            return {
                "state": "suspect",
                "errors": [_scratch_refusal(scratch, error)],
            }
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


def _scratch_refusal(path: Path, error: OSError) -> str:
    """Operator-actionable refusal for a scratch write that failed closed.

    Pass-3 opus #7: this message is the operator's only handle on the state
    that triggered the guard, so it names the path and the sanctioned action
    for BOTH triggering states — a stale leftover and a planted link — not
    just the states the guard permits.
    """

    return (
        f"scratch open refused ({type(error).__name__}) at {path} — a"
        " pre-existing file at this runner-owned name is either a stale"
        " leftover from a killed runner or a child-planted link; verify no"
        " other runner instance is live (the .lock file names one), then"
        " remove that path and re-run. The candidate itself was NOT read or"
        " modified."
    )


def _write_exclusive(path: Path, text: str) -> None:
    """Create ``path`` exclusively (O_EXCL, no-follow) and write ``text``.

    R7 codex #4: the write-capable child knows the candidate path and can
    pre-create symlinks/hardlinks at predictable runner-owned scratch/tmp
    names; a plain ``open(..., "w")`` would then write THROUGH the link and
    let the runner corrupt canonical (or arbitrary) files with its own
    authority. O_EXCL refuses any pre-existing path — symlink included —
    so a planted link becomes a structured failure, never a write-through.
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_write(path: Path, text: str) -> None:
    # Unpredictable tmp name + exclusive creation (R7 codex #4): a child
    # cannot pre-plant a link where it cannot predict the path, and O_EXCL
    # refuses one even on a collision.
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    try:
        _write_exclusive(tmp, text)
    except FileExistsError:
        # Pass-3 opus #8: a pre-existing path at this name was NOT created by
        # this call (O_EXCL refused before writing), so cleanup must not
        # delete it — scope the unlink to failures of a write THIS call began.
        raise
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    # R6-F14: fsync the parent directory so the rename itself survives a
    # power loss — the data fsync above does not make the directory entry
    # durable. Best-effort where the platform disallows directory opens.
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def process_fingerprint(pid: int) -> str | None:
    # R6-F8: a denied/hung ps is a fingerprint failure, not a runner crash —
    # None already routes the launch into the structured spawn-failure path.
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
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


STICKY_EXCERPT_BYTES = 400


def _signature_excerpt(line: str) -> str:
    """Bounded stderr excerpt that PRESERVES the deterministic marker.

    R7 codex #12: the sticky list exists to outlive the rolling stderr cap so
    a deterministic auth/exec signature still reaches classify_child_failure.
    A fixed HEAD slice (``decoded[:400]``) defeated that whenever the marker
    sat past the cutoff — detection fired on the FULL line, but the retained
    text dropped the marker, so the downstream re-scan silently downgraded a
    deterministic BLOCK to a generic retry charge. Anchor a bounded window on
    the marker instead, so the re-scan re-derives the same verdict.
    """

    if len(line) <= STICKY_EXCERPT_BYTES:
        return line
    idx = line.find(WRAPPER_EXEC_FAILED_MARKER)
    if idx < 0:
        offset = auth_signature_offset(line)
        idx = offset if offset is not None else 0
    half = STICKY_EXCERPT_BYTES // 2
    start = max(0, idx - half)
    return line[start : start + STICKY_EXCERPT_BYTES]


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
    stderr_sticky: list[str] = []
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
            # R7 (opus L3): deterministic signatures must survive the
            # rolling cap — a chatty child could otherwise evict its own
            # auth error and downgrade the deterministic block to a
            # generic three-strike charge.
            if len(stderr_sticky) < 5 and (
                WRAPPER_EXEC_FAILED_MARKER in decoded
                or _has_auth_signature(decoded)
            ):
                # R7 codex #12: retain a marker-ANCHORED window, not a fixed
                # head — a signature past char 400 must survive into the tail
                # classify_child_failure re-scans, or its deterministic block
                # decays to a generic retry charge.
                stderr_sticky.append(_signature_excerpt(decoded))

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
                # Pass-3 codex #8: a newline-free record can overflow the cap
                # before _consume ever sees a complete line, so a marker in
                # the about-to-be-discarded head would silently decay a
                # deterministic auth/exec block into a generic retry charge.
                # Detect sticky signatures on the full buffer BEFORE
                # truncating (stderr only; bounded at cap + one read).
                if pipe is not proc.stdout and len(stderr_sticky) < 5:
                    overflow = buffers[pipe].decode("utf-8", "replace")
                    if WRAPPER_EXEC_FAILED_MARKER in overflow or _has_auth_signature(
                        overflow
                    ):
                        stderr_sticky.append(_signature_excerpt(overflow))
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
        "stderr_tail": (
            [line for line in stderr_sticky if line not in stderr_tail]
            + stderr_tail
        ),
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
        # R6-F7: normalize a path-shaped binary BEFORE any cwd change.
        # launch_child runs the child with cwd=<repo root, or the state dir
        # when no repository exists>, where a relative path would re-resolve
        # (or stop resolving) after the version probe already passed from
        # the runner's own cwd. Bare command names keep PATH resolution,
        # which is cwd-independent.
        self.claude_bin = (
            str(Path(args.claude_bin).resolve())
            if os.sep in args.claude_bin
            else args.claude_bin
        )
        # R2 #1328 finding 3767068783 / pass-4 codex C-F1: the schema CLI
        # must not execute from the writable skill package after a
        # write-capable child has run - a compromised same-UID child could
        # rewrite the validator (or swap a private on-disk snapshot during
        # the validation window) and forge a valid verdict. Pin the
        # validator's SOURCE bytes in runner memory BEFORE any child launch
        # and feed them to the interpreter over stdin (python3 -) on every
        # call: no on-disk validator exists for a child to reach, and the
        # bytes the runner trusts live in its own heap. Together with the
        # wrapper pin below this is the failable I/O in __init__ (opus F2
        # keeps construction inside main()'s try); it SUPERSEDES the opus F3
        # snapshot-dir cleanup - no snapshot dir is ever created, so no
        # commit-time digest recheck is needed.
        source_cli = Path(args.schema_cli).resolve()
        self.schema_source = source_cli.read_bytes()
        self.schema = SchemaCli(self.schema_source)
        # algo#1216 R2 finding 3779532260, composed with the same in-memory
        # trust base: the exec-barrier wrapper is trust-bearing code too — a
        # prior tick's child could rewrite the worktree wrapper and thereby
        # own the next launch as the runner's UID, before in_flight is
        # durably recorded. The wrapper cannot travel over stdin the way the
        # validator does (stdin already carries the GO launch token), so pin
        # its SOURCE bytes here, before any child has run, and launch from
        # ONE 0600 unpredictable-name stage file created now and REWRITTEN from
        # these bytes immediately before every launch — children are strictly
        # sequential and the drained-outcome group-extinction check reaps
        # survivors, so no writer exists between that rewrite and the
        # interpreter's read. main() unlinks it in a finally; a hard-killed
        # runner can leak one small file in TMPDIR.
        source_wrapper = SCRIPTS_DIR / "monitor_child_wrapper.py"
        self.wrapper_source = source_wrapper.read_bytes()
        wrapper_fd, wrapper_stage_name = tempfile.mkstemp(
            prefix="monitor-wrapper-", suffix=".py"
        )
        os.close(wrapper_fd)
        self.wrapper_stage_path = Path(wrapper_stage_name)
        # algo#1216 R2 finding 3779532263: canonical state lives under
        # <repo>/.claude, and a child launched THERE gets default file access
        # only below it — Phase 6 could not touch application files. Launch
        # at the repository root when one exists (state/skill/candidate
        # paths in the prompt are absolute, so nothing else moves). The probe
        # keeps the structured-exit doctrine: a host without git falls back
        # to the state directory instead of crashing __init__.
        try:
            root_probe = subprocess.run(
                ["git", "-C", str(self.state_path.parent), "rev-parse",
                 "--show-toplevel"],
                capture_output=True, text=True,
            )
            root = root_probe.stdout.strip()
            probe_ok = root_probe.returncode == 0 and bool(root)
        except OSError:
            probe_ok = False
        self.child_cwd = root if probe_ok else str(self.state_path.parent)
        self.slice_deadline = time.monotonic() + args.slice_budget
        # Testability seam (same class as --claude-bin): scales ladder and
        # poll waits so hermetic failure-path tests finish in seconds. The
        # default 1.0 is the production contract; the runner clamps upward
        # of 0 and never above 1.
        self.wait_scale = min(1.0, max(0.001, args.wait_scale))
        # Testability seam: bounds tick ATTEMPTS in one slice so multi-slice
        # protocol tests are deterministic. None (production) = unbounded.
        self.max_ticks = getattr(args, "max_ticks", None)
        # Testability seam (same class): the child idle bound, so hermetic
        # timeout-path tests finish in seconds. The default is the
        # production contract constant.
        self.child_idle_timeout = getattr(
            args, "child_idle_timeout", MONITOR_CHILD_IDLE_TIMEOUT_SECONDS
        )
        # Testability seam (same class): the candidate-read ceiling, so a test
        # can drive the oversized-candidate rejection without writing an 8 MiB
        # file. The default is the production contract constant.
        self.max_candidate_bytes = getattr(
            args, "max_candidate_bytes", MAX_CANDIDATE_BYTES
        )
        self.tick_attempts = 0
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".monitor.lock")
        self._lock_handle: IO[bytes] | None = None
        self.ticks_completed = 0
        self.child_session_id: str | None = None
        self.owner_model: str | None = None
        self.owner_effort: str | None = None
        # F1: verification evidence lives in RUNNER MEMORY, never re-derived
        # from post-child state (an untrusted child could rewrite canonical
        # evidence). Loaded once at start; appended only by the runner.
        self.failures: list[dict[str, Any]] = []
        self.consecutive_signature: str | None = None
        self.consecutive_count = 0
        self.launch_block: dict[str, Any] | None = None
        self.launch_base_digest: str | None = None
        self.acknowledged_taint = frozenset(args.acknowledge_taint or ())

    # -- lock ------------------------------------------------------------
    def acquire_lock(self) -> None:
        # algo#1216 R2 finding 3787189736: the lock path sits inside the
        # child-writable repository — a child that unlinks and recreates it
        # lets a second runner flock the NEW inode while the first still
        # holds the old one. Two defenses: (1) acquisition verifies the
        # locked fd and the path resolve to the same inode (retrying a
        # bounded number of times across unlink races); (2) every canonical
        # commit re-verifies the held inode (_verify_lock_inode) and stops
        # as suspect on a swap — the holder detects the sabotage instead of
        # racing the usurper.
        for _ in range(5):
            # 3787662322: a FIFO (or other special file) planted at the lock
            # path makes a plain open() block forever, outside every runner
            # deadline. Open non-blocking and no-follow, then require a
            # regular file before trusting the descriptor.
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_WRONLY
                    | os.O_APPEND
                    | os.O_CREAT
                    | os.O_NONBLOCK
                    | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError:
                raise RunnerExit(
                    4,
                    "suspect_state",
                    "monitor lock path cannot be opened as a regular file"
                    " (symlink or special file planted?) — reconcile per the"
                    " Resume trust model",
                )
            import stat as _stat
            if not _stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise RunnerExit(
                    4,
                    "suspect_state",
                    "monitor lock path is not a regular file — an unknown"
                    " writer planted a special file; reconcile per the"
                    " Resume trust model",
                )
            handle = os.fdopen(fd, "ab")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise RunnerExit(3, "lock_held", "another monitor runner is active")
            try:
                path_stat = os.stat(self.lock_path)
            except FileNotFoundError:
                handle.close()
                continue
            fd_stat = os.fstat(handle.fileno())
            if (path_stat.st_ino, path_stat.st_dev) == (
                fd_stat.st_ino,
                fd_stat.st_dev,
            ):
                self._lock_handle = handle
                return
            handle.close()
        raise RunnerExit(
            4,
            "suspect_state",
            "monitor lock path kept changing inode during acquisition —"
            " an unknown writer is replacing the lock file; reconcile per"
            " the Resume trust model",
        )

    def _verify_lock_inode(self) -> None:
        handle = getattr(self, "_lock_handle", None)
        if handle is None:
            return
        try:
            path_stat = os.stat(self.lock_path)
            fd_stat = os.fstat(handle.fileno())
        except OSError:
            path_stat = None
            fd_stat = None
        if (
            path_stat is None
            or fd_stat is None
            or (path_stat.st_ino, path_stat.st_dev)
            != (fd_stat.st_ino, fd_stat.st_dev)
        ):
            raise RunnerExit(
                4,
                "suspect_state",
                "monitor lock file was replaced while held — the exclusion"
                " protocol is compromised (a second runner may be active);"
                " stop and reconcile per the Resume trust model",
            )

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
        self._verify_lock_inode()
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
        # admin#1495 finding 3790049904 / algo#1216 finding 3788363456: once
        # canonical in_flight is cleared, the launch snapshot is history — a
        # later pre-launch charge comparing canonical against the stale
        # snapshot raised a FALSE suspect_state ("unknown writer") instead of
        # following the bounded retry policy.
        self.launch_block = None
        self.launch_base_digest = None
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
        # algo#1216 R2 finding 3787189741: the dead child's candidate is the
        # only durable write-ahead record of external mutations it may have
        # fired — recovery must PRESERVE the exact in_flight attempt's
        # candidate (validated as untrusted input by shape) and delete only
        # true strays; resume then reconciles its pending intents per the
        # preserved-candidate contract before any fresh mutation.
        recorded_attempt = in_flight.get("attempt_id")
        preserved_any = False
        for stray in self.state_path.parent.glob(self.state_path.name + ".attempt-*"):
            is_recorded = (
                isinstance(recorded_attempt, str)
                and re.fullmatch(r"[0-9a-f]{32}", recorded_attempt) is not None
                and stray.name
                == self.state_path.name + f".attempt-{recorded_attempt}.md"
            )
            if is_recorded:
                self._preserve_failed(stray)
                preserved_any = True
                continue
            try:
                stray.unlink()
            except OSError:
                pass
        _heartbeat(
            "recovery: unknown prior attempt reconciled ("
            + ("candidate preserved for reconciliation" if preserved_any else "no recorded candidate found")
            + ")"
        )
        self.charge_failure(extract, "monitor-child:unknown_outcome")

    # -- tick ------------------------------------------------------------
    def remaining(self) -> float:
        return self.slice_deadline - time.monotonic()

    def _bind_owner(
        self,
        extract: dict[str, Any],
        prior: Any,
        binding_provider: Any = None,
    ) -> None:
        """Recompute the owner binding and adopt its model AND effort.

        Pass-3 (opus #3 / codex #6): this is the PRODUCER half of the effort
        seam — the only writer of ``owner_model``/``owner_effort`` in a real
        run — split out of the run path so a unit test can drive it with an
        injected binding provider. Deleting the effort adoption here fails
        that test even while every policy effort is "max"; the consumer half
        (``_child_command``) is pinned separately.
        """

        provider = binding_provider or monitor_orchestrator_binding
        runtime = extract.get("model_runtime")
        binding = provider(runtime)
        if binding.get("state") != "bound":
            raise RunnerExit(
                4, "suspect_state", "; ".join(binding.get("errors") or ["unbound"])
            )
        self.owner_model = binding["model"]
        # R7 (opus L2): the binding already computed the per-lineage effort;
        # threading it through keeps one source — a base-owned monitor must
        # not silently inherit the reviewer-leg default.
        bound_effort = binding.get("effort")
        if isinstance(bound_effort, str) and bound_effort:
            self.owner_effort = bound_effort
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
        # R6-F10: cross-check the persisted monitor_ownership record against
        # the recomputed binding. The recompute is AUTHORITATIVE — sessions
        # write the record at monitor entry and the runner never edits it —
        # so a record that contradicts the recomputed owner is owner drift,
        # not something to silently ignore. A continuity record names the
        # nominal owner in pending_owner; an owner-session record names it
        # in model.
        ownership = extract.get("monitor_ownership")
        if isinstance(ownership, dict):
            recorded_target = ownership.get("pending_owner") or ownership.get("model")
            if isinstance(recorded_target, str) and recorded_target != self.owner_model:
                raise RunnerExit(
                    5,
                    "blocked",
                    f"persisted monitor_ownership names {recorded_target!r}"
                    f" but the recomputed binding is {self.owner_model!r} —"
                    " rebind at monitor entry (the recompute is"
                    " authoritative; the runner never writes the record)",
                )

    def _child_command(self, resume_id: str | None) -> list[str]:
        # R7 codex #17: the owner-pinned child argv up to (not including) the
        # prompt, split out of launch_child so the effort threading is unit-
        # testable WITHOUT a spawn. A base-owned monitor MUST carry the
        # binding's per-lineage effort (self.owner_effort); silently falling
        # back to monitor_child_arguments' reviewer-effort default would let a
        # base lineage inherit the wrong tier the instant the two efforts
        # differ. No integration test can catch that drop while both legs are
        # "max", so the pinning tests drive BOTH halves of this seam with a
        # distinct effort: the consumer here, and the producer via
        # ``_bind_owner`` with an injected binding (pass-3 opus #3/codex #6).
        if self.owner_effort:
            tail = monitor_child_arguments(
                self.owner_model, self.owner_effort, resume_id=resume_id
            )
        else:
            tail = monitor_child_arguments(self.owner_model, resume_id=resume_id)
        return [self.claude_bin] + tail

    def launch_child(
        self, prompt: str, resume_id: str | None, ceiling: float
    ) -> dict[str, Any]:
        argv = self._child_command(resume_id)
        argv.append(prompt)
        # The barrier wrapper lives in its own exec-only file (scanner
        # structural rule: exec and subprocess never share a file).
        wrapper = [
            sys.executable,
            # Pass-10 codex: isolate the wrapper's own interpreter boot with
            # the same -I -S the validator child uses. The wrapper imports only
            # os + sys, so -I -S strips nothing it needs while closing its site
            # startup (a same-UID sitecustomize / .pth cannot run in the wrapper
            # before it launches the model). The flags gate only the wrapper's
            # own few lines - they do not survive the process replacement into
            # the model binary, and PATH / child_env are untouched, so the
            # owner-pinned model launches identically.
            "-I",
            "-S",
            str(self.wrapper_stage_path),
            "--",
        ] + argv
        # R7 codex #10: strip the ambient CLAUDE_CODE_* override knobs before
        # the owner-pinned launch. model_policy owns the canonical unset list
        # (single source — the same set the read-only voices clear): a stray
        # CLAUDE_CODE_SUBAGENT_MODEL would silently repoint the base workers
        # this child dispatches, and CLAUDE_CODE_EFFORT_LEVEL /
        # CLAUDE_CODE_PERMISSION_MODE would defeat the pinned --effort and the
        # child's intended posture. The child's write access comes from the
        # workspace .claude settings at its cwd, not from these overrides.
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key not in CLAUDE_READ_ONLY_ENV_UNSET
        }
        try:
            # algo#1216 R2 finding 3779532260: restore the stage file
            # from the __init__-pinned wrapper bytes immediately before the
            # launch — the interpreter reads only bytes this runner just
            # wrote from its own heap.
            self.wrapper_stage_path.write_bytes(self.wrapper_source)
            proc = subprocess.Popen(
                wrapper,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                cwd=self.child_cwd,
                env=child_env,
            )
        except OSError as error:
            # R7 codex #11: a spawn failure (EMFILE/ENOMEM, or a missing
            # interpreter) must surface as a structured suspect verdict — the
            # same fail-closed shape as SchemaCli._run — not a raw traceback
            # that kills the slice. The supervising session re-invokes on its
            # next wake; a persistent cause reads plainly in the reason.
            raise RunnerExit(
                4,
                "suspect_state",
                "could not stage or spawn the monitor-child wrapper"
                f" ({error.__class__.__name__}); the host denied file or"
                " process creation — resolve the resource/interpreter"
                " fault and resume",
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
        # R7 (opus L1): the one subprocess-adjacent syscall here follows the
        # same fail-closed shape as its siblings — a vanished wrapper is a
        # structured spawn failure, never a raw traceback out of run().
        try:
            child_pgid: int | None = os.getpgid(proc.pid)
        except OSError:
            child_pgid = None
        fingerprint = process_fingerprint(proc.pid) if child_pgid is not None else None
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
        # R6-F15: one instant defines both the persisted deadline_at and the
        # enforced monotonic deadline, so the record matches enforcement.
        deadline_monotonic = time.monotonic() + max(0.0, ceiling)
        deadline_wall = datetime.now(timezone.utc) + timedelta(seconds=max(0, ceiling))
        block = self.current_block(extract)
        block["owner_model"] = self.owner_model
        block["in_flight"] = {
            "attempt_id": attempt_id,
            "tick_ordinal": tick_ordinal,
            "started_at": _utcnow_iso(),
            "deadline_at": deadline_wall.isoformat(),
            "child_pid": proc.pid,
            # CR 3760683988: child_pgid is guaranteed non-None here - the
            # fingerprint guard above aborts whenever os.getpgid() raised - and
            # equals proc.pid by construction: start_new_session=True makes the
            # child a session leader, so pgid == pid. Persisting the same local
            # that the kill paths use keeps one spelling and a valid group id
            # for cross-session reaping.
            "child_pgid": child_pgid,
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
        try:
            proc.stdin.write(b"GO\n")
            proc.stdin.flush()
            proc.stdin.close()
        except OSError:
            # R6-F8: a failed GO write is post-in_flight, and the token may
            # have partially reached a wrapper that then execs — so the
            # cleanup preserves unknown-outcome semantics: kill the group
            # this tick spawned, boundedly reap, verify canonical is
            # unmutated, then charge and clear through the normal failure
            # path (never a raw traceback that strands in_flight).
            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            if not _bounded_reap(proc):
                raise RunnerExit(
                    5,
                    "blocked",
                    "monitor child could not be reaped after a failed GO"
                    " write — a possibly-live writer needs a human",
                )
            fresh = self.schema.extract(self.state_path)
            self._require_unmutated_canonical(fresh, candidate)
            self._preserve_failed(candidate)
            self.charge_failure(fresh, "monitor-child:go_write_failed")
            return "retry"
        drained = _drain_child(
            proc,
            idle_timeout=self.child_idle_timeout,
            deadline=deadline_monotonic,
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
        # R6-F6, generalized by R2 #1495 findings 3776596760 + 3777668741:
        # supervision proves only that the LEADER exited — clean OR failed —
        # and the failure path clears the only survivor record (in_flight)
        # when it commits. A same-group descendant is a live writer that can
        # mutate the candidate after validation, so prove the whole process
        # group extinct for EVERY drained outcome before any state is
        # cleared or committed. Survivors are killed (this group was spawned
        # by this runner this tick) and boundedly rechecked; a group that
        # cannot be proven extinct needs a human, and a clean tick that
        # needed the kill is charged and retried.
        if _live_group_members(child_pgid):
            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            recheck_deadline = time.monotonic() + 15
            while (
                time.monotonic() < recheck_deadline
                and _live_group_members(child_pgid)
            ):
                time.sleep(0.3)
            if _live_group_members(child_pgid):
                self._discard(candidate)
                raise RunnerExit(
                    5,
                    "blocked",
                    "same-group descendants of the monitor child survived"
                    " SIGKILL — a possibly-live writer needs a human",
                )
            # R7 (opus L5), widened to every outcome: the survivor may have
            # written canonical in the window between the post-drain extract
            # and the kill. Re-extract and re-prove against the launch
            # snapshot before ANY charge or clear, so no later step trusts a
            # mutated base — drift stops as suspect state (discarding the
            # candidate per the suspect-stop semantics), same as every other
            # path.
            fresh = self.schema.extract(self.state_path)
            self._require_unmutated_canonical(fresh, candidate)
            if drained["outcome"] == "clean" and drained["exit_code"] == 0:
                self._preserve_failed(candidate)
                self.charge_failure(fresh, "monitor-child:group_survivors")
                return "retry"
        if drained["outcome"] != "clean" or drained["exit_code"] != 0:
            self._preserve_failed(candidate)
            # R6-F9: classify the trusted diagnostic stderr BEFORE charging
            # a non-clean outcome — an auth failure followed by a hang must
            # take the deterministic block on the first attempt, not be
            # buried as generic timeout noise (or, with mixed signatures,
            # never reach the block at all). Model stdout is never scanned
            # as free text; only the buffered stderr tail is classified.
            action, detail = classify_child_failure(
                drained["exit_code"] if isinstance(drained["exit_code"], int) else -1,
                drained["stderr_tail"],
                resumed,
            )
            if action == "block":
                self._clear_in_flight(fresh)
                raise RunnerExit(5, "blocked", detail)
            if drained["outcome"] != "clean":
                self.charge_failure(fresh, f"monitor-child:{drained['outcome']}")
                return "retry"
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
            self._preserve_failed(candidate)
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
                self._preserve_failed(candidate)
                self.charge_failure(
                    fresh,
                    "monitor-child:session_mismatch"
                    if session_id
                    else "monitor-child:session_unreported",
                )
                return "retry"
        elif not isinstance(session_id, str) or not session_id:
            self._preserve_failed(candidate)
            self.charge_failure(fresh, "monitor-child:no_session_id")
            return "retry"
        if verdict is None:
            self._preserve_failed(candidate)
            self.charge_failure(fresh, "monitor-child:no_verdict")
            return "retry"
        return self._verify_and_commit(
            fresh, candidate, attempt_id, tick_ordinal, verdict, protocol
        )

    def _gate_taint(self, extract: dict[str, Any]) -> None:
        """R6-F5 + admin-portal#1495 R2 finding 3776596739: fail closed on
        taint-flagged state before ANY child launch.

        The full validator detects instruction-like content but keeps the
        structural verdict valid (advisory); the runner is the boundary where
        that advisory MUST act — it launches a write-capable owner child that
        is instructed to read the raw state file. Only path+digest records
        are ever surfaced, never the flagged text. Heuristic false positives
        are an availability hazard by design, so the recovery path is
        explicit and user-confirmed: the human inspects the named fields and
        re-runs with ``--acknowledge-taint <set-digest>`` — the digest covers
        exactly the current finding set, so new taint re-blocks.
        """

        tainted = extract.get("tainted") or []
        if not tainted:
            return
        digest = taint_digest(tainted)
        if digest in self.acknowledged_taint:
            _heartbeat(f"taint set {digest[:12]} acknowledged by operator — continuing")
            return
        listing = "; ".join(
            f"{record.get('path')} (digest {record.get('digest')})"
            for record in tainted[:10]
            if isinstance(record, dict)
        )
        raise RunnerExit(
            5,
            "blocked",
            "state carries taint-flagged (instruction-like) content at:"
            f" {listing} — a write-capable owner child must not launch on"
            " it. Inspect the named fields in the state file (the flagged"
            " text is never echoed here); if legitimate, re-run with"
            f" --acknowledge-taint {digest} to confirm as the human"
            " operator; otherwise redact the content and resume",
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
        # Same stale-snapshot clearing as charge_failure (findings
        # 3790049904 / 3788363456).
        self.launch_block = None
        self.launch_base_digest = None


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

    def _preserve_failed(self, candidate: Path) -> None:
        """algo#1216 R2 findings 3779532272 + 3787662312: a failed child's
        candidate is the only durable record of external mutations it may
        already have fired. Preservation is ATTEMPT-SCOPED (consecutive
        failures never overwrite each other), and an archival failure leaves
        the source candidate IN PLACE — deleting the evidence because the
        rename failed would be strictly worse than an unarchived candidate.
        Suspect-stop paths keep their documented discard semantics."""
        marker = candidate.name
        prefix = self.state_path.name + ".attempt-"
        attempt = (
            marker[len(prefix):-3]
            if marker.startswith(prefix) and marker.endswith(".md")
            else "unknown"
        )
        preserved = self.state_path.with_suffix(
            f".failed-candidate-{attempt}.md"
        )
        try:
            os.replace(candidate, preserved)
        except OSError:
            pass  # never destroy the write-ahead record

    def cleanup_wrapper_stage(self) -> None:
        """Remove the runner-lifetime wrapper stage file (main()'s finally).

        Best-effort by design: the file is 0600 with an unpredictable name,
        so a leak on a hard kill is bounded and harmless."""
        try:
            self.wrapper_stage_path.unlink()
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
        # R6-F6: single-read finalize — the candidate bytes are read exactly
        # ONCE; validation, digest, and commit all operate on this snapshot,
        # so the committed bytes can never differ from the validated bytes.
        candidate_text: str | None = None
        if not checks_failed:
            # R7 codex #11: the child owns this file. Read it size-BOUNDED
            # (read one char past the ceiling to detect an oversized write
            # without materializing it) and catch UnicodeDecodeError — invalid
            # UTF-8 is a ValueError, NOT an OSError, so the old OSError-only
            # guard let it escape as a raw traceback that killed the slice
            # mid-finalize. Either fault becomes a charged retry, never a crash.
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    candidate_text = handle.read(self.max_candidate_bytes + 1)
            except (OSError, UnicodeDecodeError):
                candidate_text = None
            else:
                if len(candidate_text) > self.max_candidate_bytes:
                    candidate_text = None
        if checks_failed or candidate_text is None:
            self._preserve_failed(candidate)
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
        candidate_extract = self.schema.extract_text_via_file(
            candidate_text, candidate.with_suffix(candidate.suffix + ".snap")
        )
        candidate_digest = candidate_extract.get("digest")
        if not (isinstance(candidate_digest, str) and candidate_digest):
            candidate_digest = None
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
        # algo#1216 R2 finding 3787189752: a candidate must never lose or
        # regress an operation result canonical state already carried — a
        # vanished pending QA op is an external action that may already have
        # fired. Absence is legal ONLY through a generation rollover, i.e.
        # the candidate still plans at least one operation of the same
        # dotted family for that handoff kind.
        terminal_statuses = ("complete", "failed", "skipped_dependency")
        launch_results = fresh.get("handoff_results") or {}
        cand_results = candidate_extract.get("handoff_results") or {}
        cand_ops = candidate_extract.get("handoff_operations") or {}
        handoffs_monotonic = True
        for kind, ops in launch_results.items():
            for op_id, status in ops.items():
                new_status = (cand_results.get(kind) or {}).get(op_id)
                if new_status is None:
                    if status in ("pending", "retryable"):
                        # 3787662315: an in-flight result is a mutation that
                        # may already have fired — it must reach a terminal
                        # status before any removal or generation rollover
                        # (mirrors the planner's fail-closed in-flight guard).
                        handoffs_monotonic = False
                        continue
                    family = op_id.split(":", 1)[0]
                    planned = cand_ops.get(kind) or []
                    if not any(
                        planned_id.split(":", 1)[0] == family
                        for planned_id in planned
                    ):
                        handoffs_monotonic = False
                elif status in terminal_statuses and new_status != status:
                    handoffs_monotonic = False
                elif status in ("pending", "retryable") and new_status not in (
                    "pending",
                    "retryable",
                ) + terminal_statuses:
                    handoffs_monotonic = False
        outcome_consistent = (
            (outcome == "continue" and monitor_status == "in_progress")
            or (
                outcome == "terminal"
                and monitor_status in ("complete", "paused")
                # R2-2, amended by R6-F3 and R2 #1328 finding 3767068772: a
                # terminal claim must carry a terminal-consistent ledger. No
                # handoff may be mid-flight - anything outside the allowed set
                # below (pending / retryable / malformed) rejects - but a
                # durably `failed` aggregate IS schema-terminal: the QA
                # contract records a failed handoff as a non-blocking terminal
                # warning and the exit still pauses or completes. The SESSION
                # still owns the exit flow (terminal audit, exit actions, the
                # failure warning) on receipt; this is the schema-visible floor.
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
        # Candidate taint is NOT rejected here: feedback excerpts land in
        # state by design, and the acknowledge-aware _gate_taint re-gates
        # every subsequent write-capable launch (R6-F5) — commit-time
        # rejection would three-strike legitimate feedback ingestion.
        valid = (
            candidate_extract.get("state") == "valid"
            and candidate_digest is not None
            and candidate_digest == verdict.get("post_workflow_digest")
            and deltas in ((1, 0), (0, 1))
            and candidate_extract.get("monitor_cli") == snapshot
            and outcome_consistent
            and handoffs_monotonic
            # algo#1216 R2 finding 3787189747: a terminal claim with a live
            # direction-aware deploy/backfill hold is exactly the premature
            # merge-readiness the hold exists to prevent.
            and not (
                outcome == "terminal"
                and candidate_extract.get("merge_readiness_hold") is True
            )
        )
        if not valid:
            self._preserve_failed(candidate)
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
        # Still the R6-F6 snapshot: splice from the bytes validated above,
        # never from a re-read of the (unlocked) candidate file.
        finalized = splice_monitor_cli(candidate_text, block)
        scratch = candidate.with_suffix(candidate.suffix + ".check")
        verdict_check = self.schema.validate_text_via_file(finalized, scratch)
        if verdict_check.get("state") != "valid":
            self._preserve_failed(candidate)
            self.failures.pop()  # the un-committed success marker
            self.charge_failure(fresh, "monitor-child:finalize_invalid")
            return "retry"
        # R2 #1328 finding 3767068789: the canonical check at candidate
        # verification time leaves the extraction/digest/finalize-validation
        # window unguarded - a writer that ignores the kernel lock could land
        # between it and this replace and be silently erased. Re-read
        # canonical NOW, immediately before the atomic replacement; any drift
        # is an unknown writer and stops the runner as suspect state, never a
        # clobber (identical semantics to the post-child check above).
        last_look = self.schema.extract(self.state_path)
        self._require_unmutated_canonical(last_look, candidate)
        self._verify_lock_inode()
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
        # R6-F5: the taint gate runs before ANY write-capable child can
        # launch — recovery's ledger write is state-local and safe, but no
        # tick may start on taint-flagged state without the explicit
        # operator acknowledgment.
        self._gate_taint(extract)
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
        self._bind_owner(extract, prior)
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
        # algo#1216 R2 finding 3787189757: pre-4b states are deliberately
        # schema-valid (migration tolerance), but a LEGACY resume must not
        # reach clean monitoring completion without the mandatory
        # acceptance-criteria/dependency/migration/claims gate. Refuse the
        # launch with the actionable recovery instead of silently running.
        # algo#1216 R2 finding 3787662312 (third leg): preserved sidecars
        # carrying PENDING external intents must be reconciled before another
        # write-capable child runs — otherwise the loop can duplicate the
        # very mutations the sidecar records.
        pending_sidecars = []
        for sidecar in sorted(
            self.state_path.parent.glob(
                self.state_path.stem + ".failed-candidate*"
            )
        ):
            side_extract = self.schema.extract(sidecar)
            results = side_extract.get("handoff_results") or {}
            if any(
                status in ("pending", "retryable")
                for kind in results.values()
                for status in kind.values()
            ):
                pending_sidecars.append(sidecar.name)
        if pending_sidecars:
            raise RunnerExit(
                5,
                "blocked",
                "preserved failed-candidate sidecar(s) carry unreconciled"
                " pending external intents: "
                + ", ".join(pending_sidecars)
                + " — verify each pending operation's remote postcondition"
                " per state-and-safety.md, record terminal results in"
                " canonical state, then delete the sidecar(s) and resume",
            )
        if extract.get("phases_merge_readiness") != "complete":
            raise RunnerExit(
                5,
                "blocked",
                "phases.merge_readiness is not complete — run Phase 4b"
                " (merge readiness) to completion before monitoring; a"
                " pre-4b legacy state must not bypass the gate",
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
            # Re-gate every tick: a committed candidate may have introduced
            # newly tainted text (feedback excerpts land in state by design),
            # and the next write-capable launch needs the same confirmation.
            self._gate_taint(extract)
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


def _read_ceiling(raw: str) -> int:
    """argparse ``type`` for ``--max-candidate-bytes`` (pass-3 codex #12).

    The read path is ``handle.read(self.max_candidate_bytes + 1)``. A ceiling
    below ``1`` turns that into either a no-op (``read(0)`` at ceiling ``-1``,
    which rejects every candidate) or an UNBOUNDED whole-file slurp
    (``read(-1)`` at ceiling ``<= -2``), the latter defeating the multi-GB
    write DoS cap the ceiling exists to enforce. Reject sub-1 ceilings at parse
    time so a negative seam value can never reach ``read``.
    """
    value = int(raw)  # ValueError here surfaces as an argparse error
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"--max-candidate-bytes must be >= 1 (got {value}): a ceiling below"
            " 1 makes the candidate read a no-op or an unbounded whole-file"
            " slurp, defeating the size cap"
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_file")
    parser.add_argument("--slice-budget", type=float, default=MONITOR_SLICE_BUDGET_SECONDS)
    parser.add_argument("--skill-dir", default=str(SCRIPTS_DIR.parent))
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--wait-scale", type=float, default=1.0)
    parser.add_argument("--max-ticks", type=int, default=None)
    parser.add_argument("--schema-cli", default=str(SCRIPTS_DIR / "state_schema.py"))
    parser.add_argument(
        "--child-idle-timeout",
        type=float,
        default=MONITOR_CHILD_IDLE_TIMEOUT_SECONDS,
        dest="child_idle_timeout",
        help="test seam: child silence bound (production default is the"
        " policy constant)",
    )
    parser.add_argument(
        "--max-candidate-bytes",
        type=_read_ceiling,
        default=MAX_CANDIDATE_BYTES,
        dest="max_candidate_bytes",
        help="test seam: candidate-read ceiling >= 1 (production default is the"
        " policy constant); a candidate over the ceiling is a charged retry",
    )
    parser.add_argument(
        "--acknowledge-taint",
        action="append",
        default=None,
        help="operator-confirmed taint set digest (printed by the taint"
        " block); repeatable — covers exactly the acknowledged finding set",
    )
    args = parser.parse_args()
    # Pass-4 opus F2: Runner.__init__ performs failable filesystem I/O (the
    # schema-CLI and wrapper source pins plus the wrapper stage-file mkstemp -
    # pass-5 codex F1 dropped the old snapshot mkdtemp/copyfile), so
    # construction must sit inside the structured boundary - an init crash
    # escaping as a raw traceback with
    # exit code 1 is unclassifiable to the supervising parent, which the
    # documented contract (0/3/4/5, internal_failure on code 4) forbids.
    runner: Runner | None = None
    try:
        runner = Runner(args)
        summary = runner.run()
    except RunnerExit as exit_info:
        _emit(
            {
                "runner_outcome": exit_info.outcome,
                "reason": exit_info.reason,
                "ticks_completed": (
                    runner.ticks_completed if runner is not None else 0
                ),
                "child_session_id": (
                    runner.child_session_id if runner is not None else None
                ),
            }
        )
        return exit_info.code
    except Exception as error:  # noqa: BLE001 — last-resort structured exit
        # R7 codex #11: the runner is a SUPERVISED subprocess — the parent
        # session classifies the slice from this JSON summary. An unexpected
        # exception must not escape as a raw traceback (unclassifiable, reads
        # as a hard crash); emit a structured internal_failure carrying the
        # exception detail and a nonzero code so the supervisor can act.
        _emit(
            {
                "runner_outcome": "internal_failure",
                "reason": (
                    "unhandled runner exception:"
                    f" {error.__class__.__name__}: {error}"
                ),
                "ticks_completed": (
                    runner.ticks_completed if runner is not None else 0
                ),
                "child_session_id": (
                    runner.child_session_id if runner is not None else None
                ),
            }
        )
        return 4
    finally:
        # The wrapper stage file is runner-lifetime state (see __init__);
        # every exit path — structured or not — reclaims it here.
        if runner is not None:
            runner.cleanup_wrapper_stage()
    _emit(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
