# autonomy

A skill for [Claude Code](https://claude.com/claude-code) and Codex that runs a full autonomous engineering workflow: take an issue (or take over an existing PR), resolve the repository's conventions, plan with edge-case review, validate the plan with GPT-5.6 Sol, implement, self-review, open/update the PR, then monitor CI, bot, and human review feedback until the PR is clean, paused for a human decision, or explicitly blocked — never silently stopping partway.

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

## Adapting to your org

Two pieces are organization-specific and should be edited when you adopt the skill:

- The QA-handoff owner table ships as placeholder example config (`example-org/*` → `alice-qa`/`bob-qa`). It lives in `references/monitor-exit-handoffs.md` and as the `QA_OWNER_BY_REPOSITORY` default in `scripts/handoff_decision.py`, keyed by exact repository `nameWithOwner`; `scripts/test_handoff_decision.py` fixtures use the same values. Repositories not in the table skip the QA handoff entirely, so you can also just delete the rows.
- The Linear team → QA workflow-state names (`QA_STATE_NAME_BY_TEAM` in `scripts/handoff_decision.py`, mirrored in `SKILL.md` "Ownership Transfer Rules" and `references/monitor-exit-handoffs.md`) are placeholder examples — replace with your tracker's states; teams without a mapping get no state operation.

## Validation

From the package root:

```sh
python3 scripts/validate_package.py
python3 -m unittest discover -s scripts -p 'test_*.py'
```

## License

[MIT](LICENSE)
