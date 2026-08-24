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
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
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
    sanitize_for_publication,
)

WRAPPER_EXEC_FAILED_MARKER = "MONITOR-WRAPPER-EXEC-FAILED"
_RESUME_NOT_FOUND_HINTS = ("no conversation found", "session not found", "unknown session")

# r14 F7: ONE contextual rate-limit matcher shared by sticky capture,
# overflow capture, and final classification. A bare "429" substring let
# "elapsed 429ms" enter the no-charge liveness ladder; context anchors
# (HTTP status renderings, the provider's own words) admit the real
# forms and nothing else. Returns the match offset so excerpts can be
# anchored on the marker instead of the line head.
_RATE_LIMIT_CONTEXT = re.compile(
    r"(?i)("
    r"\bhttps?/[0-9.]+\s+429\b"
    r"|\bhttp\s+429\b"
    r"|\bstatus(?:\s+code)?\s*[:=]?\s*429\b"
    r"|\berror\s+429\b"
    r"|\b429\s+too\s+many\s+requests\b"
    r"|\brate[ _-]?limit(?:ed|s)?\b"
    r"|\boverloaded\b"
    r")"
)


def rate_limit_offset(text: str) -> int | None:
    """Offset of the first contextual rate-limit marker, or None."""

    match = _RATE_LIMIT_CONTEXT.search(text)
    return match.start() if match is not None else None
DIAGNOSTIC_LINE_CAP = 50
PIPE_BUFFER_CAP = 1_048_576
# R7 codex #11: the child writes the candidate; a bounded read refuses an
# adversarial multi-GB write before it can OOM the runner. Generous vs any
# real workflow-state file (hundreds of KB), a hard ceiling vs a hostile one.
MAX_CANDIDATE_BYTES = 8 * 1_048_576

WAIT_CHUNK_SECONDS = 60

# algo#1216 finding 3813491642: the cumulative logical-work cap is enforced
# by the TRUSTED runner, not just documented for the child —
# monitor-ci-feedback.md's `MAX_ITERATIONS = 50` pseudocode is advisory to
# a faulty or compromised write-capable monitor, and the per-tick +1 delta
# check alone accepted a 50→51 candidate with a successful terminal
# outcome. Past this many cumulative monitor_iterations the only
# acceptable candidate is the documented blocked transition
# (`human:user-confirm:work-cap`). Kept in lockstep with the reference's
# literal by test_monitor_runner_unit's doc-parity regression.
MAX_WORK_ITERATIONS = 50

# algo#1216 finding 3813491661: repositories whose workflows carry a
# REQUIRED clean-exit QA handoff — a terminal candidate for one of these
# with the qa handoff still idle/unplanned is rejected. Casefolded for
# membership tests (GitHub owner/name is case-insensitive). The runner is
# deliberately import-free of package modules (it shells to the schema
# CLI), so this restates the key set of
# handoff_decision.QA_OWNER_BY_REPOSITORY — that map stays canonical, and
# test_monitor_runner_unit's parity regression fails on any drift.
MAPPED_QA_REPOSITORIES = frozenset(
    {
        "keeper-dating/matchmaking",
        "keeper-dating/admin-portal",
        "keeper-dating/calculator-api",
        "keeper-dating/keeper-lead-generator",
    }
)


def _terminal_missing_planned_qa(bound_repo: object, qa_status: object) -> bool:
    """algo#1216 finding 3813491661: a terminal candidate for a Keeper-mapped
    repository must show the clean-exit QA handoff actually planned — an idle
    or absent QA aggregate means completion was reported without assigning QA,
    moving the ticket, or recording any handoff artifact. Idle stays valid for
    unmapped repositories.

    Extracted so the manifest rule keeps a reachable pin: the r17 F9
    containment gate now preempts this branch end to end on a non-delegating
    host (a mapped launch blocks BEFORE any child produces a candidate), so the
    predicate is verified directly here rather than through the now-gated e2e
    path."""

    mapped = (
        isinstance(bound_repo, str)
        and bound_repo.casefold() in MAPPED_QA_REPOSITORIES
    )
    return mapped and qa_status in (None, "idle")

# admin#1495 finding 3825265272 / algo#1216 F3: narrow the capability probe
# to the exact surface the mapped Phase-6 handoffs exercise. The planner
# emits ``*.github.*`` (request-review / replace-assignees) and
# ``*.linear.*`` (assign-ticket / set-ticket-state) operations, and a mapped
# repository is DEFINED by a QA_OWNER_BY_REPOSITORY Linear binding — so it
# will attempt BOTH families. The r25 probe accepted any truthy
# ``permissions`` object or any MCP server, so a deny-all policy or an
# unrelated server passed while granting neither of these. Linear's
# ``record_unavailable`` fallback covers a RUNTIME outage, not a provisioning
# gap: a mapped host that never grants linear is a misconfiguration this
# fails fast on.
REQUIRED_CHILD_CAPABILITIES = frozenset({"github", "linear"})


def _capability_family_of(token: str) -> str | None:
    """Map a single permission pattern, MCP server name, or ``mcp list``
    line to the handoff capability family it names, or ``None``.
    Case-insensitive. ``linear`` is tested first so a token can only ever
    resolve to one family."""

    lowered = token.lower()
    if "linear" in lowered:
        return "linear"
    if "github" in lowered or "bash(gh" in lowered:
        return "github"
    return None


def _resolved_capability_families(
    settings_data: object, listing_text: str | None
) -> set[str]:
    """The handoff capability families the resolved user-scope surface
    GRANTS minus those it DENIES, unioned across the settings
    ``permissions.allow`` list, the ``mcpServers`` keys, and the
    exact-invocation ``mcp list`` output. Deny precedence mirrors Claude
    Code: an explicit family deny (or a catch-all ``*``) removes the family
    even when an allow entry or a configured server would otherwise grant
    it."""

    granted: set[str] = set()
    denied: set[str] = set()
    if isinstance(settings_data, dict):
        perms = settings_data.get("permissions")
        if isinstance(perms, dict):
            allow = perms.get("allow")
            if isinstance(allow, list):
                for token in allow:
                    if isinstance(token, str):
                        family = _capability_family_of(token)
                        if family:
                            granted.add(family)
            deny = perms.get("deny")
            if isinstance(deny, list):
                for token in deny:
                    if isinstance(token, str):
                        if token.strip() == "*":
                            denied |= set(REQUIRED_CHILD_CAPABILITIES)
                        else:
                            family = _capability_family_of(token)
                            if family:
                                denied.add(family)
        servers = settings_data.get("mcpServers")
        if isinstance(servers, dict):
            for name in servers:
                if isinstance(name, str):
                    family = _capability_family_of(name)
                    if family:
                        granted.add(family)
    if listing_text:
        for line in listing_text.splitlines():
            family = _capability_family_of(line)
            if family:
                granted.add(family)
    return granted - denied


class AttemptContainment:
    """Per-attempt descendant containment (r13 F8).

    On Linux with a writable cgroup v2 hierarchy, the runner creates a
    per-attempt cgroup and moves the
    PAUSED wrapper into it BEFORE sending the GO token (r14 F2: managed
    Keeper slots run ProtectControlGroups=yes with a read-only cgroup
    mount, so THOSE runs take the disclosed degraded path — the strict
    boundary activates only where the host delegates a writable
    hierarchy): the wrapper execs
    nothing until GO, descendants inherit membership, and a process
    cannot leave a cgroup by re-sessioning or double-forking. Extinction
    is proven by reading ``cgroup.procs`` and termination goes through
    ``cgroup.kill`` — no pid identity involved, so pid/pgid reuse and the
    between-snapshot setsid escape are structurally impossible inside the
    boundary. Hosts without delegation (macOS dev) DEGRADE to the
    snapshot+group proof with the reason recorded in
    ``in_flight.containment`` — disclosed, never silent.
    ``MONITOR_RUNNER_CGROUP_ROOT`` is the hermetic test seam: a plain tmpdir
    reaches the DEGRADE branches (missing root, un-creatable target, a
    directory the kernel never populated with ``cgroup.procs``) without root.
    The success branch needs a real delegated cgroup2 hierarchy — the kernel
    auto-creates ``cgroup.procs`` inside a freshly ``mkdir``-ed cgroup, which a
    plain filesystem cannot fake — so it is exercised only on a delegating
    host, never in the tmpdir seam.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def record(self) -> str:
        return f"cgroup:{self.path}"

    @staticmethod
    def create(attempt_id: str) -> "AttemptContainment | None":
        root = os.environ.get("MONITOR_RUNNER_CGROUP_ROOT", "/sys/fs/cgroup")
        base = Path(root)
        if not base.is_dir():
            return None
        target = base / f"autonomy-monitor-{os.getpid()}-{attempt_id[:12]}"
        try:
            target.mkdir(mode=0o755)
        except OSError:
            return None
        if not (target / "cgroup.procs").exists():
            # Not a real cgroup2 directory (mkdir on a plain fs creates an
            # empty dir) — remove and degrade.
            try:
                target.rmdir()
            except OSError:
                pass
            return None
        return AttemptContainment(target)

    def adopt(self, pid: int) -> bool:
        try:
            (self.path / "cgroup.procs").write_text(f"{pid}\n")
        except OSError:
            return False
        return True

    def live_pids(self) -> list[int]:
        """Fail-closed membership read — an unreadable boundary blocks."""

        try:
            raw = (self.path / "cgroup.procs").read_text()
        except OSError as error:
            raise RunnerExit(
                5,
                "blocked",
                "containment boundary unreadable"
                f" ({error.__class__.__name__}) at {self.path} — cannot"
                " prove attempt descendants extinct; needs a human",
            )
        pids: list[int] = []
        for line in raw.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids

    def kill(self) -> None:
        """Kill every member — cgroup.kill when present, else per-pid.

        Membership IS identity here: a pid read from ``cgroup.procs`` is a
        descendant of this attempt by construction, so the per-pid
        fallback needs no fingerprint check.
        """

        kill_file = self.path / "cgroup.kill"
        if kill_file.exists():
            try:
                kill_file.write_text("1\n")
                return
            except OSError:
                pass
        try:
            for pid in self.live_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        except RunnerExit:
            pass

    def remove(self) -> None:
        try:
            self.path.rmdir()
        except OSError:
            pass


# r14 F5: only GitHub's own URL shapes may bind a repository — the old
# suffix match mapped https://evil.example/Keeper-Dating/matchmaking.git
# and git@gitlab.com:Keeper-Dating/matchmaking.git to the trusted Keeper
# repository, which would arm the required-handoff manifest (and anything
# else keyed on the binding) off a foreign host's path.
_GITHUB_ORIGIN_FORMS = (
    re.compile(r"^git@github\.com:(?P<repo>[^/:\s]+/[^/:\s]+?)(?:\.git)?/?$", re.IGNORECASE),
    re.compile(r"^https://(?:[^@/\s]+@)?github\.com/(?P<repo>[^/:\s]+/[^/:\s]+?)(?:\.git)?/?$", re.IGNORECASE),
    re.compile(r"^ssh://git@github\.com(?::22)?/(?P<repo>[^/:\s]+/[^/:\s]+?)(?:\.git)?/?$", re.IGNORECASE),
)


def _repo_name_with_owner(url: str) -> str | None:
    """``owner/name`` from a GITHUB origin URL, or None otherwise.

    r14 F5: recognition is an allowlist of GitHub's own three shapes
    (scp-like ssh, https, ssh://) — any other host or shape maps to None,
    so a foreign remote can never masquerade as a trusted repository.
    None means unmapped for a fresh binding; an already-persisted binding
    disagreeing with a RESOLVABLE live origin fails closed instead
    (see current_block)."""

    text = url.strip()
    for form in _GITHUB_ORIGIN_FORMS:
        match = form.match(text)
        if match is not None:
            return match.group("repo")
    return None


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
    lines.append(f"  repository: {_render_scalar(block.get('repository'))}")
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
        # r13 F8: the containment record is optional (pre-upgrade blocks
        # lack it) — rendered first when present, deterministic either way.
        if "containment" in in_flight:
            lines.append(
                f"    containment: {_render_scalar(in_flight['containment'])}"
            )
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
    # admin#1495 r12 F19: a PRE-rename os.replace failure leaves the temp
    # this call created — remove it, but only if it still exists: after a
    # successful rename the temp path no longer names anything (a
    # post-rename directory-fsync failure has nothing to unlink, and the
    # committed target must never be touched by cleanup).
    try:
        durable_replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_parent(path: Path) -> bool:
    # R6-F14: fsync the parent directory so a rename itself survives a
    # power loss — a data fsync does not make the directory entry durable.
    # admin#1495 finding 3793025395: an fsync I/O failure is a DURABILITY
    # FAILURE and must surface. r11 finding 3825265246 CLOSED the errno
    # carve-out that remained: ENOTSUP/EINVAL/EACCES/EPERM read as
    # "successful" durability, so an unsupported or permission-limited
    # filesystem silently dropped the write-ahead guarantee the resume
    # trust model depends on. The state directory hosts the ONLY durable
    # record of possibly-fired external mutations — a platform that
    # cannot prove the rename durable fails closed; a deployment that
    # genuinely cannot fsync directories must relocate the state file,
    # not run unproven.
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(dir_fd)
    except OSError:
        return False
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


class _NoReplaceRenameUnsupported(RuntimeError):
    """The host exposes no atomic no-replace rename primitive, so the caller
    must fail closed rather than degrade to a lossy link+unlink."""


# admin#1495 finding F5: quarantining a sidecar must be ONE atomic no-replace
# move, never link-then-unlink. os.link + os.unlink leaves a window in which a
# same-UID writer replaces the SOURCE pathname between the two calls, so the
# unlink deletes the writer's newer evidence instead of the inode we linked. An
# atomic rename moves whichever inode the source names and drops the source
# name in the same syscall — a racing writer either loses to the rename (we
# quarantine its newer inode) or creates a fresh source name preserved for the
# next scan; either way no evidence is destroyed.
#
# UAPI/ABI constants for the primitives differ by platform and were verified
# against the live syscall: Linux renameat2 takes RENAME_NOREPLACE (0x1,
# linux/fs.h) with AT_FDCWD == -100; Darwin renamex_np takes RENAME_EXCL —
# 0x4 in sys/stdio.h, NOT the Linux 0x1 (0x2 there is RENAME_SWAP, which needs
# BOTH names to exist and returns ENOENT otherwise). renamex_np is fd-less, so
# Darwin never needs an AT_FDCWD value.
_RENAME_NOREPLACE = 1
_DARWIN_RENAME_EXCL = 0x4
_LINUX_AT_FDCWD = -100
# renameat2 syscall numbers by machine, used only when glibc predates the
# renameat2 wrapper (2.28); a modern GKE base image resolves the wrapper.
_LINUX_RENAMEAT2_NR = {
    "x86_64": 316,
    "aarch64": 276,
    "armv7l": 382,
    "armv8l": 382,
    "i386": 353,
    "i686": 353,
    "ppc64le": 357,
    "s390x": 347,
}


def _linux_renameat2(libc: ctypes.CDLL, src_b: bytes, dst_b: bytes) -> int:
    """Invoke Linux ``renameat2(RENAME_NOREPLACE)`` via the glibc wrapper when
    present, else the raw syscall for the running machine. Returns the raw
    return code (0 or -1 with errno set)."""

    wrapper = getattr(libc, "renameat2", None)
    if wrapper is not None:
        wrapper.restype = ctypes.c_int
        wrapper.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        ctypes.set_errno(0)
        return wrapper(
            _LINUX_AT_FDCWD, src_b, _LINUX_AT_FDCWD, dst_b, _RENAME_NOREPLACE
        )
    number = _LINUX_RENAMEAT2_NR.get(platform.machine())
    if number is None:
        raise _NoReplaceRenameUnsupported(
            f"renameat2 syscall number unknown for {platform.machine()}"
        )
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    # Variadic: pass ctypes-typed args so pointers are not truncated to int.
    return syscall(
        ctypes.c_long(number),
        ctypes.c_int(_LINUX_AT_FDCWD),
        ctypes.c_char_p(src_b),
        ctypes.c_int(_LINUX_AT_FDCWD),
        ctypes.c_char_p(dst_b),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )


def _rename_noreplace(src: Path, dst: Path) -> None:
    """Atomically rename ``src`` onto ``dst``, refusing to REPLACE an existing
    ``dst`` (raising ``FileExistsError``). Raises ``_NoReplaceRenameUnsupported``
    where the host has no such primitive (or the fs/kernel rejects the flag) so
    the caller fails closed instead of degrading to a lossy link+unlink."""

    src_b = os.fsencode(src)
    dst_b = os.fsencode(dst)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        fn = getattr(libc, "renamex_np", None)
        if fn is None:
            raise _NoReplaceRenameUnsupported("renamex_np unavailable")
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        ctypes.set_errno(0)
        rc = fn(src_b, dst_b, _DARWIN_RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        rc = _linux_renameat2(libc, src_b, dst_b)
    else:
        raise _NoReplaceRenameUnsupported(sys.platform)
    if rc == 0:
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(err, os.strerror(err), str(dst))
    if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
        # Kernel too old for renameat2, or the flag is unsupported by this
        # filesystem — fail closed, never fall back to the racy link+unlink.
        raise _NoReplaceRenameUnsupported(os.strerror(err))
    raise OSError(err, os.strerror(err), str(src))


def _quarantine_sidecar(sidecar: Path) -> Path | None:
    """Move ``sidecar`` aside under a no-clobber quarantine name (keeping the
    sidecar prefix so the retention scan and resume discovery still see it) and
    return the new path, or ``None`` when no atomic no-replace primitive is
    available, the source raced away, or the move failed. In EVERY ``None`` case
    the source is left exactly as found (an atomic rename either moves it wholly
    or does nothing), so no evidence is destroyed and the next scan retries. A
    counter suffix advances on a name collision with older quarantined
    evidence. Never falls back to link+unlink — that is the exact same-UID
    replacement race admin#1495 F5 closes."""

    for attempt in range(20):
        suffix = f".q{os.getpid()}" + (f"-{attempt}" if attempt else "")
        target = sidecar.with_name(sidecar.name + suffix)
        try:
            _rename_noreplace(sidecar, target)
        except FileExistsError:
            continue  # collided with older quarantined evidence — advance
        except _NoReplaceRenameUnsupported:
            return None  # no atomic primitive — fail closed, source untouched
        except OSError:
            return None  # vanished, raced, or unrenamable — source untouched
        # Best-effort durability. Unlike the canonical-state commit, a
        # quarantine that a crash reverts is safe: the sidecar reappears under
        # its ORIGINAL name and the next scan re-quarantines and re-parses it
        # idempotently, so an fsync failure here does not fail the run.
        if not _fsync_parent(target):
            _heartbeat(
                "sidecar quarantine parent fsync failed (move committed;"
                f" re-scan is idempotent): {target.name}"
            )
        return target
    return None


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


def _descendant_snapshot(root_pid: int) -> dict[int, dict[str, Any]]:
    """Best-effort ``{pid: {"pgid", "lstart"}}`` of live descendants.

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

    algo#1216 finding 3816160128: every snapshotted pid carries its
    ``lstart`` start-time fingerprint (the same identity source
    ``process_fingerprint`` uses for the child), so a long fork-heavy
    attempt whose pids get RECYCLED can never make liveness or cleanup
    treat an unrelated same-UID process as the recorded descendant.
    """

    try:
        completed = subprocess.run(
            [_resolve_system_binary("ps"), "-eo", "pid=,ppid=,pgid=,stat=,lstart="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    children: dict[int, list[tuple[int, int, str, str]]] = {}
    for row in completed.stdout.splitlines():
        parts = row.split()
        if len(parts) < 5:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(
            (pid, pgid, parts[3], " ".join(parts[4:]))
        )
    found: dict[int, dict[str, Any]] = {}
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, pgid, stat, lstart in children.get(parent, []):
            if pid in found:
                continue
            if not stat.startswith("Z"):
                found[pid] = {"pgid": pgid, "lstart": lstart}
            frontier.append(pid)
    return found


def _group_member_identities(pgid: int) -> dict[int, dict[str, Any]]:
    """Pre-reap group membership with lstart identities — best effort.

    r14 F21 companion: valid ONLY while the runner's own direct child is
    unreaped — its (possibly zombie) pid pins the group id against
    recycling, so every current member genuinely inherited membership
    from this tick's child. Captured members join the descendant snapshot
    with their fingerprints, which is what lets the extinction gate use
    identity-validated per-pid kills instead of a raw post-reap killpg.
    Failures return empty: this only ADDS coverage on top of the
    fail-closed gate.
    """

    try:
        completed = subprocess.run(
            [_resolve_system_binary("ps"), "-o", "pid=,stat=,lstart=", "-g", str(pgid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode not in (0, 1):
        return {}
    members: dict[int, dict[str, Any]] = {}
    for row in completed.stdout.splitlines():
        parts = row.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if parts[1].startswith("Z"):
            continue
        members[pid] = {"pgid": pgid, "lstart": " ".join(parts[2:])}
    return members


def _snapshot_identities(pids: list[int]) -> dict[int, tuple[str, str]]:
    """Bounded ``{pid: (stat, lstart)}`` over the given pids — fail-closed.

    Shared by snapshot liveness and the pre-signal validation (finding
    3816160128): an uninspectable table raises instead of answering,
    because a guess in either direction is wrong — reading as extinct
    launches a writer beside a possibly-live orphan, and signaling
    without identity can SIGKILL an unrelated recycled pid.
    """

    if not pids:
        return {}
    pid_args = ",".join(str(pid) for pid in sorted(set(pids)))
    try:
        completed = subprocess.run(
            [_resolve_system_binary("ps"), "-o", "pid=,stat=,lstart=", "-p", pid_args],
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
    identities: dict[int, tuple[str, str]] = {}
    for row in completed.stdout.splitlines():
        parts = row.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        identities[pid] = (parts[1], " ".join(parts[2:]))
    return identities


def _snapshot_entry(entry: Any) -> tuple[int | None, str | None]:
    """(pgid, lstart) from a snapshot value, tolerating the legacy int shape."""

    if isinstance(entry, dict):
        pgid = entry.get("pgid")
        lstart = entry.get("lstart")
        return (
            pgid if isinstance(pgid, int) else None,
            lstart if isinstance(lstart, str) and lstart else None,
        )
    if isinstance(entry, int):
        return entry, None
    return None, None


def _validated_kill_targets(
    snapshot: dict[int, Any],
    escaped: list[int],
    identities: dict[int, tuple[str, str]],
) -> tuple[list[int], list[int]]:
    """(pgids, pids) safe to signal — identity-validated only.

    algo#1216 finding 3816160128: ``killpg`` fires only after the CURRENT
    group leader (the process whose pid equals the pgid) matches the
    fingerprint the snapshot recorded for it; a recycled pgid, or one
    whose leader was never snapshotted, gets no group signal. A pid is
    signaled only when its current lstart matches its recorded one. A
    snapshot entry without a fingerprint (legacy shape, or ps omitted the
    column) can never validate — it is left to the fail-closed recheck,
    never guessed at.
    """

    pgids: list[int] = []
    pids: list[int] = []
    for pid in escaped:
        pgid, recorded_lstart = _snapshot_entry(snapshot.get(pid))
        current = identities.get(pid)
        if (
            current is not None
            and not current[0].startswith("Z")
            and recorded_lstart is not None
            and current[1] == recorded_lstart
        ):
            pids.append(pid)
        if pgid is None or pgid in pgids:
            continue
        _, leader_lstart = _snapshot_entry(snapshot.get(pgid))
        leader_current = identities.get(pgid)
        if (
            leader_current is not None
            and not leader_current[0].startswith("Z")
            and leader_lstart is not None
            and leader_current[1] == leader_lstart
        ):
            pgids.append(pgid)
    return pgids, pids


def _live_snapshot_pids(snapshot: dict[int, Any]) -> list[int]:
    """Fail-closed liveness over a recorded descendant snapshot.

    Mirrors ``_live_group_members``: an uninspectable table blocks rather
    than reading as extinction, because these pids were RECORDED live and
    only a trusted answer may clear them. Zombies do not count.

    algo#1216 finding 3816160128: a pid that exists but whose ``lstart``
    differs from the recorded fingerprint is a RECYCLED pid — an
    unrelated process, not the recorded descendant — so it is neither
    live evidence nor a kill target. An entry recorded WITHOUT a
    fingerprint stays fail-closed the other way: it counts as live on
    bare pid presence, exactly as before the fingerprint existed.
    """

    if not snapshot:
        return []
    identities = _snapshot_identities(list(snapshot))
    live: list[int] = []
    for pid in sorted(snapshot):
        current = identities.get(pid)
        if current is None or current[0].startswith("Z"):
            continue
        _, recorded_lstart = _snapshot_entry(snapshot.get(pid))
        if recorded_lstart is not None and current[1] != recorded_lstart:
            continue
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
        if offset is None:
            # r14 F7 re-eval: a late rate-limit marker (3000 pad chars
            # then "HTTP/2 429") was detected on the full line but the
            # retained excerpt dropped it — anchor on it like the
            # auth/exec markers so the downstream re-scan re-derives the
            # same ladder verdict.
            offset = rate_limit_offset(line)
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
    protocol: dict[str, Any] = {
        "session_id": None,
        "served_model": None,
        "result_text": None,
        "result_subtype": None,
        "result_is_error": None,
        "result_errors": [],
    }
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
            elif event.get("type") == "result":
                # algo#1216 r17 F7 (supersedes r14 F12's shape): the
                # official result union exposes the `result` string ONLY
                # on subtype=success — error variants carry the subtype
                # and errors[] WITHOUT it. Parse the discriminator FIRST
                # so an error result classifies as itself instead of
                # falling through to a generic no_verdict; the text stays
                # optional, and errors[] is retained byte-bounded and
                # publication-sanitized.
                protocol["result_subtype"] = (
                    event.get("subtype")
                    if isinstance(event.get("subtype"), str)
                    else None
                )
                protocol["result_is_error"] = (
                    event.get("is_error")
                    if isinstance(event.get("is_error"), bool)
                    else None
                )
                if isinstance(event.get("result"), str):
                    protocol["result_text"] = event["result"]
                raw_errors = event.get("errors")
                if isinstance(raw_errors, list):
                    protocol["result_errors"] = [
                        sanitize_for_publication(str(item))[:400]
                        for item in raw_errors[:5]
                    ]
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
                or rate_limit_offset(decoded) is not None
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
            except OSError:
                # r14 F4: a failed pipe (EIO on pty loss, forced close) is
                # the END of that stream, not a runner crash — treating it
                # as fatal escaped _drain_child with the child still
                # alive. The run_tick lifecycle boundary is the backstop
                # for anything that still propagates.
                selector.unregister(pipe)
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
                    if (
                        WRAPPER_EXEC_FAILED_MARKER in overflow
                        or _has_auth_signature(overflow)
                        # r14 F7 re-eval: a newline-free overflow can bury
                        # a genuine rate-limit marker exactly like an auth
                        # one — capture it sticky before truncation.
                        or rate_limit_offset(overflow) is not None
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
    # r14 F21 companion: one FINAL capture before anything can reap the
    # leader. Ancestry alone misses exit-time stragglers (children
    # reparent at parent DEATH, before any reap), so this reads GROUP
    # membership too — trustworthy exactly here, because the unreaped
    # (possibly zombie) leader still pins the group id against recycling.
    # Every member enters the snapshot WITH its lstart, so the
    # identity-validated per-pid kills cover them and the extinction gate
    # needs no raw post-reap killpg.
    descendant_snapshot.update(_descendant_snapshot(proc.pid))
    for member_pid, identity in _group_member_identities(proc.pid).items():
        descendant_snapshot.setdefault(member_pid, identity)
    if outcome != "clean":
        # r13 F3: only signal while the child is still OUR UNREAPED child —
        # Popen.returncode is None exactly while the kernel holds the pid
        # (and thus the group id) for us, so neither can have been
        # recycled. After a reap the numbers are reusable and a signal
        # could land on an unrelated same-UID process.
        if proc.returncode is None:
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
                # r13 F3: same unreaped-child guard as the non-clean kill
                # above (poll() just returned None, so this holds by
                # construction — the guard pins it structurally).
                if proc.returncode is None:
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
    result_text: str | None = None,
    result_is_error: bool | None = None,
    result_subtype: str | None = None,
    result_errors: list[str] | None = None,
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
    # r14 F12: a structured in-stream error verdict is classified FIRST —
    # it is trusted protocol metadata, and for providers that report
    # failures in-band (a quota event with EMPTY stderr was the repro)
    # the stderr-only path below sees nothing and decays the event into a
    # generic charge. The result text joins the classification input so
    # the rate-limit/auth matchers below see the in-band diagnostics.
    if result_is_error is True and isinstance(result_text, str):
        joined = joined + "\n" + result_text[:2000]
    # algo#1216 r17 F7: error variants carry their diagnostics in
    # errors[] with NO result text at all — join the bounded sanitized
    # entries so the auth/rate-limit matchers see the in-band
    # diagnostics for zero AND nonzero exits.
    if result_errors:
        joined = joined + "\n" + "\n".join(
            entry for entry in result_errors if isinstance(entry, str)
        )
    lowered = joined.lower()
    if WRAPPER_EXEC_FAILED_MARKER in joined:
        return ("block", "claude CLI binary could not be executed — install or fix PATH")
    if _has_auth_signature(joined):
        return ("block", "claude CLI authentication failure — re-authenticate the owner route")
    if resumed and any(hint in lowered for hint in _RESUME_NOT_FOUND_HINTS):
        return ("fresh_session", "monitor-child:resume_not_found")
    if rate_limit_offset(joined) is not None:
        return ("ladder", "monitor-child:rate_limited")
    # A structured error variant that matches no deterministic signature
    # is a classified execution failure, never a bare exit-code charge.
    if isinstance(result_subtype, str) and result_subtype.startswith("error"):
        return ("charge", f"monitor-child:result_{result_subtype}"[:80])
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
        # admin#1495 r12 F18: from the first created temp resource onward a
        # constructor failure never reaches main()'s finally (main holds no
        # instance yet) — guard locally, removing exactly THIS
        # construction's resources through the same cleanup main uses,
        # then re-raise. Never glob for siblings: other runners' temp
        # files are theirs.
        try:
            self._finish_construction(args)
        except BaseException:
            self.cleanup_wrapper_stage()
            raise

    def _finish_construction(self, args: argparse.Namespace) -> None:
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
        # paths in the prompt are absolute, so nothing else moves). A
        # missing-git host exits structured through _resolve_system_binary's
        # fail-closed RunnerExit (r17, admin#1495 3807823288); the OSError
        # arm below covers only probe execution faults.
        git_bin = _resolve_system_binary("git")
        try:
            # CodeRabbit 3787358695/3784681433: bounded like every other
            # subprocess site here — a hung git must not wedge __init__
            # before the lock or any heartbeat exists.
            root_probe = subprocess.run(
                [git_bin, "-C", str(self.state_path.parent), "rev-parse",
                 "--show-toplevel"],
                capture_output=True, text=True, timeout=30,
            )
            root = root_probe.stdout.strip()
            probe_ok = root_probe.returncode == 0 and bool(root)
        except (OSError, subprocess.TimeoutExpired):
            probe_ok = False
        self.child_cwd = root if probe_ok else str(self.state_path.parent)
        # algo#1216 finding 3813491661: resolve the workflow's repository
        # binding from the origin remote — runner-owned truth for the
        # required-handoff manifest, independent of anything the child
        # writes into the handoff blocks. Best-effort here; current_block
        # makes a successful resolution sticky in runner-owned state so a
        # later failed probe (or a child rewiring .git/config between
        # slices) never un-maps an already-mapped run.
        # r14 F5 re-eval: the probe result is TRI-STATE — a SUCCESSFUL
        # get-url whose origin is foreign (unparseable/other host) is a
        # trusted answer that the checkout points somewhere untrusted,
        # which must BLOCK against a persisted binding; only an actually
        # unavailable probe (nonzero exit, timeout, exec failure) leaves
        # the sticky binding standing.
        self.repository_probe: str = "unavailable"
        self.repository_hint: str | None = None
        if probe_ok:
            try:
                origin_probe = subprocess.run(
                    [git_bin, "-C", root, "remote", "get-url", "origin"],
                    capture_output=True, text=True, timeout=30,
                )
                if origin_probe.returncode == 0:
                    self.repository_hint = _repo_name_with_owner(
                        origin_probe.stdout
                    )
                    self.repository_probe = (
                        "resolved" if self.repository_hint else "foreign"
                    )
            except (OSError, subprocess.TimeoutExpired):
                self.repository_probe = "unavailable"
                self.repository_hint = None
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
        self.attempt_containment: AttemptContainment | None = None
        # admin#1495 F4: the drain's descendant snapshot, stashed per tick so
        # the structured-exit boundary and the backstop can run identity-safe
        # extinction even for an exit raised BEFORE the normal post-drain
        # block reached it (the early _require_unmutated_canonical gate).
        self._descendant_snapshot: dict[int, Any] = {}
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
        # algo#1216 finding 3813491661 + r14 F5: the repository binding is
        # STICKY runner-owned state — a later FAILED probe (or a child
        # rewiring .git/config between slices) never downgrades it back to
        # unmapped. But stickiness covers only probe UNAVAILABILITY: when
        # the live origin RESOLVES and disagrees with the persisted
        # binding, that is not drift to paper over — it is either a child
        # rewrite or an operator re-pointing the checkout, and both need a
        # human before any further binding-keyed decision.
        persisted_repo = base.get("repository")
        if not (isinstance(persisted_repo, str) and persisted_repo):
            base["repository"] = self.repository_hint
        elif self.repository_probe == "foreign":
            # r14 F5 re-eval: the probe SUCCEEDED and the origin is not a
            # trusted GitHub shape — that is a rewired checkout, not an
            # unavailable answer, and stickiness must not paper over it.
            raise RunnerExit(
                5,
                "blocked",
                "persisted repository binding"
                f" {persisted_repo!r} but the live origin resolves to an"
                " untrusted remote — a rewired .git/config (or re-pointed"
                " checkout) must be reconciled by a human; verify the"
                " origin URL and the monitor_cli.repository record, then"
                " resume",
            )
        elif (
            self.repository_hint is not None
            and self.repository_hint.casefold() != persisted_repo.casefold()
        ):
            raise RunnerExit(
                5,
                "blocked",
                "persisted repository binding"
                f" {persisted_repo!r} disagrees with the live origin"
                f" {self.repository_hint!r} — a rewired remote (or a"
                " re-pointed checkout) must be reconciled by a human;"
                " verify .git/config and the monitor_cli.repository"
                " record, then resume",
            )
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

    def _scan_sidecars(self, limit: int) -> tuple[list[Path], bool]:
        """Bounded single-pass sidecar enumeration (admin#1495 finding
        3813789211).

        Streams ``os.scandir`` entries matching either preserved-sidecar
        name shape and stops as soon as ``limit + 1`` matches are seen,
        so an unbounded accumulation costs O(limit) memory instead of a
        full directory materialization while the monitor lock is held.
        Returns ``(matches, exceeded)``; ``matches`` is meaningful only
        when ``exceeded`` is False.
        """

        failed_prefix = self.state_path.stem + ".failed-candidate"
        attempt_prefix = self.state_path.name + ".attempt-"
        matches: list[Path] = []
        try:
            with os.scandir(self.state_path.parent) as entries:
                for entry in entries:
                    name = entry.name
                    if not (
                        name.startswith(failed_prefix)
                        or name.startswith(attempt_prefix)
                    ):
                        continue
                    matches.append(self.state_path.parent / name)
                    if len(matches) > limit:
                        return matches, True
        except OSError as error:
            # admin#1495 finding 3816225740: an unenumerable state
            # directory must BLOCK, not read as "no sidecars" — the
            # sidecars this gate reconciles are the only durable record of
            # external mutations a dead child may have fired, and a
            # degraded filesystem silently bypassing the gate would launch
            # another write-capable child over them.
            raise RunnerExit(
                5,
                "blocked",
                "sidecar scan failed"
                f" ({error.__class__.__name__}) — cannot enumerate the"
                " state directory, so preserved write-ahead evidence may"
                " be invisible; fix the filesystem and resume",
            )
        return matches, False

    def recover_in_flight(self, extract: dict[str, Any]) -> None:
        """State-writing half of recovery: discard candidates and charge the
        unknown-outcome budget. Runs only on a VALID state (writes go through
        the same splice/commit path as everything else); the no-signal
        extinction check already ran immediately after the validity gate,
        before the taint and capability gates (admin#1495 r12 F6)."""

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
        # admin#1495 finding 3813789211 (recovery half): the recorded
        # candidate is recovered by its EXACT constructed path — no
        # directory walk — and stray cleanup streams os.scandir entries
        # one at a time, so recovery memory stays O(1) regardless of how
        # many strays accumulated.
        recorded_name: str | None = None
        if (
            isinstance(recorded_attempt, str)
            and re.fullmatch(r"[0-9a-f]{32}", recorded_attempt) is not None
        ):
            recorded_name = (
                self.state_path.name + f".attempt-{recorded_attempt}.md"
            )
            recorded_path = self.state_path.parent / recorded_name
            if recorded_path.exists():
                self._preserve_failed(recorded_path)
                preserved_any = True
        attempt_prefix = self.state_path.name + ".attempt-"
        try:
            with os.scandir(self.state_path.parent) as entries:
                for entry in entries:
                    if not entry.name.startswith(attempt_prefix):
                        continue
                    if recorded_name is not None and entry.name == recorded_name:
                        continue
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass
        if not preserved_any:
            # r11 finding 3825265254: the candidate is the ONLY durable
            # record of external mutations the dead child may have fired.
            # Accept its absence only when a PRIOR recovery already
            # preserved it (the failed-candidate sidecar — including an
            # F17 quarantine — exists for this attempt); otherwise the
            # remote outcome is unknowable by construction, and clearing
            # in_flight would hand the next child a blank slate to REPLAY
            # reviews/assignments/comments/ticket moves. Block for
            # explicit remote reconciliation instead.
            already_preserved = False
            if isinstance(recorded_attempt, str) and recorded_attempt:
                sidecar_base = self.state_path.with_suffix(
                    f".failed-candidate-{recorded_attempt}.md"
                )
                already_preserved = sidecar_base.exists() or any(
                    sidecar_base.parent.glob(sidecar_base.name + ".q*")
                )
            if not already_preserved:
                raise RunnerExit(
                    5,
                    "blocked",
                    "recovery found a recorded in-flight attempt"
                    f" ({recorded_attempt!r}) with NO candidate and no"
                    " preserved sidecar — the dead child may have fired"
                    " external mutations whose only record never became"
                    " durable. Verify the remote postconditions for every"
                    " pending handoff operation (GitHub review/assignee/"
                    "comment state, Linear ticket state) per the Resume"
                    " trust model, record the observed outcomes in"
                    " operation_results, then clear monitor_cli.in_flight"
                    " to resume",
                )
        _heartbeat(
            "recovery: unknown prior attempt reconciled ("
            + ("candidate preserved for reconciliation" if preserved_any else "candidate previously preserved; proceeding")
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
        # r13 F8: containment attaches while the wrapper is PAUSED — the
        # GO token has not been sent, so nothing has executed and every
        # future descendant inherits membership. A host without cgroup v2
        # delegation records the degraded mode instead, never silently —
        # for UNMAPPED repositories only; a mapped repository fails closed
        # below before GO (r17 F9 / r13 F3).
        containment = AttemptContainment.create(attempt_id)
        containment_record = (
            containment.record
            if containment is not None
            else "degraded:no-cgroup-v2-delegation"
        )
        if containment is not None and not containment.adopt(proc.pid):
            containment.remove()
            containment = None
            containment_record = "degraded:cgroup-adopt-failed"
        # algo#1216 r17 F9 / admin#1495 r13 F3 (package half): a MAPPED
        # repository's monitor child is a write-capable managed run, and
        # the snapshot fallback's between-snapshot double-fork/setsid
        # escape is not enforceable containment. Degraded containment
        # therefore fails CLOSED here — before the GO token, while the
        # paused wrapper has executed nothing: close stdin (no GO), reap
        # the wrapper, and block naming the host obligation. Unmapped
        # repositories retain the documented degraded operation; the
        # host-delegated-cgroup provisioning itself stays a host contract.
        if containment is None and self._bound_mapped_repository(extract):
            try:
                proc.stdin.close()
            except OSError:
                pass
            if not _bounded_reap(proc):
                raise RunnerExit(
                    5,
                    "blocked",
                    "paused launch wrapper could not be reaped while"
                    " refusing an uncontained mapped launch — a"
                    " possibly-live process needs a human",
                )
            raise RunnerExit(
                5,
                "blocked",
                f"cgroup v2 delegation is unavailable ({containment_record})"
                " and this repository is Keeper-mapped — a write-capable"
                " managed launch requires enforceable per-attempt"
                " containment (the snapshot fallback admits a"
                " between-snapshot setsid escape); provision delegated"
                " cgroups (MONITOR_RUNNER_CGROUP_ROOT) or run this"
                " workflow on a delegating host",
            )
        self.attempt_containment = containment
        # R6-F15: one instant defines both the persisted deadline_at and the
        # enforced monotonic deadline, so the record matches enforcement.
        deadline_monotonic = time.monotonic() + max(0.0, ceiling)
        deadline_wall = datetime.now(timezone.utc) + timedelta(seconds=max(0, ceiling))
        block = self.current_block(extract)
        block["owner_model"] = self.owner_model
        block["in_flight"] = {
            "containment": containment_record,
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
            self._containment_cleanup_if_empty()
            raise RunnerExit(
                4,
                "suspect_state",
                "canonical state failed validation between launch commit and"
                " GO — unknown writer; reconcile per the Resume trust model",
            )
        self.launch_block = committed["monitor_cli"]
        self.launch_base_digest = committed["digest"]
        # r14 F10: never `assert` in production paths (vanishes under -O),
        # and (re-eval) this branch is cleanup-aware: the paused wrapper is
        # killed and reaped and an empty containment removed before the
        # structured stop — it executes nothing without GO, so the kill is
        # safe by construction.
        if proc.stdin is None:
            try:
                proc.kill()
            except OSError:
                pass
            _bounded_reap(proc)
            self._containment_cleanup_if_empty()
            raise RunnerExit(
                4,
                "suspect_state",
                "launch wrapper has no stdin pipe — runner defect; the GO"
                " barrier cannot operate",
            )
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
            self._containment_cleanup_if_empty()
            fresh = self.schema.extract(self.state_path)
            self._require_unmutated_canonical(fresh, candidate)
            self._preserve_failed(candidate)
            self.charge_failure(fresh, "monitor-child:go_write_failed")
            return "retry"
        # r14 F4: ONE lifecycle boundary for everything after GO — an
        # exception that escapes supervision (a pipe or selector failure, a
        # protocol surprise) must never strand a live write-capable child
        # behind a raw traceback. admin#1495 F4: a structured RunnerExit is
        # NOT self-sufficient here — one can be raised BEFORE the normal
        # extinction block ran (the early _require_unmutated_canonical gate),
        # so BOTH except arms now run the common containment/descendant
        # extinction before releasing: the structured arm re-raises the
        # ORIGINAL error once extinction is proven, and any OTHER escape
        # routes into the backstop, which additionally boundedly reaps,
        # preserves evidence, and refuses to return without extinction proof.
        self._descendant_snapshot = {}
        try:
            drained = _drain_child(
                proc,
                idle_timeout=self.child_idle_timeout,
                deadline=deadline_monotonic,
            )
            # Stash the ancestry snapshot the moment it exists, so a structured
            # exit raised anywhere below (the early _require_unmutated_canonical
            # gate included) routes through identity-safe extinction with the
            # full re-sessioned-escapee set, not just the live process group.
            self._descendant_snapshot = drained.get("descendant_snapshot") or {}
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
            # r13 F8: when the attempt ran inside a cgroup boundary, the
            # containment membership IS the extinction proof and the kill
            # authority — pid identity never enters into it, and a setsid or
            # double-fork descendant orphaned between snapshots cannot leave
            # the boundary. The legacy snapshot+group proof still runs as
            # defense in depth (it is cheap and covers the degraded mode).
            # The containment boundary and the identity-safe descendant sweep
            # are the COMMON extinction — extracted to _extinguish_containment
            # and _extinguish_child_descendants so the structured-exit boundary
            # (admin#1495 F4) and the non-RunnerExit backstop run the exact same
            # proof instead of a weaker or absent one. Those methods carry the
            # r14 F21 / algo#1216 3816160128 / r13 F8 mechanics they replaced.
            if not self._extinguish_containment():
                self._preserve_failed(candidate)
                raise RunnerExit(
                    5,
                    "blocked",
                    "attempt-containment members survived cgroup kill — a"
                    " possibly-live writer needs a human",
                )
            snapshot = drained.get("descendant_snapshot") or {}
            if _live_group_members(child_pgid) or _live_snapshot_pids(snapshot):
                if not self._extinguish_child_descendants(
                    child_pgid, fingerprint, snapshot
                ):
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
            # r14 F12 re-eval: a structured error can ride a CLEAN exit 0
            # (type=result, subtype=success, is_error=true) — gating on
            # the metadata only inside the non-clean branch let that shape
            # fall through to verdict parsing and become a generic
            # no_verdict charge. Classify the structured error FIRST,
            # before the outcome/verdict split, whatever the exit code.
            protocol_meta = drained.get("protocol") or {}
            # algo#1216 r17 F7: an error VARIANT (subtype error_*) is a
            # structured error even when is_error is absent and no result
            # text exists — the union exposes `result` only for success.
            structured_error = protocol_meta.get("result_is_error") is True or (
                isinstance(protocol_meta.get("result_subtype"), str)
                and protocol_meta["result_subtype"].startswith("error")
            )
            if (
                structured_error
                and drained["outcome"] == "clean"
                and drained["exit_code"] == 0
            ):
                self._preserve_failed(candidate)
                action, detail = classify_child_failure(
                    0,
                    drained["stderr_tail"],
                    resumed,
                    result_text=protocol_meta.get("result_text"),
                    result_is_error=True,
                    result_subtype=protocol_meta.get("result_subtype"),
                    result_errors=protocol_meta.get("result_errors"),
                )
                if action == "block":
                    self._clear_in_flight(fresh)
                    raise RunnerExit(5, "blocked", detail)
                if action == "fresh_session":
                    _heartbeat(
                        "resume target gone — clearing session for a fresh"
                        " owner child"
                    )
                    self.child_session_id = None
                    self._clear_in_flight(fresh)
                    return "retry_now"
                if action == "ladder":
                    self._clear_in_flight(fresh)
                    return "retry"
                self.charge_failure(fresh, detail)
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
                    result_text=drained.get("protocol", {}).get("result_text"),
                    result_is_error=drained.get("protocol", {}).get(
                        "result_is_error"
                    ),
                    result_subtype=drained.get("protocol", {}).get(
                        "result_subtype"
                    ),
                    result_errors=drained.get("protocol", {}).get(
                        "result_errors"
                    ),
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

        except RunnerExit as exc:
            # admin#1495 F4: a structured post-GO exit is NOT proof the child
            # was handled — one can be raised BEFORE the normal extinction
            # block reached it (the early _require_unmutated_canonical gate is
            # the reproduced path), and the prior handler only swept an
            # already-empty containment, releasing the monitor with a
            # credentialed same-group or re-sessioned descendant still alive.
            # Run the SAME identity-safe extinction the normal path uses
            # (idempotent when it already ran: a removed containment and a dead
            # group are both no-ops), then rethrow the ORIGINAL error so its
            # outcome and candidate semantics are preserved (a suspect_state
            # exit still discards its candidate — this arm never preserves).
            # If a containment member or descendant cannot be proven extinct, a
            # possibly-live writer supersedes the original exit.
            containment_extinct = self._extinguish_containment()
            descendants_extinct = self._extinguish_child_descendants(
                child_pgid, fingerprint, self._descendant_snapshot
            )
            if not (containment_extinct and descendants_extinct):
                raise RunnerExit(
                    5,
                    "blocked",
                    "a monitor-child descendant or containment member could not"
                    " be proven extinct after a structured post-GO exit"
                    f" ({exc.reason}) — a possibly-live writer needs a human",
                ) from exc
            raise
        except BaseException as error:
            self._post_go_backstop(proc, child_pgid, fingerprint, candidate, error)
            raise  # unreachable: the backstop always raises


    def _extinguish_containment(self) -> bool:
        """Kill any live member of the per-attempt cgroup boundary, prove it
        empty, and remove it. Returns True when the boundary is provably
        empty (removed on the way out, ``attempt_containment`` cleared), or
        was never established; False when members outlived the bounded kill.

        r13 F8 mechanics, extracted (admin#1495 F4) so the normal post-drain
        path, the structured-exit boundary, and the non-RunnerExit backstop
        run the SAME containment proof instead of three divergent copies.
        When a cgroup boundary was used its membership IS the extinction proof
        and the kill authority — pid identity never enters into it, and a
        setsid or double-fork descendant orphaned between snapshots cannot
        leave it. Callers own preservation and the block message; an
        unreadable boundary raises from ``live_pids()`` and propagates (never
        read as extinction), and is left in place for the human.
        """

        containment = self.attempt_containment
        if containment is None:
            return True
        if containment.live_pids():
            containment.kill()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and containment.live_pids():
                time.sleep(0.3)
            if containment.live_pids():
                return False
        # r14 F6: remove the boundary once (and only once) extinction is
        # proven; a boundary that turns unreadable here raises from
        # live_pids() and is preserved rather than removed.
        try:
            if not containment.live_pids():
                containment.remove()
                self.attempt_containment = None
        except RunnerExit:
            pass
        return True

    def _extinguish_child_descendants(
        self,
        child_pgid: int | None,
        fingerprint: str | None,
        snapshot: dict[int, Any],
    ) -> bool:
        """Identity-safe extinction of surviving descendants — same-group
        members AND re-sessioned escapees the group id can no longer see.
        Returns True when neither cohort is live (nothing to do, or the kill
        proved them gone), False when a survivor outlived the bounded recheck.

        r14 F21 / algo#1216 3816160128 / #3551 3808151914 mechanics, extracted
        (admin#1495 F4) so the structured-exit boundary and the backstop reuse
        the SAME proof the normal path uses instead of a weaker or absent one.
        After ``_drain_child`` reaped the leader the group id is pinned only
        while a member still holds it, so a raw killpg is unsafe: a pgid or
        pid is signaled ONLY when its CURRENT identity still matches the
        launch snapshot, so a recycled same-UID number is never SIGKILLed.
        Uninspectable process tables fail closed (the ps helpers raise),
        never reading as extinction. Callers own preservation and the block
        message.
        """

        escaped = _live_snapshot_pids(snapshot)
        group_live = (
            _live_group_members(child_pgid) if child_pgid is not None else []
        )
        if not (group_live or escaped):
            return True
        if child_pgid is not None:
            leader_now = _snapshot_identities([child_pgid]).get(child_pgid)
            if (
                leader_now is not None
                and not leader_now[0].startswith("Z")
                and fingerprint is not None
                and leader_now[1] == fingerprint
            ):
                try:
                    os.killpg(child_pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        kill_pgids, kill_pids = _validated_kill_targets(
            snapshot,
            escaped,
            _snapshot_identities(
                sorted(
                    set(escaped)
                    | {
                        _snapshot_entry(snapshot.get(pid))[0]
                        for pid in escaped
                        if _snapshot_entry(snapshot.get(pid))[0] is not None
                    }
                )
            ),
        )
        for target in kill_pgids:
            try:
                os.killpg(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in kill_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        recheck_deadline = time.monotonic() + 15
        while time.monotonic() < recheck_deadline and (
            (child_pgid is not None and _live_group_members(child_pgid))
            or _live_snapshot_pids(snapshot)
        ):
            time.sleep(0.3)
        remaining_group = (
            _live_group_members(child_pgid) if child_pgid is not None else []
        )
        return not (remaining_group or _live_snapshot_pids(snapshot))

    def _containment_cleanup_if_empty(self) -> None:
        """r14 F6 (re-eval; admin#1495 F4 narrowed the claim): the pre-GO and
        GO-boundary abort paths remove a provably-empty per-attempt cgroup
        after the caller has already killed and reaped the paused wrapper —
        which executed nothing without GO, so the boundary is empty by
        construction. Post-GO paths do NOT route through here: both the
        structured-exit boundary and the backstop run _extinguish_containment,
        which kills surviving members first. Never kills (each caller owns its
        child handling); never removes an unreadable or still-populated
        boundary."""

        containment = self.attempt_containment
        if containment is None:
            return
        try:
            if not containment.live_pids():
                containment.remove()
                self.attempt_containment = None
        except RunnerExit:
            pass

    def _post_go_backstop(
        self,
        proc: subprocess.Popen,
        child_pgid: int | None,
        fingerprint: str | None,
        candidate: Path,
        error: BaseException,
    ) -> None:
        """r14 F4 (re-eval restored this — the r22 head CALLED it without
        defining it, lost reimplementing after a /tmp purge; the
        source-shape regression now pins call-and-definition together).

        Terminal cleanup for a non-RunnerExit escape after GO: terminate
        through the containment boundary and identity-bound signals only,
        boundedly reap the direct child, preserve the write-ahead
        candidate, and raise a structured RunnerExit — extinction proven,
        or blocking because it could not be. Never returns.

        admin#1495 F4: runs the SAME _extinguish_containment /
        _extinguish_child_descendants proof the normal path and the
        structured-exit boundary use, so a non-RunnerExit escape now also
        extinguishes re-sessioned escapees the raw group id can no longer
        see — the prior inline sweep signaled only the live process group.
        The pinned kill of our own still-held child stays first and separate:
        while we hold the Popen the kernel pins its pid, so killpg(proc.pid)
        is authorized without the fingerprint dance the reaped-leader case
        needs.
        """

        snapshot = getattr(self, "_descendant_snapshot", {}) or {}
        if proc.returncode is None:
            # Our own unreaped child: pid/pgid pinned by the kernel.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        containment_extinct = self._extinguish_containment()
        descendants_extinct = self._extinguish_child_descendants(
            child_pgid, fingerprint, snapshot
        )
        reaped = _bounded_reap(proc)
        try:
            self._preserve_failed(candidate)
        except (OSError, RunnerExit):
            pass
        if not (containment_extinct and descendants_extinct) or not reaped:
            raise RunnerExit(
                5,
                "blocked",
                "post-GO supervision failed"
                f" ({error.__class__.__name__}: {error}) and extinction"
                " could not be proven — a possibly-live writer needs a"
                " human; the write-ahead candidate is preserved",
            )
        raise RunnerExit(
            5,
            "blocked",
            "post-GO supervision failed"
            f" ({error.__class__.__name__}: {error}) — the child was"
            " terminated and proven extinct; the write-ahead candidate is"
            " preserved for resume reconciliation per the Resume trust"
            " model",
        )

    def _child_capability_probe(self, extract: dict[str, Any]) -> None:
        """admin#1495 finding 3825265272 + algo#1216 F3: fail FAST when the
        child's resolved user-scope capability surface cannot execute the
        mapped handoffs, instead of stranding after PR creation.

        Deterministic and model-free. The mapped Phase-6 handoffs emit
        ``*.github.*`` and ``*.linear.*`` operations, so a mapped run needs
        BOTH families in ``REQUIRED_CHILD_CAPABILITIES``. This extracts the
        families the resolved surface actually GRANTS (minus denials) from
        the user settings (``permissions.allow`` + ``mcpServers``) and, only
        when that is insufficient, completes the surface with the
        exact-invocation ``claude --setting-sources user mcp list`` the child
        itself would resolve — then blocks naming the MISSING families. The
        r25 probe passed on any truthy ``permissions`` object or any MCP
        server; a deny-all policy or an unrelated server slipped through.
        Mapped repositories only — an unmapped dev run uses the developer's
        own settings, and a genuinely read-only scheduled run never reaches
        this mutation path, so scheduled-readonly behavior is preserved. The
        package self-provisions NOTHING: this only reports what the host
        supplied, and the immutable per-profile descriptor remains a host
        contract.
        """

        bound = self._bound_repository(extract)
        if not (
            isinstance(bound, str)
            and bound.casefold() in MAPPED_QA_REPOSITORIES
        ):
            return
        settings_override = os.environ.get("MONITOR_RUNNER_USER_SETTINGS")
        settings_path = (
            Path(settings_override)
            if settings_override
            else Path.home() / ".claude" / "settings.json"
        )
        settings_data: object = None
        try:
            settings_data = json.loads(
                _read_regular_file(settings_path, 1_048_576).decode("utf-8")
            )
        except (OSError, ValueError, RunnerExit):
            settings_data = None
        if REQUIRED_CHILD_CAPABILITIES <= _resolved_capability_families(
            settings_data, None
        ):
            return
        # Insufficient from settings alone — complete the surface with the
        # exact-invocation MCP discovery the child itself resolves.
        listing: str | None = None
        try:
            completed = subprocess.run(
                [self.claude_bin, "--setting-sources", "user", "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if completed.returncode == 0:
                listing = completed.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            listing = None
        resolved = _resolved_capability_families(settings_data, listing)
        if REQUIRED_CHILD_CAPABILITIES <= resolved:
            return
        missing = ", ".join(sorted(REQUIRED_CHILD_CAPABILITIES - resolved))
        raise RunnerExit(
            5,
            "blocked",
            "child capability probe failed for a mapped repository"
            f" ({bound}): the user-scope surface the --setting-sources user"
            f" child resolves grants no {missing} capability, so Phase 6"
            " handoffs would strand after PR creation (admin#1495 finding"
            " 3825265272 / algo#1216 F3). The HOST must supply a trusted"
            " least-privilege user-scope policy naming the GitHub+Linear"
            " handoff surface (permission allowlist and/or MCP config) — the"
            " package deliberately never self-provisions it, and the"
            " immutable per-profile descriptor remains a host contract",
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
        # R2 re-reply 3792845972: enforce count and byte ceilings BEFORE any
        # parsing or compaction — schema-extracting every globbed sidecar
        # first made startup work unbounded (one arbitrarily large failed
        # candidate, or an unbounded accumulation, costs a full parse each).
        # admin#1495 finding 3813789211: the enumeration itself is bounded
        # too — glob() materialized every match under the monitor lock
        # (128 paths walked before the block fired in R2's repro), so the
        # scan streams os.scandir entries and stops at limit + 1: the gate
        # only needs "over the cap", never the full census.
        found, exceeded = self._scan_sidecars(self.SIDECAR_RETENTION_LIMIT)
        # admin#1495 r12 F16: over-limit is NOT an immediate block — the
        # 21st sidecar used to block BEFORE valid no-intent records could
        # compact, stranding runs whose accumulation was exactly the
        # compactable kind. The bounded batch already enumerated (limit + 1
        # entries, each parse capped by the byte ceiling, so startup work
        # stays bounded per invocation) runs through the classification
        # below FIRST, letting no-intent and redundant terminal-only
        # sidecars compact; each deletion is durable progress. The recheck
        # after the loop rescans and blocks only when the directory is
        # STILL over the limit — re-running the gate then continues
        # compaction from where this batch left off.
        sidecars = sorted(found)
        for sidecar in sidecars:
            # r14 F17: QUARANTINE before stat/parse. The old flow parsed
            # via a no-follow descriptor but later unlinked the original
            # PATHNAME — a child replacing the file between parse and
            # unlink got its substituted evidence deleted on the strength
            # of the benign content that was parsed. The atomic no-replace
            # move binds every later decision to the inode that was actually
            # read; the quarantine name keeps the sidecar prefix, so the
            # retention scan, resume's pattern discovery, and a
            # crash-mid-compaction recovery all still see it. A kept
            # sidecar simply stays under its quarantine name — renaming
            # back could clobber a newer file at the original name.
            # admin#1495 F5: the move is ONE atomic rename (renameat2 /
            # renamex_np), never link+unlink — link+unlink left a window in
            # which a same-UID writer replaced the source pathname between
            # the two calls and had its newer evidence unlinked. The atomic
            # rename has no such intermediate state (never both names on one
            # inode), is NO-CLOBBER via RENAME_NOREPLACE/RENAME_EXCL, and on
            # a host with no such primitive fails closed (None) rather than
            # degrading to the racy fallback. See _quarantine_sidecar.
            quarantined = _quarantine_sidecar(sidecar)
            if quarantined is None:
                # Vanished, raced, unrenamable, or no atomic primitive: the
                # source is untouched, nothing was deleted — record and move
                # on, fail-closed.
                unreadable_sidecars.append(sidecar.name)
                continue
            sidecar = quarantined
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
        if exceeded:
            _, still_exceeded = self._scan_sidecars(
                self.SIDECAR_RETENTION_LIMIT
            )
            if still_exceeded:
                raise RunnerExit(
                    5,
                    "blocked",
                    f"more than {self.SIDECAR_RETENTION_LIMIT} preserved"
                    " sidecars remain after bounded batch compaction —"
                    " compacted deletions are durable progress, so"
                    " re-running the gate continues from here; reconcile"
                    " and delete the remainder per state-and-safety.md"
                    " (bound enforced per batch: limit + 1 enumerated,"
                    " each parse under the byte ceiling)",
                )

    def _bound_repository(self, extract: dict[str, Any]) -> str | None:
        """The bound repository: persisted ``monitor_cli.repository``, else
        the live origin hint."""

        cli = extract.get("monitor_cli")
        persisted = cli.get("repository") if isinstance(cli, dict) else None
        return (
            persisted
            if isinstance(persisted, str) and persisted
            else self.repository_hint
        )

    def _bound_mapped_repository(self, extract: dict[str, Any]) -> bool:
        """The bound repository is one of the Keeper-mapped QA
        repositories — the shared write-capable-managed-run predicate
        consumed by the capability probe and the r17 F9 containment
        gate."""

        bound = self._bound_repository(extract)
        return (
            isinstance(bound, str)
            and bound.casefold() in MAPPED_QA_REPOSITORIES
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
        """Remove the runner-lifetime staged files: the wrapper stage file
        and the child skill snapshot directory. Called from main()'s
        finally AND from the __init__ construction guard (admin#1495 r12
        F18) — a partially constructed runner may not have created both
        yet, so each attribute is guarded.

        Best-effort by design: both carry unpredictable names, so a leak on
        a hard kill is bounded and harmless."""
        stage = getattr(self, "wrapper_stage_path", None)
        if isinstance(stage, Path):
            try:
                stage.unlink()
            except OSError:
                pass
        snapshot = getattr(self, "child_skill_dir", None)
        if isinstance(snapshot, Path):
            shutil.rmtree(snapshot, ignore_errors=True)

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
        # algo#1216 finding 3813491642: enforce the cumulative work cap
        # here, before any other acceptance math — a candidate past
        # MAX_WORK_ITERATIONS may only be the documented blocked
        # transition, so a runaway monitor converts to a human stop
        # instead of a green exit. Cross-slice by construction: the
        # counter is cumulative canonical state, not per-slice.
        iterations_after = counters_after.get("monitor_iterations", 0)
        over_cap = (
            isinstance(iterations_after, int)
            and iterations_after > MAX_WORK_ITERATIONS
        )
        if over_cap and outcome != "blocked":
            self._preserve_failed(candidate)
            self.charge_failure(fresh, "monitor-child:work_cap_exceeded")
            return "retry"
        # algo#1216 finding 3813491661: repository-bound required-handoff
        # manifest. For a mapped repository a terminal candidate must show
        # the clean-exit QA handoff actually planned — both aggregates
        # idle on a Keeper run meant completion was reported without
        # assigning QA, moving the ticket, or recording any handoff
        # artifact. Idle stays valid for unmapped repositories.
        if outcome == "terminal":
            launch_cli = fresh.get("monitor_cli")
            persisted_repo = (
                launch_cli.get("repository")
                if isinstance(launch_cli, dict)
                else None
            )
            bound_repo = (
                persisted_repo
                if isinstance(persisted_repo, str) and persisted_repo
                else self.repository_hint
            )
            qa_status = (
                candidate_extract.get("handoff_status_by_kind") or {}
            ).get("qa")
            if _terminal_missing_planned_qa(bound_repo, qa_status):
                self._preserve_failed(candidate)
                self.charge_failure(fresh, "monitor-child:handoff_missing")
                return "retry"
        # r11 finding 3825265263: the binding must also be COMPARED — a
        # handoff persisting a DIFFERENT repository than the one this
        # runner is bound to is a cross-repository mutation record
        # (mistaken or hostile child output) and must never commit,
        # whatever its status. Applies to every candidate, not just
        # terminal ones: a pending foreign-repo handoff is a mutation
        # plan aimed at another repository.
        launch_cli_binding = fresh.get("monitor_cli")
        bound_repo = (
            launch_cli_binding.get("repository")
            if isinstance(launch_cli_binding, dict)
            else None
        )
        if not (isinstance(bound_repo, str) and bound_repo):
            bound_repo = self.repository_hint
        if isinstance(bound_repo, str) and bound_repo:
            for kind, handoff_repo in (
                candidate_extract.get("handoff_bindings") or {}
            ).items():
                if (
                    isinstance(handoff_repo, str)
                    and handoff_repo
                    and handoff_repo.casefold() != bound_repo.casefold()
                ):
                    self._preserve_failed(candidate)
                    self.charge_failure(
                        fresh, "monitor-child:handoff_repo_mismatch"
                    )
                    return "retry"
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
        # algo#1216 r16 F11: destructive post-deploy entries surfaced by
        # the extract are NAMED DEFERRED WORK the PR body must carry - an
        # evidence requirement at the terminal commit, deliberately not a
        # hold (finding 3813789228: holding would force destructive DDL
        # under the still-deployed old code). A terminal candidate with a
        # nonempty merge_readiness_post_deploy must ledger a COMPLETE
        # head-bound deferred-work artifact record (the anchored PR-body
        # list) whose evidence names exactly the extracted entries at the
        # candidate's own observed head. A missing record, a stale head,
        # or a drifted list rejects the candidate for the child to
        # remediate.
        if outcome == "terminal":
            post_deploy = (
                candidate_extract.get("merge_readiness_post_deploy") or []
            )
            if post_deploy:
                head = candidate_extract.get("last_observed_head_sha")
                evidence_map = (
                    candidate_extract.get("deferred_work_evidence") or {}
                )
                recorded = (
                    evidence_map.get(head) if isinstance(head, str) else None
                )
                exact = (
                    isinstance(recorded, list)
                    and all(isinstance(item, str) for item in recorded)
                    and sorted(recorded) == list(post_deploy)
                )
                if not exact:
                    self._preserve_failed(candidate)
                    self.charge_failure(
                        fresh, "monitor-child:deferred_work_unrecorded"
                    )
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
        # R2 #1328 finding 3767068789 + r11 finding 3825265235: the
        # canonical re-check must sit IMMEDIATELY before the promotion —
        # the old order checked first and then rewrote/fsynced the
        # candidate, leaving that whole I/O window for a concurrent
        # canonical update to land and be clobbered with terminal
        # success. So: STAGE the finalized bytes into the candidate inode
        # first, then re-read canonical and verify the lock, then promote
        # that exact staged inode with nothing but the checks between.
        # Any drift is an unknown writer and stops the runner as suspect
        # state, never a clobber. The residual check-to-rename instant is
        # enforced by the kernel lock; this check is its tripwire.
        atomic_write(candidate, finalized)
        last_look = self.schema.extract(self.state_path)
        self._require_unmutated_canonical(last_look, candidate)
        self._verify_lock_inode()
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
            # algo#1216 r17 F1 (sibling site): os.write may write SHORT —
            # a truncated wrapper would exec a prefix of the trusted
            # source. Loop to completion.
            view = memoryview(self.wrapper_source)
            while view:
                view = view[os.write(stage_fd, view):]
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
        # admin#1495 r12 F6: the READ-ONLY no-signal liveness check runs
        # IMMEDIATELY after the validity gate, before the taint and
        # capability gates — a persistently failing gate must never hide
        # an already-live write-capable child behind its own block; the
        # live-child report outranks every gate. Signal authority is
        # unchanged (none), and an invalid state still only NOTES the
        # record above — no liveness conclusion is drawn from untrusted
        # state.
        if isinstance(prior_in_flight, dict):
            self._reconcile_recorded_orphan(prior_in_flight)
        # R6-F5: the taint gate runs before ANY write-capable child can
        # launch — recovery's ledger write is state-local and safe, but no
        # tick may start on taint-flagged state without the explicit
        # operator acknowledgment.
        self._gate_taint(extract)
        # finding 3825265272: capability fail-fast before any child launch.
        self._child_capability_probe(extract)
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
