# Persistent Reference Library — V2 Step 2

Implemented and verified on 06/10/2026. The normal entrypoint is `run_server.py`.
No worker, BrowserPool, Dola client, polling/download engine, or dashboard rewrite.

## Real database/runtime verification

Phase 1 was completed before Step 2 code changes:

- Stopped the existing service; real DB started at user_version=0 with 0 tasks.
- Started normal entrypoint twice; both starts reached user_version=2.
- projects/scenes/assets/scene_assets and tasks.scene_id exist.
- tasks count stayed 0; foreign_key_check returned no violations.
- tasks.db.before_v2.bak contains the pre-migration schema and was unchanged on restart.
- Health, dashboard, history, and OpenAPI returned HTTP 200.
- No real old task existed to read; missing-task route returned 404. Nonempty legacy
  task/history/API-key preservation is covered by temporary fixture tests.

After Step 2, the same normal entrypoint was started twice again:

- Additive schema version 3 adds nullable tasks.reference_snapshot TEXT.
- tasks.db.before_v3.bak contains version 2; the original V2 backup is preserved.
- Count remained 0, both backups were unchanged on restart, FK check remained clean.
- New routes appear in actual OpenAPI; history/projects GET returns 200.
- Invalid old generation payload returns 422; missing-scene generation returns 404.
- A separate connection could acquire/release a SQLite write transaction while the
  service was running. No live generation or quota was consumed.

Local reports (ignored by Git):

- diagnostics/v2_migration_verification.json
- diagnostics/reference_library_runtime_verification.json
- diagnostics/v2_phase1_start_1.log and v2_phase1_start_2.log
- diagnostics/reference_library_start_1.log and reference_library_start_2.log

The final service was left running at http://127.0.0.1:8000/.
Interactive API forms are available at http://127.0.0.1:8000/docs.

## Endpoints

All new routes use the existing admin authentication (`X-Admin-Key` when configured).
Scene generation additionally uses existing client authentication and quotas
(`Authorization: Bearer ...` when API keys are enabled).

| Method | Path | Input / behavior |
| --- | --- | --- |
| POST | /api/admin/projects | JSON name; minimal project creation |
| GET | /api/admin/projects | List projects |
| POST | /api/admin/projects/{project_id}/scenes | JSON scene_number, scene_name, prompt, model, ratio, duration, start_end |
| GET | /api/admin/projects/{project_id}/scenes | Ordered scenes |
| GET | /api/admin/scenes/{scene_id} | Scene configuration |
| PATCH | /api/admin/scenes/{scene_id} | Editable configuration only |
| POST | /api/admin/projects/{project_id}/assets | Multipart name, type, file; returns immutable asset metadata |
| GET | /api/admin/projects/{project_id}/assets | Project asset metadata list |
| GET | /api/admin/assets/{asset_id} | Asset metadata |
| DELETE | /api/admin/assets/{asset_id} | Delete unused asset; in-use asset returns 409 |
| GET | /api/admin/scenes/{scene_id}/assets | Ordered references with asset metadata |
| POST | /api/admin/scenes/{scene_id}/assets | JSON asset_id, position, reference_alias |
| DELETE | /api/admin/scenes/{scene_id}/assets/{asset_id} | Detach reference |
| PUT | /api/admin/scenes/{scene_id}/assets/order | JSON asset_ids: complete list in new order |
| PATCH | /api/admin/scenes/{scene_id}/assets/{asset_id} | JSON reference_alias |
| POST | /api/admin/scenes/{scene_id}/generate | Optional JSON count (default 1; existing per-request batch 1–5) |

Create/upload/attach returns 201; generation returns 202 with the existing task or
batch response. Invalid metadata/aliases return 422; missing records return 404;
SQL uniqueness or in-use deletion conflicts return 409. Storage failures return 500
with a generic message and server-side diagnostic logging.

Assets are not publicly mounted under /videos; metadata contains relative file_path.
There is no preview/upload/dashboard UI beyond the existing FastAPI docs.

## Persistent files and failure handling

DOLA_ASSET_DIR still defaults to assets, resolved relative to the project module root.
Files use `<project_id>/<asset_id>.<ext>` and are never added to UPLOADED_REFERENCES.

Upload checks project/type/name, supported filename extension, bounded file size,
actual Pillow-verified JPEG/PNG/WEBP format, and decompression-bomb limits. MIME and
client filename do not determine the stored path. Canonical suffix comes from the
actual image: JPEG -> .jpg, PNG -> .png, WEBP -> .webp. Existing .jpeg metadata remains
supported. UUID IDs and exclusive file creation prevent overwriting source assets.

Upload writes the immutable source before inserting metadata. Ordinary insert/write
exceptions remove the owned partial file; no asset row is created on write failure.

Delete is refused while an asset is attached OR retained by any task snapshot,
including hidden/failed historical attempts. This protects source provenance and
restart recovery after references have been detached from the current scene.

Unused deletion stages the source in its project directory, deletes metadata in a
transaction, then removes the staged file. DB failure restores the source; unlink
failure compensates metadata and restores the file. Startup reconciles recognized
staged deletes left by interruption: row exists -> restore file; row absent -> finish
cleanup. Files are preserved rather than overwritten if staging is ambiguous.

## Generation integration and snapshot

`Scene + ordered scene_assets -> _submit_video -> TaskStore.create_batch -> _run_task
-> BrowserPool.generate_video -> existing UI worker -> existing polling/download`.

The adapter captures scene configuration and ordered references in one read
transaction. Alias names and paths come from the SAME ordered list. The shared
submit function retains existing model/duration/client quota/account/queue checks,
batch creation, task scheduling, and response behavior.

Each scene attempt saves a JSON array in tasks.reference_snapshot:

```json
[
  {
    "asset_id": "stable-asset-id",
    "reference_alias": "Riven",
    "position": 1,
    "file_path": "project-id/stable-asset-id.webp"
  }
]
```

NULL = old/temporary generation path. [] = scene attempt with no reference images.
Store validates scene ownership, immutable asset paths, unique ordered positions and
aliases inside the task-insert transaction. Generic update cannot alter the snapshot.
The legacy reference_images column retains ordered `asset://<id>` identifiers for
scene attempts; file resolution uses the saved snapshot, never current scene_assets.

Scene.prompt remains the editable source (`@Riven ... @Cabin`). Task.prompt stores the
resolved attempt prompt (`@Image1 ... @Image2`), including existing start/end guidance.
Scene edits/reorder/alias changes do not change earlier task inputs or selected output.

_run_task loads stable paths from the task snapshot, including queued-task recovery
after restart, and passes them to the unchanged worker. Persistent roots are never
registered for temporary cleanup. The legacy uploaded:// and public-URL paths still
use their existing per-task copies and cleanup. Dola uploads can repeat per attempt.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest test_asset_library test_production_store test_merge_compatibility test_reference_aliases test_account_uuid test_video_tags test_pool_transactions
```

83/83 pass: 31 Step 2 tests, 32 foundation tests, and 20 existing compatibility/
regression tests. Coverage includes actual HTTP multipart/JSON routes and mocked
workers, upload formats/security/write/DB failures, deletion compensation/recovery,
asset reuse, ordering/alias updates, ownership, snapshots, post-submit edits,
restart-style worker inputs, source persistence after success/failure, queue/key
quota checks, empty snapshots, and the old temporary-reference batch pipeline.
Python compilation, pip check, and Git whitespace validation pass.

Tests use temporary databases/files; Dola is never called. Generation end-to-end
against Dola is not verified and the real account pool is currently empty.

## Changed files in Step 2

- asset_library.py, asset_api.py, scene_generation.py (new).
- store.py, production_store.py: snapshot migration/validation, alias update, read capture.
- server.py: shared submit helper, persistent-path resolution, admin route registration.
- media.py: shared image-byte validation.
- test_asset_library.py (new), test_production_store.py, test_video_batch.py.
- requirements-dev.txt (new), .gitignore, context.md, V2_DATA_MODEL.md, this document.

Not implemented: large Project/Scene UI, JSON multi-scene import, Generate All,
queue redesign, selected-output UI, final-frame extraction, or GPT integration.
