# Production UX: bulk references, safe replacement/removal and candidates

Implemented on 06/10/2026. Uses the existing Project/Scene/Asset/Task model, tasks.db,
vanilla Projects UI, shared batch submission, queue, BrowserPool, worker, review and
selected_task_id. No new service/framework/worker engine, no change to Template 5.

## Bulk Reference Workspace

Reference Library and the project Missing References panel offer Bulk Upload References.
The workspace shows missing requirements/using scenes on the left and a multi-file
selection/drop zone plus local staging cards on the right. Long lists scroll within
the workspace. A direct file drop onto a missing-reference card opens/maps staging
for that name; select-file controls also exist per missing name.

Supported filenames: JPG/JPEG/PNG/WEBP. Staging never starts an upload. Each card shows
preview, filename, Map to missing reference, Type, remove-from-staging and item status.
Type is one of character/environment/prop/special_state. Filename suggestion compares
case-insensitively after stripping image extension and normalizing spaces, underscores
and hyphens. Only one exact normalized match is preselected; zero or multiple matches
remain unselected. Explicit drop onto a reference is an intentional mapping.

The user may correct mappings and upload any subset. Two pending files mapped to the
same name are excluded until corrected, preventing ambiguous replacement. The Ready
to Upload summary names mapped files and requirements with no mapped image.

Upload N References reuses the single-upload API sequentially. Each item independently
succeeds/fails; one corrupt image does not hide or roll back other successes. Results
stay in staging, and failed items can be removed/remapped/retried. After all items,
existing resolve operations run for missing Scene requirements, followed by one project
readiness refresh. No new bulk backend transaction/API is needed. Leaving the workspace
releases local files/object URLs; staging is intentionally local and not persisted.

## Asset replace and remove

Reference Library exposes Replace Image and Delete. Replace previews old vs new before
confirmation and explains historical/current generation behavior. Delete opens a Remove
confirmation showing affected Scene count. UI Delete means remove from current usage,
not the old guarded physical-delete API.

New admin endpoints (existing X-Admin-Key required):

- POST /api/admin/assets/{asset_id}/replace: multipart file; returns new asset,
  retired_asset_id, affected_scenes, historical_backing_retained and optional cleanup_warning.
- POST /api/admin/assets/{asset_id}/remove: returns retirement/removal information.

Replace validates/writes a new UUID-named persistent file before the metadata operation.
One SQLite transaction retires the old Asset, inserts the new identity with the same
name/type, and rebinds current scene_assets/scene_reference_requirements to the new ID.
Position, alias and requirement name are preserved. IO/transaction failures roll back
bindings and clean the owned new file. Never overwrite the old physical image.

Remove retires the current Asset, removes resolved scene_assets links and sets matching
requirement asset_id=NULL. It does NOT delete requirements, names, aliases, order, Scenes,
Tasks, selection or generated video. Readiness becomes MISSING_REFERENCES; production
status can remain SELECTED if a valid selected video already exists.

Retired Assets are excluded from current Library, import matching, resolve and new
attachments. The current name becomes available for another upload. Historical backing
metadata/path/file is retained when ANY Task snapshot references it, including queued,
failed or soft-deleted attempts. Task snapshots/configuration are never rewritten.
Already queued candidates continue using their captured old file; future submissions
capture the new current binding. Retired images remain available through the existing
safe authenticated image route when historical backing exists.

If no Task snapshot needs the retired Asset and no current binding remains, cleanup
uses the existing staged/guarded physical deletion. Cleanup errors preserve backing
and return cleanup_warning after current bindings were successfully committed. The
existing DELETE /api/admin/assets/{id} still refuses attached or snapshot-retained Assets;
those protections were not removed. Snapshot-retained files are not garbage collected.

## Schema migration: version 5

Adds one nullable column: assets.retired_at REAL. All existing Assets start active (NULL).
Replaces assets_project_name_nocase with a partial unique index:

    UNIQUE assets(project_id, name COLLATE NOCASE) WHERE retired_at IS NULL

This is necessary to keep historical Asset identity/name/path intact while allowing a
new current Asset with the same reference name. API trim/Unicode-casefold uniqueness
still applies to active Assets. No new entity/table, no task-schema change, no JSON
import fields. Existing transactional migration/foreign-key/backup mechanisms are reused.
A v4 database gets tasks.db.before_v5.bak before migration; existing backups are never
replaced. Older schema versions traverse the existing migration chain to v5.

Fixture tests cover rollback and two initializations. A read-only native SQLite backup
of the user's actual v4 DB was migrated/reopened twice in a temporary directory: all
original table columns/rows were preserved, foreign keys were clean. Source snapshot
contained 1 Task, 1 Project, 2 Scenes and 4 Assets. Production DB was NOT migrated and
the user's server was NOT restarted. Normal restart applies v5 and loads new endpoints.
Report: diagnostics/production_ux_migration_verification.json.

## x1-x5 candidate API and UI

Both existing generation endpoints accept optional JSON:

    {"count": 3, "request_id": "unique-action-id"}

- POST /api/admin/scenes/{scene_id}/generate: count candidates for one Scene.
- POST /api/admin/projects/{project_id}/generate: count candidates PER eligible Scene.
- Missing body/count defaults to x1; strict integer range 1..5. No count in Scene/import JSON.
- request_id is optional, strict 8..64 ASCII letters/digits/underscore/hyphen.

Each action creates N sibling Tasks per eligible Scene in one creation transaction,
with unique task IDs, same scene_id and individually persisted prompt/config/reference
snapshots. One action shares batch_id. For a project batch, batch_index/batch_count are
GLOBAL action task metadata, not a per-scene index; no review logic depends on grouping.

Per-scene x1 without request_id keeps the old single Task response. xN or an action with
request_id uses the existing BatchTaskResponse. Public legacy /v1/videos/generations
request/response and batching remain compatible; Scene IDs stay out of its public schema.

Project response keeps existing fields and adds candidates_per_scene, created_scenes.
created means Task count; requested/skipped remain Scene counts. Existing eligibility
uses backend skip_reason: active candidates, selected output, missing requirements and
invalid configuration are excluded. A ready unselected Scene with completed attempts
remains eligible as before. Recheck active/selection/config revision before commit;
quota/pending limits cover the full multiplied task count. No partial generation batch
is committed if SQL/quota/pending checks fail.

UI has per-scene Candidates and project Candidates per Scene x1..x5 controls, default x1.
Before ALL Scene/Project submissions confirmation displays count and total Tasks:
40 eligible Scenes x3 =120 Tasks. API/queue capacity can still reject the whole request;
confirmation is an estimate, actual created/skipped response is authoritative.

## Queue, duplicate protection and review

N candidates means N persisted queued Tasks, not N immediate browser workers. Existing
queue, API-key limits, BrowserPool semaphore/account locks decide execution. Busy/cooling
eligible accounts wait using the unchanged Step 4 admission system. No worker/polling/
download/recovery code was rewritten in this step.

UI action lock/loading state prevents double clicks. Every confirmation generates one
stable random request_id reused for retry from that confirmation. Backend transaction
rejects a Scene with ANY preexisting active candidate BEFORE inserting all its siblings.
When request_id is supplied, a deterministic batch ID scoped to Scene/Project, ID and
client hash is stored in existing tasks.batch_id. A repeated action returns 409 and creates
zero new Tasks, even after completion. No idempotency table is added. Intentional future
regeneration uses a new request_id. Old API clients without request_id get the active
Scene guard but must send request_id for durable terminal-action replay protection.

Review Versions includes all attempts, newest first, with normal select/unselect/video
preview. Completed candidates can be selected while siblings run. Regenerate creates
more versions and preserves the previous selection; new completion never auto-selects.
Exactly one Task remains selected through scenes.selected_task_id.

Scene status inspects all candidates, prioritizing processing over queued when no selection;
valid selection retains SELECTED while active candidate counts remain visible. Cards show
total/completed/processing/queued counts. candidate_revision changes whenever an older
candidate changes too, ensuring review polling updates every attempt rather than latest
Task only. Existing production summary/completion and selected-videos ordering remain.

## Files changed

Implementation: asset_api.py, asset_library.py, production_store.py, scene_workflow.py,
store.py, server.py, project_generation.py, web/index.html.
New tests: test_production_ux.py, test_production_ux_ui.py.
Existing fixture/smoke updates: test_video_batch.py (isolated imports), test_scene_import.py
(latest schema assertion), test_project_review_ui.py/test_project_workflow_ui.py
(new mandatory generation confirmations). Existing assertions/coverage remain.
Documentation: PRODUCTION_UX.md, context.md.

## Verification

239 backend tests PASS: 201 prior +38 new production UX/lifecycle/migration tests.

    .\.venv\Scripts\python.exe -m unittest test_production_ux test_asset_preview test_project_review test_project_queue test_scene_import test_asset_library test_production_store test_merge_compatibility test_reference_aliases test_account_uuid test_video_tags test_pool_transactions -q

Browser tests PASS:

    .\.venv\Scripts\python.exe test_production_ux_ui.py
    .\.venv\Scripts\python.exe test_project_workflow_ui.py
    .\.venv\Scripts\python.exe test_project_review_ui.py

New acceptance checks multi-file staging/drop/mapping/suggestions, no early upload,
15-success/1-failure partial results, direct requirement drops, resolve/readiness,
40x3=120 confirmation and one request on double click, replace old/new previews,
historical snapshots/files and queued backing preservation, current removal -> missing,
re-upload, review while siblings queued, selected output surviving regenerate x5,
8 versions, counts/processing priority, switch selection and 40/40 selected outputs.
The existing review browser test actually plays/decodes a local recorded MP4.

Three legacy browser suites passed: Generate/History batch, video tags, edit-selection/
delete. They use installed bundled Chromium via an in-memory channel override because
system Chrome is unavailable. No live Dola generations or quota consumed.

Reports/screenshots: diagnostics/production_ux_ui_verification.json,
production_ux_bulk.png, production_ux_replace.png, production_ux_candidates.png.

No manual API calls/IDs are required for the normal workflows. Restart the user's server
manually once, then reload the browser. No dependencies added, no commit/push performed.
No GPT/timeline/ZIP/final-frame/scoring/automatic best-candidate selection was added.
