## Resolved Project Profile

**Before any other phase, resolve the project's conventions.** This section defines the variables that the rest of the workflow references. Resolve them once at workflow start and persist them in `resolved_conventions` in the state file.

### Discovery Order

Search these sources in priority order (first match wins per variable, but scan ALL sources for `PROTECTED_BRANCHES` and `ISSUE_TRACKER` which union/cross-check):

1. **CLAUDE.md** (project root and workspace-level) — look for "Common Commands", "Quality", "Before Committing", "Development", "Package Manager", etc.
2. **Package manifest** (`package.json`, `Makefile`, `Cargo.toml`, `pyproject.toml`, etc.) — `scripts` section for commands
3. **CI config** (`.github/workflows/*.yml`, `.circleci/config.yml`, etc.) — for quality check steps and protected branches
4. **Git config** — `git remote show origin` for default branch; branch protection rules if accessible
5. **Harness capabilities** — inventory environment-provided tracker mutation tools and determine whether the session is managed/orchestrated or local/interactive

Also extract any repository-mandated PR-feedback inventory command (for example `yarn scripts x fetch-pr-comments --exclude-diff`) into `review_feedback_inventory_steps`. Run it as an additional context/inventory source during takeover and monitoring; REST/GraphQL remain identity, pagination, edit, reply, and resolution truth.

### `BASE_BRANCH`

The base branch this PR targets (or will target). Needed for every `origin/<base_branch>` reference throughout the workflow — diff scope, rebases, merge-base calculations.

**Resolution:**

1. **Entry B (PR takeover):** Use `baseRefName` from `gh pr view --json baseRefName`. Persist to `base_branch` in state immediately.
2. **Entry A (solve an issue):** Resolve in this order, first match wins:
   - CLAUDE.md: explicit "Base branch", "PR target", or "Development workflow" mention (e.g., `dev`, `staging`)
   - `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`
   - `git remote show origin | awk '/HEAD branch/ {print $NF}'`
3. Persist to `base_branch` in state before any command that depends on `origin/<base_branch>` runs.

If `base_branch` cannot be resolved, BLOCK with a clear error — every downstream step depends on it.

### `QUALITY_CHECK_STEPS`

A structured list of `[runner, script]` pairs to execute sequentially for quality validation. **No raw shell strings** — each step is a runner + script pair.

**Resolution:**

1. Search CLAUDE.md for sections like "Before Committing", "Quality", "Common Commands"
2. Extract each command, split into runner and script (e.g., `yarn lint:fix` → `["yarn", "lint:fix"]`, `cargo test` → `["cargo", "test"]`, `make check` → `["make", "check"]`)
3. Validate each runner exists: `command -v <runner>` (more portable than `which`). If a discovered runner is missing, set the workflow to BLOCKED and notify the user which quality step cannot be executed. Do NOT silently skip discovered quality checks.

   Typical quality-check runners (`yarn`, `npm`, `pnpm`, `cargo`, `make`, `python`, `ruff`, `mypy`, `pytest`, etc.) should be on `PATH`. Project-local binaries like `eslint`/`prettier` are invoked through the package manager, so the resolved runner stays `yarn`/`npm`/etc. — no special-casing for `./node_modules/.bin/` or `npx --no-install` is needed.

   For Yarn 4 / Corepack environments: if `yarn` is missing but `corepack` is present, the user needs `corepack enable` first; the skill does NOT do this automatically — it BLOCKs and reports the missing runner so the user can take the right setup action.

4. If NO quality check steps found: set to `[]` and log a warning — do not block, but note that quality checks will be skipped

Also resolve `non_gating_checks` from explicit repository guidance. This is a name/policy map, not an agent-created exception. A check may be non-gating only when CLAUDE.md or equivalent repository policy says so; persist the exact rule and any touched-file condition. All other CI checks remain gating.

**Example resolutions:**

```yaml
# Yarn/npm monorepo
quality_check_steps:
  - ["yarn", "lint:fix"]
  - ["yarn", "format"]
  - ["yarn", "test:types"]
  - ["yarn", "test"]

# Rust project
quality_check_steps:
  - ["cargo", "fmt", "--check"]
  - ["cargo", "clippy"]
  - ["cargo", "test"]

# Python project
quality_check_steps:
  - ["ruff", "check", "--fix", "."]
  - ["mypy", "."]
  - ["pytest"]
```

### `DEV_SERVER_FRONTEND` / `DEV_SERVER_BACKEND`

Commands to start development servers for runtime verification. The default is advisory, but a repository-resolved mandatory verification rule overrides it.

**Resolution:**

1. Search CLAUDE.md for "Development", "dev server", "Start" sections
2. Search package manifest for `dev`, `start`, `serve` scripts
3. If found: store the full command (e.g., `"yarn dev:admin"`, `"cargo run"`, `"python manage.py runserver"`)
4. If NOT found: set to `null` — advisory verification may waive, but a matching repository-mandatory rule will BLOCK until a usable verification path exists or the user explicitly waives it.

Also resolve `runtime_verification_policy` from repository instructions. Persist
`mandatory_kinds` as a subset of `ui`, `api`, and `performance`, plus the exact
source rule/evidence for each. Repository mandates override the skill's advisory
default; absence of a mandate leaves that kind advisory.

### `PROTECTED_BRANCHES`

Branches that must never be directly committed to or force-pushed.

**Resolution (deduplicated union of ALL sources):**

1. **Always include:** `["main", "master", "prod"]`
2. **Git default branch:** `git remote show origin | grep 'HEAD branch'` — add to set
3. **CLAUDE.md mentions:** scan for branch names in rules/guidelines (e.g., `staging`, `develop`, `production`) — add to set
4. **CI config:** look for branch filters in workflow triggers — add to set
5. Deduplicate and store as a list

**Example:** `["prod", "master", "staging"]`

### `ISSUE_TRACKER`

Configuration for issue/ticket integration.

**Resolution (search ALL sources, then decide):**

1. Search CLAUDE.md for "Linear", "Jira", "GitHub Issues", "ticket", "issue" integration sections
2. Search for environment variables: `$LINEAR_API_KEY`, `$JIRA_*`, etc.
3. Search for ticket ID patterns in recent git history: `git log --oneline -20` — look for patterns like `WEB-1234`, `PROJ-567`, `#123`, `[TICKET-ID]`

**Decision logic:**

- **Single tracker found with clear config:** Use it. Store type, project prefix, API key env var, title format
  ```yaml
  issue_tracker:
    type: "linear"
    project_prefix: "WEB"
    api_key_env: "LINEAR_API_KEY"
    title_format: "{PREFIX}-{ID} {type}: {description}"
  ```

The presence of `api_key_env` describes the local fallback; it does NOT mean the key is required in every environment. Also resolve and persist:

```yaml
session_environment: "managed" # managed|local
issue_tracker:
  write_path: "environment_tool" # environment_tool|local_api|none
  ticket_required: true
  ticket_exemption_reason: null
```

Resolution is deterministic:

1. If an orchestrator/managed-agent marker or managed mutation tool is present, set `session_environment: managed`. Select `environment_tool` when an authorized tracker mutation tool is available; otherwise select `none`. A managed session NEVER selects `local_api`, even if a key exists.
2. Otherwise set `session_environment: local`. Prefer an available environment-provided tracker tool; if none exists and the configured key is present, select `local_api`; otherwise select `none`.
3. Persist the selected path at `resolved_conventions.issue_tracker.write_path` before Phase 5. Use the same path for ticket enforcement and the QA handoff (which fires at the first clean monitor exit — approved or paused).
4. Require `ISSUE_TRACKER.api_key_env` only after `resolved_conventions.issue_tracker.write_path: local_api` is selected. Never block a correctly configured managed tool path because the raw key is absent.
5. Once a ticket is validated, persist its human identifier at `validated_ticket.identifier` and its opaque tracker record ID at `validated_ticket.provider_id`, with the validation timestamp. Edited/relinked PR metadata invalidates stale validation. When building a handoff-helper request, map these fields to `issue_tracker.ticket_identifier` and `issue_tracker.ticket_provider_id` respectively; never substitute one for the other.
6. Record the repository's ticket rules now, but do not finalize `ticket_required` until both the actual branch and `change_type` are known. After every Scope Analysis (and again immediately before Phase 5), recompute the decision and persist the exact matching exemption rule verbatim. An exemption removes mandatory creation/linking; it does not require deleting a valid ticket already present.

- **No tracker found:** Set `type: "none"` — issue tracker enforcement will be skipped (no blocking)
  ```yaml
  issue_tracker:
    type: "none"
  ```
- **Ambiguous (multiple trackers, conflicting config):** BLOCK and ask user
  ```text
  WORKFLOW BLOCKED — Found references to both Linear (WEB-XXXX in git history) and GitHub Issues
  (#NNN in CLAUDE.md). Which issue tracker should this workflow use? Provide the tracker type
  and any required configuration.
  ```

---

## Entry Points

### Entry A: Solve an Issue

The user provides an issue, bug report, feature request, or context to work from.

1. Read and understand the full context provided
2. **Initialize state file** — create `.claude/workflow-state.local.md` with `workflow_id`, `description`, `current_phase: "entry"`, and the `## Prompt Ledger` body section seeded with the kickoff prompt as sequence 1 (core invariant), so state exists for resume if the session is interrupted
3. **Resolve `base_branch`** — resolve per the `BASE_BRANCH` section in Resolved Project Profile above and persist to state immediately, before any command that references `origin/<base_branch>`.
4. **Resolve Project Profile** — execute the remaining discovery steps above to populate `resolved_conventions` in the state file before continuing. This MUST complete before any phase begins.
5. Explore the codebase to understand the affected areas
6. **Run Scope Analysis & Skill Selection** from the issue/context (see below) so `change_type` is known before branch/ticket classification.
7. **Choose the repository-compliant branch name and finalize ticket policy.** If the current branch is protected, create the branch using the prefix required for the classified change (for example `chore/` for exempt maintenance or `feature/` for ticketed product work), then recompute `ticket_required` from the final branch + `change_type` and persist the exact rule:
   ```bash
   # Check current branch against PROTECTED_BRANCHES
   CURRENT_BRANCH=$(git branch --show-current)
   # If CURRENT_BRANCH is in PROTECTED_BRANCHES list → create a feature branch
   git checkout -b <resolved-prefix>/<descriptive-name>
   ```
8. Proceed to **Phase 1: Plan**

### Entry B: Take Over a PR

The user provides a PR number or URL from another agent or person.

1. Fetch the PR: `gh pr view <number> --json title,body,headRefName,baseRefName,files,reviewDecision`. **Capture** `baseRefName` from the response into a local variable (to be persisted as `base_branch` in step 2's state file init). Fetch feedback separately through the REST endpoints defined in Phase 6 so account type and edit timestamps remain authoritative.
2. **Initialize state file** (before any git operations):
   - Create `.claude/workflow-state.local.md` with `workflow_id`, `description`, `current_phase: "takeover"`, `pr_number`, `base_branch` = the `baseRefName` captured in step 1, and the `## Prompt Ledger` body section seeded with the takeover instruction as sequence 1 (core invariant; the PR's inherited trail is imported into its own block when the body is read in step 5). **Persist this before any command that references `origin/<base_branch>` runs** — it is the first value written to state for this workflow.
   - This ensures state exists even if subsequent steps fail
3. Check out the branch safely:

   ```bash
   # Capture current branch BEFORE any checkout (needed for stash restore later)
   PRE_TAKEOVER_BRANCH=$(git branch --show-current)
   # Persist $PRE_TAKEOVER_BRANCH in state file's pre_takeover_branch field

   # Preflight: ensure clean working tree
   git status --porcelain
   # If dirty: stash uncommitted work (including untracked files) using a
   # race-free nonce-keyed lookup. `git rev-parse stash@{0}` is NOT race-safe
   # because another process can push a stash between our push and our lookup.
   if [ -n "$(git status --porcelain)" ]; then
     STASH_NONCE="autonomy-$(date +%s)-$$-$RANDOM"
     STASH_MSG="autonomy-workflow: $STASH_NONCE before PR takeover"

     # Snapshot the stash stack head BEFORE push so we can detect the "nothing
     # to stash" case (git stash push exits 0 even when no entry is created).
     STASH_STACK_BEFORE=$(git rev-parse --verify --quiet refs/stash || echo "")

     if ! git stash push -u -m "$STASH_MSG"; then
       echo "ERROR: failed to stash dirty work; aborting before PR checkout" >&2
       exit 1
     fi

     STASH_STACK_AFTER=$(git rev-parse --verify --quiet refs/stash || echo "")
     if [ "$STASH_STACK_AFTER" = "$STASH_STACK_BEFORE" ]; then
       # No stash entry was created (working tree was effectively clean after
       # ignoring untracked-but-tracked-as-ignored files). Persist the shell
       # empty string here; serialize it to state as YAML `null` so the
       # field stays consistent with `stash_ref: { string|null }` in the
       # schema. The restore guard checks `-n "${STASH_REF:-}"` which treats
       # both empty string and unset as falsy.
       STASH_REF=""
     else
       # Walk the stash list, matching by NONCE-CONTAINMENT (not by structural
       # subject parsing). Git formats subjects as "On <branch>: <user-msg>"
       # OR "<user-msg>", and branch names may contain ": " (e.g.
       # "feature/foo: bar"), which defeats any structural prefix-strip
       # approach. The nonce we generated (timestamp+PID+RANDOM) is unique
       # within the host's stash list, so substring match is sufficient.
       STASH_REF=""
       while IFS=$'\t' read -r sha subject; do
         case "$subject" in
           *"$STASH_NONCE"*)
             STASH_REF="$sha"; break
             ;;
         esac
       done < <(git stash list --format='%H%x09%s')

       if [ -z "$STASH_REF" ]; then
         echo "ERROR: stash push reported success but no stash entry matches nonce $STASH_NONCE" >&2
         echo "       Aborting before checkout to avoid losing untracked work." >&2
         exit 1
       fi
     fi
     # Persist $STASH_REF (full SHA, possibly empty if no-op) in state file
     # before any checkout runs.
   fi
   # Exit-0 plus an unchanged refs/stash may be a legitimate no-op, but checkout
   # is safe only if the working tree is now actually clean.
   if [ -n "$(git status --porcelain)" ]; then
     echo "ERROR: working tree remains dirty after stash; aborting before PR checkout" >&2
     exit 1
   fi
   # If clean from the start: stash_ref stays null — nothing to restore later
   # Use gh pr checkout (handles forks, tracking, etc.)
   gh pr checkout <number>
   ```

   **Note:** `STASH_REF` is captured by exact-message match using a unique nonce; this is race-free regardless of concurrent stash-push activity from other processes.

4. **Resolve Project Profile** — execute the discovery steps above to populate `resolved_conventions` in the state file before continuing. `base_branch` was already persisted in step 2. This MUST complete before any phase begins.
5. Read the PR description and all feedback from the paginated issue-comment, review, and inline-comment REST endpoints; use GraphQL only to supplement thread resolution state
6. Understand what's been done and what's pending
7. Assess current state:
   - Are there failing checks? → Note them
   - Are there unaddressed review comments? → Note them
   - Is the branch out of date? → Note it
   - Are there merge conflicts? → Note them
8. **Run Scope Analysis & Skill Selection** (see section below) — uses `git diff` against `base_branch` (now persisted in state) to classify scope and select applicable gstack skills. Recompute `ticket_required` from the checked-out branch + classified `change_type`, persisting the exact exemption rule.
9. If there's implementation work remaining → proceed to **Phase 1: Plan** for the remaining work
10. If implementation is done → set `phases.plan`, `phases.plan_review`, and `phases.implementation` to `"complete"` in state (structurally not applicable for a completed PR takeover). Set `current_phase: "self_review"` and `phases.self_review: "in_progress"`. Proceed to **Phase 4: Self-Review**.

    **Takeover phase-transition bookkeeping:** Update both `current_phase` AND the corresponding `phases.*` status at every transition on the takeover path:
    - `current_phase: "self_review"`, `phases.self_review: "in_progress"` → Phase 4
    - `current_phase: "runtime_verification"`, `phases.runtime_verification.status: "in_progress"` → Runtime Verification
    - `current_phase: "pr"`, `phases.pr: "in_progress"` → Phase 5
    - `current_phase: "monitor"`, `phases.monitor: "in_progress"` → Phase 6

    Mark each phase with its valid terminal status when it finishes:
    - `phases.self_review` → `"complete"` or `"blocked"`
    - `phases.runtime_verification.status` → `"complete"`, `"blocked"`, or `"waived"`
    - `phases.pr` → `"complete"`
    - `phases.monitor` → `"paused"`, `"complete"`, or `"blocked"` (see Phase 6 condition (c) for when `blocked` applies)

    This ensures resume behavior is unambiguous if the session is interrupted.

    **Note on unaddressed review comments:** If step 7 identified unaddressed human or bot review comments, carry them forward into Phase 4. The agent MUST address them during the Phase 4 takeover comment-handling step (Phase 4, step 7), not defer them to Phase 6. Phase 6 Step 2 will handle only NEW comments that arrive after the Phase 5 push.

---

## Scope Analysis & Skill Selection

**Runs at TWO points:** (1) Entry A step 6 / Entry B step 8 — before Phase 1; (2) after Phase 3 — recompute from the actual diff since implementation may change scope. After each run, recompute branch/type-dependent ticket policy; Entry A then selects its final branch prefix in step 7.

**Source of truth:** `git diff --name-only origin/<base_branch>...HEAD` (actual diff, not planned files). For Entry A before any commits exist (no diff available), fall back to classifying from the issue/context description — infer which areas of the codebase are likely affected based on the issue's symptoms and affected features. This initial classification may be imprecise; it will be recomputed from the actual diff after Phase 3.

### Step 1: Check gstack Availability

```bash
GSTACK_DIR=""
[ -d ".claude/skills/gstack" ] && GSTACK_DIR=".claude/skills/gstack"
[ -z "$GSTACK_DIR" ] && [ -d "$HOME/.claude/skills/gstack" ] && GSTACK_DIR="$HOME/.claude/skills/gstack"
```

If not found, set `gstack_integration.available: false` in state and skip gstack-dependent skill selection in Step 4. **Steps 2-3 (scope classification and change type) MUST still run regardless of gstack availability** — they produce data used by non-gstack features like the `skill_only` runtime verification exemption and backend-only routing.

### Step 2: Classify Scope from Diff

```bash
CHANGED_FILES=$(git diff --name-only origin/<base_branch>...HEAD 2>/dev/null || echo "")
```

- `scope_frontend`: any file matching `*.tsx`, `*.jsx`, `*.css`, `*.scss`, `*.html`, or paths containing `components/`, `pages/`, `views/`, `app/`
- `scope_backend`: any file matching paths containing `api/`, `server/`, `services/`, `routes/`
- `scope_tests_only`: `CHANGED_FILES` is non-empty AND ALL changed files are in `__tests__/`, `*.test.*`, `*.spec.*`
- `scope_skill_only`: `CHANGED_FILES` is non-empty AND ALL changed files are in `.claude/skills/` OR `.agents/skills/` (some repos symlink `.claude/skills/` to `.agents/skills/` — both paths count as skill-only)
- **Empty diff guard:** If `CHANGED_FILES` is empty, set both `scope_tests_only` and `scope_skill_only` to `false`. An empty diff means no commits yet — use the issue/context fallback from above, not vacuous truth.

### Step 3: Classify Change Type

From entry context:

- `bug_fix`: issue/context mentions bug, error, regression, or "fix"
- `feature`: new functionality being added
- `refactor`: restructuring without behavior change
- `skill_only`: changes are only to `.claude/skills/` or `.agents/skills/` files (no runtime code)

**Precedence:** `change_type` is a single value. When multiple signals could apply (e.g., a bug-fix that also touches `.claude/skills/`), resolve in this order (first match wins):

1. **`skill_only`** — if `scope_skill_only == true` (from Step 2), set `change_type = "skill_only"` regardless of entry context. The file-scope signal is authoritative.
2. **`bug_fix`** — if entry context mentions bug/error/regression/"fix" AND `scope_skill_only == false`.
3. **`refactor`** — if entry context explicitly describes restructuring without behavior change.
4. **`feature`** — default.

### Step 4: Select Skills via Capability-Gated Matrix

| Skill                    | Condition                                                     | Capability Gate                                        | Phase Integration               |
| ------------------------ | ------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------- |
| `/investigate` adapter   | `change_type == bug_fix` AND Entry A                          | None (pure analysis)                                   | Phase 1 augmentation            |
| `/review` adapter        | Any code change (not `skill_only`)                            | None (structured review always possible)               | Phase 4 primary self-review     |
| `/qa` adapter            | `scope_frontend == true` AND NOT `scope_tests_only`           | `DEV_SERVER_FRONTEND != null` AND browse binary exists | Runtime Verification primary    |
| `/design-review` adapter | `scope_frontend == true` AND NOT `scope_tests_only`           | Browse binary exists AND `DEV_SERVER_FRONTEND != null` | After Runtime Verification      |
| `/cso` adapter           | Any code change (not `skill_only` and not `scope_tests_only`) | None (code-tracing only, no external deps)             | Pre-PR security gate (Phase 4a) |
| `/autoplan` adapter      | Any change (not `skill_only`)                                 | Mandatory Codex version/live-catalog preflight passes  | Phase 2 replacement             |

**Browse binary capability gate:**

```bash
B=""
[ -x ".claude/skills/gstack/browse/dist/browse" ] && B=".claude/skills/gstack/browse/dist/browse"
[ -z "$B" ] && [ -x "$HOME/.claude/skills/gstack/browse/dist/browse" ] && B="$HOME/.claude/skills/gstack/browse/dist/browse"
# If B is empty, browse-dependent skills (/qa, /design-review) are not selected
```

After classification, recompute `ticket_required` from the actual branch and
`change_type`. Also intersect the actual diff kinds with
`runtime_verification_policy.mandatory_kinds`; missing optional adapter tooling
may waive advisory checks, but never a repository-mandatory one.

`/benchmark` is NOT auto-selected — it requires baseline data and explicit opt-in.

### Step 5: Persist to State

Store selections in `gstack_integration` in the state file. See State Tracking for schema.

### Adapter Architecture

gstack skills have their own preambles, setup gates, AskUserQuestion calls, and sibling scripts. The adapters in this workflow implement a **supported subset** of each gstack skill's behavior:

- Each adapter checks a **capability gate** before running
- Adapters use autonomy's own state and conventions (resolved base branch, quality checks, dev servers)
- **Never run in adapters**: update checks (`gstack-update-check`), telemetry (`gstack-analytics`), install scripts, browser-open flows, `gstack-config`, `gstack-review-log`, or AskUserQuestion prompts
- **Auto-resolve ASK items** with "fix as recommended" (autonomous mode)
- Adapter status is logged to `gstack_integration.*` (informational only — `phases.*` remains authoritative)
- **Graceful degradation**: if an adapter's capability gate fails or execution errors, set `gstack_integration.{skill}.status = "skipped"`, fall back to existing behavior, and continue. **Exception:** Phase 2 still requires the mandatory GPT-5.6 Sol plan gate from the core model policy; an adapter failure cannot downgrade or bypass that gate.

### Security Model (Autonomous Mode)

- **Trusted:** Adapter logic defined in this skill package, reading gstack SKILL.md files for reference (passive read)
- **Forbidden in autonomous mode:** `gstack-update-check`, `gstack-config`, `gstack-analytics`, session tracking, `gstack-review-log`, any install/setup scripts, `bun install`, `curl | bash`
- **Allowed:** browse binary (headless testing), codex CLI (already trusted in Phase 2)

---
