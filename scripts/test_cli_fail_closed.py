"""CLI fail-closed tests that must live apart from the API test files.

These tests run the helper CLIs as real subprocesses.  The repository's AI
Skill Security Scan flags any single file that pairs ``subprocess`` with an
eval-substring call name (``evaluate_model_policy``, ``evaluate_state_text``),
so the structural rule — same as the live-supervision split in
``test_model_policy_supervision.py`` — is to keep subprocess usage in files
that import nothing from the package under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


class SanitizeCliTests(unittest.TestCase):
    """admin#1495 finding 3813789220: the publication-sanitizer chokepoint
    must be invokable exactly as the prompt-trail contract states —
    ``model_policy.py --sanitize < raw > clean``. Subprocess-based, so it
    lives here per this file's structural rule."""

    def test_sanitize_mode_round_trips_stdin(self) -> None:
        raw = (
            "please deploy with Authorization: Bearer abc123def456ghi789 "
            "and mail alice@example.com; keep this-ordinary-text"
        )
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "model_policy.py"), "--sanitize"],
            input=raw.encode("utf-8"),
            capture_output=True,
            timeout=120,
        )
        stdout = completed.stdout.decode("utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[REDACTED: authorization_bearer_header]", stdout)
        self.assertIn("[REDACTED: email_address]", stdout)
        self.assertIn("keep this-ordinary-text", stdout)
        self.assertNotIn("abc123def456ghi789", stdout)


class CliFailClosedTests(unittest.TestCase):
    """Non-UTF-8 stdin must produce the blocked envelope, never a traceback."""

    def _run(self, script: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script)],
            input=b"\xff\xfe not json",
            capture_output=True,
            timeout=120,
        )

    def test_model_policy_non_utf8_stdin_fails_closed_with_envelope(self) -> None:
        completed = self._run("model_policy.py")
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(payload["state"], "blocked")
        self.assertTrue(payload["errors"])

    def test_handoff_decision_non_utf8_stdin_fails_closed_with_blocked_plan(self) -> None:
        completed = self._run("handoff_decision.py")
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(payload["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
