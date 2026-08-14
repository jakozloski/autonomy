#!/usr/bin/env python3
"""Launch-barrier wrapper for the owner-pinned monitor child.

Lives in its own file for the same structural rule that splits the
subprocess-using test files from the evaluators: the scanner treats
process-replacement (`os.execvp`) and subprocess orchestration in ONE file
as a dangerous combination, so the runner (subprocess, no exec) spawns this
wrapper (exec, no subprocess).

Contract (see monitor_runner.py's write protocol): the wrapper execs its
argv ONLY after the runner has persisted the spawn registration and sent
the GO token; EOF or any other token means the runner died or aborted
before registering this process — exit WITHOUT executing the model.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        separator = sys.argv.index("--")
    except ValueError:
        return 2
    argv = sys.argv[separator + 1 :]
    if not argv:
        return 2
    token = sys.stdin.readline()
    if token.strip() != "GO":
        return 0
    try:
        os.execvp(argv[0], argv)
    except OSError as error:
        # R6-F7: a deterministic launch failure must surface as the marker
        # the runner classifies into an immediate actionable block — a raw
        # traceback here was charged as generic retryable exit_1 and burned
        # the three-attempt child budget before blocking. The literal
        # mirrors monitor_runner.WRAPPER_EXEC_FAILED_MARKER (this file may
        # import nothing that pulls subprocess next to exec).
        print(
            f"MONITOR-WRAPPER-EXEC-FAILED: {argv[0]!r}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 127
    return 127  # pragma: no cover — execvp does not return


if __name__ == "__main__":
    sys.exit(main())
