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
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
from typing import Any, IO

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from handoff_decision import QA_OWNER_BY_REPOSITORY  # noqa: E402
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

    def __init__(
        self,
        schema_source: bytes,
        remaining_seconds: "Callable[[], float] | None" = None,
    ) -> None:
        # algo#1216 finding 3806594995: four post-child CLI calls at a fixed
        # 120s each could overrun the parent's 2700s attempt ceiling by 60s.
        # When the runner provides its slice clock, every call's timeout is
        # capped at the remaining budget (floor 5s so a call can still fail
        # fast rather than hang the accounting).
        self._remaining_seconds = remaining_seconds
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
            call_timeout = 120.0
            if self._remaining_seconds is not None:
                call_timeout = min(
                    call_timeout, max(5.0, self._remaining_seconds())
                )
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-", *mode, str(target)],
                input=self._source,
                capture_output=True,
                timeout=call_timeout,
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
    liveness = block.get("liveness")
    if liveness is None:
        lines.append("  liveness: null")
    else:
        lines.append("  liveness:")
        lines.append(f"    rung: {liveness['rung']}")
        lines.append(
            f"    next_retry_at: {_render_scalar(liveness.get('next_retry_at'))}"
        )
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
    durable_replace(tmp, path)


def _fsync_parent(path: Path) -> bool:
    # R6-F14: fsync the parent directory so a rename itself survives a
    # power loss — a data fsync does not make the directory entry durable.
    # admin#1495 finding 3793025395: an fsync I/O failure is a DURABILITY
    # FAILURE and must surface (injected EIO was silently swallowed while
    # durable_replace reported success). Only the platform's genuine
    # inability to open/fsync a directory (ENOTSUP/EINVAL/EACCES-class,
    # e.g. some non-POSIX filesystems) stays best-effort.
    import errno

    best_effort = (errno.ENOTSUP, errno.EINVAL, errno.EACCES, errno.EPERM)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError as error:
        return error.errno in best_effort
    try:
        os.fsync(dir_fd)
    except OSError as error:
        return error.errno in best_effort
    finally:
        os.close(dir_fd)
    return True


def durable_replace(source: Path, target: Path) -> None:
    """admin#1495 R2 finding 3791925163: EVERY namespace update on the
    canonical-state commit path routes through one helper that fsyncs the
    target's parent after the rename — previously only atomic_write's
    internal rename was durable, so a crash after the final
    candidate-to-canonical replace could roll a reported-committed tick
    back. Finding 3793025395: a failed directory fsync raises — the caller
    must never report a commit the disk may not hold."""
    os.replace(source, target)
    if not _fsync_parent(target):
        raise RunnerExit(
            4,
            "suspect_state",
            f"directory fsync failed after replacing {target.name} — the"
            " rename may not be durable on disk; treat the commit as"
            " unproven and reconcile per the Resume trust model",
        )


def _read_regular_file(path: Path, ceiling: int) -> bytes:
    """algo#1216 finding 3792942225: a plain open() follows child-plantable
    special files — a FIFO blocks the runner past its slice deadline until
    a writer supplies bytes. Open O_NONBLOCK|O_NOFOLLOW, prove S_ISREG on
    the OPEN descriptor, and read at most ceiling+1 bytes (the caller's
    size checks stay meaningful). O_NONBLOCK on a regular file has no
    effect on reads, so normal candidates are unaffected."""

    import stat as stat_module

    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise RunnerExit(
                4,
                "suspect_state",
                f"{path.name} is not a regular file — a planted special"
                " file cannot be trusted input; reconcile per the Resume"
                " trust model",
            )
        chunks: list[bytes] = []
        remaining_budget = ceiling + 1
        while remaining_budget > 0:
            chunk = os.read(fd, min(1_048_576, remaining_budget))
            if not chunk:
                break
            chunks.append(chunk)
            remaining_budget -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


_SANITIZED_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"


def _resolve_system_binary(name: str) -> str:
    """mm#3551 finding 3806719679: resolve a bare command against the
    sanitized system PATH (user-writable ambient PATH entries excluded).
    Falls back to the bare name when nothing matches — the subsequent probe
    or spawn then fails loudly with the real cause instead of silently
    resolving through a writable directory."""

    import shutil as shutil_module

    # Hermetic-test seam (same class as --claude-bin/--wait-scale): an
    # explicit MONITOR_RUNNER_BIN_<NAME> override names the binary directly.
    # The runner's own environment is the operator's trust domain — the
    # finding's hazard was the ambient PATH's writable first directory,
    # which this resolution still never consults.
    override = os.environ.get(f"MONITOR_RUNNER_BIN_{name.upper()}")
    if override:
        return override
    found = shutil_module.which(name, path=_SANITIZED_SYSTEM_PATH)
    if found is None:
        # admin#1495 finding 3807823288: falling back to the bare name let
        # later spawns resolve through the ambient PATH — the exact hole
        # this resolver exists to close. A host without the binary on the
        # system paths is broken; fail closed with the sanctioned fixes.
        raise RunnerExit(
            5,
            "blocked",
            f"required binary {name!r} not found on the sanitized system"
            f" PATH ({_SANITIZED_SYSTEM_PATH}) — install it there or set"
            f" MONITOR_RUNNER_BIN_{name.upper()} to its absolute path;"
            " ambient-PATH fallback is disabled by design",
        )
    return found


def process_fingerprint(pid: int) -> str | None:
    # R6-F8: a denied/hung ps is a fingerprint failure, not a runner crash —
    # None already routes the launch into the structured spawn-failure path.
    try:
        completed = subprocess.run(
            [_resolve_system_binary("ps"), "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value or None


def _descendant_snapshot(root_pid: int) -> dict[int, int]:
    """Best-effort {pid: pgid} of every live descendant of ``root_pid``.

    #3551 finding 3808151914: a descendant that calls ``setsid`` leaves the
    recorded process group, so group inspection alone cannot see it — but
    while its ANCESTRY is intact the ``ppid`` chain still reaches it. The
    drain loop calls this periodically while the leader lives, and the
    extinction gate then proves every snapshotted pid dead in addition to
    the group. Residual (documented, not silent): a descendant spawned and
    orphaned entirely BETWEEN two snapshots evades the chain walk — the
    poll cadence bounds that window; the strict boundary is a cgroup/PID
    namespace, available only on Linux hosts. Failures here return the
    facts gathered so far (empty on total failure): the snapshot only
    ADDS coverage on top of the fail-closed group gate, so a snapshot
    error must not convert a provable group answer into a block.
    """

    try:
        completed = subprocess.run(
            [_resolve_system_binary("ps"), "-eo", "pid=,ppid=,pgid=,stat="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    children: dict[int, list[tuple[int, int, str]]] = {}
    for row in completed.stdout.splitlines():
        parts = row.split()
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, pgid, parts[3]))
    found: dict[int, int] = {}
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, pgid, stat in children.get(parent, []):
            if pid in found:
                continue
            if not stat.startswith("Z"):
                found[pid] = pgid
            frontier.append(pid)
    return found


def _live_snapshot_pids(snapshot: dict[int, int]) -> list[int]:
    """Fail-closed liveness over a recorded descendant snapshot.

    Mirrors ``_live_group_members``: an uninspectable table blocks rather
    than reading as extinction, because these pids were RECORDED live and
    only a trusted answer may clear them. Zombies do not count.
    """

    if not snapshot:
        return []
    pid_args = [str(pid) for pid in sorted(snapshot)]
    try:
        completed = subprocess.run(
            [_resolve_system_binary("ps"), "-o", "pid=,stat=", "-p", ",".join(pid_args)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunnerExit(
            5,
            "blocked",
            "process-table inspection failed"
            f" ({error.__class__.__name__}) — cannot prove recorded"
            " descendants extinct; needs a human",
        )
    if completed.returncode not in (0, 1):
        raise RunnerExit(
            5,
            "blocked",
            "process-table inspection returned"
            f" {completed.returncode} — cannot prove recorded descendants"
            " extinct; needs a human",
        )
    live: list[int] = []
    for row in completed.stdout.splitlines():
        parts = row.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if not parts[1].startswith("Z"):
            live.append(pid)
    return live


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
            [_resolve_system_binary("ps"), "-o", "pid=,stat=", "-g", str(pgid)],
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
    # #3551 finding 3808151914: record descendants while ancestry is intact
    # so session-escaped ones remain provable after the leader dies.
    descendant_snapshot: dict[int, int] = {}
    last_snapshot = 0.0
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
            lowered_line = decoded.lower()
            if len(stderr_sticky) < 5 and (
                WRAPPER_EXEC_FAILED_MARKER in decoded
                or _has_auth_signature(decoded)
                # admin#1495 finding 3807823268: rate-limit and
                # resume-not-found classification read only the rolling
                # 20-line tail — a 429 followed by 30 cleanup lines lost
                # its marker and decayed to a generic retry charge. These
                # deterministic signatures are sticky like auth/exec ones.
                or "429" in lowered_line
                or "rate limit" in lowered_line
                or "overloaded" in lowered_line
                or "no conversation found" in lowered_line
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
        if now - last_snapshot >= 1.0:
            descendant_snapshot.update(_descendant_snapshot(proc.pid))
            last_snapshot = now
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
        "descendant_snapshot": descendant_snapshot,
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


def _remote_name_with_owner(remote_url: str) -> str | None:
    """Parse `Org/Repo` from an origin remote URL (ssh or https shapes).

    Exact-match consumers (the QA mapping) treat any unparseable shape as
    unmapped — fail-inert, mirroring the prose gate's exact-nameWithOwner
    rule."""
    tail = remote_url
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    if ":" in tail and "@" in tail.split(":", 1)[0]:
        tail = tail.split(":", 1)[1]
    else:
        parts = tail.split("//", 1)
        if len(parts) == 2:
            segments = parts[1].split("/")
            tail = "/".join(segments[1:])
    pieces = [piece for piece in tail.split("/") if piece]
    if len(pieces) == 2:
        return "/".join(pieces)
    return None


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
        # mm#3551 finding 3806719679 (in-package half): a bare command name
        # resolved through the ambient PATH trusts a same-UID-writable first
        # PATH directory. Bare names resolve ONCE, at startup, against the
        # sanitized system PATH; path-shaped values resolve absolute as
        # before. (The mutable-checkout import chain half of that finding
        # remains the host deployment contract.)
        self.claude_bin = (
            str(Path(args.claude_bin).resolve())
            if os.sep in args.claude_bin
            else _resolve_system_binary(args.claude_bin)
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
        self.schema = SchemaCli(
            self.schema_source, remaining_seconds=lambda: self.remaining()
        )
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
        # admin#1495 R2 finding 3722356278 (follow-up 3777166503): the
        # write-capable monitor child is ordered to re-read this package's
        # SKILL.md/references and run its scripts, and the live skill_dir
        # can sit INSIDE the mutable PR checkout — a mid-run push or
        # re-checkout swaps the prose and scripts a later child trusts.
        # Snapshot the whole instruction surface OUTSIDE the worktree now,
        # digest-pinned, and hand every child the snapshot path instead.
        # This pins the mid-run window only; like the wrapper/validator pins
        # above it cannot authenticate boot-time content (the UID-writable
        # boot-chain residual documented at SchemaCli._run).
        self.child_skill_dir = Path(
            tempfile.mkdtemp(prefix="monitor-skill-snap-")
        )
        self.skill_manifest: dict[str, str] = {}
        snapshot_sources = [self.skill_dir / "SKILL.md"]
        snapshot_sources += sorted((self.skill_dir / "references").glob("*.md"))
        snapshot_sources += sorted((self.skill_dir / "scripts").glob("*.py"))
        for snapshot_source in snapshot_sources:
            relative = snapshot_source.relative_to(self.skill_dir)
            staged = self.child_skill_dir / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            payload = snapshot_source.read_bytes()
            staged.write_bytes(payload)
            self.skill_manifest[str(relative)] = hashlib.sha256(
                payload
            ).hexdigest()
        # algo#1216 R2 finding 3779532263: canonical state lives under
        # <repo>/.claude, and a child launched THERE gets default file access
        # only below it — Phase 6 could not touch application files. Launch
        # at the repository root when one exists (state/skill/candidate
        # paths in the prompt are absolute, so nothing else moves). The probe
        # keeps the structured-exit doctrine: a host without git falls back
        # to the state directory instead of crashing __init__.
        try:
            # CodeRabbit 3787358695/3784681433: bounded like every other
            # subprocess site here — a hung git must not wedge __init__
            # before the lock or any heartbeat exists.
            root_probe = subprocess.run(
                [_resolve_system_binary("git"), "-C", str(self.state_path.parent), "rev-parse",
                 "--show-toplevel"],
                capture_output=True, text=True, timeout=30,
            )
            root = root_probe.stdout.strip()
            probe_ok = root_probe.returncode == 0 and bool(root)
        except (OSError, subprocess.TimeoutExpired):
            probe_ok = False
        self.child_cwd = root if probe_ok else str(self.state_path.parent)
        # algo#1216 R2 finding 3813491661: terminal acceptance must know
        # whether this repository is QA-mapped, and an all-idle handoffs
        # block carries no repo name — so derive nameWithOwner from the
        # repository's own origin remote (environmental truth the child
        # cannot rewrite via a candidate), matched EXACTLY against the
        # planner's mapping table. No remote / no repo ⇒ unmapped, and the
        # gate stays inert.
        self.qa_mapped_repository: str | None = None
        if probe_ok:
            try:
                url_probe = subprocess.run(
                    ["git", "-C", root, "remote", "get-url", "origin"],
                    capture_output=True, text=True,
                )
            except OSError:
                url_probe = None
            if url_probe is not None and url_probe.returncode == 0:
                name = _remote_name_with_owner(url_probe.stdout.strip())
                if name in QA_OWNER_BY_REPOSITORY:
                    self.qa_mapped_repository = name
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
        # algo#1216 finding 3806594992 / mm#3551 finding 3806719734:
        # canonical state is child-writable between ticks — its reads get
        # the same FIFO-safe bounded treatment as candidates.
        return _read_regular_file(
            self.state_path, MAX_CANDIDATE_BYTES
        ).decode("utf-8")

    def current_block(self, extract: dict[str, Any]) -> dict[str, Any]:
        block = extract.get("monitor_cli")
        base = dict(block) if isinstance(block, dict) else {
            "schema_version": 1,
            "child_session_id": None,
            "owner_model": self.owner_model,
            "last_completed_attempt_id": None,
            "in_flight": None,
            "liveness": None,
        }
        base.setdefault("liveness", None)
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
        # Finding 3813789211 (recovery half): address the RECORDED candidate
        # by its exact path — recovery knows the attempt id, so it never
        # needs a directory walk to find it — and bound the stray sweep with
        # the same scandir ceiling as the launch gate. An over-limit
        # accumulation deletes one bounded batch here; the launch gate's
        # retention block then owns the remainder.
        recorded_name = None
        if (
            isinstance(recorded_attempt, str)
            and re.fullmatch(r"[0-9a-f]{32}", recorded_attempt) is not None
        ):
            recorded_name = self.state_path.name + f".attempt-{recorded_attempt}.md"
            recorded_path = self.state_path.parent / recorded_name
            if recorded_path.exists():
                self._preserve_failed(recorded_path)
                preserved_any = True
        strays, _over = self._bounded_sidecar_scan(
            (self.state_path.name + ".attempt-",),
            self.SIDECAR_RETENTION_LIMIT,
        )
        for stray in strays:
            if recorded_name is not None and stray.name == recorded_name:
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
        # algo#1216 finding 3792942221 (in-package half): the child env is
        # built from an ALLOWLIST, not ambient-minus-three — an unrelated
        # ambient sentinel provably reached the child under the old
        # denylist. This is leakage hygiene, not a credential boundary: the
        # child still reaches gh/claude auth through HOME by design, and
        # the role/credential separation R2 asks for remains a host
        # contract. FAKE_* passes only under the test harness's fake
        # claude bin, never a real one.
        # mm#3551 finding 3806719670: the CLAUDE_ PREFIX forwarded Keeper's
        # VM bootstrap secrets (lazy-init stores the active OAuth token and
        # the full account bundle under CLAUDE_*-named variables). Exact
        # names only: the child needs its config dir; every other CLAUDE_*
        # variable is either one of the three deliberately-unset overrides
        # or something the child must not inherit.
        allowed_prefixes = ("LC_",)
        allowed_names = {
            "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
            "TZ", "LANG", "COLUMNS", "LINES", "SSH_AUTH_SOCK", "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR",
            # algo#1216 finding 3807740755: Keeper agent VMs run an
            # OAuth-only Claude contract — the child's OWN session auth
            # arrives through this one variable (startup.sh / the
            # orchestrator token refresher), and stripping it left the
            # child unauthenticated. The ACCOUNT-token bundle and every
            # other CLAUDE_* name stay excluded (finding 3806719670).
            "CLAUDE_CODE_OAUTH_TOKEN",
        }
        fake_child = Path(self.claude_bin).name != "claude"
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key not in CLAUDE_READ_ONLY_ENV_UNSET
            and (
                key in allowed_names
                or key.startswith(allowed_prefixes)
                or (fake_child and key.startswith("FAKE_"))
            )
        }
        try:
            # algo#1216 R2 finding 3779532260: restore the stage file
            # from the __init__-pinned wrapper bytes immediately before the
            # launch — the interpreter reads only bytes this runner just
            # wrote from its own heap.
            self._verify_skill_snapshot()
            self._restage_wrapper()
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

        # Finding 3791925160: the sidecar gate runs before EVERY launch —
        # a candidate preserved by the previous tick in this same slice
        # must be reconciled before another write-capable child runs.
        self._gate_sidecars(extract)

        tick_ordinal = self.ticks_completed + 1
        attempt_id = uuid.uuid4().hex
        candidate = self.state_path.parent / (
            self.state_path.name + f".attempt-{attempt_id}.md"
        )
        base_digest = extract.get("digest")
        if not isinstance(base_digest, str):
            raise RunnerExit(4, "suspect_state", "canonical digest unavailable")
        prompt = monitor_child_prompt(
            # Finding 3722356278: the child reads prose/scripts from the
            # launch-time snapshot, never the live (possibly checked-out)
            # package directory.
            str(self.child_skill_dir),
            str(self.state_path),
            str(candidate),
            attempt_id,
            tick_ordinal,
        )
        # admin#1495 finding 3793025396: the ceiling is computed HERE,
        # after the sidecar gate above (each sidecar extract can consume up
        # to 120s) — an exact-head repro launched with a NEGATIVE ceiling
        # computed before that spend. A floor-or-less ceiling never
        # launches: the slice returns exhausted and the parent re-invokes.
        ceiling = min(
            PER_ATTEMPT_CEILING_SECONDS,
            self.remaining() - MONITOR_SLICE_CLEANUP_MARGIN_SECONDS,
        )
        if ceiling < MONITOR_CHILD_MIN_VIABLE_SECONDS:
            return "exhausted"
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
            self._preserve_failed(candidate)
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
        # #3551 finding 3808151914: the group gate cannot see a descendant
        # that re-sessioned away from the recorded pgid, so the drain's
        # ancestry snapshot extends the extinction proof to every pid that
        # was ever observed as a descendant while the leader lived.
        escaped = _live_snapshot_pids(drained.get("descendant_snapshot") or {})
        if _live_group_members(child_pgid) or escaped:
            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            snapshot = drained.get("descendant_snapshot") or {}
            for pid in escaped:
                for target in {snapshot.get(pid), None}:
                    try:
                        if target is not None:
                            os.killpg(target, signal.SIGKILL)
                        else:
                            os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            recheck_deadline = time.monotonic() + 15
            while time.monotonic() < recheck_deadline and (
                _live_group_members(child_pgid)
                or _live_snapshot_pids(snapshot)
            ):
                time.sleep(0.3)
            if _live_group_members(child_pgid) or _live_snapshot_pids(snapshot):
                self._preserve_failed(candidate)
                raise RunnerExit(
                    5,
                    "blocked",
                    "descendants of the monitor child (same-group or"
                    " re-sessioned) survived SIGKILL — a possibly-live"
                    " writer needs a human",
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
            self._preserve_failed(candidate)
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
            # algo#1216 finding 3792942218: the candidate is the only
            # durable record of external mutations the child may already
            # have fired — a suspect stop PRESERVES it for the human
            # reconciliation it demands, never destroys it. (Supersedes the
            # earlier discard-on-suspect semantics.)
            self._preserve_failed(candidate)
        raise RunnerExit(
            4,
            "suspect_state",
            "canonical state changed under the monitor lock (digest or"
            " control drift vs the launch snapshot) — unknown writer; the"
            " child's candidate is preserved as a failed-candidate sidecar;"
            " reconcile per the Resume trust model before resuming"
            " monitoring",
        )

    def _preserve_failed(self, candidate: Path) -> None:
        """algo#1216 R2 findings 3779532272 + 3787662312: a failed child's
        candidate is the only durable record of external mutations it may
        already have fired. Preservation is ATTEMPT-SCOPED (consecutive
        failures never overwrite each other), and an archival failure leaves
        the source candidate IN PLACE — deleting the evidence because the
        rename failed would be strictly worse than an unarchived candidate
        (the sidecar gate also scans raw `.attempt-*` strays for exactly
        this case). Suspect stops preserve too (finding 3792942218): the
        stop demands human reconciliation, and the candidate is its
        evidence."""
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
            durable_replace(candidate, preserved)
        except OSError:
            pass  # never destroy the write-ahead record


    # Sidecars a run may retain at once even after compaction: past this,
    # startup work is unbounded and the audit unreadable — a human cleans up.
    SIDECAR_RETENTION_LIMIT = 20
    # Default work cap when canonical state resolves no override; the
    # canonical definition is monitor-ci-feedback.md's MAX_ITERATIONS = 50
    # (state override key: resolved_conventions.monitor_constants
    # .max_iterations, per the state-and-safety template).
    WORK_CAP_DEFAULT = 50

    def _bounded_sidecar_scan(
        self, prefixes: tuple[str, ...], limit: int
    ) -> tuple[list[Path], bool]:
        """admin#1495 R2 finding 3813789211: never materialize an unbounded
        sidecar listing while the monitor lock is held. os.scandir stops at
        limit+1 matches, so runaway accumulation costs O(limit) name checks
        instead of a full-directory glob/set/sort. Returns (paths, over):
        paths is sorted and complete only when over is False; when over is
        True the caller blocks on count alone and never parses anything.
        A directory that cannot be enumerated is suspect, not a crash."""
        matches: list[Path] = []
        over = False
        try:
            with os.scandir(self.state_path.parent) as entries:
                for entry in entries:
                    if not any(
                        entry.name.startswith(prefix) for prefix in prefixes
                    ):
                        continue
                    matches.append(self.state_path.parent / entry.name)
                    if len(matches) > limit:
                        over = True
                        break
        except OSError as error:
            raise RunnerExit(
                4,
                "suspect_state",
                "cannot enumerate the state directory for sidecar"
                f" reconciliation ({error.__class__.__name__}) — resolve the"
                " filesystem fault and resume",
            )
        return (sorted(matches), over)

    def _gate_sidecars(
        self, extract: dict[str, Any], compact_no_status: bool = False
    ) -> None:
        """admin#1495 R2 finding 3791925160: reconcile every preserved
        sidecar before EVERY launch, not only at startup — a candidate
        preserved by tick N must gate tick N+1's launch in the same slice.

        Per sidecar, in order: an unreadable/invalid extract fails closed
        (matchmaking#3551 finding 3790012750 — suspect extracts carry empty
        handoff_results, which is "cannot rule pending out", never "no
        pending work"); a pending/retryable result blocks for verify-before-
        retry reconciliation; a terminal-only sidecar whose every result is
        already recorded terminally in CANONICAL state is redundant evidence
        and is COMPACTED (deleted, logged); a terminal-only sidecar carrying
        evidence canonical lacks blocks with the merge instruction — the
        runner never merges history unsupervised. A post-compaction count
        above SIDECAR_RETENTION_LIMIT blocks (bounded startup work)."""

        pending_sidecars: list[str] = []
        unreadable_sidecars: list[str] = []
        unmerged_sidecars: list[str] = []
        conflicting_sidecars: list[str] = []
        canonical_results = extract.get("handoff_results") or {}
        canonical_digests = extract.get("handoff_result_digests") or {}
        canonical_terminal = {
            (kind_name, operation_id): status
            for kind_name, kind in canonical_results.items()
            if isinstance(kind, dict)
            for operation_id, status in kind.items()
            if status in ("complete", "failed", "skipped_dependency")
        }
        canonical_record_digests = {
            (kind_name, operation_id): digest
            for kind_name, kind in canonical_digests.items()
            if isinstance(kind, dict)
            for operation_id, digest in kind.items()
        }
        # algo#1216 finding 3792942215 (residue): a failed _preserve_failed
        # rename leaves the raw `.attempt-*` candidate behind — it carries
        # the same possibly-fired-mutation evidence, so the gate covers
        # BOTH name shapes.
        # R2 re-reply 3792845972 + finding 3813789211: enforce count and
        # byte ceilings BEFORE any parsing or compaction, and bound the
        # ENUMERATION itself — the old glob/set/sort materialized every
        # matching path (reproduced at 128) before the count check ran.
        sidecars, over_limit = self._bounded_sidecar_scan(
            (
                self.state_path.stem + ".failed-candidate",
                self.state_path.name + ".attempt-",
            ),
            self.SIDECAR_RETENTION_LIMIT,
        )
        if over_limit:
            raise RunnerExit(
                5,
                "blocked",
                f"more than {self.SIDECAR_RETENTION_LIMIT} preserved sidecars"
                " exceed the retention"
                f" limit ({self.SIDECAR_RETENTION_LIMIT}) — reconcile and"
                " delete them per state-and-safety.md before resuming;"
                " unbounded sidecar accumulation makes startup work"
                " unbounded (bound enforced before any sidecar is"
                " enumerated past the limit or parsed)",
            )
        for sidecar in sidecars:
            try:
                sidecar_bytes = sidecar.stat().st_size
            except OSError:
                sidecar_bytes = None
            if sidecar_bytes is None or sidecar_bytes > self.max_candidate_bytes:
                # Oversized or unstatable: never parse it — same fail-closed
                # class as an unreadable sidecar, decided from metadata only.
                unreadable_sidecars.append(sidecar.name)
                continue
            side_extract = self.schema.extract(sidecar)
            if side_extract.get("state") != "valid":
                unreadable_sidecars.append(sidecar.name)
                continue
            results = side_extract.get("handoff_results") or {}
            side_digests = side_extract.get("handoff_result_digests") or {}
            statuses = [
                (kind_name, operation_id, status)
                for kind_name, kind in results.items()
                if isinstance(kind, dict)
                for operation_id, status in kind.items()
            ]
            if not statuses:
                # admin#1495 finding 3793025403: a valid sidecar recording
                # ZERO operation results carries no external intents to
                # reconcile — retaining it only feeds the retention block.
                # Compaction runs at ENTRY only (compact_no_status): the
                # attempt-scoped preservation contract (3779532272) keeps
                # the CURRENT slice's failure evidence intact for resume
                # diagnosis, so a mid-slice launch gate never deletes what
                # the previous tick just preserved; a later session's entry
                # sweeps them.
                if compact_no_status:
                    try:
                        sidecar.unlink()
                        _heartbeat(
                            f"sidecar compacted (no operation evidence): {sidecar.name}"
                        )
                    except OSError:
                        pass
                continue
            if any(
                status in ("pending", "retryable")
                for _, _, status in statuses
            ):
                pending_sidecars.append(sidecar.name)
                continue
            terminal = [
                entry
                for entry in statuses
                if entry[2] in ("complete", "failed", "skipped_dependency")
            ]
            # R2 re-reply 3792845972 + follow-up 3793041749: compare the
            # ENTIRE terminal record (canonical-JSON digest covering
            # attempts/evidence/timestamps), not the status — matching
            # status with differing history is still evidence a human must
            # reconcile, never "redundant".
            def _record_digest(kind_name: str, operation_id: str) -> str | None:
                kind_map = side_digests.get(kind_name)
                if isinstance(kind_map, dict):
                    value = kind_map.get(operation_id)
                    return value if isinstance(value, str) else None
                return None

            if terminal and all(
                canonical_terminal.get((kind_name, operation_id)) == status
                and canonical_record_digests.get((kind_name, operation_id))
                == _record_digest(kind_name, operation_id)
                and _record_digest(kind_name, operation_id) is not None
                for kind_name, operation_id, status in terminal
            ):
                # Redundant evidence: canonical already carries the SAME
                # terminal record for every operation this sidecar names.
                try:
                    sidecar.unlink()
                    _heartbeat(
                        f"sidecar compacted (redundant terminal evidence): {sidecar.name}"
                    )
                except OSError:
                    pass
                continue
            if terminal and any(
                (kind_name, operation_id) in canonical_terminal
                and (
                    canonical_terminal[(kind_name, operation_id)] != status
                    or canonical_record_digests.get(
                        (kind_name, operation_id)
                    )
                    != _record_digest(kind_name, operation_id)
                )
                for kind_name, operation_id, status in terminal
            ):
                conflicting_sidecars.append(sidecar.name)
                continue
            if terminal:
                unmerged_sidecars.append(sidecar.name)
        if unreadable_sidecars:
            raise RunnerExit(
                5,
                "blocked",
                "preserved failed-candidate sidecar(s) failed validation"
                " (truncated, malformed, or unreadable): "
                + ", ".join(unreadable_sidecars)
                + " — the runner cannot prove they carry no pending external"
                " intents, so treat every operation they may record as"
                " possibly fired: verify remote postconditions per"
                " state-and-safety.md, record terminal results in canonical"
                " state, then delete the sidecar(s) and resume",
            )
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
        if conflicting_sidecars:
            raise RunnerExit(
                5,
                "blocked",
                "preserved failed-candidate sidecar(s) carry terminal"
                " evidence that CONFLICTS with canonical state's record for"
                " the same operation: "
                + ", ".join(conflicting_sidecars)
                + " — a human must reconcile which record is true (verify"
                " the remote postcondition per state-and-safety.md, correct"
                " canonical state if needed), then delete the sidecar(s)"
                " and resume; the runner never chooses between conflicting"
                " histories",
            )
        if unmerged_sidecars:
            raise RunnerExit(
                5,
                "blocked",
                "preserved failed-candidate sidecar(s) carry TERMINAL"
                " operation evidence canonical state does not record: "
                + ", ".join(unmerged_sidecars)
                + " — record each terminal result in canonical state (the"
                " runner never merges history unsupervised), then delete the"
                " sidecar(s) and resume",
            )

    def _verify_skill_snapshot(self) -> None:
        """Re-verify the staged instruction surface against the __init__
        manifest immediately before a launch (finding 3722356278): the
        snapshot lives outside the worktree, so drift here means something
        tampered with the runner's own staging area — never continue."""
        # admin#1495 finding 3793025406: hashing manifest entries alone
        # accepts ADDITIONS — a planted scripts/hashlib.py shadows stdlib
        # when the child runs `python3 scripts/state_schema.py` (that
        # invocation puts the script dir on sys.path). The staged tree must
        # EQUAL the manifest: no extra files, regular files only (lstat —
        # a symlink is a redirect out of the staging area).
        actual: set[str] = set()
        for found in self.child_skill_dir.rglob("*"):
            if found.is_dir() and not found.is_symlink():
                continue
            rel = str(found.relative_to(self.child_skill_dir))
            actual.add(rel)
            mode = os.lstat(found).st_mode
            import stat as stat_module
            if not stat_module.S_ISREG(mode):
                raise RunnerExit(
                    4,
                    "suspect_state",
                    f"child skill snapshot contains a non-regular file"
                    f" ({rel}) — the staged instruction surface was"
                    " tampered with; reconcile per the Resume trust model",
                )
        expected = set(self.skill_manifest)
        if actual != expected:
            unexpected = sorted(actual - expected)[:5]
            missing = sorted(expected - actual)[:5]
            raise RunnerExit(
                4,
                "suspect_state",
                "child skill snapshot tree does not equal the manifest"
                f" (unexpected: {unexpected}; missing: {missing}) — the"
                " staged instruction surface was modified outside the"
                " runner; reconcile per the Resume trust model",
            )
        for relative, digest in self.skill_manifest.items():
            staged = self.child_skill_dir / relative
            live = hashlib.sha256(
                _read_regular_file(staged, MAX_CANDIDATE_BYTES)
            ).hexdigest()
            if live != digest:
                raise RunnerExit(
                    4,
                    "suspect_state",
                    "child skill snapshot drifted before launch"
                    f" ({relative}) — the staged instruction surface was"
                    " modified outside the runner; reconcile per the Resume"
                    " trust model",
                )

    def cleanup_wrapper_stage(self) -> None:
        """Remove the runner-lifetime staged files (main()'s finally): the
        wrapper stage file and the child skill snapshot directory.

        Best-effort by design: both carry unpredictable names, so a leak on
        a hard kill is bounded and harmless."""
        try:
            self.wrapper_stage_path.unlink()
        except OSError:
            pass
        shutil.rmtree(self.child_skill_dir, ignore_errors=True)

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
                # Finding 3792942225: FIFO-safe — a planted special file at
                # the candidate path must fail closed, never block the read.
                raw_candidate = _read_regular_file(
                    candidate, self.max_candidate_bytes
                )
                candidate_text = raw_candidate.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                candidate_text = None
            except RunnerExit:
                candidate_text = None
            else:
                if len(raw_candidate) > self.max_candidate_bytes:
                    candidate_text = None
        if checks_failed or candidate_text is None:
            self._preserve_failed(candidate)
            self.charge_failure(fresh, "monitor-child:verdict_mismatch")
            return "retry"
        snapshot = self.launch_block
        base_digest = self.launch_base_digest
        if snapshot is None or base_digest is None:
            self._preserve_failed(candidate)
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
        launch_digests = fresh.get("handoff_result_digests") or {}
        cand_digests = candidate_extract.get("handoff_result_digests") or {}
        launch_attempts = fresh.get("handoff_result_attempts") or {}
        cand_attempts = candidate_extract.get("handoff_result_attempts") or {}
        handoffs_monotonic = True
        # algo#1216 finding 3806594975: a terminal claim over an EMPTY
        # handoff map is vacuously "consistent" — require the launch-time
        # handoff-kind set to survive into the candidate (a kind may gain
        # records, never vanish), so zero-evidence terminal commits fail.
        if outcome == "terminal":
            for kind in launch_results:
                if kind not in cand_results:
                    handoffs_monotonic = False
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
                    # admin#1495 finding 3793025410: same-family rollover
                    # tolerance must never cover an operation whose EXACT id
                    # is still in the candidate's plan — that erased a
                    # completed result for a currently-planned op.
                    if op_id in planned:
                        handoffs_monotonic = False
                    elif not any(
                        planned_id.split(":", 1)[0] == family
                        for planned_id in planned
                    ):
                        handoffs_monotonic = False
                elif status in terminal_statuses and new_status != status:
                    handoffs_monotonic = False
                elif status in terminal_statuses and new_status == status:
                    # Finding 3806594980: a terminal record is IMMUTABLE —
                    # same-status evidence rewrites must not commit. Compare
                    # the full-record digests both extracts expose.
                    if (launch_digests.get(kind) or {}).get(op_id) != (
                        cand_digests.get(kind) or {}
                    ).get(op_id):
                        handoffs_monotonic = False
                elif status in ("pending", "retryable") and new_status not in (
                    "pending",
                    "retryable",
                ) + terminal_statuses:
                    handoffs_monotonic = False
                elif status in ("pending", "retryable") and new_status in (
                    "pending",
                    "retryable",
                ):
                    # Finding 3806594980: attempts on an in-flight record
                    # are NONDECREASING — a reset re-opens the three-attempt
                    # side-effect cap.
                    old_attempts = (launch_attempts.get(kind) or {}).get(op_id)
                    new_attempts = (cand_attempts.get(kind) or {}).get(op_id)
                    if (
                        isinstance(old_attempts, int)
                        and isinstance(new_attempts, int)
                        and new_attempts < old_attempts
                    ):
                        handoffs_monotonic = False
                elif (
                    status in ("pending", "retryable")
                    and new_status == "skipped_dependency"
                ):
                    # algo#1216 finding 3792942214: skipped_dependency is
                    # schema-defined proof that NO attempt occurred
                    # (attempts 0) — a record that was already in flight can
                    # never become one.
                    handoffs_monotonic = False
        # admin#1495 finding 3793025414: gate records are compared against
        # launch state, not only the candidate's own derived hold — a child
        # must never DELETE a required backfill or flip required to false
        # (hold released by record surgery instead of verified completion).
        # pending -> complete is the one legitimate forward transition.
        launch_backfill = fresh.get("merge_readiness_backfill") or {}
        cand_backfill = candidate_extract.get("merge_readiness_backfill") or {}
        backfill_monotonic = True
        for name, launch_record in launch_backfill.items():
            if launch_record.get("required") is not True:
                continue
            cand_record = cand_backfill.get(name)
            if not isinstance(cand_record, dict):
                backfill_monotonic = False
                continue
            if cand_record.get("required") is not True:
                backfill_monotonic = False
                continue
            launch_state = launch_record.get("state")
            cand_state = cand_record.get("state")
            if launch_state == "complete" and cand_state != "complete":
                backfill_monotonic = False
            elif launch_state == "pending" and cand_state not in (
                "pending",
                "complete",
            ):
                backfill_monotonic = False
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
        # algo#1216 R2 finding 3813491661: on a QA-mapped repository the
        # clean-exit contract runs the QA handoff at the FIRST terminal exit
        # (complete or paused) — yet the runner accepted a terminal candidate
        # whose every handoff was still idle, so a child could report
        # completion with QA never assigned, the ticket never moved, and no
        # handoff artifact recorded. The kind-keyed view makes the check
        # direct: mapped repo + terminal claim + idle (or absent) qa record
        # is a contract violation, never progress. Pending/retryable stay
        # governed by the terminal-consistency rule; durably failed is
        # schema-terminal and passes here.
        if (
            outcome == "terminal"
            and self.qa_mapped_repository is not None
            and candidate_extract.get("handoff_kind_statuses", {}).get("qa")
            in (None, "idle")
        ):
            self._preserve_failed(candidate)
            self.charge_failure(fresh, "monitor-child:qa_handoff_missing")
            return "retry"
        # algo#1216 R2 finding 3813491642: the documented MAX_ITERATIONS
        # work cap bound only the UNTRUSTED child's prose loop — the trusted
        # runner accepted any one-step advance, so a faulty child could work
        # past the cap forever (probe: 50→51 committed as a success). The
        # cap is read from the LAUNCH-time canonical (never the candidate —
        # see the constants-stability term in `valid`), and an over-cap work
        # advance is acceptable ONLY as the documented keyed blocked exit.
        # Distinct signature so a three-strike block names the real cause.
        fresh_constants = fresh.get("monitor_constants") or {}
        work_cap = fresh_constants.get("max_iterations", self.WORK_CAP_DEFAULT)
        if (
            deltas == (1, 0)
            and counters_after.get("monitor_iterations", 0) > work_cap
            and not (
                outcome == "blocked"
                and monitor_status == "blocked"
                and candidate_extract.get("blocked_evidence_present") is True
            )
        ):
            self._preserve_failed(candidate)
            self.charge_failure(fresh, "monitor-child:work_cap_exceeded")
            return "retry"
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
            # Finding 3813491642 (second half): a candidate that edits the
            # resolved monitor constants would loosen what the NEXT tick's
            # launch-time read enforces — constants are resolved at entry
            # and never legitimately change mid-monitor.
            and candidate_extract.get("monitor_constants")
            == fresh.get("monitor_constants")
            and outcome_consistent
            and handoffs_monotonic
            and backfill_monotonic
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
        # Finding 3791925163: the final namespace update is the commit the
        # runner reports — it must be durable too, not only the candidate's
        # own rename inside atomic_write.
        durable_replace(candidate, self.state_path)
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
    def _persist_liveness(
        self, extract: dict[str, Any], rung: int, wait_seconds: float
    ) -> None:
        """algo#1216 finding 3806594998 (+admin 3806647918, mm 3806719722):
        the ladder rung persists BEFORE the wait, so a slice_exhausted
        re-invocation resumes the escalation instead of restarting at rung
        1 — state-and-safety's persist-next_retry_at-before-wait rule."""
        from datetime import timedelta

        block = self.current_block(extract)
        block["liveness"] = {
            "rung": rung,
            "next_retry_at": (
                datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.commit_block(block)

    def _restage_wrapper(self) -> None:
        """Rewrite the wrapper stage from the __init__-pinned bytes.

        mm#3551 finding 3808151918: ``Path.write_bytes`` FOLLOWS a
        child-planted symlink at the stage path, turning the rewrite into a
        write-through to an arbitrary same-UID target. Unlink (removing any
        planted link NAME, never its target) and recreate with
        ``O_EXCL|O_NOFOLLOW`` so the bytes land only in a brand-new regular
        file owned by this runner — a re-plant inside the unlink→create
        window makes ``O_EXCL`` fail closed instead of following."""
        try:
            self.wrapper_stage_path.unlink()
        except OSError:
            pass
        stage_fd = os.open(
            self.wrapper_stage_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(stage_fd, self.wrapper_source)
        finally:
            os.close(stage_fd)

    def _clear_liveness_ladder(self) -> None:
        """End-of-ladder cleanup after a non-retry outcome.

        Finding 3807740774: rebuild from a FRESH extract — the run loop's
        pre-tick extract predates the tick's own commit, and reusing it
        overwrote the just-committed ``child_session_id`` and
        ``last_completed_attempt_id`` with their stale values."""
        fresh_after_tick = self.schema.extract(self.state_path)
        cleared = self.current_block(fresh_after_tick)
        cleared["liveness"] = None
        self.commit_block(cleared)

    def _resume_liveness_wait(self, extract: dict[str, Any]) -> None:
        block = extract.get("monitor_cli")
        liveness = block.get("liveness") if isinstance(block, dict) else None
        if not isinstance(liveness, dict):
            return
        deadline_raw = liveness.get("next_retry_at")
        if not isinstance(deadline_raw, str):
            return
        try:
            deadline = datetime.strptime(
                deadline_raw, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return
        remaining_wait = (deadline - datetime.now(timezone.utc)).total_seconds()
        while remaining_wait > 0:
            budget = self.remaining() - MONITOR_SLICE_CLEANUP_MARGIN_SECONDS
            if budget <= MONITOR_CHILD_MIN_VIABLE_SECONDS:
                return  # the loop's own budget check returns the slice
            chunk = min(remaining_wait, WAIT_CHUNK_SECONDS * self.wait_scale, budget)
            _heartbeat(
                f"resuming interrupted ladder wait ({int(remaining_wait)}s left)"
            )
            time.sleep(max(0.0, chunk))
            remaining_wait = (
                deadline - datetime.now(timezone.utc)
            ).total_seconds()

    def _resume_liveness_rung(self, extract: dict[str, Any]) -> int:
        block = extract.get("monitor_cli")
        liveness = block.get("liveness") if isinstance(block, dict) else None
        if not isinstance(liveness, dict):
            return 0
        rung = liveness.get("rung")
        return rung if isinstance(rung, int) and rung >= 1 else 0

    def _ladder_wait_seconds(self, ladder_rung: int) -> float:
        index = min(ladder_rung - 1, len(LIVENESS_BACKOFF_LADDER_SECONDS) - 1)
        return LIVENESS_BACKOFF_LADDER_SECONDS[index] * self.wait_scale

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
        # very mutations the sidecar records. Entry additionally compacts
        # no-status sidecars (finding 3793025403).
        self._gate_sidecars(extract, compact_no_status=True)
        if extract.get("phases_merge_readiness") != "complete":
            raise RunnerExit(
                5,
                "blocked",
                "phases.merge_readiness is not complete — run Phase 4b"
                " (merge readiness) to completion before monitoring; a"
                " pre-4b legacy state must not bypass the gate",
            )
        # Finding 3806594998: resume the persisted ladder rung so a fresh
        # slice continues the escalation instead of restarting at rung 1.
        retries = self._resume_liveness_rung(extract)
        # Finding 3807740769: an interrupted ladder WAIT resumes too — a
        # persisted next_retry_at still in the future is time the previous
        # slice already owed; launching immediately would collapse the
        # backoff. Sleep the remainder (slice-budget-bounded chunks).
        self._resume_liveness_wait(extract)
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
            if result == "exhausted":
                # Finding 3793025396: run_tick's own pre-launch gates spent
                # the ceiling below the viable floor — return the slice
                # instead of launching a child with no time to live.
                return {
                    "runner_outcome": "slice_exhausted",
                    "ticks_completed": self.ticks_completed,
                    "child_session_id": self.child_session_id,
                }
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
                # Persist the rung BEFORE the wait (finding 3806594998) so
                # a slice boundary mid-ladder resumes, not restarts.
                self._persist_liveness(
                    extract, retries, self._ladder_wait_seconds(retries)
                )
                if not self.wait_between_ticks(ladder_rung=retries):
                    return {
                        "runner_outcome": "slice_exhausted",
                        "ticks_completed": self.ticks_completed,
                        "child_session_id": self.child_session_id,
                    }
                continue
            if retries:
                self._clear_liveness_ladder()
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


def _require_isolated_boot() -> None:
    """R2 admin#1495 re-reply 3792845974 (finding 3791925158, the in-package
    enforceable half): plain ``python3 monitor_runner.py`` loads PYTHONPATH,
    site hooks, and user customizations BEFORE this file's first line — a
    same-UID writer of any of those surfaces owns the gate. The runner
    cannot attest an immutable install (that remains the host contract),
    but it CAN refuse an unisolated boot: require ``python3 -I -S``, the
    same isolation its own children already run under. sys.path[0] is
    re-added explicitly at module top, so imports are auditable."""

    if sys.flags.isolated and sys.flags.no_site:
        return
    print(
        json.dumps(
            {
                "runner_outcome": "blocked",
                "reason": (
                    "monitor_runner requires an isolated interpreter boot:"
                    " launch it as `python3 -I -S .../monitor_runner.py ...`"
                    " (isolated, no site hooks) — a plain `python3` boot"
                    " consumes PYTHONPATH/sitecustomize before any integrity"
                    " check runs (finding 3791925158); the immutable-install"
                    " half of that finding remains a host deployment"
                    " contract this check cannot attest"
                ),
            }
        ),
        flush=True,
    )
    raise SystemExit(5)


def main() -> int:
    _require_isolated_boot()
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
