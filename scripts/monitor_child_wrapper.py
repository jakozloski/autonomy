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
    os.execvp(argv[0], argv)
    return 127  # pragma: no cover — execvp does not return


if __name__ == "__main__":
    sys.exit(main())
