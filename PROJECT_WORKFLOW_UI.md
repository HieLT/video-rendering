# Complete Project Production UI

The existing vanilla HTML/CSS/JS Projects tab now exposes the normal production
workflow end to end. No framework, database migration, generation engine, worker,
polling/download implementation or task history format was changed in this step.

## Browser workflow

1. Open **Projects**. Cards show project name, scene count, selected count and
   IN PROGRESS / COMPLETE. An empty home includes Create Project.
2. **Create Project** accepts a nonblank name and opens the new project. **Rename**
   reuses PATCH /projects/{id}; no project deletion UI was added.
3. **Import Scenes** accepts the Tool Import JSON contract in SCENE_IMPORT.md.
   **Validate** is read-only and shows scene count, unique/matched/missing references,
   field errors including scene numbers, duplicate aliases and scene conflicts.
   Invalid JSON or edited-after-validation input cannot be imported. A valid preview
   enables **Import N Scenes**. Missing references are allowed, not failed imports.
4. **Reference Library** lists this project's references, type, authenticated image
   thumbnail and the scene numbers using each asset. Upload accepts a reference
   name, one of the four existing types and JPG/JPEG/PNG/WEBP.
5. **Missing References** aggregates names across the project and lists using scenes.
   Its Upload action pre-fills the reference name. Each scene also shows ordered
   names/aliases and Resolved / Missing, plus an Upload Missing Reference action.
6. After every successful upload the UI obtains current scene requirements and calls
   the existing resolve endpoint for unresolved scenes. Readiness refreshes without a
   separate user resolve action. **Refresh references** retries resolution if upload
   succeeded but a resolve request failed; the uploaded file is retained.
7. **Generate All Ready** refreshes backend state before showing eligible count and
   skips for active attempts, existing selections, missing refs and invalid config.
   Eligibility uses skip_reason from the existing backend policy, including completed
   unselected scenes that are eligible to regenerate. The submit result reports
   actual created/skipped counts and grouped skip reasons; concurrent changes may
   legitimately change counts between confirmation and admission.
8. Scene cards show readiness, production status, latest attempt, selected video,
   Generate / Regenerate and Review Versions. Queued/processing attempts poll using
   the existing refresh timer. A selected scene with an active new attempt shows
   both selection and active status. Polling never requests History.
9. Review Versions retains saved prompts/reference aliases/errors, newest-first
   attempts, playable MP4 preview, Select / Unselect and immutable history.
   Selection updates the header counter; selecting all scenes shows Production
   complete. The explicit existing bulk latest-completed action remains available.
10. **View Selected Outputs** uses selected-videos and renders playable selected
    outputs in scene-number order with selected/total count. No export was added.

Panels stay inside the Projects tab and use explicit Back to project / Back to
Projects navigation. The scene list and open review retain their context when
returning from a panel. Network actions show loading text where relevant and prevent
concurrent submissions. User content is escaped, and video URLs allow only HTTP(S).
API detail messages are displayed instead of bare HTTP status numbers.

## Only new backend endpoint

GET /api/admin/assets/{asset_id}/image

- Requires the existing X-Admin-Key authentication.
- Looks up the asset in the store; accepts no user-supplied filesystem path.
- Reuses validate_asset_path / resolve_asset_path, including extension, exact
  project/asset filename and resolved-root containment checks.
- Returns JPG/JPEG, PNG or WEBP using FileResponse with the correct image media type.
- Unknown, unsafe or missing images return 404; invalid authentication returns 401.
- Cache-Control: private, no-store; X-Content-Type-Options: nosniff.
- UI fetches the image with the admin header and uses a temporary browser object URL.
  Object URLs are revoked when leaving/reloading the panel; admin keys never appear
  in image URLs. Persistent asset identity/storage remain unchanged.

## Files changed for this supplement

- web/index.html: extends the existing Projects markup/styles and ProjectReview IIFE.
- asset_api.py: read-only authenticated asset-image route and module description.
- test_asset_preview.py: four tests for supported media, authentication, missing
  images, malicious/tampered metadata and no generation/storage mutations.
- test_project_workflow_ui.py: full browser acceptance using real fixture APIs and
  an in-memory TaskStore, temporary persistent asset root and mock generation.
- PROJECT_WORKFLOW_UI.md: this workflow/implementation/verification record.
- context.md: current-step handoff.

Earlier Step 1-5 changes remain in the working tree. This list describes this
supplement only. No new dependencies are required.

## Verification

201 backend tests passed, including the 197 Step 1-5 tests and four asset-preview tests:

    .\.venv\Scripts\python.exe -m unittest test_asset_preview test_project_review test_project_queue test_scene_import test_asset_library test_production_store test_merge_compatibility test_reference_aliases test_account_uuid test_video_tags test_pool_transactions -q

Browser acceptance passed:

    .\.venv\Scripts\python.exe test_project_workflow_ui.py
    .\.venv\Scripts\python.exe test_project_review_ui.py

The new acceptance goes through browser controls to create a project, validate/import
40 scenes with missing references, upload persistent PNG/WebP images, load thumbnails,
automatically resolve readiness, confirm/submit Generate All, render queued and
processing, review and actually decode/play a local fixture MP4, select/unselect,
regenerate, switch selection, reach 40/40 selected and view 40 ordered selected outputs.
It also checks blank names, invalid JSON/duration, duplicate aliases, import conflicts,
read-only validation, missing-upload name prefill and eligibility excluding missing
scenes. Task completion/processing is simulated by the fixture, not a real Dola worker.
No manual API calls or IDs are entered by the browser user.

Existing browser regressions passed: test_video_batch.browser_checks,
test_video_tags_ui.main and test_dashboard_delete.main. They ran with installed
bundled Chromium via an in-memory launch override because a system Chrome channel
is unavailable; existing test files were unchanged. Generate/History, tags and
edit-selection/delete still work.

Reports/screenshots (local ignored diagnostics):
- diagnostics/project_workflow_ui_verification.json
- diagnostics/project_review_ui_verification.json
- diagnostics/project_workflow_ui.png
- diagnostics/project_reference_library_ui.png
- diagnostics/project_workflow_detail_ui.png

All UI generation was mocked; live Dola generations: 0. The real tasks.db, history,
backups and assets were not used by these tests. The user's running server was not
stopped, restarted or replaced in this supplement. Restart it manually once to load
asset_api.py, then reload the browser to load the updated HTML.

## Remaining manual API usage and scope

None in the normal Project -> Import -> References -> Generate -> Review -> Select
workflow. Existing generation-account setup and optional Gateway authentication
remain the existing prerequisites for real generation.

Final-frame continuity, ZIP/export, automatic editing, timeline, video thumbnail
extraction, GPT integration and frontend framework migration remain out of scope.
