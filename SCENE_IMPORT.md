# Project / Scene workflow and Template 5 import (Step 3)

Implemented on the existing TaskStore connection and tasks.db. Schema version 4.
Step 1/2 documentation describes earlier milestones; this document is the current import contract.

## Migration and persistent requirements

Adds UNIQUE assets(project_id, name COLLATE NOCASE) and scene_reference_requirements:

| Column | Meaning |
|---|---|
| scene_id | FK scenes, cascade delete |
| position | Positive integer, primary key with scene_id |
| name | Required project asset name |
| reference_alias | Non-empty alias, unique within scene case-insensitively |
| asset_id | Nullable FK assets, restrict delete |

Unique within scene: name (NOCASE), alias (NOCASE), resolved asset_id.
Existing scene_assets rows are backfilled with their existing names, positions and aliases.
scene_assets remains the resolved attachment list used by the existing generation path.
Attach/detach/reorder/alias updates and explicit resolve update both representations atomically.
Missing requirements remain durable rows with asset_id=null; no fake Asset is created.
Asset-ID reorder rejects while requirements are missing, so it cannot discard missing slots.
Explicit detach removes both the resolved link and requirement.

Before any migration writes, check all project asset names using trim + Unicode casefold.
If collisions exist, abort with project ID, asset IDs and original names. Never rename assets.
The same check is repeated inside the migration transaction. For version 3, preserve a
SQLite backup at tasks.db.before_v4.bak before applying version 4. Original backups are
never overwritten. Migration is transactional, repeatable, and does not rebuild tasks.
Uploads/create_asset enforce trim + Unicode casefold uniqueness under BEGIN IMMEDIATE.
The SQL NOCASE index additionally protects ASCII names from direct duplicate insertion;
SQLite NOCASE is not Unicode casefold, so direct SQL writers must use the storage API.
Import detects ambiguous existing names rather than choosing one.

## Final JSON contract

```json
{
  "scenes": [
    {
      "scene_number": 1,
      "scene_name": "The First Reaction",
      "model": "seedance-2.5",
      "ratio": "16:9",
      "duration": 30,
      "start_end": false,
      "references": [
        {"name": "Riven Two Years Later", "alias": "Riven"},
        {"name": "Cabin Interior", "alias": "Cabin"},
        {"name": "Broken Sword", "alias": "Sword"}
      ],
      "prompt": "@Riven enters @Cabin carrying @Sword."
    }
  ]
}
```

Every shown scene field is required; unknown fields are rejected. scenes must be a
non-empty array. scene_number must be a positive JSON integer; duration must be the
integer 10, 15 or 30; start_end must be a JSON boolean. scene_name and prompt are strings.
Empty prompt can be imported as a draft but cannot generate. Canonical models are
seedance-2.0 and seedance-2.5; current engine aliases are accepted. Supported ratios:
16:9, 9:16, 1:1, 4:3, 3:4. Reference count uses REFERENCE_IMAGE_MAX_COUNT.
start_end=true requires exactly two references, first=start and second=end.

Asset lookup uses name within the same project, trim + casefold. Do not provide asset_id.
References array order becomes positions 1,2,3...; repeated name/asset is rejected.
Alias is trimmed, one leading @ is removed, empty or remaining @ is rejected.
Aliases must be unique case-insensitively. Prompt aliases use the existing alias resolver.
Prompt content is stored exactly as decoded from JSON, including newlines/whitespace;
only a new generation task receives the existing resolved @ImageN prompt.

Template 5: emit this exact object structure, stable library asset names, bare aliases,
references in generation order, real JSON booleans/integers, and the full prompt string.
Missing images are allowed; GPT does not need asset IDs and import does not generate.

## Endpoints

All /api/admin routes use existing X-Admin-Key authentication.

| Method | Path | Purpose |
|---|---|---|
| POST / GET | /api/admin/projects | Create / list |
| GET / PATCH | /api/admin/projects/{project_id} | Get / rename {name} |
| POST / GET | /api/admin/projects/{project_id}/scenes | Create / list ordered by scene_number |
| GET / PATCH | /api/admin/scenes/{scene_id} | Get / update editable scene configuration |
| POST | /api/admin/projects/{project_id}/scenes/import/validate | Read-only preview |
| POST | /api/admin/projects/{project_id}/scenes/import | Atomic import (201) |
| GET | /api/admin/scenes/{scene_id}/references | Persistent requirement list |
| POST | /api/admin/scenes/{scene_id}/references/resolve | Match missing names to uploaded assets |
| POST | /api/admin/scenes/{scene_id}/generate | Existing Step 2 submission path |

PATCH supports scene_number, scene_name, prompt, model, ratio, duration, start_end.
Renumber enforces UNIQUE(project_id,scene_number). Existing task snapshots remain unchanged.
Import validates the whole payload inside one BEGIN IMMEDIATE transaction before inserts.
Any invalid scene or insert failure rolls back the whole batch. Existing scene numbers
cause a conflict (409); other validation errors use 422. No overwrite/merge/renumber.

Preview returns 200 with valid, scene_count, unique_reference_count,
matched_reference_count, missing_reference_count, references_matched, references_missing,
conflicts, validation_errors (path/code/message), and the normalized scene plan.
Import returns that report plus imported_count and saved scene details. Unknown project is 404.
Preview uses a read transaction and performs no database writes.

## Computed readiness and resolution

Get/list/import scene responses include references, ready, readiness, missing_references,
and validation_errors. No duplicate status column is stored.

- READY: non-empty prompt, supported configuration, all requirements resolved, valid aliases,
  resolved link order matches requirements, and persistent image files exist safely.
- MISSING_REFERENCES: at least one unresolved name. validation_errors can also contain
  configuration errors. Generate returns 409 with the missing names and submits no task.
- INVALID_CONFIGURATION: no unresolved names but prompt/configuration/files are invalid.
  Generate returns 422. This allows empty scene drafts without mislabeling them READY.

Upload a missing image into the same project, then POST references/resolve. Resolution
is atomic and preserves names, aliases and positions; a second resolve is harmless.
No background resolver is added. Upload alone does not resolve a pending requirement.
Resolved imported scenes reuse existing Scene -> task submission, immutable reference_snapshot,
quota checks and worker queue. Persistent files and existing temporary uploads keep their
Step 2 lifecycle. Worker, BrowserPool, Dola, polling and download are unchanged in Step 3.

## Verification

40 Step 3 tests plus all 83 previous tests: 123 passed.

```powershell
.\.venv\Scripts\python.exe -m unittest test_scene_import test_asset_library test_production_store test_merge_compatibility test_reference_aliases test_account_uuid test_video_tags test_pool_transactions -q
```

Covers exact prompts, scene order, reference/alias matching, missing persistence,
upload/resolve/generate, duplicate names including Unicode, migration collision refusal,
backfill/restart/selected task preservation, strict validation, preview without writes,
validation before INSERT, batch rollback, resolve rollback and immutable old snapshots.
All generation tests use mocked workers and temporary databases; no live Dola generation.

Not implemented: Generate All, queue redesign, large UI, selected-output UI, final frames,
GPT integration, project/bulk deletion. The existing development service may have an empty
history, so preservation of populated legacy history is verified with SQLite fixtures.

Runtime verification: normal run_server.py entrypoint migrated the real database 3 -> 4
and passed two starts. FK check clean, counts unchanged (all zero), history/projects HTTP
200, new routes in OpenAPI, write transaction available, old backups preserved, and
v4 backup unchanged on second start. No generation was submitted. Report:
diagnostics/scene_import_runtime_verification.json. Local service: http://127.0.0.1:8000.
