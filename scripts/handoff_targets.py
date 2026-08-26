#!/usr/bin/env python3
"""Shared handoff-target facts - an evaluator-free dependency leaf.

admin#1495 r19 F7: the monitor runner independently restated two
planner-owned facts - the Linear-mapped repository membership and the
canonical operation-family shapes - and the parity tests covered only
repository equality plus one normal plan with subset assertions, leaving
reviewer-only and outage variants free to drift inside terminal
acceptance. Both facts now live HERE, once; the planner
(handoff_decision) and the runner (monitor_runner) import this module
instead of restating them, and the parity tests compare the planner's
actually-minted families against these declarations bidirectionally per
shape class. The schema (state_schema) is the one consumer whose
deployment mode forbids a runtime import: the runner's SchemaCli feeds
its SOURCE to ``python -I -S -`` over stdin (no ``__file__``, no
package directory on ``sys.path`` - the C-F1 pinned-bytes design), so
its vocabulary dicts stay boot-self-contained and bind to this leaf
through comment plus test parity instead.

Deliberately import-light: stdlib-only, with NO imports from any other
package module. The importing consumers pull this leaf in, so an import
in the other direction would be a cycle, and the runner's structural
rule (a subprocess-using file never imports the package evaluators)
requires the leaf to stay evaluator-free. Isolated boot is preserved:
each importing consumer already inserts its own directory at
``sys.path[0]`` before sibling imports (monitor_runner's model_policy
import, handoff_decision's state_schema import), so this module
resolves under direct script invocation from any cwd.

Ownership split: this leaf owns the NAME facts - membership, family
names, canonical leg shapes. handoff_decision stays authoritative for
the QA OWNER values (its map derives its key set from
``LINEAR_MAPPED_REPOSITORIES``), and state_schema stays authoritative
for the operation-ID grammar and each family's identity arity.
"""

from __future__ import annotations

# The Linear-mapped repositories - the workflows whose clean-exit QA
# handoff carries a Linear tracker leg. Canonical nameWithOwner
# spellings: handoff_decision keys its owner map on these EXACTLY (a
# basename or case-drifted match would hand off forks), while the
# runner consumes the casefolded identity set below for membership
# tests (GitHub owner/name is case-insensitive as an identity).
LINEAR_MAPPED_REPOSITORIES = (
    "Keeper-Dating/admin-portal",
    "Keeper-Dating/calculator-api",
    "Keeper-Dating/keeper-lead-generator",
    "Keeper-Dating/matchmaking",
)
LINEAR_MAPPED_REPOSITORY_IDENTITIES = frozenset(
    name.casefold() for name in LINEAR_MAPPED_REPOSITORIES
)

# Canonical operation-family shapes, by scenario leg. The qa handback
# pair is the universal GitHub ownership transfer; the reviewer pairs
# mint one request/verify per routed reviewer; the roundtrip pairs
# mirror both for the human-review scenario. Only family NAMES live
# here - state_schema owns the ID grammar and each family's identity
# arity, and the planner owns dependency ordering and payloads.
QA_REQUIRED_GITHUB_FAMILIES = frozenset(
    ("qa.github.replace_assignees", "qa.github.verify_assignees")
)
QA_REVIEWER_FAMILIES = frozenset(
    ("qa.github.request_review", "qa.github.verify_review_request")
)
ROUNDTRIP_HANDBACK_FAMILIES = frozenset(
    (
        "roundtrip.github.replace_assignees",
        "roundtrip.github.verify_assignees",
    )
)
ROUNDTRIP_REVIEWER_FAMILIES = frozenset(
    (
        "roundtrip.github.request_review",
        "roundtrip.github.verify_review_request",
    )
)
# The cross-kind unions the runner's target-manifest derivation and
# reviewer floor key on: a persisted operation of any member family
# records the corresponding RESOLVED target class in the launch state's
# write-ahead plan.
HANDBACK_OPERATION_FAMILIES = (
    QA_REQUIRED_GITHUB_FAMILIES | ROUNDTRIP_HANDBACK_FAMILIES
)
REVIEWER_OPERATION_FAMILIES = (
    QA_REVIEWER_FAMILIES | ROUNDTRIP_REVIEWER_FAMILIES
)

# The canonical Linear-leg shapes the planner can mint for a mapped
# repository: the full bind/assign/state chain, the runtime-outage
# record, or the assign chain with a state-outage record. A non-idle
# mapped QA aggregate's qa.linear.* families must equal exactly one of
# these at terminal acceptance.
QA_LINEAR_LEG_SHAPES = (
    frozenset(
        (
            "qa.linear.verify_ticket_binding",
            "qa.linear.assign_ticket",
            "qa.linear.verify_ticket_assignee",
            "qa.linear.set_ticket_state",
            "qa.linear.verify_ticket_state",
        )
    ),
    frozenset(("qa.linear.record_unavailable",)),
    frozenset(
        (
            "qa.linear.verify_ticket_binding",
            "qa.linear.assign_ticket",
            "qa.linear.verify_ticket_assignee",
            "qa.linear.record_state_unavailable",
        )
    ),
)
# Every qa.linear.* family some canonical leg shape can mint - the union
# state_schema's qa vocabulary key set is pinned against (see the module
# docstring for why the schema binds by parity, not import).
QA_LINEAR_OPERATION_FAMILIES = frozenset().union(*QA_LINEAR_LEG_SHAPES)
