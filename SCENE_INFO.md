# Scene Info: production management metadata

Current schema version: 6. This feature does not change generation execution, Template 5, bulk references, candidate counts, or history.

## Storage and migration

`ALTER TABLE scenes ADD COLUMN scene_summary TEXT` adds a nullable column in the existing tasks.db. Existing Scenes start with NULL. The existing backup and transactional migration mechanism is reused; a v5 database produces `.before_v6.bak`. Older databases traverse existing migrations first (the backup filename identifies their next migration). No tasks/history tables are rebuilt. Failed migration rolls back; restart is idempotent.

Import and manual edit execute only `UPDATE scenes SET scene_summary=? WHERE id=?`. In particular, scenes.updated_at is preserved because generation uses it as the configuration revision. No project timestamps, Scene names/configuration, reference requirements/bindings, Tasks, selected output, or assets change.

## Strict separate JSON

```json
{"scenes":[{"scene_number":1,"summary":"Production description"}]}
```

The top-level object accepts only scenes, a non-empty array. Each entry accepts only scene_number (positive integer, not boolean) and summary (string). Duplicate scene numbers and unknown fields are rejected. Strings, including whitespace and empty strings, are stored exactly. Empty summaries display the same fallback as NULL. Manual edit also accepts null to clear a summary.

Matching uses existing Scene.scene_number within the requested Project only. Unknown numbers are reported; import rejects the entire batch, creates no Scenes, and changes nothing. Re-import overwrites only summaries supplied in that batch. Validation is read-only; import revalidates under BEGIN IMMEDIATE so the preview cannot authorize a stale batch.

## Admin endpoints

All routes require X-Admin-Key:

- POST /api/admin/projects/{project_id}/scene-info/import/validate: read-only report with valid, total_entries, matched_count, missing_scene_numbers, validation_errors, conflicts.
- POST /api/admin/projects/{project_id}/scene-info/import: atomic update; adds updated_count to report. Returns 200, or 409 for unknown Scenes, 422 for malformed entries, 404 for missing Project.
- PATCH /api/admin/scenes/{scene_id}/summary: exactly {"summary":"text"} or {"summary":null}; returns the updated Scene. Extra fields are rejected.

Existing Scene list/detail/production status responses expose scene_summary through the existing row serialization. No generation route or request model changes.

## UI

Project header has separate Import Scenes (Template 5) and Import Scene Info actions. Scene Info panel offers paste, Validate, preview counts/not-found/errors, then Import. Editing the pasted text invalidates validation. No automatic upload or generation follows an import.

Summary appears immediately below the Scene title, above status and technical references, in a larger font. It is clamped to three lines. Show more appears only when the rendered content overflows; Show less restores the clamp. Expanded state survives scene refreshes. No summary displays subtle No scene info imported. Edit summary opens a small textarea panel; saving only calls the metadata endpoint. Text is HTML-escaped, never inferred from the prompt and never AI-generated.

## Generation independence

scene_generation.prepare_scene_request remains unchanged. It explicitly constructs VideoGenRequest from existing prompt/model/ratio/duration/start_end/reference aliases/name/count and returns the existing snapshot. scene_summary cannot enter that payload. Metadata updates do not invalidate prepared candidate submissions. BrowserPool, Dola worker, polling, download and queue were not changed.

## Verification (2026-10-06)

- 252 backend tests passed (239 existing + 13 Scene Info tests), including migration/backup/restart/rollback, NULL defaults, read-only validation, strict contract, project scope, atomic unknown/SQL rejection, re-import/manual clear, exact preservation of all other Scene fields/references/Tasks/selection, unchanged generation payload and revision-race submission.
- Scene Info browser acceptance passed: two exact Vietnamese summaries and titles for isolated test_riven fixture, unknown Scene 41 blocked, validation read-only, title/summary placement, three-line clamp, expand/collapse, manual edit and HTML escaping; no page errors.
- All three existing browser suites passed: 40-Scene Project workflow, review/selected outputs, and bulk references / 120 x3 Tasks / x5 regeneration.
- A read-only SQLite backup of the current real DB migrated v4 to v6 in a temporary directory. All existing rows were preserved (1 Task, 2 Scenes, 4 Assets). The existing test_riven Project in that copy matched Scenes 1 and 2; importing the requested summaries changed only scene_summary. The original DB was not migrated or edited.
- Reports: diagnostics/scene_info_ui_verification.json, scene_info_acceptance.png, scene_info_migration_verification.json (ignored generated artifacts).

The live server was not restarted. User restarts run_server.py normally to load the new code and migrate the original DB. No live Dola generation was attempted.

## Changed files

scene_info.py (migration and dedicated metadata store methods); production_store.py (schema version/migration chain/mixin); asset_api.py (three admin routes); web/index.html (import and summary UI); test_scene_info.py; test_scene_info_ui.py; test_scene_import.py and test_production_ux.py (latest-schema expectations); SCENE_INFO.md; context.md.
