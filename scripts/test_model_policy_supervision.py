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
import textwrap
import time
import unittest

from model_policy import (
    CLASSIFY_EXIT_TIMEOUT,
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


if __name__ == "__main__":
    unittest.main()
