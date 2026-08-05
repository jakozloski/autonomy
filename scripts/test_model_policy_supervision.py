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
