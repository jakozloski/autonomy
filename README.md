# autonomy

[![validate](https://github.com/jakozloski/autonomy/actions/workflows/validate.yml/badge.svg)](https://github.com/jakozloski/autonomy/actions/workflows/validate.yml)

A skill for [Claude Code](https://claude.com/claude-code) and Codex that takes a GitHub issue to a merge-ready PR on its own. It reads the repository's conventions, plans, gets the plan approved by a second model (GPT-5.6 Sol), implements, reviews its own diff, opens the PR, and then keeps working CI failures and review feedback until the PR is clean. When it can't finish, it stops and tells you which gate failed instead of quietly shipping less.

This is the workflow I run daily at Keeper. The published copy is identical to mine except for org-specific config, which ships as placeholders (see "Adapting to your org").

```
 DISCOVER      PLAN        PLAN REVIEW     IMPLEMENT     SELF-REVIEW    SHIP        MONITOR
┌─────────┐  ┌────────┐  ┌────────────┐  ┌──────────┐  ┌───────────┐  ┌────────┐  ┌─────────────┐
│ repo    │─▶│ plan + │─▶│ GPT-5.6 Sol│─▶│ commit   │─▶│ reviews + │─▶│ PR w/  │─▶│ CI + bots + │
│ conven- │  │ edge   │  │ must       │  │ per plan │  │ security  │  │evidence│  │ humans until│
│ tions   │  │ cases  │  │ approve    │  │ item     │  │ gate      │  │        │  │ clean       │
└─────────┘  └────────┘  └────────────┘  └──────────┘  └───────────┘  └────────┘  └─────────────┘
```

You can also point it at an existing PR instead of an issue. "Take over this PR" picks up a branch that already exists (yours, a teammate's, a bot's) and works the CI failures, bot findings, and review comments until the PR is clean. Useful purely as a PR babysitter.

## Design

The core `SKILL.md` is short on purpose: long autonomous runs compact their context, and the routing rules and invariants have to survive that. Detailed procedures live in `references/` and get re-read at set phase boundaries. Anything that must be deterministic ends up as plain Python in `scripts/`, stdlib only, with unit tests; that covers model gating, handoff planning, and package validation. State lives in `.claude/workflow-state.local.md`, so a killed run resumes where it left off.

| Path | Purpose |
| --- | --- |
| `SKILL.md` | Core: invariants, mandatory model policy, phase state machine, completion semantics |
| `references/project-and-entry.md` | Repository-convention discovery and entry-point routing |
| `references/phases-1-5.md` | Plan → plan review → implement → self-review → update PR |
| `references/monitor-ci-feedback.md` | Monitor loop: CI, bot and human feedback handling |
| `references/monitor-exit-handoffs.md` | Exit conditions and QA/review handoffs |
| `references/state-and-safety.md` | State schema, resume semantics, stash safety, secret redaction |
| `references/heading-manifest.md` | Map from the former single-file skill's headings to these files |
| `scripts/` | `model_policy.py`, `handoff_decision.py`, `validate_package.py` + their tests |
| `agents/openai.yaml` | Codex interface metadata |

## Install

Any agent (Claude Code, Codex, Cursor, Copilot, and 70+ others) via the [skills CLI](https://github.com/vercel-labs/skills):

```sh
npx skills add jakozloski/autonomy
```

Or clone it directly. Claude Code (user-level):

```sh
git clone https://github.com/jakozloski/autonomy ~/.claude/skills/autonomy
```

Codex:

```sh
git clone https://github.com/jakozloski/autonomy ~/.codex/skills/autonomy
```

Project-level (shared with a team): vendor this directory into your repo (e.g. `.agents/skills/autonomy/`) and symlink it from `.claude/skills/autonomy`.

Invoke with `/autonomy`, or ask for "solve this issue autonomously" / "take over this PR" / "full autonomy".

## Requirements

The skill checks its model gates up front and blocks with a reason when one is unavailable, instead of quietly downgrading:

- Claude Code `>= 2.1.170` with access to Claude Fable 5 (`claude-fable-5`) at `max` effort
- Codex CLI `>= 0.144.0` with access to GPT-5.6 Sol at `ultra` reasoning (plan-review and code-review gates)
- `gh` CLI authenticated for the target repository
- Python 3 (standard library only) for the helper scripts

The models are floors, not pins. When a newer eligible model shows up (in the Codex live catalog, or a newer Fable/Mythos-family model observed in Claude Code), the gate selects it automatically and records the swap in state and the run's audit trail. Down-tier variants (`-mini`, `-nano`) are never selected, and anything below a floor still blocks.

## FAQ

**Why block instead of falling back to a cheaper model?**
Because a run on a quietly-substituted model looks fine right up until the PR is wrong. The gates exist to make the review guarantee real; when one fails, the skill says which one and stops. Going up is automatic, though: newer models above the floor get adopted and logged (see Requirements).

**Can I run it Claude-only, without Codex?**
Not out of the box. The cross-vendor gate is the point: the model that writes the code is not the model that approves it. If you want a Claude-only variant anyway, the gates live in two places, the "Mandatory Model Policy" section of `SKILL.md` and `scripts/model_policy.py`. `scripts/test_model_policy.py` pins the expected decisions, so change both together and the tests will tell you what you missed.

**What does a run cost?**
More than you'd guess from a chat session. A full issue-to-clean-PR cycle makes a lot of frontier-model calls: plan-review rounds, multi-pass self-review, the monitor loop. Keep issues small if cost matters.

## Adapting to your org

Two pieces are organization-specific and should be edited when you adopt the skill:

- The QA-handoff owner table ships as placeholder example config (`example-org/*` → `alice-qa`/`bob-qa`). It lives in `references/monitor-exit-handoffs.md` and as the `QA_OWNER_BY_REPOSITORY` default in `scripts/handoff_decision.py`, keyed by exact repository `nameWithOwner`; `scripts/test_handoff_decision.py` fixtures use the same values. Repositories not in the table skip the QA handoff entirely, so you can also just delete the rows.
- The Linear team → QA workflow-state names (`QA_STATE_NAME_BY_TEAM` in `scripts/handoff_decision.py`, mirrored in `SKILL.md` "Ownership Transfer Rules" and `references/monitor-exit-handoffs.md`) are placeholder examples. Replace them with your tracker's states; teams without a mapping get no state operation.

## Security and trust

An autonomous agent that reads PR comments is an agent strangers can talk to. The skill treats issue bodies, review comments, and bot findings as untrusted input:

- Feedback found in PR comments, reviews, and bot output is evaluated against the session's scope rules before any action; instructions are never executed merely because they arrived in a comment (`references/monitor-ci-feedback.md`).
- Text the skill posts (PR bodies, replies) passes through secret/token redaction patterns first (`references/state-and-safety.md`, "Secret/Token Redaction").
- Human review threads are never auto-resolved, and unknown or deleted commenter identities fail closed into manual-review blockers instead of becoming reply or assignment targets.
- All workflow state stays local in `.claude/workflow-state.local.md`; nothing is sent anywhere your existing `gh`/tracker credentials don't already go.

## Validation

From the package root:

```sh
python3 scripts/validate_package.py
python3 -m unittest discover -s scripts -p 'test_*.py'
```

## License

[MIT](LICENSE)
