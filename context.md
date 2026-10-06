# Context dự án Video Rendering

Cập nhật: 06/10/2026. Tài liệu này mô tả trạng thái hiện tại, thay thế các ghi chú milestone cũ từng được nối vào cuối file.

## 1. Dự án làm gì?

Tool tạo và quản lý video bằng Dola, đồng thời tổ chức các lần generate thành workflow sản xuất phim. Một phim là Project gồm khoảng 30–50 Scenes. Mỗi Scene có prompt, cấu hình generation, reusable reference images và một output được người dùng chọn làm kết quả cuối cùng.

Luồng sử dụng hiện tại:

**Projects → Create Project → Import Scenes JSON → Upload References → Generate All Ready → Review Versions → Select Output → View Selected Outputs.**

Người dùng hoàn thành luồng này trong browser, không cần Postman/curl, sửa database hoặc nhập project_id/scene_id/asset_id/task_id thủ công. Tab tạo video và History cũ vẫn hoạt động độc lập với Projects.

## 2. Kiến trúc và nguyên tắc

- Backend: Python 3.11, FastAPI, Uvicorn, asyncio.
- Storage: SQLite; dữ liệu Project/Scene/Asset dùng chung `tasks.db` với tasks/history/API keys. Pool dùng `pool_usage.db`.
- Browser automation: Patchright + Chromium persistent profiles cho từng tài khoản.
- Generation: reuse `BrowserPool`, `video_worker_ui.py`, Dola submission, polling, recovery và download MP4 hiện tại.
- Frontend: vanilla HTML/CSS/JavaScript trong `web/index.html`, không cần build, không React/Vue.
- Không rewrite generation engine; V2 mở rộng data model và điều phối trên pipeline cũ.
- Giữ backward compatibility: task cũ không có Scene vẫn hợp lệ; API generate cũ không bị thay thế.

Pipeline chung:

Dashboard/API → lưu task `queued` → chờ giới hạn API key/pool → giữ khóa tài khoản → `processing` → worker gửi Dola → polling → tải MP4 vào `downloads/` → `completed` hoặc trạng thái lỗi/recovery.

## 3. Data model V2

Schema hiện tại: `PRAGMA user_version = 6` sau khi TaskStore khởi tạo/migrate. Các mốc đầu đã có thay đổi schema; Step 4, Step 5 và bổ sung UI đầu tiên không thêm migration; Production UX thêm retired_at và partial unique index ở v5.

| Entity | Vai trò |
| --- | --- |
| `projects` | Phim/project; tên và timestamps |
| `scenes` | Production unit: project, số/tên Scene, prompt, model, ratio, duration, start_end, selected_task_id, timestamps |
| `assets` | Reference reusable: project, tên, type, persistent file_path, created_at |
| `scene_assets` | References đã resolve; giữ asset, position và reference_alias |
| `scene_reference_requirements` | Tên/alias/position cần cho Scene, kể cả khi chưa upload Asset; asset_id nullable |
| `tasks` | Một generation attempt; thêm scene_id nullable và reference_snapshot |

**Scene không phải Task.** Scene tồn tại trước generation. Generate/regenerate tạo Task mới, giữ các attempts cũ. Ví dụ Scene 26 có Task A failed, Task B completed, Task C completed; chọn C bằng `scenes.selected_task_id`.

Task giữ prompt đã resolve và snapshot references theo thứ tự, gồm asset_id, reference_alias, position, file_path. Chỉnh Scene hoặc resolve references về sau không sửa snapshot của attempt cũ.

Một Asset được nhiều Scenes cùng Project reuse. Identity lâu dài là bản ghi Asset và file persistent, không phải temporary `uploaded://` token. Tên Asset phải unique trong Project theo trim + Unicode casefold ở storage API. Aliases và thứ tự ảnh được giữ khi chuyển sang `@ImageN`.

Selection chỉ nhận task thuộc cùng Scene, completed, chưa bị soft-delete và có video URL. Chuyển selection không sửa/xóa Task. Task đang được Scene chọn không được xóa; cần unselect hoặc chọn output khác trước. Physical-delete Asset đang được Scene hoặc Task snapshot sử dụng vẫn bị chặn. Production Remove được phép bỏ current bindings và retire Asset, giữ backing file nếu history cần dùng.

Migration là additive, có transaction, bật foreign keys/busy timeout, tạo SQLite backup trước migration và không ghi đè backup cũ. Không drop/rebuild tasks để thêm V2, không làm mất history. Collision tên Asset phải báo lỗi để xử lý, không tự đổi tên.

## 4. Các chức năng Projects hiện có

### Project và import

- Projects Home có Create Project, project cards, số Scenes, selected/total và IN PROGRESS/COMPLETE; có empty state.
- Create validate tên không rỗng và tự mở Project mới. Rename dùng API hiện có.
- Project Detail có summary, selected counter và Scene list theo scene_number tăng dần.
- Import Scenes nhận Tool Import JSON; Validate không ghi database.
- Preview hiển thị số Scenes, unique/matched/missing references; lỗi JSON, duration, alias trùng, Scene conflict có detail cụ thể.
- Import chỉ được bật sau validation hợp lệ; chỉnh JSON sẽ hủy preview đã validate.
- Import cả batch trong một transaction, không overwrite Scene đã tồn tại. Missing References không làm import thất bại.

JSON contract hiện tại yêu cầu các fields: scene_number, scene_name, model, ratio, duration, start_end, references, prompt. Mỗi reference có name và alias. Xem mẫu đầy đủ trong `SCENE_IMPORT.md`; không tự diễn giải một format Template 5 khác với contract này.

### Reference Library và readiness

- Library chỉ hiển thị Assets của Project hiện tại: thumbnail, tên, type và Scenes sử dụng.
- Types: character, environment, prop, special_state. Ảnh: jpg/jpeg/png/webp.
- Project aggregate Missing References theo tên, hiển thị Scenes cần dùng; Upload prefill tên reference.
- Sau upload, UI tự gọi existing resolve operation và refresh readiness. Nếu resolve lỗi sau upload, file đã upload được giữ; Refresh references cho phép retry.
- Mỗi Scene hiển thị tên references, alias, Resolved/Missing và nút Upload Missing Reference.
- Reference ảnh persistent được xem qua `GET /api/admin/assets/{asset_id}/image`: admin authentication, validate Asset và đường dẫn trong storage root; không nhận arbitrary filesystem path.
- UI fetch thumbnail có admin header rồi tạo browser object URL, không đưa admin key vào URL ảnh.

Readiness gồm READY, MISSING_REFERENCES, INVALID_CONFIGURATION; là kết quả tính từ config/references/files, không phải task status được lưu lại.

### Queue và Generate All

- Scene Generate/Regenerate và Project Generate All dùng chung submission/worker pipeline.
- Generate All tạo một attempt cho mỗi Scene đủ điều kiện, theo scene_number; kiểm tra quota/pending limits và ghi batch atomically.
- Bỏ qua Scene có active generation, đã có selected output, thiếu references hoặc config không hợp lệ. Backend recheck trước khi commit để tránh race.
- UI confirmation refresh backend state và hiển thị đúng số eligible theo skip_reason. Result hiển thị created/skipped và nhóm lý do; không chờ video hoàn thành.
- Accounts busy/cooldown nhưng còn hợp lệ khiến Task tiếp tục queued; không bị fail chỉ vì một lần scan không lấy được khóa.
- `processing` chỉ bắt đầu khi đã có capacity và account lock. Semaphore/API key/account vẫn giới hạn số chạy thật.
- FIFO admission gần đúng trong runners; không cam kết FIFO tuyệt đối giữa API keys/recovery.
- Không có tài khoản usable hoặc hết quota/credit báo lỗi rõ; không retry tùy tiện worker failures, không chờ sang ngày mới để có quota.
- Queue dựa trên SQLite tasks và asyncio trong process hiện tại, không thêm Redis/Celery/service scheduler.

### Review, selection và output

- Review Versions: attempts mới nhất trước, generation number, status/error, saved prompt/references, preview MP4.
- Select/Unselect cập nhật selected counter; regenerate không mất history và không tự thay selection cũ.
- Completion của worker không tự chọn output. Optional Select latest completed chỉ chạy khi người dùng bấm và giữ các selection đã có.
- Production statuses: READY, MISSING REFERENCES, INVALID CONFIGURATION, QUEUED, PROCESSING, NEEDS REVIEW, SELECTED.
- Production status ưu tiên selected output hợp lệ; Scene selected vẫn có thể có một attempt mới queued/processing, UI hiển thị cả hai.
- Có completed attempt chưa chọn thì NEEDS REVIEW, kể cả latest attempt fail; readiness vẫn được theo dõi riêng.
- Project COMPLETE khi có ít nhất một Scene và tất cả Scenes có selected output hợp lệ; Project rỗng không complete.
- View Selected Outputs hiển thị selected/total và videos theo Scene order; chưa có export hoặc ghép phim.
- Polling Projects chỉ refresh khi dirty/có active tasks; không tải History trong lúc review Project.

## 5. Các chức năng cũ được giữ

- Generate riêng lẻ: Seedance 2.0/2.5, duration yêu cầu 10/15/30 giây, ratio, prompt, optional Name, references, aliases và batch 1–5 attempts.
- Batch kiểm tra quota cả batch; references dùng bản sao độc lập cho các tasks. Tên task/file có scene name và Task ID để tránh trùng.
- Start / End yêu cầu hai ảnh đúng thứ tự; hiện bổ sung chỉ dẫn vào prompt. Không cam kết native first_frame/last_frame hoặc output đúng từng khung hình.
- History/All Video Tasks, filters, copy prompt/ID, preview, open chat, Continue, Stop, delete và bulk delete theo trạng thái.
- Stop dừng runner/monitor cục bộ, không hủy video đã gửi trên Dola. Soft-delete ẩn Task khỏi danh sách thường và giữ file gốc.
- Edit-selection cũ copy MP4 từ downloads sang tag, có filter và delete guard. Đây là selection để edit, độc lập với Scene selected output của V2.
- Accounts: profile riêng, Google/Facebook/cookies, verify/retry/open web, bulk Google import, scheduling, quota UUID/reset quota, credit/cooldown.
- API keys: quản lý auth, quota, allowed durations và concurrency riêng; admin auth dùng X-Admin-Key, generation auth theo Authorization policy hiện có.
- Giữ custom monitoring window 7 ngày cho video 30s/Continue; đây là thời gian theo dõi, không phải độ dài video.
- Try 30s đã bỏ. Extension `extensions/dola30/` bổ sung option 30s trong Dola; click được option không chứng minh video thật dài đủ 30 giây.

## 6. File và dữ liệu quan trọng

| File/nhóm | Vai trò |
| --- | --- |
| `run_server.py` | Launcher một process, Windows ProactorEventLoop cho Patchright |
| `server.py`, `store.py` | FastAPI/auth/task submission/recovery/admin; SQLite tasks và migration |
| `browser_pool.py`, `browser.py` | Accounts, locks, quota, FIFO admission, persistent Chromium |
| `video_worker_ui.py`, `video_worker.py` | Dola UI generation, polling/resume, tải video/phân loại lỗi |
| `production_store.py`, `scene_workflow.py`, `scene_import.py` | V2 storage, requirements, validation/import/resolve |
| `asset_storage.py`, `asset_library.py`, `asset_api.py` | Safe paths, persistent uploads/deletion recovery, admin APIs |
| `scene_generation.py`, `project_generation.py` | Snapshot references và submission Scene/Generate All |
| `review_store.py` | Versions, production status, summary, selection/selected outputs |
| `reference_aliases.py`, `media.py`, `video_tags.py` | Alias mapping, ảnh temporary/download, edit-selection cũ |
| `web/index.html` | Toàn bộ dashboard; Projects mở rộng trong ProjectReview IIFE |
| `config.py`, `requirements.txt`, `requirements-dev.txt` | Config và dependencies |

Storage local: tasks.db, pool_usage.db, accounts/, downloads/, tag/, assets/, diagnostics/ và log. `DOLA_ASSET_DIR` mặc định assets, root tương đối được resolve theo thư mục source; file Asset dạng project_id/asset_id.ext. DOWNLOAD_DIR/DB_PATH có thể override bằng config.

Không commit profiles, cookies, mật khẩu, keys, local DB hoặc diagnostics nhạy cảm. Giữ các thay đổi custom và history khi cập nhật upstream; không reset/clean working tree để thay bằng main.

## 7. Cách chạy trên máy hiện tại

Workspace: `C:\Users\Administrator\Downloads\video-rendering`.
Venv: `.venv` bên trong workspace; đường dẫn `C:\dola\...` trong ghi chú cũ không phải đường dẫn hiện tại.

```powershell
Set-Location C:\Users\Administrator\Downloads\video-rendering
.\.venv\Scripts\python.exe run_server.py --host 127.0.0.1 --port 8000
```

Dashboard: http://127.0.0.1:8000/ ; health: /health ; API docs: /docs. Không dùng /web làm dashboard. Launcher tự đặt working directory về project và chạy workers=1, reload=False.

Nếu cần cài dependencies/browser:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m patchright install chromium
```

Config đọc `.env.local`, biến môi trường terminal ưu tiên. Default code: MAX_CONCURRENCY=100, MAX_PENDING_TASKS=500; local config có thể override. Để không dùng explicit application proxy, đặt `DOLA_PROXY=`; mặc định code là http://127.0.0.1:7890. Không chép nội dung keys/passwords vào context.

Sau sửa Python cần restart server; sau sửa HTML cần reload/Ctrl+F5. Người dùng muốn tự chạy server; không tự mở lại hoặc dừng server của người dùng chỉ để kiểm thử UI.

Lỗi SQLite database is locked đã sửa ở BrowserPool: `_clear_expired_rate_limits` commit transaction ngay cả khi UPDATE không đổi dòng nào, tránh giữ write lock lúc startup. Có regression test `test_pool_transactions.py`. Không xóa DB/history để chữa lock.

## 8. Việc đã làm và kiểm thử

V2 đã hoàn thành Step 1 data model, Step 2 persistent references, Step 3 Scene import, Step 4 reliable queue/Generate All, Step 5 versions/review/selected outputs, và bổ sung full browser workflow.

Kết quả gần nhất của phiên triển khai:

- 239 backend tests pass: 201 tests cũ và 38 tests Production UX/lifecycle/migration mới.
- Browser acceptance tạo Project rồi import 40 Scenes, upload PNG/WebP, thumbnail decode, tự resolve, Generate All, queued/processing, play MP4 fixture, select/unselect/regenerate/switch selection, đạt 40/40 selected và xem output đúng thứ tự.
- Browser review test cũ pass; ba regression suites Generate/History batch, video tags và edit-selection/delete pass.
- Browser tests dùng Chromium đã cài, temporary/in-memory stores/assets và mock generation, không tiêu quota Dola.
- Earlier migration/startup smoke đã kiểm tra schema/backups/history preservation. Không coi fixture pass là đã xác nhận end-to-end generation thật với Dola sau thay đổi UI.
- Bổ sung UI đầu tiên thêm endpoint đọc Asset image. Production UX thêm replace/remove và migration v5; chỉ kiểm tra migration trên bản backup tạm của DB thật, không migrate DB thật hoặc restart server người dùng.

Chạy lại bộ test đã xác định là offline:

```powershell
.\.venv\Scripts\python.exe -m unittest test_production_ux test_asset_preview test_project_review test_project_queue test_scene_import test_asset_library test_production_store test_merge_compatibility test_reference_aliases test_account_uuid test_video_tags test_pool_transactions -q
.\.venv\Scripts\python.exe test_project_workflow_ui.py
.\.venv\Scripts\python.exe test_project_review_ui.py
```

Không mặc định chạy toàn bộ test_*.py: repo có script thử nghiệm live có thể mở profile/gửi generation. Regression browser cũ có chỗ yêu cầu system Chrome; phiên kiểm thử dùng launch override trong RAM để chạy bằng bundled Chromium, không sửa các test đó.

Reports/screenshots nằm trong diagnostics/. Tài liệu chi tiết: V2_DATA_MODEL.md, REFERENCE_LIBRARY.md, SCENE_IMPORT.md, GENERATE_ALL.md, PROJECT_REVIEW.md, PROJECT_WORKFLOW_UI.md, PRODUCTION_UX.md. Hai tài liệu đầu chứa chi tiết milestone trước; schema hiện tại lấy theo code và SCENE_INFO.md; JSON import vẫn theo SCENE_IMPORT.md, không theo riêng version cũ trong milestone.

## 9. Trạng thái Git và phạm vi còn lại

Nhánh kiểm tra hiện tại: `adding-more-scene`. Các thay đổi V2/UI còn trong working tree; phiên này không commit/push. Giữ nhánh custom, không tự sửa main hay bỏ các thay đổi sẵn có.

Chưa triển khai: final-frame continuity/extraction, ZIP/export, ghép phim/timeline/automatic editing, video thumbnail extraction, GPT integration, React/Vue migration. Không tự mở rộng các phần này khi chưa có yêu cầu.

## 10. Production UX mới nhất (06/10/2026)

- Bulk Reference Workspace hỗ trợ chọn nhiều ảnh, drop zone và drop trực tiếp lên missing reference; staging local, preview, mapping, type và filename suggestion chỉ khi normalized match duy nhất. Không upload trước confirm, không bắt buộc đủ mọi references. Results từng item, lỗi một ảnh không mất các ảnh thành công; auto resolve và refresh sau batch.
- Replace Image tạo Asset/file UUID mới và rebind current requirements/scene_assets atomically; giữ alias/order/name/type. Old snapshot và physical image của Tasks (kể cả queued/failed/soft-deleted) không bị đổi.
- Delete trong Library là Production Remove: giữ requirement/name/alias/position nhưng asset_id=NULL, readiness MISSING_REFERENCES. Historical backing được retire/giữ, unused backing cleanup qua existing physical-delete guards. Legacy DELETE bảo vệ history vẫn tồn tại.
- V5 thêm assets.retired_at nullable và unique index chỉ cho active names; retired Assets không xuất hiện trong current Library/import/resolve. Restart bình thường tự backup trước migration; không drop/rebuild tasks. Migration clone v4->v5 đã giữ nguyên dữ liệu thật có 1 Task, 2 Scenes và 4 Assets.
- Scene và Generate All có x1–x5 candidates, default x1. count là execution option, không thêm vào Template 5/Scene JSON. Cùng Scene/config/snapshot, IDs riêng, chung batch_id, queued qua engine cũ. Project xN tạo eligible_scene_count*N Tasks atomically và giữ policy skip hiện có.
- Confirm luôn hiện total Tasks/cost attempts trước generate. UI khóa double-click; request_id scoped deterministic batch_id chặn lặp cả khi batch đã completed (409, không tạo thêm Tasks). Clients cũ không gửi request_id chỉ có active Scene guard; future intentional Regenerate dùng request_id mới.
- Status đọc tất cả candidates, processing ưu tiên queued; selected vẫn SELECTED khi có candidates mới active. Scene card hiện total/completed/processing/queued. candidate_revision giúp review polling cập nhật cả older candidates. Selection cũ/history được giữ khi regenerate.
- Browser acceptance mới pass 40 Scenes x3=120 Tasks, regenerate x5, replace/remove/bulk và 40/40 selected; x1/review/legacy browser regressions pass. Tests dùng fixtures/mock, không tiêu Dola quota.

Chạy acceptance mới: .\.venv\Scripts\python.exe test_production_ux_ui.py

Milestone Production UX: implement đúng ba phần Production UX trên. Chi tiết file/API/schema/verification trong PRODUCTION_UX.md. Server/database thật không được restart/migrate bởi agent; người dùng tự restart để nạp migration và APIs mới. Không commit/push, không mở rộng feature ngoài scope.


## Scene Info (2026-10-06)

Current task completed: separate Scene Info import and Scene summary UI. See SCENE_INFO.md for the current schema/API/verification contract. Schema v6 adds nullable scenes.scene_summary using the existing backup/transactional migration chain; old Scenes remain NULL. Dedicated import and manual edit update only scene_summary, preserving even updated_at (generation revision).

Project header now has Import Scene Info separately from Template 5 Import Scenes. Exact JSON is {"scenes":[{"scene_number":1,"summary":"..."}]}. Validate is read-only and previews entries/matches/unknown numbers. Matching is Project-scoped. Unknown numbers reject the whole import; no Scene creation. Re-import is allowed. Summary appears below the Scene title, clamped to three lines with Show more/Show less when needed; Edit summary is available. Missing summary is never derived from the prompt.

New admin routes: POST /projects/{id}/scene-info/import/validate, POST /projects/{id}/scene-info/import, PATCH /scenes/{id}/summary (all under /api/admin). Generation payload construction, Template 5, bulk references, x1-x5, workers, BrowserPool and queue are unchanged.

Verification: 252 backend tests passed; new Scene Info browser acceptance and all three existing Project/review/production UX browser suites passed. Actual DB was read-only backed up to a temporary copy: v4->v6 preserved 1 Task, 2 Scenes, 4 Assets; existing test_riven Scenes 1/2 imported the exact requested Vietnamese summaries on the copy, changing only summary. Live DB/server remain untouched; user restarts manually. Reports and screenshot are in diagnostics/. No live Dola generation, no commit/push.
