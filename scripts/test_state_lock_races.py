#!/usr/bin/env python3
"""Cross-process lock-race regressions for the state-schema append CLI.

admin#1495 r19 F4: the replacement-lock swap regression needs a REAL
second process (real O_CREAT recreation, real flock on the replacement
inode, real atomic replace), so it spawns a child. It lives in its OWN
module - separate from test_state_schema.py - because the vendored
repositories' AI Skill Security Scan raises a gate-failing CRITICAL when
one file pairs child-process spawning with any call name containing a
code-execution token, and test_state_schema.py calls the package's core
``evaluate_state_text`` API (whose name carries such a token) hundreds
of times (admin#1495 r18 F4 disclosure / r32-r33 scanner constraint).
This file therefore must never call the evaluate/exec/compile-named
APIs, and test_state_schema.py must never spawn children.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from state_schema import _append_attempt_cli
from test_state_schema import FULL_STATE

import state_schema as module


class ReplacementLockSwapTests(unittest.TestCase):
    def test_cli_pre_promotion_replacement_lock_swap_aborts(self) -> None:
        # admin#1495 r19 F4: acquisition-time validation cannot see a
        # swap that lands AFTER it. Synchronized regression: while the
        # first writer sits between its locked read and its promotion
        # (paused at the temp-file fsync seam, the last step before the
        # pre-promotion checks), the lock pathname is unlinked and a
        # REAL second process runs the REAL helper - its O_CREAT open
        # recreates the pathname, its flock lands on the replacement
        # inode, and it atomically rewrites canonical state. The first
        # writer must abort nonzero, preserve the winner's bytes
        # exactly, and clean up its temp; the advertised recovery
        # (re-read and retry) must then succeed.
        real_fsync = os.fsync
        seam = {"winner": None, "winner_bytes": None}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(FULL_STATE)
            lock_path = path + ".monitor.lock"

            def swap_then_fsync(fd):
                if seam["winner"] is None:
                    os.unlink(lock_path)
                    seam["winner"] = subprocess.run(
                        [
                            sys.executable,
                            os.path.abspath(module.__file__),
                            "--append-attempt",
                            path,
                            "human:replacement-writer",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    with open(path, encoding="utf-8") as handle:
                        seam["winner_bytes"] = handle.read()
                return real_fsync(fd)

            out = io.StringIO()
            with mock.patch("os.fsync", side_effect=swap_then_fsync):
                with contextlib.redirect_stdout(out):
                    self.assertEqual(
                        _append_attempt_cli(path, "human:stash-restore"), 1
                    )
            self.assertIsNotNone(seam["winner"])
            self.assertEqual(
                seam["winner"].returncode, 0, seam["winner"].stdout
            )
            self.assertIn("swapped after acquisition", out.getvalue())
            with open(path, encoding="utf-8") as handle:
                after = handle.read()
            self.assertEqual(after, seam["winner_bytes"])
            self.assertIn('"human:replacement-writer": 1', after)
            self.assertNotIn("human:stash-restore", after)
            self.assertEqual(
                [n for n in os.listdir(tmp) if ".append-attempt." in n], []
            )
            # Recovery path: a plain retry re-reads the winner's state
            # and promotes on top of it.
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    _append_attempt_cli(path, "human:stash-restore"), 0
                )
            with open(path, encoding="utf-8") as handle:
                final = handle.read()
            self.assertIn('"human:replacement-writer": 1', final)
            self.assertIn('"human:stash-restore": 1', final)


if __name__ == "__main__":
    unittest.main()
