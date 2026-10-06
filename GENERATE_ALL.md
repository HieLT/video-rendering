# Reliable queue + Generate All Ready (Step 4)

Implemented using the existing TaskStore, tasks.db, asyncio runners, BrowserPool and
video_worker_ui.py path. Schema stays at version 4; no new tables/migration or queue service.

## Root cause and admission audit

Previously: request -> SQLite queued row -> asyncio _run_task -> API-key limiter ->
premature processing update -> BrowserPool semaphore -> one account scan -> account lock -> worker.
BrowserPool skipped every locked account then raised `No available accounts in pool` after
that single scan. The API accepted busy accounts but the executor did not wait for them.
Processing was marked before acquiring the global semaphore or a usable account.
Also, all_accounts_limited / all_accounts_quota_blocked ignored cooling accounts, producing
false admission rejection when a usable account could return after cooldown.

| Existing resource/state | Meaning and Step 4 policy |
|---|---|
| asyncio per-account lock | Busy browser profile, including other browser sessions: wait |
| BrowserPool semaphore | Global execution capacity: wait while queued |
| KeyConcurrencyLimiter | Per-key concurrency, 0 unlimited: wait while queued |
| scheduling=false | Disabled: not a retryable candidate |
| login_ok != 1 / auth_state != active | Invalid/unverified authentication: not eligible |
| cooldown_until | Risk cooldown, existing 1800-second policy: wait if otherwise eligible |
| rate_limited / used_today >= DAILY_LIMIT | Daily window exhausted: do not wait for next day |
| quota_blocked / known credit_balance < 2 | Not usable in this window |
| missing profile | Failed configuration: skip it; fail if no usable alternatives |

Daily/credit exhaustion uses existing typed errors and failure_code `429`.
No eligible account uses NoUsableAccountsError / failure_code `NO_USABLE_ACCOUNTS`.
HTTP submission keeps existing admission errors: no accounts 503, exhausted quota 429.
If a pool becomes unusable after accepting a task, its runner fails clearly instead of waiting forever.
Cooling accounts that would be eligible after cooldown prevent false all-exhausted rejection.

## Queue and transitions

BrowserPool.generate_video now wraps the original account-selection/worker invocation with
small FIFO admission gates. A gate is released immediately after acquiring an account lock,
so multiple accounts still execute in parallel, up to the existing semaphore/key limits.
If all eligible accounts are busy/cooling, the original selection returns a typed temporary
admission error. The admission head yields with asyncio.sleep(1.0), holding no pool semaphore,
then requeues. Other waiting gates do not poll. This also notices admin configuration changes
and cooldown expiry without a separate scheduler. Recheck latency is normally about one second.

Fairness is near FIFO within admitted runners, with bounded retry rotation. It is not an
absolute global FIFO guarantee across API keys, remote image downloads or recovery sessions.
Startup queued recovery now orders by created_at, batch_index, id.
Only temporary admission errors are retried; arbitrary worker failures are never resubmitted.
The original worker rotation for daily/credit/risk failures remains intact.

Task transitions:

- queued: accepted and waiting for key capacity, references, pool capacity, account availability.
- processing + started_at: only inside on_account_selected, after semaphore and account lock.
- queued again: existing risk/quota rotation found only temporarily unavailable alternatives.
- completed / failed / needs_recovery: existing execution outcomes.
- stopped: monitoring cancelled; no new generation starts later for this task.

Per-key capacity is held by an admitted runner while it waits for an account, preserving the
existing limiter model. Worker, Dola submission, polling, MP4 download and recovery pipeline
are reused; no worker or Dola rewrite.

## Generate All contract

`POST /api/admin/projects/{project_id}/generate` (202), no request body required.
Uses existing X-Admin-Key plus the same client Authorization policy as per-scene generation.

```json
{
  "project_id": "project-id",
  "requested": 3,
  "created": 2,
  "skipped": 1,
  "batch_id": "batch-identifier",
  "tasks": [
    {"scene_id": "scene-1", "scene_number": 1, "task_id": "task-1"},
    {"scene_id": "scene-2", "scene_number": 2, "task_id": "task-2"}
  ],
  "skipped_scenes": [
    {"scene_id": "scene-3", "scene_number": 3, "reason": "MISSING_REFERENCES"}
  ]
}
```

Loads scenes by scene_number. For each eligible READY scene, creates exactly one attempt.
Skip precedence: ACTIVE_GENERATION_EXISTS (queued/processing, not deleted), ALREADY_SELECTED
(valid selected completed output belonging to scene), then MISSING_REFERENCES or
INVALID_CONFIGURATION. A selected output is preserved even if scene config is later edited.
Old completed/failed/stopped attempts without selection do not block a new attempt.
Per-scene `/api/admin/scenes/{scene_id}/generate` still permits explicit regeneration.

Both project and per-scene generation use prepare_scene_request and `_submit_video` for
model/duration/client policy, aliases, persistent paths, prompt resolution and Start/End suffix.
Project preparation uses `_submit_video(..., prepare_only=True)` before any task writes.
Task scene_id, resolved prompt, immutable reference snapshot and configuration are captured;
source Scene prompt/references are not mutated.

Every created task shares one batch_id, including a one-task project batch. batch_index is
1..created in scene_number order; batch_count is created, excluding skipped scenes.
A zero-task result has batch_id=null. The legacy count=1..5 API retains its original behavior,
including no batch_id for count=1 and existing batch naming/count metadata.

## Creation atomicity and concurrent requests

create_project_batch uses BEGIN IMMEDIATE. It recalculates readiness and rechecks active tasks,
selected output, queue capacity, client quota and captured scene updated_at before inserts.
All heterogeneous task rows use the same insert helper as legacy create_batch, in one transaction.
SQL/validation/quota failure rolls back every task; none are scheduled before commit.
A scene changed during preparation causes 409 and the request can be retried.
A scene becoming active/selected meanwhile is skipped, and actual batch_count/index are recomputed.
A scene absent from the prepared request but appearing eligible at commit gets NOT_IN_REQUEST,
so it is not generated without a snapshot. requested reflects the scenes checked in the creation phase.

Runtime failures after commit are independent; a failed video does not roll back other attempts.
404 unknown project, 429 client quota/queue capacity, 409 consistency/constraint conflict,
500 unexpected database failure with whole-batch rollback. Response returns after scheduling,
without waiting for videos to complete.

## Project generation status

`GET /api/admin/projects/{project_id}/generation-status`
returns project_id and ordered scenes with existing readiness/reference fields plus:

- generation_status
- latest_task (latest visible attempt, full task data)
- active_task (latest visible queued/processing attempt)
- selected_task (valid selected completed attempt)
- skip_reason

Display precedence: active QUEUED/PROCESSING -> SELECTED -> non-ready readiness -> latest
attempt status -> READY when there are no attempts. Terminal states include COMPLETED,
FAILED, STOPPED and NEEDS_RECOVERY. readiness remains independently visible, so an old failed
attempt does not hide the fact that a scene can now be generated.
No Scene.status column is duplicated.

## Stop / delete

Existing `/api/admin/tasks/{task_id}/stop` now works before an account/conversation exists.
It cancels the current runner (including gate/semaphore/key waits) and persists stopped.
A task stopped before its runner registers is rejected by the runner's live-row check.
The account-selected callback checks the row again before invoking the worker.
Cancellation releases admission gates, account/semaphore/key resources and temporary copies.
Persistent library images are not cleanup targets. Existing delete refuses live tasks; after
stop it can delete the row normally. No remote Dola cancellation or Stop redesign is added.

## Files and verification

Step 4 code: browser_pool.py, server.py, store.py, asset_api.py, scene_generation.py,
new project_generation.py. Tests: new test_project_queue.py and isolated-api fixture additions
in test_video_batch.py. Documentation: GENERATE_ALL.md and context.md.

163 tests passed: all 123 prior regressions plus 40 Step 4 tests. Coverage includes busy/release,
no busy-loop, eventual execution, parallel accounts, cooldown/risk, invalid/empty/daily-exhausted
pools, key/semaphore waits, correct started_at transition, stop before/after runner registration,
temporary copy cleanup, ordered batches/snapshots, selection and concurrent-request rechecks,
quota/SQL rollback, per-scene regeneration and legacy temporary batch execution.

```powershell
.\.venv\Scripts\python.exe -m unittest test_project_queue test_scene_import test_asset_library test_production_store test_merge_compatibility test_reference_aliases test_account_uuid test_video_tags test_pool_transactions -q
```

Runtime smoke test: two normal run_server.py starts; schema stays 4, FK check clean,
counts/history and original backups unchanged, new routes registered, missing project generate
and status return 404, history/projects return 200, and DB write lock available.
Report: diagnostics/project_queue_runtime_verification.json. Service: http://127.0.0.1:8000.
The real local database/pool is empty; populated-history and generation behavior are verified
with temporary fixtures and mocked workers. No live Dola generation was submitted.

Not implemented: large dashboard UI, selected-output UI, final-frame extraction, automatic
editing, GPT integration, external queue systems or remote cancellation.
