"""Package-wide undefined-global gate (algo#1216 r18 F1 / admin#1495 r14 F6).

A function body that references a module global the module never defines
imports fine and fails only at call time — and when the call site sits
inside a broad ``except``, the NameError is silently converted into the
handler's failure path (`os.killpg` without ``import os`` turned every
process-group kill into ``internal_failure``). This is the Ruff F821 class
of defect, enforced here with the standard library's ``symtable`` scope
analysis so the gate runs in every consumer repository's CI through plain
``unittest discover``, with no linter installation required.

Mechanism: for every package module, build its symbol table, walk every
nested function scope, and require each name the scope resolves as a
GLOBAL reference to be either a builtin, a module attribute that exists
after import, or a name some function assigns via ``global``. Module-level
statements are exempt by construction — a genuinely missing name there
already fails the import this gate performs.
"""

from __future__ import annotations

import builtins
import importlib
import symtable
import unittest
from pathlib import Path

PACKAGE_SCRIPTS_DIR = Path(__file__).resolve().parent


def _walk_scopes(root: symtable.SymbolTable) -> list[symtable.SymbolTable]:
    """Every scope reachable from ``root``, root included. Bounded like
    every loop in this package: a source file's scope tree is finite; the
    explicit stack just linearizes it."""

    seen: list[symtable.SymbolTable] = []
    stack = [root]
    while stack:
        scope = stack.pop()
        seen.append(scope)
        stack.extend(scope.get_children())
    return seen


def _global_name_uses(source: str, filename: str) -> tuple[set[str], set[str]]:
    """(names resolved as global references in nested scopes, names a
    ``global`` statement assigns)."""

    used: set[str] = set()
    declared: set[str] = set()
    root = symtable.symtable(source, filename, "exec")
    for scope in _walk_scopes(root):
        module_scope = scope is root
        for symbol in scope.get_symbols():
            if not symbol.is_global():
                continue
            if symbol.is_declared_global() and symbol.is_assigned():
                declared.add(symbol.get_name())
            if module_scope:
                # Module scope resolves at import; a missing name there
                # already fails the import this gate performs anyway.
                continue
            if symbol.is_referenced():
                used.add(symbol.get_name())
    return used, declared


def _undefined_globals(module_name: str, path: Path) -> set[str]:
    module = importlib.import_module(module_name)
    used, declared = _global_name_uses(
        path.read_text(encoding="utf-8"), str(path)
    )
    defined = set(vars(module)) | set(dir(builtins)) | declared
    return used - defined


class TestNoUndefinedModuleGlobals(unittest.TestCase):
    def test_every_package_module_resolves_every_global(self) -> None:
        failures: list[str] = []
        for path in sorted(PACKAGE_SCRIPTS_DIR.glob("*.py")):
            missing = _undefined_globals(path.stem, path)
            for name in sorted(missing):
                failures.append(f"{path.name}: {name}")
        self.assertEqual(
            failures,
            [],
            "function bodies reference globals their module never defines"
            " (the F821 class that turned os.killpg into a NameError inside"
            f" a broad except): {failures}",
        )

    def test_the_gate_itself_flags_a_missing_global(self) -> None:
        # The gate must be able to fail: a synthetic module whose function
        # loads an undefined global is exactly the defect class under test.
        used, declared = _global_name_uses(
            "def broken():\n    return os.getpid()\n", "<synthetic>"
        )
        self.assertIn("os", used)
        self.assertEqual(declared, set())

    def test_global_statement_assignments_are_not_false_positives(self) -> None:
        used, declared = _global_name_uses(
            "def writer():\n    global cache\n    cache = 1\n"
            "def reader():\n    return cache\n",
            "<synthetic>",
        )
        self.assertIn("cache", declared)
        # reader's global reference to `cache` must be satisfied by
        # writer's declared assignment, not reported as undefined.
        self.assertIn("cache", used)


if __name__ == "__main__":
    unittest.main()
