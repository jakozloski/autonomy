# autonomy

A skill for [Claude Code](https://claude.com/claude-code) and Codex that runs a full autonomous engineering workflow: take an issue (or take over an existing PR), resolve the repository's conventions, plan with edge-case review, validate the plan with GPT-5.6 Sol, implement, self-review, open/update the PR, then monitor CI, bot, and human review feedback until the PR is clean, paused for a human decision, or explicitly blocked — never silently stopping partway.

```
 DISCOVER      PLAN        PLAN REVIEW     IMPLEMENT     SELF-REVIEW    SHIP        MONITOR
┌─────────┐  ┌────────┐  ┌────────────┐  ┌──────────┐  ┌───────────┐  ┌────────┐  ┌─────────────┐
│ repo    │─▶│ plan + │─▶│ GPT-5.6 Sol│─▶│ commit   │─▶│ reviews + │─▶│ PR w/  │─▶│ CI + bots + │
│ conven- │  │ edge   │  │ must       │  │ per plan │  │ security  │  │evidence│  │ humans until│
│ tions   │  │ cases  │  │ approve    │  │ item     │  │ gate      │  │        │  │ clean       │
└─────────┘  └────────┘  └────────────┘  └──────────┘  └───────────┘  └────────┘  └─────────────┘
```

Works from either end:

- **From an issue** — "solve issue #123" takes it from repository discovery to a clean, reviewed PR.
- **From an existing PR** — "take over this PR" picks up a branch that already exists (yours, a teammate's, or a bot's) and works CI failures, bot findings, and review comments until the PR is clean. Useful purely as a PR babysitter.

## Design

The core `SKILL.md` is intentionally short so its routing rules and invariants survive context compaction; detailed procedures live in `references/` and are re-read at defined phase boundaries. Decisions that must be deterministic (model gating, handoff planning, package validation) are delegated to pure-stdlib Python helpers in `scripts/`, each covered by unit tests. Durable state at `.claude/workflow-state.local.md` makes every run resumable.

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

Claude Code (user-level):

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

The skill enforces a mandatory model policy and BLOCKs (rather than silently downgrading) when a gate is unavailable:

- Claude Code `>= 2.1.170` with access to Claude Fable 5 (`claude-fable-5`) at `max` effort
- Codex CLI `>= 0.144.0` with access to GPT-5.6 Sol at `ultra` reasoning (plan-review and code-review gates)
- `gh` CLI authenticated for the target repository
- Python 3 (standard library only) for the helper scripts

## FAQ

**Why does it BLOCK instead of falling back to a cheaper model?**
The review gates are the product. A plan that GPT-5.6 Sol never approved, or a run on a silently-substituted model, looks identical to a real run right up until the PR is wrong. Blocking loudly keeps the guarantee honest: the skill names the gate that failed and stops, instead of shipping something weaker under the same name.

**Can I run it Claude-only, without Codex?**
Not out of the box — the cross-vendor gate is deliberate: the model that writes the code is not the model that approves it. If you want a Claude-only variant, the gates are concentrated in the "Mandatory Model Policy" section of `SKILL.md` and in `scripts/model_policy.py`; `scripts/test_model_policy.py` pins the expected decisions, so change both together and the test suite will keep you honest.

**What does a run cost?**
A full issue→clean-PR cycle makes many frontier-model calls (plan-review rounds, multi-pass self-review, monitor iterations). It optimizes for "actually done", not minimum tokens — scope the issue tightly if cost matters.

## Adapting to your org

Two pieces are organization-specific and should be edited when you adopt the skill:

- The QA-handoff owner table ships as placeholder example config (`example-org/*` → `alice-qa`/`bob-qa`). It lives in `references/monitor-exit-handoffs.md` and as the `QA_OWNER_BY_REPOSITORY` default in `scripts/handoff_decision.py`, keyed by exact repository `nameWithOwner`; `scripts/test_handoff_decision.py` fixtures use the same values. Repositories not in the table skip the QA handoff entirely, so you can also just delete the rows.
- The Linear team → QA workflow-state names (`QA_STATE_NAME_BY_TEAM` in `scripts/handoff_decision.py`, mirrored in `SKILL.md` "Ownership Transfer Rules" and `references/monitor-exit-handoffs.md`) are placeholder examples — replace with your tracker's states; teams without a mapping get no state operation.

## Security and trust

Autonomous PR work means reading content strangers can influence — issue bodies, review comments, bot findings. The skill treats all of it as untrusted input:

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
