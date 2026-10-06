Historical foundation (Step 1): this document records schema version 2. Current Step 2 adds reference_snapshot at version 3, persistent upload and scene generation APIs. See REFERENCE_LIBRARY.md for current behavior and real database verification.

# V2 data model foundation

Implemented scope: SQLite migration and storage only. Generation API, BrowserPool,
workers, polling, downloads, and dashboard remain unchanged.

## Migration

`TaskStore` enables foreign keys and migrates the configured `DOLA_DB_PATH`
(default `tasks.db`) to `PRAGMA user_version=2` on initialization.

Before the first migration of a file database, SQLite backup API creates
`<database>.before_v2.bak`. A complete backup is published atomically using a
same-directory hard link; an existing original backup is never overwritten.
In-memory databases have no backup file. Backup failure aborts before schema changes.

Legacy additive columns and V2 schema are initialized in one `BEGIN IMMEDIATE`
transaction. Column existence is inspected rather than catching arbitrary
`OperationalError`. Foreign-key violations fail migration. Version is updated
inside the successful transaction; errors roll back and close the connection.
No tasks table rebuild and no conversion of task names into scenes occur.

For the existing running service: stop it with Ctrl+C, then run the normal
`run_server.py` command once. Do not run concurrent service instances during migration.
The implementation was tested on temporary databases; the live database was not migrated.

## Actual schema

IDs: TEXT. Timestamps: REAL Unix seconds.

- `projects`: id, name, created_at, updated_at.
- `scenes`: id, project_id, scene_number, scene_name, prompt, model, ratio,
  duration, start_end, selected_task_id, created_at, updated_at.
- `assets`: id, project_id, name, type, file_path, created_at.
- `scene_assets`: scene_id, asset_id, position, reference_alias.
- `tasks`: all existing fields plus nullable scene_id.

Constraints:

- Unique `(project_id, scene_number)`; scene_number >= 1.
- Duration in 10/15/30; start_end in 0/1.
- Asset types: character, environment, prop, special_state.
- Scene-asset primary key `(scene_id, asset_id)`; positive position;
  unique `(scene_id, position)` and case-insensitive `(scene_id, reference_alias)`.
- FK projects -> scenes/assets: RESTRICT on delete.
- FK scenes -> tasks via tasks.scene_id: RESTRICT on delete.
- FK tasks -> scenes via scenes.selected_task_id: RESTRICT on delete.
- Scene-asset scene FK: CASCADE on delete; asset FK: RESTRICT on delete.

Indexes: tasks(scene_id, created_at, id), assets(project_id),
scene_assets(asset_id), scenes(selected_task_id), plus uniqueness indexes.

Ownership, completed-output selection, and Unicode alias uniqueness are enforced
by storage methods. SQL NOCASE is an additional ASCII uniqueness check. Direct SQL
can bypass these application invariants and is not the supported write interface.
An existing tasks.scene_id column without the approved foreign key is rejected:
it cannot be repaired safely with ADD COLUMN without rebuilding tasks.

## Storage API

All methods are available on `TaskStore`; `ProductionStoreMixin` shares its
connection and global lock. Getters return dictionaries or None. Lists return
dictionaries in deterministic order. Missing required parents raise ValueError;
SQLite unique/FK violations raise IntegrityError. Writes roll back on failure.

- create_project(name, *, project_id=None)
- get_project(project_id), list_projects(), update_project(project_id, *, name)
- create_scene(project_id, scene_number, scene_name='', prompt='',
  model='seedance-2.0', ratio='16:9', duration=10, start_end=False, *, scene_id=None)
- get_scene(scene_id), list_project_scenes(project_id), update_scene(scene_id, **fields)
- create_asset(project_id, name, asset_type, file_path, *, asset_id=None)
- get_asset(asset_id), list_project_assets(project_id)
- attach_asset_to_scene(scene_id, asset_id, position, reference_alias)
- detach_asset_from_scene(scene_id, asset_id): returns whether a link was deleted
- list_scene_assets(scene_id): ordered by position, includes asset metadata
- reorder_scene_assets(scene_id, asset_ids): complete list, no omissions/duplicates;
  atomically assigns positions 1..N without transient uniqueness collisions
- select_scene_task(scene_id, task_id), clear_scene_selection(scene_id)
- create_batch(..., *, scene_id=None); create(..., scene_id=None) delegates to it

Project and scene IDs default to UUID hex strings. Asset metadata is registered
using the ID already chosen for its persistent file; asset_id defaults to the file
stem. All asset/project path components must contain only letters, digits, '_' or '-'.

A task must belong to the scene, be completed, not soft deleted, and have a nonempty
video_url to be selected. Selection does not touch edit_selected or tag copies.
New attempts do not change selection. Delete selected tasks and generic updates
that invalidate selected outputs are refused with ValueError. Generic task updates
cannot change scene_id, including assigning one to a legacy task.

Scene configuration edits never mutate task input snapshots. Draft scenes may have
empty prompts. Model and ratio are nonempty strings here; generation keeps its
existing validation when integrated later. Start/end reference-count validation
belongs to future generation submission, not draft scene editing.

## Persistent assets

`DOLA_ASSET_DIR` defaults to `assets`. Relative roots are anchored at the project
module directory, independent of the caller's working directory.

`asset_storage.asset_relative_path(project_id, asset_id, extension)` accepts
.jpg/.jpeg/.png/.webp, normalizes extension case, and returns
`<project_id>/<asset_id>.<ext>`. Metadata paths must already use that canonical form.
`resolve_asset_path` confines paths to the configured root, including resolved
symlink paths. Absolute paths, traversal, backslashes, and mismatched IDs are rejected.

create_asset registers metadata only: it does not upload, decode, write, overwrite,
or delete files. There is no asset update operation. The future upload layer must
verify actual image format, persist a new immutable file, and then register metadata.
No asset is connected to temporary-reference cleanup or generation in this step.

## Verification

Run from the project directory:

```powershell
.\.venv\Scripts\python.exe -m unittest test_production_store test_merge_compatibility test_reference_aliases test_account_uuid test_video_tags test_pool_transactions -v
```

52 tests pass: 32 V2 tests and 20 existing compatibility/regression tests.
They cover backups, fresh/legacy/idempotent migration, task and API-key preservation,
FK validation, migration failure, CRUD, immutable metadata, supported paths/extensions,
reference ordering/aliases, task snapshots, selection/deletion guards, and rollback
without remaining SQLite write locks. No live Dola generation was performed.

Not implemented: V2 endpoints or UI, asset uploads, JSON import, Generate All,
queue changes, per-attempt persistent reference snapshots, final-frame extraction,
or GPT integration.
