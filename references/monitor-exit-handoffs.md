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

- If ANY gating check has `bucket == "pending"`: do NOT evaluate exit conditions. Sleep 60 seconds, go back to Step 1.
- If ANY gating check has `bucket == "fail"` or `bucket == "cancel"`: do NOT evaluate exit conditions. Go back to Step 1 immediately.
- Only proceed if every gating check has `bucket` in `{"pass", "skipping"}` and every excluded check has persisted repository-policy evidence.

#### MANDATORY VERIFICATION GATE

Before evaluating any exit condition that ends the loop (conditions a, c, d), you MUST execute and print a sanity-check verification block. This is a hard precondition: declaring exit without printing this block is a workflow violation.

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
- The verification block must be RE-RUN at the start of every Step 4 pass. Do NOT cache the result across iterations.

**Why this gate exists:** Without it, agents can mistakenly declare PAUSED while bot comments remain unreplied (observed failure mode: agent reports "all 30 replied" while 1 new Bugbot comment is open from a recent rescan). The sanity block gives a quick visual check; the canonical rules are the authority.

#### Exit conditions

After confirming all checks are terminal and passing AND the verification gate is satisfied, evaluate the conditions below in this exact order — **first match wins**. The order is:

**(c) → draft-PR gate → (a) → (b) → (d) → (e)**

Condition (c) is checked FIRST so that terminal exhaustion, `CHANGES_REQUESTED`, and unresolved human threads cannot be bypassed by an APPROVED match in (a) or a re-poll match in (b). The draft-PR gate (defined below, after the lettered conditions) sits immediately after (c); it is not an exit — when it fires, it flips the draft PR to ready and continues the loop. The conditions are written below in lettered order for readability, but the FIRING ORDER is (c) → draft-PR gate → (a) → (b) → (d) → (e).

- **(a) If `reviewDecision == "APPROVED"` AND `grace_elapsed(post_push_until)` AND `all_feedback_addressed` AND `stable_poll_confirmed` AND `isDraft == false` AND `branch_completion_ready`:**
  - (See canonical definition of `all_feedback_addressed` above in the Step 4 preamble. See "Stable-poll gate" below for `stable_poll_confirmed` — the same two-clean-polls-separated-by-`BOT_GRACE_WINDOW` rule applies to condition (a) as well as (d). No exit, whether complete or paused, may fire without two separated clean polls — the gate exists to catch Bugbot comments that arrive during the grace window.)
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
  - Track clean polls per the stable-poll gate below (append `{head_sha, observed_at}` only when canonical feedback is fully clean and grace elapsed); confirmation requires two observations of the same head separated by `BOT_GRACE_WINDOW`.
  - Output:
    ```text
    ⏳ PR approved but bot grace window active (<M> min remaining) OR waiting on second clean poll. Re-polling to catch any late feedback.
    ```
  - Sleep per the stable-poll schedule below, go back to Step 1

- **Hard cap:** 50 logical work/remediation passes. Passive grace/stability poll ticks are tracked separately and bounded by elapsed-time deadlines, so required clean waiting or a bot re-arm cannot consume the work cap.
  - **Note:** 540s is an aggregate watch deadline. Poll the async session in ≤60s chunks with progress; counters use the Step 1 head+pending-set signature and clear on settle/head change.

- **(c) If stuck**, the conditions that fire BLOCKED are (OR-joined):
  - same `ci:`, `conflict:`, or `ready:` failure signature in `attempt_log` has 3+ attempts (`ready:flip` is logged by the draft-PR gate when `gh pr ready` fails)
  - OR `unreplied_all` is non-empty AND `unreplied_actionable` is empty — all unreplied inline bot comments exhausted
  - OR `exhausted_feedback` is non-empty, regardless of whether a warning reply/ack succeeded
  - OR `manual_unknown_feedback` is non-empty
  - OR `manual_branch_protection_blockers` is non-empty (approved PR still blocked by a human-only ruleset/code-owner/additional-approval gate)
  - OR `reviewDecision == "CHANGES_REQUESTED"` — a human reviewer explicitly asked for changes; this workflow doesn't auto-resolve human feedback
  - OR `unresolved_human_threads > 0` — at least one human-authored inline thread has `isResolved: false` on GitHub; the workflow does not auto-resolve human review concerns (see Phase 6 Step 2 → "Detect unaddressed human inline threads")

  Action when condition (c) fires:
  - **Review-roundtrip handoff (conditional):** if `CHANGES_REQUESTED` and/or `unresolved_human_threads` are the ONLY triggers (no CI/conflict/ready/protection blocker and both feedback blocker maps empty), evaluate the durable per-reviewer record. Eligibility still requires complete current reply/ack/push evidence and a known non-bot non-actor reviewer.
  - For eligible reviewers, run the **Review-roundtrip handoff** below before terminal state: persist per-reviewer operations pending, execute/verify them, and store complete or failed. Ineligible or unknown/deleted authors remain manual blockers and are never assignment targets.
  - After conditional handoff operations have durable terminal results (or no handoff was eligible), set `phases.monitor: "blocked"` and stop the loop. A resume with pending operations re-fetches postconditions before retry.
  - Notify user with clear explanation of what's blocking
  - For exhausted inline bot comments: `⚠️ WORKFLOW BLOCKED — N bot review comment(s) could not be addressed automatically. Flagged for human review.`
  - For exhausted feedback: `⚠️ WORKFLOW BLOCKED — N feedback item(s) reached the automatic-attempt limit. Warning replies do not clear the blocker; human review is required.`
  - For `CHANGES_REQUESTED` and/or unresolved human threads where the roundtrip handoff ran (feedback addressed this session): `⚠️ WORKFLOW BLOCKED — awaiting <reviewer>'s re-review. Roundtrip complete: feedback addressed, every comment replied to, review re-requested, PR reassigned to <reviewer>. Re-invoke /autonomy after their re-review.`
  - For `CHANGES_REQUESTED` where the handoff did NOT run (single message; if `unresolved_human_threads > 0` also fires, the CHANGES_REQUESTED message subsumes it — emit ONE message, not both): `⚠️ WORKFLOW BLOCKED — Human reviewer requested changes. Address them and resolve all open inline threads on GitHub, then have the reviewer re-request review; re-invoke /autonomy afterward.`
  - For unresolved human threads only (no CHANGES_REQUESTED, handoff did NOT run): `⚠️ WORKFLOW BLOCKED — N unresolved human inline thread(s). Address the comments, then have a human mark the threads as resolved on GitHub. Re-invoke /autonomy afterward.`
  - Do NOT keep retrying the same failing approach
  - A successful exhaustion warning post prevents duplicate notifications only; it never satisfies `all_feedback_addressed`.

- **(d) If everything is clean AND `all_feedback_addressed` AND `stable_poll_confirmed` AND `isDraft == false` AND `branch_pause_ready`** (the unapproved pause may accept a proven approval-only `BLOCKED` state; approved/unexplained protection blocks never do):
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

#### QA handoff (repo-conditional — conditions (a) and (d))

Run this handoff at the FIRST clean exit — condition (a) (approved → `complete`) or condition (d) (clean but unapproved → `paused`). Preview QA runs in parallel with code review, so the paused exit transfers QA ownership too; it still never merges and never writes `complete`. Whichever exit fires second re-verifies the recorded operation postconditions instead of re-executing (a human reassignment in between is human action, not drift to correct). The helper scenario is `approved_qa` for condition (a) and `clean_unapproved` for condition (d); both plan identical operations. Resolve the exact repository identity with `gh repo view --json nameWithOwner --jq .nameWithOwner`; same-name forks fail closed.

The rows below are placeholder examples — replace them (together with the matching `QA_OWNER_BY_REPOSITORY` defaults in `scripts/handoff_decision.py` and the fixtures in `scripts/test_handoff_decision.py`) with your organization's repositories and QA owners.

| Exact `nameWithOwner`        | GitHub PR assignee | Linear ticket assignee |
| ---------------------------- | ------------------ | ---------------------- |
| `example-org/web-app`        | `alice-qa`         | Alice Example          |
| `example-org/marketing-site` | `alice-qa`         | Alice Example          |
| `example-org/api-service`    | `alice-qa`         | Alice Example          |
| `example-org/admin-portal`   | `bob-qa`           | Bob Example            |
| anything else                | none — skip        | none — skip            |

The handoff transfers ownership AND stage: for a validated Linear ticket, the plan also moves the ticket to its team's QA-ready workflow state — ticket team `WEB` → **"Preview QA"**, `ADM` → **"Ready for QA"**; tickets on any other team get no state operation (move them manually if a QA state exists). Workflow-state IDs are team-scoped: resolve the ID by that exact name within the ticket's own team. The shipped `WEB`/`ADM` values are placeholder examples — keep this paragraph and `QA_STATE_NAME_BY_TEAM` in `scripts/handoff_decision.py` in sync when you replace them.

For a mapped repository with `write_path` set to `environment_tool` or `local_api`, resolve the target Linear user through that authorized tracker path before planning and persist its exact provider ID plus display name. With `write_path: none`, do not require or fabricate a QA-user provider ID: the helper records the unavailable Linear handoff after GitHub verification. Build the operation plan with `scripts/handoff_decision.py` and execute one pending operation at a time:

1. Build the helper input from durable `operation_results`. Before any API call, persist `handoffs.qa.scenario`, exact targets, and the first operation as `pending` with attempt/`started_at`. On resume, a pending helper result must produce `verify_before_retry`; verify the supplied postcondition before marking complete or persisting `retryable`. Never replay the mutation directly.
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
7. Only after every planned operation has a durable terminal result may the firing exit write its terminal status — `complete` for condition (a), `paused` for condition (d). Any failed operation appends `⚠️ QA handoff failed: assign <login> / ticket <ID> (assignee + QA state) manually.` but does not un-clean or block the PR.

On resume, inspect any pending operation's remote postcondition first. If it already holds, mark complete without repeating the mutation; otherwise retry within the three-attempt rule.

#### Review-roundtrip handoff (condition (c), human feedback only)

This handoff is eligible only when human review feedback is the sole block and the durable record proves, for each target reviewer: known non-bot/non-actor identity; every current inline root has a verified reply newer than its last edit; every current review body has been evaluated/acknowledged; all corresponding fixes are pushed; and no unaddressed blocker remains. Unknown/deleted accounts, bot accounts, edited feedback, or a push without replies are ineligible and stay manual blockers.

For the sorted/deduplicated eligible reviewer set:

1. Persist `handoffs.review_roundtrip` targets and canonical `operation_results` before any call. Use the same `verify_before_retry` resume protocol as the QA handoff.
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
- all of: gating checks terminal/passing, `all_feedback_addressed`, `branch_pause_ready`, AND `grace_elapsed(post_push_until)`. **Unlike the (a)/(d) exits, the flip deliberately does NOT require `stable_poll_confirmed`.** Flipping is not an exit; the loop continues with a fresh grace window. `post_push_until` MUST be armed whenever a draft enters monitoring, and a null timestamp never qualifies this gate.

Action when it fires (state first, action second — crash-safe ordering):

1. Persist `post_push_until = now + BOT_GRACE_WINDOW` and CLEAR `clean_poll_timestamps` in state. (If the session dies before step 2 completes, resume re-enters the loop with the PR still a draft and the grace window armed; the gate simply re-fires after fresh clean polls.)
2. Flip the PR: `gh pr ready <PR_NUMBER>`. If the command fails, log `ready:flip` in `attempt_log` and return to Step 1 — 3 attempts with the same signature trigger the standard 3-strike BLOCK via condition (c).
3. Output:
   ```text
   📣 PR #<number> marked ready for review — checks green, feedback addressed, branch current.
   Bugbot's single per-PR review triggers on this flip. Continuing monitor loop to catch its feedback.
   ```
4. Return to Step 1, treating the flip exactly like a push event — the fresh grace window plus cleared clean polls give Bugbot's ~13-min scan the same coverage a post-push scan would get.

Rules enforced by this gate:

- Conditions (a) and (d) MUST NOT fire while `isDraft == true`. Exiting the loop with a draft PR would strand it: Bugbot never runs, and humans never see it marked ready.
- If condition (c) fires (BLOCKED) while the PR is still a draft, LEAVE it as a draft. A blocked PR is by definition not ready for human review; the draft state is the correct signal to the team.
- Never convert a ready PR back to draft (takeover or otherwise) — Bugbot's single run cannot be re-armed by flipping state.
- If Bugbot is absent from the repo or its single run was already consumed (e.g., takeover of a PR that was marked ready at some point in its life), the flip simply produces no new feedback: the fresh grace window elapses, two clean polls confirm, and (a)/(d) fire normally on subsequent passes. Do NOT special-case or wait indefinitely for a Bugbot review that may never come.

#### Stable-poll gate (prevents exiting right as Bugbot posts a new comment)

Track `clean_poll_timestamps: []` as `{head_sha, observed_at}` records. This gate requires two clean observations of the same fresh PR head separated by at least `BOT_GRACE_WINDOW` before allowing exit. A head change clears the list and re-arms grace.

**Polling schedule:**

A single long sleep would violate the host contract. Enforce the stable-poll gate by elapsed-time comparison across async/≤60s wait chunks and iteration re-entries, with a brief progress update at least once per minute. Keep every wait INSIDE the turn (an async wait polled in ≤60s chunks); never implement a wait by ending the turn or scheduling a wake-up longer than the provider's prompt-cache TTL (~5 minutes) — a turn-ending wait past the TTL forces the next pass to re-read the entire accumulated context at full input price, which costs far more than the poll it defers. The ≤60s chunk bound is therefore a cost invariant as well as a host-contract one: waiting changes when the next check runs, never what it evaluates.

> **This schedule is reached only via conditions (b)/(e).** Every clean pre-grace or pre-stability wait sets `loop_reason = "wait_repoll"` before its ≤60s chunk. When grace matures, the passive read-only pass promotes back to `work` before draft flip, handoff, pause, or completion. Thus required waiting never consumes the logical-work cap.

- After a Step 4 pass shows the canonical `unreplied_all == 0` AND `grace_elapsed(post_push_until)`:
  1. Record `{head_sha: headRefOid, observed_at: now}` in `clean_poll_timestamps`:
     - If the list is empty → append (this becomes the FIRST observation; never overwritten until cleared).
     - If the list has exactly 1 entry → append (this becomes the MOST RECENT observation).
     - If the list already has 2 entries → **update only the second slot** to `now`; do NOT touch the first slot. This preserves the original first-observation timestamp so the measured gap keeps growing across iterations.
  2. If `clean_poll_timestamps` has exactly 1 entry → `stable_poll_confirmed = false`. Set `loop_reason = "wait_repoll"`, wait at most 60s with progress, and re-evaluate without incrementing the logical work counter.
  3. If both entries have the current `headRefOid` AND `(second.observed_at - first.observed_at) >= BOT_GRACE_WINDOW` → `stable_poll_confirmed = true`. Continue evaluating exit conditions.
  4. If observations use different SHAs, clear/re-arm. If the gap is too short, wait at most 60s and re-evaluate; elapsed timestamps, not one sleep duration, determine completion.

Keep **the FIRST entry and the MOST RECENT entry** in `clean_poll_timestamps` (not the two most recent). Subsequent observations update only the second slot; preserve the first until the gate fires or a dirty observation clears it.

**On any dirty observation** (new/edited/unacknowledged human or bot feedback on any surface, unresolved bot/human thread, non-empty `exhausted_feedback`, non-empty `manual_unknown_feedback`, `unreplied_all > 0`, or any push): CLEAR `clean_poll_timestamps` entirely and return to the appropriate processing step. The draft→ready flip also clears it. An acknowledgment-only iteration is still dirty for stability purposes; the next clean observation starts a new window.

**Condition ordering note:** On every Step 4 pass (including re-entries after stable-poll sleeps), evaluate conditions in the order **`(c) → draft-PR gate → (a) → (b) → (d) → (e)`** — first match wins. Condition (c) MUST come first so any BLOCK trigger (terminal exhaustion, non-empty `manual_unknown_feedback`, `CHANGES_REQUESTED`, `unresolved_human_threads > 0`, exhausted ack post) cannot be silently bypassed by an APPROVED match in (a) or a grace-window match in (b). The draft-PR gate comes next: while `isDraft == true`, a pass that satisfies the clean-pass preconditions (see the gate definition) flips the PR to ready and returns to Step 1 instead of exiting, so (a)/(d) only ever fire on a ready PR. After (c) is cleared and the PR is ready, (a) fires for `APPROVED + grace + stable_poll_confirmed + feedback` and completes the workflow; (d) fires for the same preconditions without approval and pauses. If approval lands between the first clean poll and the second, the second re-evaluation picks up the new `reviewDecision` and (a) takes precedence over (d) — no additional polling is required after approval.

---

### PHASE_6_SELF_REVIEW (Diff-Scoped Post-Fix Review)

Common procedure referenced by Phase 6 Steps 1 (sub-step 8a), 2 (sub-step 10a), and 3 (sub-steps 3a, 7a) — called within a monitor loop iteration — and by Phase 4 step 7 (takeover fixes, called before the monitor loop begins; `monitor_iterations` will be 0 at that point, producing `session_id` like `"phase_4_takeover_iter0_call1"`).

**Fallback chain inside the monitor loop:** uses the same review-tool fallback chain as **Phase 4's "Tool selection is mandatory with fallback chain" section** (items 1–5 of the chain: gstack `/review`, `octo:review`, `feature-dev:code-reviewer`, `general-purpose`, BLOCK). Reference to "Phase 4 step 4" elsewhere refers to running `QUALITY_CHECK_STEPS`, not the review fallback chain. Including the `general-purpose` subagent fallback is especially important when `change_type == "skill_only"` — the loop must NEVER BLOCK on review-tool unavailability mid-iteration, since the monitor loop has no way to escalate to the user without aborting cleanly. Fall through to `general-purpose`, log the degraded review path in `gstack_integration.review.notes`, and continue.

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

  1. REVIEW_FILES = git diff --name-only $REVIEW_BASE..HEAD
     If empty → return (no code changes to review)
  2. Run the review tool (same fallback chain as Phase 4), scoped to REVIEW_FILES
  3. Log all findings to finding_ledger: session_id, phase=phase_context, pass_number=1
     Initialize convergence[session_id] = {
       pass_actionable_counts: [open_count],
       last_diff_content_hash: SHA256(git diff $REVIEW_BASE..HEAD),
       prev_diff_content_hash: null,
       adversarial_triggered: false
     }
  4. Fix each actionable finding, commit. Append "fixed" resolution entries.
     Mark false positives with justification. Append "false_positive" entries.
     files_changed_in_last_pass = files changed by pass-one fixes (may be empty)
  5. If files_changed_in_last_pass is non-empty (mandatory re-review when fixes changed files):
     a. Re-union TOUCHED_FILES with files_changed_in_last_pass
     b. Re-run QUALITY_CHECK_STEPS, commit auto-fixes (boundary check)
     c. Pass 2 scope = the set union of files with open findings from pass 1
        and `files_changed_in_last_pass`
     d. Run review tool on pass-2 scope, log findings: pass_number=2
     e. Fix actionable, commit. Append "fixed" entries.
        Mark false positives. Append "false_positive" entries.
     f. files_changed_in_last_pass = files changed by pass-two fixes (may be empty)
     g. Re-union TOUCHED_FILES with pass-two fix files
     h. Re-run QUALITY_CHECK_STEPS, commit auto-fixes (boundary check)
     i. For open findings from pass 1 absent in pass 2: append "auto_closed" entries
     j. Update convergence[session_id]
     k. Apply ALL convergence rules (1-5), scoped to session_id:
        - Rule 1 (reappearance) → BLOCK
        - Rule 2 (oscillation) → BLOCK
        - Rule 3 (non-decrease) → adversarial escalation (Phase 4 step 6a). If unresolved → BLOCK
        - Rule 4 (cross-reviewer dispute) → adversarial escalation. If unresolved → BLOCK
        - If that escalation changes files, union them into TOUCHED_FILES and
          files_changed_in_last_pass, then BLOCK: the two ordinary review passes
          are exhausted and adversarial code cannot approve itself.
  6. Rule 5: If (any open findings remain after pass 2 in the finding_ledger for this session_id)
     OR (files_changed_in_last_pass is non-empty after pass 2)
     → BLOCK unconditionally, notify user. Final-pass fixes cannot be left unreviewed.
  7. Verify clean working tree (git diff --name-only HEAD should be empty)
```

---
