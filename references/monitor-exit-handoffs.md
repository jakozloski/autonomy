### Phase 6 Session Ownership (cheap orchestrator, pinned workers)

Runs at every ENTRY into monitoring — the Phase 5→6 transition and every resume that re-enters Phase 6 — BEFORE the first Step 1 poll:

1. **Bind:** call `monitor_orchestrator_binding(resolved_conventions.model_runtime, session_model=<this session's model>)` from `scripts/model_policy.py`. It REBINDS the persisted legs and never re-selects, re-checking floors because state is untrusted: a landed-ready above-floor reviewer leg is the nominal owner (`orchestrator_on_reviewer`); otherwise the base lineage keeps the session (`orchestrator_on_base`) — ownership is a cost decision and never a new way to block. Reviewer ownership additionally REQUIRES a confirmed write-capable base worker path (step 4): when the host cannot enforce a per-agent base model for write work, record `orchestrator_on_base` and monitor on the base lineage as before this role existed. `invalid` means the persisted gate record is corrupt or this session's model matches no recorded leg: treat it as suspect state (re-derive or BLOCK per the Resume trust model), never guess a model.
2. **Persist:** write `monitor_ownership` (`lineage`, `model`, `bound_at`, `reason_code` required — plus `pending_owner`, required non-null exactly when the binding is a continuity binding; `state_schema.py` fails a partial block closed) and append `monitor-ownership:<lineage>:<model>` to the Decision Audit Trail. Re-binding on later entries overwrites the block with a fresh `bound_at` — the record describes the CURRENT session's owner, not history (history lives in the trail).
3. **Owner-pinned execution (automatic — no human model selection):** ownership is enforced by EXECUTION LOCUS, never by re-modeling a live session. A session that ENTERS monitoring already ON the nominal owner model monitors inline ONLY while no `monitor_cli` block exists in state; once that block exists, EVERY session — owner-model sessions included — drives monitoring through the runner, because the runner's kernel lock is the exclusion protocol for canonical Phase 6 writes and an inline writer outside it could race a committed candidate (the base-digest check is check-then-replace, not a lock). Every other session — a continuity binding (`orchestrator_continuity`, truthful own-lineage record, `pending_owner` = the nominal owner) — does NOT monitor inline: it drives monitor iterations through the owner-pinned slice runner, ONE foreground invocation per slice: `python3 "$LOADED_SKILL_DIR/scripts/monitor_runner.py" <state-file>`. The runner is deterministic control flow (no LLM): it holds a kernel lock for the whole slice (a second runner exits `lock_held` immediately), recomputes the binding from persisted state and blocks on owner drift, then loops owner-pinned `claude -p` child ticks — first launch pins `--model <owner>`, later ticks `--resume` the recorded `monitor_cli.child_session_id` so the OWNER session is the warm cache lineage — under the standard supervision bounds (silence 180s, per-child ceiling capped so the slice always returns inside the parent's own attempt ceiling). Each child executes EXACTLY ONE iteration and writes a full updated state to a per-attempt CANDIDATE file, NEVER canonical state; the runner is the sole canonical committer: it verifies the child's protocol-reported served model equals the bound owner (mismatch BLOCKS — identity is the contract), validates the candidate, checks the exact counter transition, checks the runner-owned `monitor_cli` block came back value-identical, checks canonical (digest AND control block) still matches the launch snapshot recorded as `in_flight.base_workflow_digest` — ANY drift under the held lock is an unknown writer: the candidate is discarded and the runner stops as suspect state per the Resume trust model, never a clobber, never a retry on mutated input, then finalizes with ONE atomic replace that also records `child_session_id` and clears `in_flight` — there is no post-commit acknowledgement write to lose. Child failures are phase-aware: launch-class failures wait on the liveness ladder; unknown-outcome failures (idle kill, lost stream, no verdict) discard the candidate, reconcile, and charge the budget — external side effects stay safe because every loop action is idempotency-guarded by the ack/reply/`last_processed_*` machinery; three same-signature failures BLOCK. Recovery after a dead runner has NO kill authority (record provenance cannot be proven locally against a write-capable child): a recorded in-flight child is either proven extinct — fail-closed process-table inspection, where an unprovable answer blocks rather than reads as extinction — and reconciled, or the runner BLOCKS naming the recorded pid for manual termination. There is NO inline fallback under a continuity binding — wrong-owner inline monitoring is the exact contract violation this mechanism exists to prevent; when the runner blocks (CLI missing/old, auth, owner drift), the workflow BLOCKS with the runner's actionable reason. The parent session treats each runner invocation as one supervised foreground call (`slice_exhausted` → invoke again per its own loop; `terminal`/`blocked` → execute the corresponding exit flow); `monitor_cli` in state is the runner's fail-closed control block and is RUNNER-OWNED — no session or child ever edits it.
4. **Capability boundary (MANDATORY under `lineage: "reviewer"`):** the orchestrator session polls, classifies feedback, drafts replies and PR-body syncs, updates state, and DISPATCHES. It never writes code/config/tests, never decides a disputed finding's substance, never overrides a reviewer verdict, and never waives a gate. Substantive work escalates to a base-lineage worker, recording `worker-dispatch:<trigger>` in the Decision Audit Trail:
   - **Fix work (write-capable):** an Agent-tool WORKER on the frozen base selection with the working side's toolset — a working voice, not a review voice, so the review voices' read-only boundary does not apply to it; what DOES apply is the same host-enforcement rule as every agent-tool voice (confirmed per-agent model + max effort, environment overrides compatible). No compliant write path ⇒ no reviewer ownership (step 1's precondition; bind `orchestrator_on_base` at the next boundary instead) — fix work must always have a policy-compliant actor.
   - **Judgment calls (read-only):** the explicit read-only base CLI voice, unchanged.
     Worker diffs go through the unchanged `PHASE_6_SELF_REVIEW` procedure and its pinned voices. Escalation triggers (any one): a work item changing code/config/tests; a human comment whose resolution is not mechanical; conflicting findings between voices; a merge-readiness re-verdict; a third consecutive triage pass unable to classify the same item.
5. **Terminal audit:** before a condition-(a)/(d) exit under reviewer ownership, dispatch ONE read-only base-lineage audit of the exit decision's own inputs — the fresh CI/review/feedback data the exit evaluated, the `all_feedback_addressed` computation, the currency of `merge_readiness.claims_audit` and `ac_conformance`, and the handoff plan the exit will execute; record its verdict, and a failed audit re-enters the loop as a work item. Exit conditions themselves stay deterministic — the audit reviews the evidence they consumed, it is not a new exit condition and it does not re-run the checks.

Why this role exists: the monitor loop is the workflow's long tail — hours of polling on a large warm cached context whose traffic dwarfs its output, while the loop's own decisions are triage and dispatch. Owning that session at the reviewer tier reprices its whole cache lineage; the substantive work stays exactly where the floors put it (base-lineage workers, pinned review voices). The binding and its convergence rule run identically when the reviewer leg is degraded or absent — a run that cannot afford the split simply records base ownership and behaves as every run did before this section existed.

### Step 4: Evaluate Loop Exit

**Step 4 is only reached if Steps 1-3 made no pushes this iteration.** If any step pushed, it returns to the loop top — Step 4 never runs with stale post-push data.

**Before evaluating any exit condition**, re-fetch CI status, PR review state, and bot feedback with fresh data:

```bash
# 1. Fresh CI status
CHECKS=$(gh pr checks <PR_NUMBER> --json name,bucket,link)

# 2. Fresh PR review/branch state (feedback identity comes from REST below)
gh pr view <PR_NUMBER> --json reviewDecision,isDraft,mergeStateStatus,mergeable,headRefOid
# Use this fresh reviewDecision for exit conditions below; isDraft feeds the draft-PR gate.
# The fresh mergeStateStatus feeds the "branch up to date" precondition: if it is BEHIND,
# the branch is NOT up to date — return to Step 3 instead of flipping/exiting, so a base
# push that landed after Step 3 cannot let the gate mark a stale PR ready or clean.

# 3. Fresh feedback — re-run all three Phase A REST metadata queries (issue
#    comments, reviews, inline comments), the GraphQL reviewThreads query, and
#    the top-level comment/review checks from Step 2. Re-compute unreplied_all,
#    unreplied_actionable, and all_feedback_addressed using the canonical rules in
#    "Compute unreplied inline comment sets". Exit evaluation needs completeness,
#    identity, and timestamps — never bodies. Any record that needs evaluation
#    sends the loop back to Step 2, which performs its Phase B body fetch there.
```

Before any other Step 4 decision, compare fresh `headRefOid` with `last_observed_head_sha`. If state is null (first Step 4 pass of this workflow), just persist the observed SHA and continue — Phase 5 already armed `post_push_until` for the agent's own push, and re-arming here would silently add a full extra grace window before any exit. If the SHA CHANGED from the persisted value, persist the new SHA, set `post_push_until = now + BOT_GRACE_WINDOW`, clear `clean_poll_timestamps`, clear transient `ci:watch_timeout:*` and `branch:status_unknown:*` counters, and return to Step 1. The changed-SHA branch covers collaborator pushes that the local push path never observed. Every clean-poll record must carry this same head SHA.

**After re-fetching bot feedback**, evaluate in this order:

1. **Check for terminal exhaustion first:** If `unreplied_actionable` is empty AND (`unreplied_all` is non-empty OR all top-level/review items are exhausted with failed ack posts), do NOT return to Step 2 — fall through to exit condition evaluation so condition (c) can fire the BLOCKED signal. Returning to Step 2 would just churn.
2. **If there is actionable (non-exhausted) unaddressed feedback:** this includes inline bot roots, unresolved bot threads ready for verified resolution, unacknowledged bot or human top-level comments/review bodies, and new/edited human feedback that has not been evaluated/replied to. Return to Step 2 immediately.

Define two fresh predicates. `branch_completion_ready` requires `mergeable == "MERGEABLE"` and `mergeStateStatus` in `{"CLEAN", "HAS_HOOKS", "UNSTABLE"}`. `branch_pause_ready` also permits `BLOCKED` only when unapproved and protection evidence proves missing approval is the sole blocker. An approved `BLOCKED` PR follows Step 3's concrete fix/manual/three-strike handler and populates `manual_branch_protection_blockers` when human action is required; it never falls through to exits. Other stale/conflicting/unknown states return to Step 3.

**Definition of `all_feedback_addressed`** (canonical, used everywhere):

- `unreplied_all` is empty (all inline bot comments have replies that are newer than the bot comment's last edit — checked via REST `in_reply_to_id` and `updated_at` comparison, per the canonical rules in "Compute unreplied inline comment sets")
- All top-level bot comments acknowledged (ID in `acknowledged_top_level_comments` with matching `bot_updated_at`, or existing `<!-- ack:comment:<id> -->` from `authenticated_actor` that is newer than the bot comment's REST `updated_at`)
- All bot review summaries with unique actionable items acknowledged (in `acknowledged_top_level_reviews` with matching `review_updated_at`, or implicitly resolved)
- `unresolved_bot_threads == 0` after verified GraphQL resolution
- Every external-human top-level issue comment and non-empty review body is acknowledged at its current edit timestamp
- `exhausted_feedback` is empty
- `manual_unknown_feedback` is empty

Note: `all_feedback_addressed` uses `unreplied_all`, not `unreplied_actionable`, and independently requires both `exhausted_feedback` and `manual_unknown_feedback` to be empty. A successfully posted warning reply/ack does not clear exhaustion or an unknown-identity blocker.

Evaluate the fresh `CHECKS` snapshot (not stale Step 1 results):

- If ANY gating check has `bucket == "pending"`: do NOT evaluate exit conditions. Set `loop_reason = "wait_repoll"` (a pending-check wait is passive and never consumes the work-iteration cap), sleep ≤60 seconds, go back to Step 1.
- If ANY gating check has `bucket == "fail"` or `bucket == "cancel"`: do NOT evaluate exit conditions. Go back to Step 1 immediately.
- Only proceed if every gating check has `bucket` in `{"pass", "skipping"}` and every excluded check has persisted repository-policy evidence.

**CI-config self-verification (diff-conditional, GitHub Actions scope):** when the PR diff (`git diff origin/<base_branch>...HEAD --name-only`) touches `.github/workflows/**`, the `skipping` tolerance above does not extend to the modified workflows themselves. Resolve each modified workflow's PR-runnable jobs and verify through the checks/Actions API — never a comment's claim or an arbitrary URL — that each has a run at the current `headRefOid` with bucket `pass`: for a workflow this diff modifies, `skipping` is exactly the failure this gate exists to catch (a head-commit skip token, even one merely describing a skip mechanism, silently disables it). Manual-only, deploy-only, or secret-gated jobs pass only through an explicit recorded exception plus a named substitute validation (config lint / dry-run) in the PR's Evidence section; other providers' CI configs (`.gitlab-ci.yml`, `.circleci/`, `Jenkinsfile`, `azure-pipelines.yml`) always take that provider-specific substitute-validation path — the Actions API cannot verify them. On success, update the PR body's single anchored `<!-- autonomy:ci-evidence -->` record from `CI evidence: pending for head <sha>` to the verified run links (zero-or-one matching anchor; duplicate anchors fail closed), ledgered as `handoffs.pr_artifacts` operation `ci-evidence:<head_sha>` with the write-ahead → re-fetch → complete|failed lifecycle. Until satisfied, the draft-PR gate must not flip and exits (a)/(d) must not fire — with one flip-only exception: a modified workflow that cannot run while the PR is a draft (its sole PR trigger is `ready_for_review`, or its conditions exclude drafts) is excluded from the FLIP precondition, because the flip itself creates its first run; it remains fully required for exits (a)/(d).

An unsatisfied CI-config self-verification is a work item, never a wait state: log `ci:config_unverified:<headRefOid>:<modified-jobset-hash>` in `attempt_log` (a `ci:` family key, so condition (c)'s existing three-strike bullet covers it) and remediate by cause — a head-level skip token needs a new non-skipping head (amend/reword + `--force-with-lease` after the documented preflight) or an authorized re-run where a run object already exists; a job-level condition skip needs the condition fixed or the explicit exception recorded with its substitute proof. Any resulting push clears `clean_poll_timestamps`, re-arms `post_push_until`, supersedes the ci-evidence record with a fresh `pending for head <new-sha>` operation under the new head's ID, and returns to Step 1. Three identical signatures BLOCK via condition (c).

#### MANDATORY VERIFICATION GATE

Before EXECUTING the draft-PR flip or any exit condition that ends the loop (conditions a, c, d), you MUST execute and print a sanity-check verification block — run it after the pass's canonical evaluation selects that outcome and before performing it. This is a hard precondition: declaring exit (or flipping) without printing this block is a workflow violation. (Reminder: when `defect_evidence_mode != "none"`, the core Validation-Before-Push evidence re-bind — `evaluated_head_sha`/`analyzed_head_sha` equal to the push HEAD — applies to every monitor-loop push too; a monitor fix commit invalidates both until re-run.)

**This block is a SANITY CHECK.** The canonical unreplied detection above (compute `unreplied_all` / `unreplied_actionable` from REST `in_reply_to_id` + `authenticated_actor` + edit-timestamp comparison + `thread_reply_timestamps` grace) is authoritative. This block must not diverge from it — if the simplified count here disagrees with the canonical values, trust the canonical values for gating decisions and log the discrepancy for investigation.

```bash
# Simplified counting pass — sanity check only.
OWNER=$(gh repo view --json owner --jq '.owner.login')
REPO=$(gh repo view --json name --jq '.name')
ACTOR=$(gh api user --jq .login)
ALL=$(gh api --paginate "repos/$OWNER/$REPO/pulls/<PR_NUMBER>/comments" \
  --jq '.[] | {id: .id, author: .user.login, author_type: .user.type, in_reply_to: .in_reply_to_id}')
printf '%s\n' "$ALL" | AUTHENTICATED_ACTOR="$ACTOR" python3 -c "
import os, sys, json
comments = [json.loads(l.strip()) for l in sys.stdin if l.strip()]
actor = os.environ['AUTHENTICATED_ACTOR']
root_bot = [c for c in comments if c['in_reply_to'] is None and c['author_type'] == 'Bot' and c['author'] != actor]
reply_targets = {
    c['in_reply_to'] for c in comments
    if c['in_reply_to'] is not None
    and (c['author'] == actor or (c['author_type'] == 'User' and c['author']))
}
unreplied = [c for c in root_bot if c['id'] not in reply_targets]
print(f'VERIFICATION (sanity): root_bot={len(root_bot)} replied={len(root_bot)-len(unreplied)} unreplied={len(unreplied)}')
for c in unreplied:
    print(f'  UNREPLIED {c[\"id\"]} {c[\"author\"]}')
"
```

**Required output line** (must appear in agent's response before any exit signal):

```text
VERIFICATION (sanity): root_bot=N replied=M unreplied=K
FEEDBACK GATE: unresolved_bot_threads=B unacked_human=H exhausted=E manual_unknown=U
```

**Gating rules (authoritative, using the canonical `unreplied_all` / `unreplied_actionable`):**

- If `unreplied_actionable > 0` → return to Step 2 immediately. Do NOT exit.
- If `unreplied_actionable == 0` AND `unreplied_all > 0` → this is terminal exhaustion. Fall through to exit-condition evaluation so condition (c) fires BLOCKED. Do NOT return to Step 2 (nothing more to do there).
- If `unreplied_all == 0` → proceed to evaluate exit conditions below.
- Any non-zero `unresolved_bot_threads`, unacknowledged current human item, `exhausted_feedback`, or `manual_unknown_feedback` count prevents exit regardless of the simplified inline count.
- The verification block must be RE-RUN, fresh, for every flip or exit it gates; a pass that resolves to a (b)/(e) re-poll takes no externally visible action and skips it. Do NOT cache or reuse a prior pass's result.

**Why this gate exists:** Without it, agents can mistakenly declare PAUSED while bot comments remain unreplied (observed failure mode: agent reports "all 30 replied" while 1 new Bugbot comment is open from a recent rescan). The sanity block gives a quick visual check; the canonical rules are the authority.

#### Exit conditions

After confirming all checks are terminal and passing AND the verification gate is satisfied, evaluate the conditions below in this exact order — **first match wins**. The order is:

**(c) → draft-PR gate → R2 gate → (a) → (b) → (d) → (e)**

Condition (c) is checked FIRST so that terminal exhaustion, `CHANGES_REQUESTED`, and unresolved human threads cannot be bypassed by an APPROVED match in (a) or a re-poll match in (b). The draft-PR gate (defined below, after the lettered conditions) sits immediately after (c); it is not an exit — when it fires, it flips the draft PR to ready and continues the loop. The R2 review gate (defined after the lettered conditions) sits between the draft-PR gate and (a): in Keeper repositories it intercepts the would-be clean exits until R2 has approved the current head. The conditions are written below in lettered order for readability, but the FIRING ORDER is (c) → draft-PR gate → R2 gate → (a) → (b) → (d) → (e).

- **(a) If `reviewDecision == "APPROVED"` AND `grace_elapsed(post_push_until)` AND `all_feedback_addressed` AND `stable_poll_confirmed` AND `isDraft == false` AND `branch_completion_ready` AND CI-config self-verification satisfied (when applicable) AND the merge-readiness holds are clear (the draft-PR gate's two direction-aware rechecks, run here for the ready PR — the draft-hold never armed or already released on a ready PR, so the exits are where the hold must live: an additive/mixed-direction migration still `pending`, a required backfill in `merge_readiness.backfill` not verified `complete`, or a `dependencies: "hazard_documented"` contract still not live, means do NOT exit — note `exit:deploy-hold` / `exit:dependency-hold` in the iteration output, post the `### Deploy order` PR comment if not already posted, set `loop_reason = "wait_repoll"`, and continue the loop through the polling schedule (a hold-wait is a passive poll tick — ≤60s chunks whose tick refresh re-verifies the held check — never a work iteration burning the 50-pass cap). A hold persisting past `BOT_GRACE_WINDOW` of continuous hold time (measured from the persisted `hold_started_at`, set on the first held tick, cleared by any tick or work pass that finds no live hold — the span survives resume) is a human dependency, not a wait: record `human:deploy-hold` / `human:dependency-hold` in `attempt_log` and exit through condition (c) — an unbounded tick loop with `phases.monitor: "in_progress"` is exactly the silent strand the Terminal-exit turn contract forbids, and applying the migration / making the dependency live is human action; each key's fixed verifier (the grammar rows above) re-runs the applied/live-state query on resume and clears the key once it verifies. `unverified` state never holds, same rationale as the gate) AND the PR body's Prompt Trail is current per the core's sync gate (synchronize first; a failed sync exits BLOCKED with `prompt-trail:stale` — SKILL.md Phase 6):**
  - (See canonical definition of `all_feedback_addressed` above in the Step 4 preamble. See "Stable-poll gate" below for `stable_poll_confirmed` — the same two-clean-polls-separated-by-`BOT_GRACE_WINDOW` rule applies to condition (a) as well as (d). No exit, whether complete or paused, may fire without two separated clean polls — the gate exists to catch Bugbot comments that arrive during the grace window.)
  - The approval must be HUMAN: at least one current APPROVED formal review authored by a REST account type `User` reviewer. A bot approval (e.g. Keeper's `r2-keeper`) can flip the aggregate `reviewDecision` to APPROVED but satisfies only the R2 gate, never (a) — with only bot approvals, this pass evaluates (d) instead (Final Rule 6: never treat a bot as a human reviewer).
  - The fresh non-`BLOCKED` completion predicate prevents missing code-owner/additional-approval or other protection gates from being mistaken for completion.
  - Before writing terminal monitor state, run the **QA handoff** below (if a prior paused exit already recorded it `complete`, re-verify its stored postconditions instead of re-executing — a human reassignment since then is human action, not drift): persist its per-operation targets/status as pending, execute one operation at a time, verify remote postconditions, and record each as complete or failed. Resume checks postconditions before retrying any pending operation.
  - After every required operation has a durable `complete|failed` result, set `phases.monitor: "complete"` and output the success signal plus any recorded non-blocking handoff warning:
    ```text
    ✅ WORKFLOW COMPLETE — PR #<number> approved and all checks passing.
    Bot grace window elapsed — no late feedback detected. All comments addressed.
    Sanity VERIFICATION: unreplied=0 confirmed across 2 clean polls separated by BOT_GRACE_WINDOW.
    ```

- **(b) If `reviewDecision == "APPROVED"` BUT (NOT `grace_elapsed(post_push_until)` OR NOT `stable_poll_confirmed`):**
  - Bot reviewers may still post feedback after the recent push. Do NOT declare workflow complete.
  - Track clean polls per the stable-poll gate below (append `{head_sha, observed_at}` whenever canonical feedback is fully clean — grace need not have elapsed to record); confirmation requires two observations of the same head separated by `BOT_GRACE_WINDOW`.
  - Output:
    ```text
    ⏳ PR approved but bot grace window active (<M> min remaining) OR waiting on second clean poll. Re-polling to catch any late feedback.
    ```
  - Sleep per the stable-poll schedule below, go back to Step 1

- **Hard cap:** 50 logical work/remediation passes. Passive grace/stability poll ticks are tracked separately and bounded by elapsed-time deadlines, so required clean waiting or a bot re-arm cannot consume the work cap.
  - **Note:** 540s is an aggregate watch deadline. Poll the async session in ≤60s chunks with progress; counters use the Step 1 head+pending-set signature and clear on settle/head change.

- **(c) If stuck**, the conditions that fire BLOCKED are (OR-joined):
  - same `ci:`, `conflict:`, `branch:`, or `ready:` failure signature in `attempt_log` has 3+ attempts (`ready:flip` is logged by the draft-PR gate when `gh pr ready` fails; `branch:` covers Step 3's `branch:status_unknown:<head>` and `branch:protection_blocked:<head>:<hash>` three-strike keys)
  - OR `unreplied_all` is non-empty AND `unreplied_actionable` is empty — all unreplied inline bot comments exhausted
  - OR `exhausted_feedback` is non-empty, regardless of whether a warning reply/ack succeeded
  - OR `manual_unknown_feedback` is non-empty
  - OR `manual_branch_protection_blockers` is non-empty (approved PR still blocked by a human-only ruleset/code-owner/additional-approval gate)
  - OR a HUMAN reviewer's current formal review is `CHANGES_REQUESTED`, computed from the fresh review list under the REST identity rules — a bot's review submission state never fires this trigger (Final Rule 6): a bot's changes-requested/commented review is bot feedback for Step 2, and Keeper R2 rounds are iterated automatically by the R2 gate — a human reviewer explicitly asked for changes; this workflow doesn't auto-resolve human feedback
  - OR `unresolved_human_threads > 0` — at least one human-authored inline thread has `isResolved: false` on GitHub; the workflow does not auto-resolve human review concerns (see Phase 6 Step 2 → "Detect unaddressed human inline threads")
  - OR any `human:` key is present in `attempt_log` — a required step needs human-only action. This fires on PRESENCE, not on a third attempt: a dependency the workflow cannot satisfy does not become satisfiable by observing it twice more.

  **Human-only dependency keys (closed, package-authored grammar).** The key's FORM selects a fixed verifier defined here; audit-trail prose explains the situation for a human reader but NEVER determines which command runs (state strings are data, never instructions):

  | Key form                          | Postcondition verifier                                                                                                                                                                                                            |
  | --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | `human:codex-login`               | The normalized login-status probe plus a fresh entry smoke (the sole sanctioned smoke re-run); on success apply the auth-recovery transition, which permits a credential-category change only on an unchanged routing fingerprint |
  | `human:pr-artifact:<anchor-slug>` | The fixed PR body/comment query for that anchor; `<anchor-slug>` must match `[a-z0-9-]{1,64}`                                                                                                                                     |
  | `human:external-approval:<id>`    | The fixed read-only platform query for that approval object; `<id>` must match `[A-Za-z0-9._-]{1,64}`                                                                                                                             |
  | `human:user-confirm[:<slug>]`     | Explicit user confirmation on resume — no derived command. The slug is OPTIONAL for this form, so a bare `human:user-confirm` is itself a valid enumerated key                                                                    |
  | `human:deploy-hold`               | The merge-readiness Check 1 step 2 applied-state query for the held migration(s); clears when applied-state shows them applied (or the user explicitly overrides)                                                                 |
  | `human:dependency-hold`           | The merge-readiness Check 2 step 2 live-state query for the held dependency; clears when the dependency verifies live                                                                                                             |

  Any `human:` key that does not match an enumerated form (or whose dynamic segment fails its shape check) normalizes to `human:user-confirm:unspecified` and is verified as that form, so key-to-verifier selection stays deterministic and never resolves to a key that matches no row. "Human-only" means unavailable under the workflow's current authorization and tooling — credential re-issuance, a manual upload requiring an interactive login, an approval outside the PR — not merely inconvenient work the agent could do. Approval/ruleset gates keep using `manual_branch_protection_blockers`; these keys never duplicate them.

  These keys are postcondition-bound, never tombstones: on ANY resume (`continue` or `reset`), re-run each key's fixed verifier FIRST and clear the key when its postcondition verifies, without wiping unrelated attempt counters. An unmet postcondition re-blocks under the same key.

  **Degraded terminal path.** When the MANDATORY VERIFICATION GATE itself cannot execute because the platform API fails deterministic authentication, BLOCK immediately with the exact API failure evidence in place of the gate's printed block — the only permitted substitute for that block, and never a reason to exit non-terminally.

  Action when condition (c) fires:
  - **Review-roundtrip handoff (conditional):** if `CHANGES_REQUESTED` and/or `unresolved_human_threads` are the ONLY triggers (no CI/conflict/ready/protection blocker and both feedback blocker maps empty), evaluate the durable per-reviewer record. Eligibility still requires complete current reply/ack/push evidence and a known non-bot non-actor reviewer — and, in Keeper repositories, the R2 gate satisfied on the current head (else route to the R2 gate instead of exiting; see the roundtrip section).
  - For eligible reviewers, run the **Review-roundtrip handoff** below before terminal state: persist per-reviewer operations pending, execute/verify them, and store complete or failed. Ineligible or unknown/deleted authors remain manual blockers and are never assignment targets.
  - After conditional handoff operations have durable terminal results (or no handoff was eligible), set `phases.monitor: "blocked"` and stop the loop. A resume with pending operations re-fetches postconditions before retry.
  - Every blocked exit persists at least one durable blocker-evidence source the schema can extract (R2 #1328 finding 3767068764): one of the three feedback maps, an `attempt_log` key (`human:*`, `prompt-trail:stale`, or a three-strike `ci:`/`conflict:`/`branch:`/`ready:` record), or the roundtrip ledger. A human-feedback-only block where no roundtrip was eligible records `human:user-confirm:review-block` in `attempt_log` BEFORE persisting the status — a blocked status with no extractable evidence is rejected by the monitor runner.
  - Notify user with clear explanation of what's blocking
  - For exhausted inline bot comments: `⚠️ WORKFLOW BLOCKED — N bot review comment(s) could not be addressed automatically. Flagged for human review.`
  - For exhausted feedback: `⚠️ WORKFLOW BLOCKED — N feedback item(s) reached the automatic-attempt limit. Warning replies do not clear the blocker; human review is required.`
  - For `CHANGES_REQUESTED` and/or unresolved human threads where the roundtrip handoff ran (feedback addressed this session): `⚠️ WORKFLOW BLOCKED — awaiting <reviewer>'s re-review. Roundtrip complete: feedback addressed, every comment replied to, review re-requested, PR reassigned to <reviewer>. Re-invoke /autonomy after their re-review.`
  - For `CHANGES_REQUESTED` where the handoff did NOT run (single message; if `unresolved_human_threads > 0` also fires, the CHANGES_REQUESTED message subsumes it — emit ONE message, not both): `⚠️ WORKFLOW BLOCKED — Human reviewer requested changes. Address them and resolve all open inline threads on GitHub, then have the reviewer re-request review; re-invoke /autonomy afterward.`
  - For unresolved human threads only (no CHANGES_REQUESTED, handoff did NOT run): `⚠️ WORKFLOW BLOCKED — N unresolved human inline thread(s). Address the comments, then have a human mark the threads as resolved on GitHub. Re-invoke /autonomy afterward.`
  - For a `human:` dependency: `⚠️ WORKFLOW BLOCKED — <exact human action required>. The workflow cannot perform it under its current authorization. Once done, re-invoke /autonomy; the recorded postcondition is re-verified automatically before work resumes.`
  - Append the **Stranded work** section (see the core's Blocked-Exit Work Preservation) whenever local work exists — branch, ahead-of-origin count, HEAD SHA, and exact resume commands — so finished work is never left invisible in a worktree.
  - Do NOT keep retrying the same failing approach
  - A successful exhaustion warning post prevents duplicate notifications only; it never satisfies `all_feedback_addressed`.

- **(d) If everything is clean AND `all_feedback_addressed` AND `stable_poll_confirmed` AND `grace_elapsed(post_push_until)` AND `isDraft == false` AND `branch_pause_ready` AND CI-config self-verification satisfied (when applicable) AND the merge-readiness holds are clear (same two direction-aware rechecks as condition (a) — a paused exit on a ready PR with a live hold reads as "done, merge when approved", which is exactly the signal the hold exists to prevent. A live hold here takes condition (a)'s own hold action, not a fall-through: do NOT exit and do NOT fall through to (e) — note `exit:deploy-hold` / `exit:dependency-hold`, post the `### Deploy order` PR comment if not already posted, set `loop_reason = "wait_repoll"`, and continue the loop through the polling schedule under the same `hold_started_at` bound and `human:deploy-hold` / `human:dependency-hold` condition-(c) escalation — approval is never a precondition for waiting out a hold) AND the PR body's Prompt Trail is current per the core's sync gate (synchronize first; a failed sync exits BLOCKED with `prompt-trail:stale`)** (the unapproved pause may accept a proven approval-only `BLOCKED` state; approved/unexplained protection blocks never do):
  - Run the **QA handoff** below FIRST (same operations, table, and durable ledger as condition (a); helper scenario `clean_unapproved`) — preview QA runs in parallel with code review, so a clean unapproved PR still routes to the QA owner and its validated ticket moves to the QA-ready state. Skip execution if the handoff is already recorded `complete`. A failed operation appends the same non-blocking warning as in (a).
  - Only after every planned handoff operation has a durable `complete|failed` result, set `phases.monitor` to `"paused"` in state file. The paused exit never writes `complete` and never merges; code-review approval is still the gate for condition (a).
  - Output:
    ```text
    ✅ WORKFLOW PAUSED — PR #<number> is clean and marked ready for review.
    All checks passing. Sanity VERIFICATION: unreplied=0 confirmed across 2 clean polls separated by BOT_GRACE_WINDOW.
    Branch up to date. QA handoff recorded (assignee/reviewer + ticket → QA state). Awaiting human code-review approval. Re-run `/autonomy` to resume monitoring if needed.
    ```
  - **End the loop** (do NOT sleep and re-poll)

- **(e) If everything is clean BUT NOT `grace_elapsed(post_push_until)` OR NOT `stable_poll_confirmed`:**
  - Silent re-poll — no output. Sleep per the stable-poll schedule below, go back to Step 1

**Helper:** `grace_elapsed(ts) = (ts is null) OR (parse_utc(current_time) >= parse_utc(ts))`

**R2 review gate (Keeper repositories — pre-human review; evaluated between the draft-PR gate and (a)/(d)):**

Keeper's review flow routes implementer → R2 (`r2-keeper`, Keeper's review bot) → human (QA/reviewer routing): a PR is ready for HUMAN review only once R2 has approved it (user directive, 2026-08-11). The gate applies when the fresh `nameWithOwner`'s owner is exactly `Keeper-Dating` (fail-closed exact match, like the QA table; forks never match) and fires on any pass that reaches the point where (a)/(d) would otherwise fire — ready PR, gating checks passing, `all_feedback_addressed`, `grace_elapsed`, `stable_poll_confirmed` — while the gate is unsatisfied. Its sub-state is DERIVED from fresh PR data on every pass (no new state keys, so a resume or takeover re-derives everything): actor-posted ask comments carrying the anchor `<!-- autonomy:r2-request -->`, R2's formal reviews (REST login `r2-keeper[bot]`, account type `Bot`), human formal reviews (REST type `User`), and the two Decision Audit Trail records named below.

- **Satisfied — fall through to (a)/(d) — when:** R2's newest formal review approves the CURRENT head, judged STRUCTURALLY: its `commit_id` equals the fresh `headRefOid` AND its `r2-review-signals` blob reports `blocking_findings_count: 0` (or, where the platform permits, its GitHub state is literally APPROVED). Never key on GitHub's raw review state alone — R2's delivery contract forces self-authored reviews to `COMMENT` (R2 #1328 finding 3767068740), so a raw-state predicate can never fire — and never accept a result bound to an older head. A later push un-satisfies the gate, so EVERY fix round — including fixes for a HUMAN reviewer's feedback — goes back through R2 before any handback (user correction, 2026-08-12, superseding the 2026-08-11 first-human-review-disarms rule). The only permanent disarm is an explicit user waiver (`r2-gate:waived` in the Decision Audit Trail). On the pass where R2 approval first satisfies the gate on a PR with no human reviewer engaged yet, perform the deferred human handoff BEFORE falling through: request the routed human reviewers and set the single ball-holder assignee exactly as the draft-PR gate's step 2 specifies (same routing, precedence, and atomic replacement); the (a)/(d) QA handoff then runs at the exit as specified. When a human roundtrip is pending instead (a human reviewer's feedback was just addressed), satisfaction hands to condition (c)'s review-roundtrip handoff — re-request and reassign that reviewer.
- **Initial ask — NEVER automated:** with no prior actor ask comment, no R2 formal review, and no `r2-gate:authorized` trail record, the first ask requires explicit user authorization: record `human:user-confirm:r2-review-authorization` in `attempt_log` and exit through condition (c), asking the user plainly — everything else is done (ready, checks green, feedback addressed); should R2 be asked to review? On resume, explicit confirmation clears the key: append `r2-gate:authorized` to the Decision Audit Trail FIRST, then post the ask. An explicit "skip R2" answer appends `r2-gate:waived` instead and disarms the gate for this PR.
- **Posting an ask (authorized — the trail record, a prior actor ask comment, or any R2 formal review proves the initial gate was passed):** post ONE top-level PR comment whose visible body is exactly `@r2 please do the code review` — the `@r2` mention IS the trigger; a bare "R2 please..." (or any untagged variant) does not fire it (user corrections, 2026-08-12) — plus the `<!-- autonomy:r2-request -->` anchor. Always a FRESH comment, never an edit (the bot listens to comment creation); if a wrong or untagged ask exists, delete it and repost so exactly one live ask remains. Idempotency by fresh derivation: zero-or-one correctly-tagged actor ask newer than R2's newest formal review — if one already exists, never post another. Every ask AFTER R2's first formal review (its findings fixed and pushed) posts automatically at the next gate-firing pass — including the round that follows a HUMAN reviewer's feedback, since those fixes re-enter this gate too; the user is never re-asked for follow-up rounds.
- **Pending wait (an ask newer than R2's newest formal review):** an R2 session typically takes ~80 minutes but can exceed 105 with ZERO visible activity — no placeholder, no session marker — and the user's standing instruction is to wait for the reply (2026-08-11: "It can sometimes take longer than 105 minutes, just wait"). Every pass is a passive hold: `loop_reason = "wait_repoll"`, ≤60s chunks with progress, ticks never burn the work cap, and the `BOT_GRACE_WINDOW` continuous-hold bound does NOT apply — this wait has **no elapsed-time bound**. If the session must end before the review lands, the pending ask survives as PR data: a resume re-derives it and continues the wait. **Never push while an ask is pending** — a push supersedes the running R2 session and wastes it. Replies/acks may proceed; remediation for anything arriving mid-wait (feedback, CI, branch state) is queued and starts only after R2's formal review lands. Throughout R2 rounds the PR stays with the implementer side: the invoking user remains the sole assignee (set them if the field is empty at the first ask), and no reviewer request, assignee transfer, or QA handoff runs while the gate is unsatisfied.
- **R2 findings (newest formal review not APPROVED):** R2's review body and inline findings are BOT feedback — Step 2's normal machinery applies (verify every claim against the code, fix real issues, reply to every root, justify false positives, resolve threads), then push and let the loop settle; the next gate-firing pass re-asks automatically. Iterate until R2 approves.
- **No timer stall path:** elapsed time alone never posts a re-ask and never exits the gate — a fresh ask is posted only after R2's newest formal review lands and its findings are fixed and pushed, or on the user's explicit instruction. Only the user decides a pending ask is dead (they can re-trigger R2, waive the gate, or route to a human directly); a session that must end while waiting simply resumes the wait later.

#### QA handoff (repo-conditional — conditions (a) and (d))

Run this handoff at the FIRST clean exit — condition (a) (approved → `complete`) or condition (d) (clean but unapproved → `paused`). Preview QA runs in parallel with code review, so the paused exit transfers QA ownership too; it still never merges and never writes `complete`. Whichever exit fires second re-verifies the recorded operation postconditions instead of re-executing (a human reassignment in between is human action, not drift to correct). The helper scenario is `approved_qa` for condition (a) and `clean_unapproved` for condition (d); both plan identical operations. Resolve the exact repository identity with `gh repo view --json nameWithOwner --jq .nameWithOwner`; same-name forks fail closed. In Keeper repositories the R2 review gate precedes these exits, so the first clean exit — and this handoff — occurs only after R2 has approved the PR (or the user explicitly waived the gate).

| Exact `nameWithOwner`                 | GitHub PR assignee | Linear ticket assignee |
| ------------------------------------- | ------------------ | ---------------------- |
| `Keeper-Dating/matchmaking`           | `tjkeeper`         | Timothy Jhon Pascual   |
| `Keeper-Dating/keeper-lead-generator` | `tjkeeper`         | Timothy Jhon Pascual   |
| `Keeper-Dating/calculator-api`        | `tjkeeper`         | Timothy Jhon Pascual   |
| `Keeper-Dating/admin-portal`          | `shafqatukhan`     | Shafqat                |
| anything else                         | none — skip        | none — skip            |

This table restates `QA_OWNER_BY_REPOSITORY` in `scripts/handoff_decision.py`, which is canonical at runtime; a sync test in `scripts/test_handoff_decision.py` fails if the two drift.

The handoff transfers ownership AND stage: for a validated Linear ticket, the plan also moves the ticket to its team's QA-ready workflow state — ticket team `WEB` → **"Vercel Preview QA"**, `ADM` → **"Dev - Ready for QA"**; tickets on any other team get no state operation (move them manually if a QA state exists). Workflow-state IDs are team-scoped: resolve the ID by that exact name within the ticket's own team.

**QA rehearsal (advisory, non-blocking preflight — mapped repositories only):** before planning the handoff operations, assemble a rehearsal record with evidence appropriate to each Test-plan AC: runtime-observable ACs need evidence from the current preview deployment or runtime, bound to the AC ID and the current `headRefOid` and verified through trusted APIs (checks, deployments) — never a comment's claim or an arbitrary URL; documentation/test/CI/migration ACs use their natural artifacts. For changed UI surfaces, exercise initial-load and refresh transitions, the error paths of changed flows (including a stale-session retry), each sibling surface sharing a changed pattern (an affordance added to one step of a flow raises the same expectation on its neighbors), and any device- or browser-specific claim on the actual device profile — desktop emulation is not evidence for it. The record is ONE anchored PR comment (`<!-- autonomy:qa-rehearsal -->`; zero-or-one matching anchor, duplicates fail closed) so the QA owner actually receives it — terminal output alone is not delivery — ledgered as `handoffs.pr_artifacts` operation `qa-rehearsal:<head_sha>`. This preflight is advisory and never blocks the exit or fabricates evidence: unexercised items are listed in that comment by stable identifier — the mirrored `AC-n` ID for ticket-derived items, the Test-plan item ordinal (`TP-n`) for every other item including all items of a ticketless PR — never verbatim untrusted AC text; a delivery failure persists `failed` while the handoff continues, and the handoff warning then states that the QA owner did not receive the rehearsal artifact.

For a mapped repository with `write_path` set to `environment_tool` or `local_api`, resolve the target Linear user through that authorized tracker path before planning and persist its exact provider ID plus display name. With `write_path: none`, do not require or fabricate a QA-user provider ID: the helper records the unavailable Linear handoff after GitHub verification. Build the operation plan with `scripts/handoff_decision.py` and execute one pending operation at a time:

1. Build the helper input from durable `operation_results`. Before any API call, persist `handoffs.qa.scenario`, exact targets, and the first operation as `pending` with attempt/`started_at`. On resume, a pending helper result must produce `verify_before_retry`; verify the supplied postcondition before marking complete or persisting `retryable`. Never replay the mutation directly. The helper's `qa.*` operation IDs embed a target digest (repository, PR, assignee, ticket, QA user/state), so a ledger persisted for DIFFERENT targets — another PR, a re-keyed ticket, a changed owner map — never satisfies the current plan: prior-target terminal records are pruned with a warning and an in-flight prior-target record fails closed, mirroring the roundtrip generation contract.
2. **Replace the complete GitHub assignee set atomically** through the Issues API (a pull request is also an issue):
   ```bash
   jq -cn --arg login "$TARGET_LOGIN" '{assignees: [$login]}' |
     gh api --method PATCH "repos/$OWNER/$REPO/issues/$PR_NUMBER" --input -
   ```
   This is replacement, not additive assignment; stale third-party assignees and the implementer are removed in the same write. JSON construction prevents shell/JSON injection.
3. Re-fetch `gh pr view "$PR_NUMBER" --json assignees` and compare the sorted login array to the exact expected array. GitHub may silently omit an ineligible login; response success without the exact postcondition is failure.
4. Record the GitHub mutation and verification operations `complete|failed`, including attempts, response/evidence IDs, and verification timestamp.
5. If the tracker is Linear and a ticket was validated, pass `validated_ticket.identifier` as `issue_tracker.ticket_identifier` and `validated_ticket.provider_id` as `issue_tracker.ticket_provider_id`. For an authorized write path, also pass the resolved QA user provider ID/name and assign through `resolved_conventions.issue_tracker.write_path`:
   - `environment_tool`: use only the authorized environment/orchestrator mutation tool.
   - `local_api`: use the configured raw API key only in a persisted local session.
   - `none`: do not require QA-user resolution; after GitHub verification persist the Linear operation as failed/unavailable with `verified_at` and a non-empty `error`, and never switch paths implicitly.
     For an authorized path, assign the ticket by QA-user provider ID only; the display name is a cross-check. When the ticket's team has a mapped QA state (see the note under the table), also resolve that state's team-scoped ID by exact name and pass `issue_tracker.qa_state` (`provider_id` + `name`); if the state cannot be resolved (e.g. renamed in Linear), pass `qa_state: null` with a non-empty `qa_state_unresolved_reason` so the helper records a manual state move instead of blocking. Never relink or rename the PR — a title relink can regress the ticket's state.
   - If `ticket_required == false` and no ticket exists, plan no tracker operation; GitHub QA assignment still proceeds. If an exempt PR already has a validated ticket, hand it off normally.
6. For `environment_tool` or `local_api`, re-fetch the ticket through the same authorized path and verify the exact expected provider user ID and, when a state operation was planned, the exact expected workflow-state ID. Record the Linear mutation/verification operations `complete|failed`. For `none`, make no tracker call; the durable unavailable result from step 5 is the terminal Linear outcome.
7. Only after every planned operation has a durable terminal result may the firing exit write its terminal status — `complete` for condition (a), `paused` for condition (d). A dependency descendant the plan never attempted persists its rendered `skipped_dependency` record — `attempts: 0` and an `error` naming the failed dependency, never fabricated attempt evidence — so the terminal plan round-trips the state schema. Any failed operation appends `⚠️ QA handoff failed: assign <login> / ticket <ID> (assignee + QA state) manually.` but does not un-clean or block the PR.

On resume, inspect any pending operation's remote postcondition first. If it already holds, mark complete without repeating the mutation; otherwise retry within the three-attempt rule.

#### Review-roundtrip handoff (condition (c), human feedback only)

This handoff is eligible only when human review feedback is the sole block and the durable record proves, for each target reviewer: known non-bot/non-actor identity; every current inline root has a verified reply newer than its last edit; every current review body has been evaluated/acknowledged; all corresponding fixes are pushed; and no unaddressed blocker remains. Unknown/deleted accounts, bot accounts, edited feedback, or a push without replies are ineligible and stay manual blockers. In Keeper repositories the handback itself waits for R2 (user correction, 2026-08-12, superseding the 2026-08-11 disarm rule): eligibility additionally requires the R2 gate satisfied on the current head — R2's newest APPROVED review with no later push. When every other eligibility condition holds but R2 has not approved the fixed state, do NOT exit blocked: proceed to the R2 gate (its automatic ask, pending wait, and iterate machinery), and run this handback on the pass after the gate is satisfied.

For the sorted/deduplicated eligible reviewer set:

1. Persist `handoffs.review_roundtrip` targets and canonical `operation_results` before any call. Use the same `verify_before_retry` resume protocol as the QA handoff. The helper's operation IDs embed the feedback generation (a digest of the eligible reviewers' evidence — review IDs, edit timestamps, pushed fixes), so a completed earlier round's ledger never satisfies a later round: the planner ignores prior-generation terminal records (with a warning) and fails closed on a prior-generation record still in flight — verify that mutation's postcondition and record a terminal result, then re-plan.
2. Re-request each review as a separate idempotent operation (`gh pr edit <number> --add-reviewer <login>`). Persist and verify each reviewer independently so partial multi-reviewer success resumes safely.
3. Replace the complete assignee set with the exact eligible reviewer array using one Issues REST `PATCH` with `{ "assignees": [...] }`; do not use additive `--add-assignee`/`--remove-assignee` calls.
4. Re-fetch assignees and review requests. Assignees must equal the expected sorted set; requested reviewers must contain every target. Record verification per operation.
5. Leave the issue-tracker ticket where it currently is — with the implementer, or with the QA owner if a prior clean exit already ran the QA handoff. No compensating ticket write is needed during a roundtrip.
6. After every operation is durably `complete|failed`, write `phases.monitor: blocked` and emit the appropriate roundtrip message plus warnings for failed targets. Never claim “every comment replied” unless the durable eligibility proof still matches current edit timestamps.

If nothing was addressed, any target is ineligible, or another block co-fires, skip automatic reassignment and emit the normal manual BLOCKED result.

#### Draft-PR gate (flip draft → ready on the first clean pass after the grace window)

PRs are created as drafts in Phase 5 because Bugbot skips drafts and reviews each PR only ONCE — when it is first marked ready — and never re-scans later pushes. CodeRabbit reviews drafts, so the draft phase still gets CI + CodeRabbit coverage. This gate spends Bugbot's single review on final code and makes "ready for review" mean exactly that to human reviewers.

Evaluated on every Step 4 pass, after condition (c) and before (a)/(b)/(d)/(e). It FIRES when ALL of:

- condition (c) does NOT fire, AND
- `isDraft == true` (from the Step 4 re-fetch), AND
- `post_push_until != null`, AND
- **the merge-readiness holds are clear** — two rechecks, both direction-aware:
  - If `merge_readiness.deploy_order` is `"hazard_documented"` AND `merge_readiness.hazard_direction` is `"additive"` or `"mixed"`: re-verify applied-state NOW (merge-readiness.md Check 1 step 2, same credentials rule) — an additive-direction migration still `pending` on an environment the base branch deploys to, or a required backfill in `merge_readiness.backfill` not verified `complete`, means the draft-hold is still load-bearing: do NOT flip; note `ready:deploy-hold` in the iteration output (a wait, not a failure — never `attempt_log`), set `loop_reason = "wait_repoll"`, and continue the loop through the polling schedule — the tick refresh re-verifies applied-state, so a released hold is observed without burning work iterations; past `BOT_GRACE_WINDOW` of continuous hold time (`hold_started_at`), take the condition-(c) human-dependency exit defined for the (a)/(d) holds. The hold releases when applied-state shows the migration applied or the user explicitly overrides. A `"destructive"`-direction hazard does NOT hold the flip: its safe order is merge/deploy first, apply after (Check 1 step 3), so `pending` at flip time IS the documented safe state — holding on it strands the PR or pressures applying the drop early, the exact breakage Check 1 warns against. `unverified` applied-state (no credentials) does not hold the flip — the documented ordering is the mitigation where state cannot be read, and holding forever on unreadable state would strand every migration PR in draft.
  - If `merge_readiness.dependencies` is `"hazard_documented"` (Check 2 step 2: merged-but-not-live): re-verify the dependency's live state NOW (Check 2 step 2's method — applied-state for schema dependencies, deploy state for services). Still not live → do NOT flip; note `ready:dependency-hold` in the iteration output (a wait, not a failure — never `attempt_log`), set `loop_reason = "wait_repoll"`, and continue the loop through the polling schedule (the tick refresh re-verifies the dependency's live state; the same `BOT_GRACE_WINDOW` hold bound (`hold_started_at`) and condition-(c) human-dependency exit apply). `unverified` live state does not hold the flip, same rationale as above. AND
- all of: gating checks terminal/passing, `all_feedback_addressed`, `branch_pause_ready`, `grace_elapsed(post_push_until)`, the PR body's Prompt Trail current per the core's sync gate (synchronize before flipping; a failed sync blocks the flip with `prompt-trail:stale`), AND CI-config self-verification satisfied when applicable (Step 4's diff-conditional gate; draft-unrunnable `ready_for_review`-only workflows are excluded at the flip and verified after it). **Unlike the (a)/(d) exits, the flip deliberately does NOT require `stable_poll_confirmed`.** Flipping is not an exit; the loop continues with a fresh grace window. `post_push_until` MUST be armed whenever a draft enters monitoring, and a null timestamp never qualifies this gate.

Action when it fires (state first, action second — crash-safe ordering):

1. Persist `post_push_until = now + BOT_GRACE_WINDOW` and CLEAR `clean_poll_timestamps` in state. (If the session dies before step 2 completes, resume re-enters the loop with the PR still a draft and the grace window armed; the gate simply re-fires after fresh clean polls.)
2. Flip the PR: `gh pr ready <PR_NUMBER>`. If the command fails, log `ready:flip` in `attempt_log` and return to Step 1 — 3 attempts with the same signature trigger the standard 3-strike BLOCK via condition (c). After a successful flip, request the human review the flip exists to invite — EXCEPT in a repository under the R2 review gate (owner `Keeper-Dating`), where this entire request/assignee step is DEFERRED: the PR stays with the implementer side until R2 approves, and the R2 gate executes exactly this step at that point. The step: `gh pr edit <PR_NUMBER> --add-reviewer <login>` for each reviewer the repository's review-routing conventions or the user's standing routing guidance name for the diff's surfaces (Keeper-Dating/matchmaking: backend-only diffs → `motykadaw` or `michal-janicki`, whichever is more relevant to the ticket's domain — judge by recent history of the touched area, `motykadaw` when nothing distinguishes them; frontend/UI → `tjkeeper`; mixed → both sides' picks), and set exactly ONE of them as the sole assignee — the ball-holder; when several reviewers are routed, pick the one the repository's precedence convention names (Keeper: `tjkeeper` whenever the diff has frontend/UI surfaces for him, else the backend reviewer; same atomic replacement the handoffs above use). When no convention resolves a reviewer, skip the request rather than guess — the (a)/(d) handoffs still route ownership at exit. A failed request or assignment appends `⚠️ Reviewer request failed: request <login> manually.` to the flip output but never un-flips, blocks, or re-drafts the PR.
3. Output:
   ```text
   📣 PR #<number> marked ready for review — checks green, feedback addressed, branch current.
   Review requested: <logins; "deferred to the R2 review gate" in Keeper repositories; or "none resolved by routing — ownership routes at exit">.
   Bugbot's single per-PR review triggers on this flip. Continuing monitor loop to catch its feedback.
   ```
4. Return to Step 1, treating the flip exactly like a push event — the fresh grace window plus cleared clean polls give Bugbot's ~13-min scan the same coverage a post-push scan would get.

Rules enforced by this gate:

- The flip is autonomous. Its preconditions are the only authorization it needs: never pause a flip-eligible draft to ask the user whether to mark it ready or whom to ping, and never park a clean draft for the user to look over first — a stalled clean draft that pings nobody is exactly the failure this gate exists to remove (user directive, 2026-08-02). The Keeper R2 gate's initial-ask user question is NOT a flip pause: it fires only after the flip, at the first pass where the loop would otherwise exit clean (user directive, 2026-08-11).
- Conditions (a) and (d) MUST NOT fire while `isDraft == true`. Exiting the loop with a draft PR would strand it: Bugbot never runs, and humans never see it marked ready.
- If condition (c) fires (BLOCKED) while the PR is still a draft, LEAVE it as a draft. A blocked PR is by definition not ready for human review; the draft state is the correct signal to the team.
- Never convert a ready PR back to draft (takeover or otherwise) — Bugbot's single run cannot be re-armed by flipping state.
- If Bugbot is absent from the repo or its single run was already consumed (e.g., takeover of a PR that was marked ready at some point in its life), the flip simply produces no new feedback: the fresh grace window elapses, two clean polls confirm, and (a)/(d) fire normally on subsequent passes. Do NOT special-case or wait indefinitely for a Bugbot review that may never come.

#### Stable-poll gate (prevents exiting right as Bugbot posts a new comment)

Track `clean_poll_timestamps: []` as `{head_sha, observed_at}` records. This gate requires two clean observations of the same fresh PR head separated by at least `BOT_GRACE_WINDOW` before allowing exit. A head change clears the list and re-arms grace.

**Polling schedule:**

A single long sleep would violate the host contract. Enforce the stable-poll gate by elapsed-time comparison across async/≤60s wait chunks and iteration re-entries, with a brief progress update at least once per minute. Keep every wait INSIDE the turn; never implement a wait by ending the turn: a non-terminal turn end violates the core's Terminal-exit turn contract and strands the run on hosts without a guaranteed wake-up — that liveness guarantee, plus host portability, is the rationale. Cost is NOT the rationale: at these constants, chunked in-turn waiting at cache-read prices and a single post-TTL full re-read are the same order of magnitude, and every tick ROUND pays a cache-read of the whole accumulated context — which is why each tick MUST be one sequential wait→refresh operation in a single tool round where the host permits composing them, never concurrent and never one round per query. A wait primitive whose single call exceeds 60s is permissible only when it still yields a progress heartbeat at least once per minute — no uninterrupted silent interval may exceed 60 seconds — and stays under the ~5-minute prompt-cache TTL. Waiting changes when the next check runs, never what it evaluates.

> **This schedule is reached via conditions (b)/(e), the merge-readiness deploy/dependency holds** (draft-PR gate and exits (a)/(d))**, and the R2 gate's pending-ask wait** (no elapsed-time bound — wait for R2's reply; pushes and remediation deferred while pending). Every clean pre-grace, pre-stability, or hold wait sets `loop_reason = "wait_repoll"` before its ≤60s chunk; a hold-wait tick additionally re-verifies the held check in its refresh — for the R2 wait, whether R2's formal review has landed. When grace matures, the passive read-only pass promotes back to `work` before draft flip, handoff, pause, or completion. Thus required waiting never consumes the logical-work cap.

- After a Step 4 pass shows the canonical `unreplied_all == 0` (recording does NOT wait for `grace_elapsed` — grace and the stability window run CONCURRENTLY; `grace_elapsed(post_push_until)` remains an independent conjunct of every exit, so an exit still requires full grace AND a ≥`BOT_GRACE_WINDOW` observed-clean span on the exit head):
  1. Record `{head_sha: headRefOid, observed_at: now}` in `clean_poll_timestamps`:
     - If the list is empty → append (this becomes the FIRST observation; never overwritten until cleared).
     - If the list has exactly 1 entry → append (this becomes the MOST RECENT observation).
     - If the list already has 2 entries → **update only the second slot** to `now`; do NOT touch the first slot. This preserves the original first-observation timestamp so the measured gap keeps growing across iterations.
  2. If `clean_poll_timestamps` has exactly 1 entry → `stable_poll_confirmed = false`. Set `loop_reason = "wait_repoll"`, wait at most 60s with progress, and re-evaluate without incrementing the logical work counter.
  3. If both entries have the current `headRefOid` AND `(second.observed_at - first.observed_at) >= BOT_GRACE_WINDOW` → `stable_poll_confirmed = true`. Continue evaluating exit conditions.
  4. If observations use different SHAs, clear/re-arm. If the gap is too short, wait at most 60s and re-evaluate; elapsed timestamps, not one sleep duration, determine completion.

Keep **the FIRST entry and the MOST RECENT entry** in `clean_poll_timestamps` (not the two most recent). Subsequent observations update only the second slot; preserve the first until the gate fires or a dirty observation clears it.

**On any dirty observation** (new/edited/unacknowledged human or bot feedback on any surface, unresolved bot/human thread, non-empty `exhausted_feedback`, non-empty `manual_unknown_feedback`, `unreplied_all > 0`, or any push): CLEAR `clean_poll_timestamps` entirely and return to the appropriate processing step. The draft→ready flip also clears it. An acknowledgment-only iteration is still dirty for stability purposes; the next clean observation starts a new window.

**Condition ordering note:** On every Step 4 pass (including re-entries after stable-poll sleeps), evaluate conditions in the order **`(c) → draft-PR gate → R2 gate → (a) → (b) → (d) → (e)`** — first match wins. Condition (c) MUST come first so any BLOCK trigger (terminal exhaustion, non-empty `manual_unknown_feedback`, human `CHANGES_REQUESTED`, `unresolved_human_threads > 0`, exhausted ack post) cannot be silently bypassed by an APPROVED match in (a) or a grace-window match in (b). The draft-PR gate comes next: while `isDraft == true`, a pass that satisfies the clean-pass preconditions (see the gate definition) flips the PR to ready and returns to Step 1 instead of exiting, so (a)/(d) only ever fire on a ready PR. The R2 gate follows in Keeper repositories: an otherwise exit-ready pass asks/waits on R2 instead of exiting until the gate is satisfied. After (c) is cleared, the PR is ready, and the R2 gate is satisfied, (a) fires for `human APPROVED + grace + stable_poll_confirmed + feedback + merge-readiness holds clear` and completes the workflow; (d) fires for the same preconditions without approval and pauses. If approval lands between the first clean poll and the second, the second re-evaluation picks up the new `reviewDecision` and (a) takes precedence over (d) — no additional polling is required after approval.

---

### PHASE_6_SELF_REVIEW (Diff-Scoped Post-Fix Review)

Common procedure referenced by Phase 6 Steps 1 (step 9), 2 (sub-step 10a), and 3 (sub-step 3a in the out-of-date flow, step 9 in the conflict flow) — called within a monitor loop iteration — and by two pre-monitor-loop call sites where `monitor_iterations` is still 0: Phase 4 step 7 (takeover fixes → `session_id` like `"phase_4_takeover_iter0_call1"`) and Phase 4b merge-readiness fixes (→ `"phase_4b_iter0_call1"`).

**Fallback chain inside the monitor loop:** uses the same review-tool fallback chain as **Phase 4's "Tool selection is mandatory with fallback chain" section** (items 1–5 of the chain: gstack `/review`, `octo:review`, `feature-dev:code-reviewer`, `general-purpose`, BLOCK). Reference to "Phase 4 step 4" elsewhere refers to running `QUALITY_CHECK_STEPS`, not the review fallback chain. Including the `general-purpose` subagent fallback is especially important when `change_type == "skill_only"`. The rule is: **never BLOCK before the chain is exhausted** — an unavailable or failing tool at items 1–3 must fall through to the next item, log the degraded review path in `gstack_integration.review.notes`, and continue, rather than aborting the iteration. This matters because the monitor loop cannot escalate to the user without aborting cleanly, and items 1–3 are routinely unavailable (no gstack, no codex, `feature-dev` not installed) for reasons that say nothing about the diff.

That is **not** a licence to skip the review. If `general-purpose` — item 4, which is effectively always available — also fails, the chain is exhausted and item 5 applies: **BLOCK**. Self-review is mandatory and may not be waived, so continuing past an exhausted chain would push unreviewed code. The two cases are distinct: tool unavailable mid-chain → fall through and continue; final fallback failed → BLOCK and surface it.

**`session_id` uniqueness:** The procedure runs at varying points within a monitor-loop iteration. `state.monitor_iterations` is persisted at the **TOP** of each iteration (see Phase 6 pseudocode — increment + state write happen as the first action of each loop pass). So when this procedure reads `state.monitor_iterations`, it gets the current iteration number, NOT the previous one. To ensure session_ids are unique even within a single iteration (multiple sub-steps may invoke this procedure), the procedure also reads-and-increments `state.monitor_self_review_call_count`:

`session_id = "{phase_context}_iter{state.monitor_iterations}_call{call_number}"`

where `call_number` is the post-increment value of `state.monitor_self_review_call_count`. The counter starts at 0, is reset to 0 at iteration TOP (immediately after `monitor_iterations` is bumped), and increments to 1, 2, 3, ... as the procedure is called within the iteration.

```text
PHASE_6_SELF_REVIEW(phase_context, REVIEW_BASE):
  # Read-modify-write to STATE — the counter must survive between sub-step calls
  # within the same iteration (Phase 6 Step 1, 2, and 3 may each invoke this).
  # Reading state.monitor_self_review_call_count, incrementing, and writing back
  # MUST be a single atomic update inside this procedure; otherwise multiple
  # invocations could collide on the same call number.
  state.monitor_self_review_call_count = (state.monitor_self_review_call_count or 0) + 1
  call_number                          = state.monitor_self_review_call_count
  session_id = "{phase_context}_iter{state.monitor_iterations}_call{call_number}"

  1. REVIEW_FILES = git diff --name-only -z "$REVIEW_BASE"..HEAD, parsed with a
     NUL-safe reader (canonical boundary rule) so unusual filenames cannot
     drop a changed file from mandatory review scope.
     If empty:
       # Do NOT return yet. An empty committed diff says nothing about the
       # working tree: `git diff` here excludes uncommitted modifications and
       # untracked files, so returning early would skip step 7 and let local
       # changes bypass review entirely.
       Run step 7's clean-tree check now (tracked + untracked, per step 7).
       If the tree is clean  → return (genuinely no changes to review)
       If the tree is dirty  → the caller left work uncommitted; commit or
                               surface it, then re-enter with a REVIEW_BASE
                               that covers it. Never return on a dirty tree.
  2. Run the review tool (same fallback chain as Phase 4), scoped to REVIEW_FILES.
     Before this and before every later pass in this procedure, recompute the
     diff-triggered review focus lines from THIS session's
     git diff "$REVIEW_BASE"..HEAD and append them to every review prompt
     (definition in Phase 4); sessions never reuse another session's triggers.
     Tier floor: these are review-response fixes — never the Small tier (Phase 4
     auto-scaling; Review-Fix Integrity in merge-readiness.md). Apply the
     test-integrity tripwire there to every commit in $REVIEW_BASE..HEAD before
     running the review tool — AND to every fix commit this procedure itself
     creates (steps 4 and 5e), at commit time; the pre-review scan covers
     inherited commits only, so a fix commit created after it is unscanned
     until its own tripwire pass runs.
  3. Log all findings to finding_ledger: session_id, phase=phase_context, pass_number=1
     Initialize convergence[session_id] = {
       pass_actionable_counts: [open_count],
       last_diff_content_hash: SHA256(git diff "$REVIEW_BASE"..HEAD),
       prev_diff_content_hash: null,
       adversarial_triggered: false
     }
  4. Fix each actionable finding, commit. Append "fixed" resolution entries.
     Mark false positives with justification. Append "false_positive" entries.
     pass_number = 1
     files_changed_in_last_pass = files changed by pass-one fixes (may be empty)
  5. While files_changed_in_last_pass is non-empty OR open findings remain
     (no pass cap — reviewing continues until a pass leaves nothing to review):
     a. Re-union TOUCHED_FILES with files_changed_in_last_pass
     b. Re-run QUALITY_CHECK_STEPS, commit auto-fixes (boundary check)
     c. pass_number += 1; next-pass scope = the set union of files with open
        findings from the previous pass, `files_changed_in_last_pass`, and
        direct consumers of exported symbols changed by the previous pass's
        fixes (consumer-widening rule in merge-readiness.md)
     d. Run review tool on that scope, log findings: pass_number
     e. Fix actionable, commit. Append "fixed" entries.
        Mark false positives. Append "false_positive" entries.
     f. files_changed_in_last_pass = files changed by this pass's fixes (may be empty)
     g. For open findings from the previous pass absent in this pass:
        append "auto_closed" entries
     h. Update convergence[session_id]
     i. Apply ALL convergence rules (1-5), scoped to session_id:
        - Rule 1 (reappearance) → BLOCK
        - Rule 2 (oscillation) → BLOCK
        - Rule 3 (non-decrease) → adversarial escalation (Phase 4 step 6a). If unresolved → BLOCK
        - Rule 4 (cross-reviewer dispute) → adversarial escalation. If unresolved → BLOCK
        - If that escalation changes files, union them into TOUCHED_FILES and
          files_changed_in_last_pass and continue: the loop is uncapped, so the
          next ordinary pass reviews them — adversarial code never approves
          itself and never ships unreviewed.
  6. Rule 5 (clean-pass exit, no cap): the loop exits only when a pass leaves
     no open findings and no files changed by fixes. Rules 1-4 BLOCK on
     divergence; a rising pass count alone never does, and exiting with open
     findings or unreviewed fix commits is never allowed.
  7. Verify clean working tree (git status --porcelain=v1 should be empty — tracked AND untracked)
```

---
