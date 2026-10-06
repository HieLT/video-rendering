# Project review, scene versions and selected output (Step 5)

Current workflow: Project -> Scenes -> generation attempts -> review -> explicit selected output.
Reuses tasks, scenes.selected_task_id and the existing Scene generation endpoint.
Schema stays at version 4: no version/status columns, new tables or migration.
Earlier milestone documents remain historical; this document describes the Step 5 review contract.

## APIs

All routes retain existing X-Admin-Key authentication. Generation also uses existing client
Authorization policy. Read/select/unselect operations do not start generation.

| Method | Route | Behavior |
|---|---|---|
| GET | /api/admin/scenes/{scene_id}/generations | All attempts, newest first |
| PUT | /api/admin/scenes/{scene_id}/selected-generation | Select with {"task_id":"..."} |
| DELETE | /api/admin/scenes/{scene_id}/selected-generation | Clear selection |
| POST | /api/admin/scenes/{scene_id}/generate | Existing Generate/Regenerate, unchanged pipeline |
| GET | /api/admin/projects/{project_id}/generation-status | Adds production_status, summary, production_complete |
| GET | /api/admin/projects/{project_id}/selected-videos | Final selected output list in scene order |
| POST | /api/admin/projects/{project_id}/select-latest-completed | Optional explicit bulk selection |

Generations response:

```json
{
  "scene_id": "scene-id",
  "selected_task_id": "task-2",
  "generations": [
    {
      "task_id": "task-2",
      "generation_number": 2,
      "scene_id": "scene-id",
      "created_at": 1234567890,
      "status": "completed",
      "model": "seedance-2.5",
      "ratio": "16:9",
      "duration": 30,
      "video_url": "/videos/task-2.mp4",
      "error": null,
      "failure_code": null,
      "batch_id": null,
      "selected": true
    }
  ]
}
```

The actual response additionally includes batch_index/count, start_end, started_at, finished_at,
prompt, reference_snapshot and deleted_at. prompt is the saved/resolved attempt prompt;
reference_snapshot is decoded JSON when available. No snapshot is reconstructed from edited Scene data.

Version number is derived from ascending created_at, batch_index, id; review returns the reverse
order. Soft-deleted rows remain listed with deleted_at, keeping historical numbering stable.
They are marked DELETED in the UI and cannot be selected. No persisted version counter is added.

## Selection, regeneration and history

PUT/DELETE reuse existing select_scene_task/clear_scene_selection transactions. PUT returns the
Scene row with selected_task_id. Validation requires task belongs to Scene, completed,
not soft-deleted and non-empty video_url. Unknown Scene/task is 404; invalid selection is 422.
Switching selection only updates Scene selection and updated_at; existing tasks remain unchanged.
A failed selection write rolls back to the previous output.

Regenerate creates a new Task through the existing endpoint. Old attempts remain in history;
previous selection remains selected while the new attempt queues, runs and completes.
Completing a task never auto-selects it. The user must explicitly select the new output.

Selected tasks cannot be soft-deleted. The existing History delete endpoint now returns a clear
409 rather than an uncaught store exception when deletion is blocked by Scene selection.
Unselect or select another version first. Existing edit_selected/history tagging is separate
from Scene final-output selection and is retained.

Optional select-latest-completed runs in one transaction, preserving valid existing selections.
For each unselected Scene it chooses the newest non-deleted completed attempt with a video URL,
ordered by attempt creation, batch index, ID. It skips missing/unusable completed outputs.
Response: project_id, changed, skipped_count, selected_scenes (scene_id/task_id), skipped_scenes.
No worker completion event invokes this operation automatically.

## Production status and project completion

Existing generation_status, readiness, latest_task, active_task, selected_task and skip_reason
remain compatible. Added production_status and completed_attempt_count are derived, not stored.

Production-status precedence:

1. SELECTED: valid selected output exists. Later regeneration does not undo this production decision.
2. QUEUED / PROCESSING: active attempt exists and Scene is not selected.
3. NEEDS_REVIEW: no active attempt/selection, but a visible completed attempt exists.
4. MISSING_REFERENCES / INVALID_CONFIGURATION: current Scene inputs are not ready, with no completed output to review.
5. READY: inputs are ready, no active/completed/selected output. A failed latest attempt can still be READY for regeneration.

Thus a failed latest attempt plus an older completed attempt is NEEDS_REVIEW. An edited prompt
or new missing requirement does not hide an already completed output from review: readiness
continues to report whether the Scene can generate again. The UI disables generation if not ready.
A selected Scene can have an active regeneration; production_status remains SELECTED while
active_task and the existing generation_status still show its actual execution state.

```json
{
  "project_id": "project-id",
  "summary": {
    "total_scenes": 40,
    "selected": 32,
    "needs_review": 3,
    "processing": 2,
    "queued": 1,
    "missing_references": 2,
    "invalid_configuration": 0,
    "ready": 0
  },
  "production_complete": false,
  "scenes": []
}
```

scenes contains the full ordered existing Scene read model plus the new production fields.
Summary partitions Scenes by production_status; selected Scenes are counted as selected even
if a later attempt is active. UI polling checks active_task, independently of this summary.
production_complete is true exactly when total_scenes > 0 and every Scene has a valid selected
output. Completed tasks without selection do not complete the project. Empty projects are incomplete.

## Final selected video contract

```json
{
  "project_id": "project-id",
  "total_scenes": 40,
  "selected_count": 32,
  "complete": false,
  "videos": [
    {
      "scene_id": "scene-id",
      "scene_number": 1,
      "scene_name": "The First Reaction",
      "task_id": "task-id",
      "video_url": "/videos/task-id.mp4",
      "duration": 30,
      "model": "seedance-2.5",
      "ratio": "16:9"
    }
  ]
}
```

Only valid selected outputs are returned, ORDER BY scene_number. Duration/model/ratio come
from the selected Task snapshot, not the editable Scene. local_path is omitted because the
existing task model stores video_url; no guessed filesystem path is introduced. No ZIP/export.

## Minimal vanilla dashboard UI

Added Projects tab to web/index.html; existing Generate form/History remain intact.

- List/open projects; Scene cards ordered by scene_number.
- Production badges, reference readiness/missing names, latest status and selected video link.
- Generate/Regenerate, Review Versions, Select, switch selection and Unselect.
- Inline Versions panel: generation number, status, completed video controls, saved prompt,
  reference aliases, failed error/code and visible selected star/outline.
- Header selected/total counter, production summary and completion indicator.
- Existing Generate All Ready and explicit optional select-latest-completed actions.
- Optional Gateway key shares the existing generation form's key, without adding persistence.

Uses the existing ten-second auto-refresh timer. Active Project status is polled; unrelated
video history is not polled from this tab. Versions reload only on a changed relevant Scene
signature or an explicit refresh/action, so unchanged polling does not restart video playback.
Manual Refresh works even for inactive/fully selected projects. Request tokens prevent stale
Project/Scene responses from replacing a newer navigation choice. Video URLs are restricted
to HTTP(S), and names/prompts/errors are escaped before HTML rendering.

## Files and validation

Step 5 code: new review_store.py; production_store.py mixin wiring; store.py read-model fields;
asset_api.py review routes; server.py selected-delete conflict handling; web/index.html UI.
Tests: new test_project_review.py and test_project_review_ui.py. Documentation: PROJECT_REVIEW.md,
context.md. Generation worker, BrowserPool, Dola, polling/download engine remain unchanged in Step 5.

197 backend tests passed: all 163 existing regressions plus 34 review/selection tests.
Covers version order/filtering, URLs/errors/snapshots, selection validation/rollback,
switching/clearing, immutable history, regenerate without replacing selection, review status
precedence, summary/completion, selected video ordering/snapshot duration, delete protection,
Generate All selected skip, optional bulk-select safety/rollback and read-only models.

Browser smoke passed using real temporary/in-memory API fixtures and a locally recorded MP4
that was actually decoded/played: Project list/Scene status, Versions, Select/Switch/Unselect,
selected counter/completion, Regenerate, polling, manual Refresh, shared Gateway auth and legacy
Generate/History. Also passed all three existing browser suites: batch Generate, edit-selection
and dashboard/history deletion. Legacy suites used installed Chromium via a test-only launch
channel override; production browser configuration and the old test sources are unchanged.

```powershell
.\.venv\Scripts\python.exe -m unittest test_project_review test_project_queue test_scene_import test_asset_library test_production_store test_merge_compatibility test_reference_aliases test_account_uuid test_video_tags test_pool_transactions -q
.\.venv\Scripts\python.exe test_project_review_ui.py
```

Runtime: two normal run_server.py starts passed, review routes/UI available, schema remains 4,
FK check clean, counts/backups unchanged and DB write lock released. No actual Dola generation.
The real local database is empty; populated history/output behavior is covered by fixtures.
Reports: diagnostics/project_review_runtime_verification.json and project_review_ui_verification.json.
Screenshot: diagnostics/project_review_ui.png. Service: http://127.0.0.1:8000.

Not implemented: final-frame continuity, ZIP/export packages, automatic editing, GPT integration,
video timeline/editor, generated thumbnails or frontend framework migration.
