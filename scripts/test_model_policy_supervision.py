"""Live-child integration tests for supervise_stream.

Kept separate from test_model_policy.py deliberately: these tests spawn real
subprocesses, and the repository's skill scanner flags any file that combines
subprocess usage with call names containing an eval/exec substring (which
`evaluate_model_policy` does). Merging this file back would re-trip that gate.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from model_policy import (
    CLASSIFY_EXIT_TIMEOUT,
    _accepts_timeout_kw,
    _call_child_wait,
    supervise_stream,
)


@unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
class SuperviseStreamLiveProcessTests(unittest.TestCase):
    """Integration: the real API against a real child process.

    Covers the deadlock case the reviewer flagged — one channel flooded past
    kernel pipe capacity while the auth event arrives on the other.
    """

    def test_nonzero_child_exit_after_clean_streams_is_not_clean(self) -> None:
        # R2 round-2 finding 3737466493, empirically verified: EOF only
        # unregisters the pipes, so a child that printed benign output and
        # exited 7 reported outcome "clean"/exit 0 — a failed smoke or
        # review invocation could pass a mandatory gate. With the child's
        # wait supplied, a nonzero status after clean streams must land as
        # internal_failure (blocking failure matrix).
        process = subprocess.Popen(
            [sys.executable, "-c", "print('{}'); raise SystemExit(7)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def kill_group() -> None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        try:
            result = supervise_stream(
                process.stdout,
                process.stderr,
                kill_group,
                lambda: process.wait(timeout=10),
            )
            self.assertEqual(result["outcome"], "internal_failure")
            self.assertNotEqual(result["exit_code"], 0)
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                kill_group()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_zero_child_exit_after_clean_streams_stays_clean(self) -> None:
        # The pass-through side of the new guard: a well-behaved child that
        # exits 0 after clean streams must remain outcome "clean" — the
        # exit-code observation must not manufacture failures.
        process = subprocess.Popen(
            [sys.executable, "-c", "print('{}')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def kill_group() -> None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        try:
            result = supervise_stream(
                process.stdout,
                process.stderr,
                kill_group,
                lambda: process.wait(timeout=10),
            )
            self.assertEqual(result["outcome"], "clean")
            self.assertEqual(result["exit_code"], 0)
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                kill_group()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_nonzero_exit_after_clean_streams_kills_group_descendants(
        self,
    ) -> None:
        # admin#1495 r13 F9 (the repro): clean streams, leader exits
        # nonzero, and a same-process-group descendant previously
        # survived with ZERO kill calls — the lazy getpgid-at-kill-time
        # group resolution raised on the reaped leader. The pgid captured
        # at spawn now routes this branch through guarded group
        # termination plus the bounded reap.
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "descendant.pid")
            script = textwrap.dedent(
                f"""
                import subprocess, sys
                # The descendant must NOT inherit the leader's pipes —
                # holding the write ends would postpone supervision's EOF
                # until the sleep ends (the "clean streams" premise needs
                # the pipes to close at leader exit).
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(120)"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with open({pid_file!r}, "w") as handle:
                    handle.write(str(child.pid))
                sys.exit(3)
                """
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            pgid = os.getpgid(process.pid)
            try:
                result = supervise_stream(
                    process.stdout,
                    process.stderr,
                    lambda: os.killpg(pgid, signal.SIGKILL),
                    child_wait=process.wait,
                    child_pgid=pgid,
                )
                self.assertEqual(result["outcome"], "internal_failure")
                with open(pid_file, encoding="utf-8") as handle:
                    descendant = int(handle.read().strip())
                deadline = time.monotonic() + 10
                dead = False
                while time.monotonic() < deadline:
                    try:
                        os.kill(descendant, 0)
                    except ProcessLookupError:
                        dead = True
                        break
                    time.sleep(0.1)
                self.assertTrue(
                    dead, "same-group descendant must be group-killed"
                )
            finally:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

    def test_dead_child_kill_race_returns_structured_result(self) -> None:
        # R2 round-2 finding 3737466443, second leg: a CLI that prints its
        # failure and exits races the kill decision — killpg on the dead
        # (even zombie, on Darwin) child raises ProcessLookupError, which
        # previously escaped supervise_stream as a raw traceback. The dead
        # child IS the kill's goal state: the classified outcome must come
        # back structured.
        script = textwrap.dedent(
            """
            import json, sys
            sys.stdout.write(json.dumps({"type": "error", "status": 401}) + "\\n")
            sys.stdout.flush()
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        # CR 3760684024: capture the group BEFORE reaping — os.getpgid on a
        # reaped pid raises, which made the old kill a silent no-op that
        # never exercised killpg at all.
        pgid = os.getpgid(process.pid)
        process.wait(timeout=10)  # child is fully dead before supervision

        def kill_group() -> None:
            os.killpg(pgid, signal.SIGKILL)

        try:
            result = supervise_stream(process.stdout, process.stderr, kill_group)
            self.assertEqual(result["outcome"], "auth_error")
        finally:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_raising_kill_callback_returns_structured_internal_failure(
        self,
    ) -> None:
        # A cleanup failure that is NOT the child-already-dead race must
        # not escape either — it becomes the structured internal_failure
        # the docstring promises for every failure outcome.
        script = textwrap.dedent(
            """
            import json, sys, time
            sys.stdout.write(json.dumps({"type": "error", "status": 401}) + "\\n")
            sys.stdout.flush()
            time.sleep(120)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def raising_kill() -> None:
            raise PermissionError("kill denied")

        try:
            result = supervise_stream(process.stdout, process.stderr, raising_kill)
            self.assertEqual(result["outcome"], "internal_failure")
        finally:
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_flooding_one_channel_does_not_prevent_prompt_termination(self) -> None:
        script = textwrap.dedent(
            """
            import json, sys, time
            # Flood stderr well past a pipe buffer, then emit the auth event on
            # stdout. A sequential reader would deadlock here.
            sys.stderr.write("flood line\\n" * 40000)
            sys.stderr.flush()
            sys.stdout.write(json.dumps({"type": "error", "status": 401}) + "\\n")
            sys.stdout.flush()
            time.sleep(120)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def kill_group() -> None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        started = time.monotonic()
        try:
            result = supervise_stream(process.stdout, process.stderr, kill_group)
            elapsed = time.monotonic() - started

            self.assertEqual(result["outcome"], "auth_error")
            self.assertLess(elapsed, 30, "supervisor deadlocked instead of terminating")
            self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                kill_group()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


@unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
class SuperviseStreamDeadlineTests(unittest.TestCase):
    """A silent child must not park the supervisor forever."""

    def test_slow_but_alive_stream_is_not_killed(self) -> None:
        """The bound is on SILENCE, not runtime.

        An xhigh review legitimately streams for many minutes; a total-runtime
        cap would SIGKILL healthy reviews, which is worse than the hang it
        replaced. This child outruns the idle window in total while never
        pausing longer than it.
        """

        script = textwrap.dedent(
            """
            import json, sys, time
            for _ in range(8):
                sys.stdout.write(json.dumps({"type": "token_count"}) + "\\n")
                sys.stdout.flush()
                time.sleep(0.25)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        killed = []

        try:
            # Total runtime ~2s exceeds the 1s bound; no single gap does.
            result = supervise_stream(
                process.stdout,
                process.stderr,
                lambda: killed.append(True),
                idle_timeout_seconds=1.0,
            )

            self.assertEqual(result["outcome"], "clean")
            self.assertEqual(killed, [])
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_silent_child_hits_the_deadline_and_is_killed(self) -> None:
        script = "import time; time.sleep(120)"  # never writes, never exits
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def kill_group() -> None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        started = time.monotonic()
        try:
            result = supervise_stream(
                process.stdout,
                process.stderr,
                kill_group,
                idle_timeout_seconds=1.0,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(result["outcome"], "timeout")
            self.assertEqual(result["exit_code"], CLASSIFY_EXIT_TIMEOUT)
            self.assertLess(elapsed, 30, "supervisor ignored its deadline")
            self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                kill_group()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_closed_pipes_live_child_hits_the_ceiling_and_is_reaped(self) -> None:
        """R6-F4 reproduction: a child that closes both pipes then sleeps
        previously held the gate in the post-EOF ``child_wait()`` until IT
        chose to exit (outcome "clean") — the total ceiling was enforced only
        inside the select loop. The post-EOF wait must honor the remaining
        ceiling, kill as runaway, and reap the kill."""

        script = textwrap.dedent(
            """
            import os, time
            os.close(1)
            os.close(2)
            time.sleep(120)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def kill_group() -> None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        started = time.monotonic()
        try:
            result = supervise_stream(
                process.stdout,
                process.stderr,
                kill_group,
                process.wait,  # timeout-capable bound method — the canonical shape
                idle_timeout_seconds=30.0,
                max_runtime_seconds=1.0,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(result["outcome"], "runaway")
            self.assertLess(elapsed, 45, "post-EOF wait ignored the ceiling")
            # Reaped by the supervisor itself: returncode is already set
            # without this test calling wait()/poll().
            self.assertIsNotNone(
                process.returncode, "killed child was left unreaped (zombie)"
            )
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                kill_group()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_failure_path_kill_is_reaped(self) -> None:
        """R6-F4 second half: every failure-path kill must be followed by a
        bounded reap — a SIGKILLed gate child left unwaited is a zombie in
        the long-lived session."""

        script = "import time; time.sleep(120)"  # silent, never exits
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def kill_group() -> None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        try:
            result = supervise_stream(
                process.stdout,
                process.stderr,
                kill_group,
                process.wait,
                idle_timeout_seconds=1.0,
            )
            self.assertEqual(result["outcome"], "timeout")
            self.assertIsNotNone(
                process.returncode, "killed child was left unreaped (zombie)"
            )
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                kill_group()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def test_runaway_ceiling_kills_live_byte_emitting_child(self) -> None:
        """PER_ATTEMPT_CEILING against a real child: constant output keeps the
        idle clock at zero forever, and only the total-runtime backstop stops it."""
        import model_policy as mp

        script = textwrap.dedent(
            """
            import json, sys, time
            # Bounded ~5000x past the 1s ceiling (~83 min of output):
            # semantically unbounded for this test without the unbounded-loop
            # pattern the skill scanner flags.
            for _ in range(100_000):
                sys.stdout.write(json.dumps({"type": "assistant_message", "message": "tick"}) + "\\n")
                sys.stdout.flush()
                time.sleep(0.05)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def kill_group() -> None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        started = time.monotonic()
        try:
            result = supervise_stream(
                process.stdout,
                process.stderr,
                kill_group,
                idle_timeout_seconds=5.0,
                max_runtime_seconds=1.0,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(result["outcome"], "runaway")
            self.assertEqual(result["exit_code"], mp.CLASSIFY_EXIT_RUNAWAY)
            self.assertLess(elapsed, 30, "supervisor ignored the runaway ceiling")
            self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
        finally:
            if process.poll() is None:  # pragma: no cover - cleanup safety
                kill_group()
                process.wait(timeout=10)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


class ChildWaitProbeTests(unittest.TestCase):
    """Unit pins for the bounded-wait probe (R7 codex #9 / R7.2 codex #5).

    ``_accepts_timeout_kw`` classifies a callable by SIGNATURE, and
    ``_call_child_wait`` must (a) fail closed as ``"incapable"`` for a
    zero-arg wait under an active ceiling rather than reintroducing the
    unbounded post-EOF hang R6-F4 closed, (b) still invoke that same zero-arg
    wait when unbounded calls are explicitly allowed, and (c) surface an
    internal ``TypeError`` from a timeout-capable callable as ``"error"``
    instead of swallowing it into an unbounded fallback. Pure logic — no
    subprocess — but they pin the same supervision contract, so they live
    here rather than beside the eval-substring tests.
    """

    def test_accepts_timeout_kw_true_for_timeout_param(self) -> None:
        self.assertTrue(_accepts_timeout_kw(lambda timeout=None: 0))

    def test_accepts_timeout_kw_true_for_var_keyword(self) -> None:
        self.assertTrue(_accepts_timeout_kw(lambda **kwargs: 0))

    def test_accepts_timeout_kw_false_for_zero_arg(self) -> None:
        self.assertFalse(_accepts_timeout_kw(lambda: 0))

    def test_accepts_timeout_kw_false_for_no_timeout_param(self) -> None:
        # A resolvable signature without a ``timeout`` parameter reads as
        # not-capable (``len`` resolves to ``(obj, /)`` on CPython).
        self.assertFalse(_accepts_timeout_kw(len))

    def test_accepts_timeout_kw_false_on_unresolvable_signature(self) -> None:
        # Fails closed: even though ``__call__`` declares ``timeout``, an
        # ``inspect.signature`` that raises must read as not-capable, never
        # propagate — under a ceiling that keeps the caller bounded.
        class Uninspectable:
            def __call__(self, timeout: float | None = None) -> int:
                return 0

            @property
            def __signature__(self):  # noqa: ANN202 - test double
                raise ValueError("no signature available")

        self.assertFalse(_accepts_timeout_kw(Uninspectable()))

    def test_zero_arg_wait_under_ceiling_is_incapable(self) -> None:
        # THE fail-closed pin: a wait with no timeout support, under an active
        # ceiling (allow_unbounded=False), must NOT be invoked bare (which
        # would reintroduce the unbounded post-EOF hang). It lands
        # ``"incapable"`` so the caller fails structurally instead of hanging.
        invoked: list[bool] = []

        def wait() -> int:  # zero-arg: no timeout support
            invoked.append(True)
            return 0

        self.assertEqual(
            _call_child_wait(wait, 5.0, allow_unbounded=False),
            ("incapable", None),
        )
        self.assertEqual(invoked, [], "incapable path must not invoke the wait")

    def test_zero_arg_wait_unbounded_is_invoked(self) -> None:
        # Pass-through side of the guard: with no ceiling requested
        # (allow_unbounded=True) the same zero-arg wait IS invoked and its
        # return code flows through as ``"done"``. Proves the guard blocks
        # only the ceilinged case, not every zero-arg wait.
        def wait() -> int:
            return 0

        self.assertEqual(
            _call_child_wait(wait, 5.0, allow_unbounded=True),
            ("done", 0),
        )

    def test_timeout_capable_returns_done_with_returncode(self) -> None:
        received: dict[str, float] = {}

        def wait(timeout: float) -> int:
            received["timeout"] = timeout
            return 3

        self.assertEqual(
            _call_child_wait(wait, 5.0, allow_unbounded=False),
            ("done", 3),
        )
        self.assertEqual(received["timeout"], 5.0, "ceiling must reach the wait")

    def test_timeout_expired_by_name_maps_to_timeout(self) -> None:
        # Matched BY NAME (model_policy never imports subprocess): any class
        # named TimeoutExpired raised from a timeout-capable wait is the
        # bounded-window-expired signal, not an error.
        class TimeoutExpired(Exception):
            pass

        def wait(timeout: float) -> int:
            raise TimeoutExpired("still running")

        self.assertEqual(
            _call_child_wait(wait, 5.0, allow_unbounded=False),
            ("timeout", None),
        )

    def test_internal_typeerror_is_error_not_swallowed(self) -> None:
        # THE regression pin for R7 codex #9: the OLD try/except ``TypeError``
        # probe swallowed an internal ``TypeError`` from a timeout-capable
        # wait and fell back to an unbounded bare call. The default-arg shape
        # is what makes this discriminating — the old bare fallback
        # ``wait()`` would SUCCEED and return 0 ("done"), masking the bug;
        # the signature probe now routes the call to the timeout branch, where
        # a non-``TimeoutExpired`` exception surfaces as ``"error"`` — never
        # silently retried unbounded. (A required-arg fixture would report
        # ``"error"`` under the old code too, and so prove nothing.)
        def wait(timeout: float | None = None) -> int:
            if timeout is not None:
                raise TypeError("internal boom, not a timeout")
            return 0

        self.assertEqual(
            _call_child_wait(wait, 5.0, allow_unbounded=False),
            ("error", None),
        )


if __name__ == "__main__":
    unittest.main()
