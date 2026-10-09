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
- Scheduler mới kiểm tra theo ETA cho mọi duration, không giữ Chrome mở liên tục 7 ngày. Timeout cũ chỉ còn trong legacy worker.
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

Config đọc `.env.local`, biến môi trường terminal ưu tiên. Default code: MAX_CONCURRENCY=100, MAX_PENDING_TASKS=500; local config có thể override. Proxy mặc định trong code là rỗng (`DOLA_PROXY=`), không cấu hình explicit application proxy. Không chép nội dung keys/passwords vào context.

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


## Main queue integration (2026-10-06)

Video Tasks, Generate Scene and Generate All now share VideoScheduler. Project reference snapshots are copied in order into .job_media/<task-id> and persist across restarts. Back up .job_media with tasks.db and assets when moving machines.

BrowserQueue admits at most 10 Playwright sessions across the workspace, including Open Web and bulk account import. Bulk import retains its worker count and 0.5-second launch spacing; workers beyond the browser ceiling wait for admission. A waiting video reserves its account but closes Chromium after confirmed acceptance. Scheduled checks reopen it. Recovery checks do not consume another generation quota slot.

First check is ETA minus five minutes, never earlier than now. Missing ETA defaults to 30 minutes. Each inspection lasts up to 20 seconds after navigation, followed by at most three additional checks five minutes apart, then needs_recovery. Generation permits up to 10 retries after the first attempt, preserving dispatch checkpoints. Unknown submission outcomes are inspected instead of blindly resubmitted.

MAX_PENDING_TASKS stays at 500 by default and counts unfinished needs_recovery records. API key concurrency remains enforced. Task count and browser-session capacity are different limits.

Video Tasks defaults to All with optional pagination and video/time filters (UTC+7). Batch grouping, scene names, edit tags, Gmail groups, bulk import and Project selection workflows remain. Project Versions exposes scheduler timing and Check now/Stop. Stop ends local monitoring only; it does not cancel remote Dola generation. Stop needs_recovery tasks before deleting records.

Renew Dola account and duplicate Favorite UI/API are intentionally excluded. Existing Project Start/End reference validation is retained.

Run one server via python run_server.py --host 127.0.0.1 --port 8000, without --reload. A workspace scheduler lock prevents two scheduler servers. Existing schema-v6 databases receive additive scheduling columns with a .before_queue.bak snapshot before migration. Restart the server to activate; no live database migration or live Dola generation was performed during integration.

Validation uses temporary SQLite databases, mocked Dola workers and browser fixtures, including Generate All, scene reference order, restart recovery, quota claims, selection, batch UI and time filters.

## Cập nhật vận hành và Git (2026-10-07)

Phần này cập nhật trạng thái sau các milestone ở trên; các ghi chú cũ “không commit/push” chỉ mô tả thời điểm của milestone đó.

- Nhánh làm việc: `adding-more-scene`. Đã merge main `21d5aeac` vào bản Project `20806774` qua commit `e0aa7471`; bản sửa Busy là `1ee6adf7`. Cả hai đã push lên `origin/adding-more-scene`. Nhánh dự phòng trước merge: `backup/adding-more-scene-before-queue-20261006` (local).
- Sửa Busy lúc khởi động: task lịch sử `needs_recovery` có `phase=ready` chưa thuộc scheduler mới không tự giữ account. `recover_reservations()` và `release()` cùng loại các record này khỏi danh sách giữ account. Test hồi quy nằm trong `test_queue_merge.py`.
- Theo yêu cầu người dùng, đã gọi Stop cho 26 task recovery cũ đang giữ account trên server local; không xóa video hoặc record lịch sử. Lúc kiểm tra sau đó còn một account Busy do Open Web thực sự đang mở. Đây là thao tác trên dữ liệu local, không phải dữ liệu được push GitHub.
- Busy vẫn hợp lệ khi account đang mở browser hoặc được scheduler giữ cho video chưa hoàn tất, kể cả lúc Chrome đã đóng để chờ ETA. Task scheduler mới ở trạng thái `needs_recovery` vẫn giữ account; Check now tiếp tục kiểm tra, Stop nhả reservation. Đóng cửa sổ Open Web sẽ nhả khóa phiên đó. Không ép nhãn Active cho account chưa đăng nhập hợp lệ.

### Tắt máy và chạy lại

Dola có thể tiếp tục xử lý yêu cầu đã nhận trên server của họ khi máy local tắt. Sau khi chạy lại dự án, task `queued`/`processing` được phục hồi từ SQLite; task quá lịch kiểm tra sẽ chờ slot để kiểm tra kết quả và tải video. Không tự chạy lại task `needs_recovery` hoặc `stopped`; dùng Check now khi task có conversation/result đã lưu. Nếu bị ngắt ngay lúc gửi yêu cầu, checkpoint có thể yêu cầu kiểm tra thủ công để tránh tạo trùng. Nên chờ trạng thái Waiting for Dola rồi Ctrl+C trước khi tắt máy.

Cơ chế này áp dụng cả Project và Video Tasks. Video gen từ Project là cùng một task hiển thị trong Video Tasks và Project → scene → Versions; không cần mở tab nào để scheduler chạy. Video hoàn tất vẫn gắn với scene và lịch sử phiên bản tương ứng.

Khi chuyển máy, clone code chưa đủ: cần giữ `tasks.db`, `pool_usage.db`, `accounts/`, `.job_media/`, `assets/`, `downloads/`, `tag/` nếu có và cấu hình local phù hợp. Profile đăng nhập có thể cần Verify/đăng nhập lại trên máy mới. Dữ liệu và secrets local không được đẩy lên GitHub bằng lần push code này.

### Cấu hình gen và số candidates trong Project

- Generate Scene/Generate All dùng `model`, `ratio`, `duration` đã lưu trên từng scene, thường đến từ JSON Import Scenes; không lấy giá trị đang chọn ở form Video Tasks.
- Tại lần kiểm tra ngày 2026-10-07, cả 14 scene trong database local dùng `seedance-2.5`, `16:9`, `30`. Đây là dữ liệu tại thời điểm kiểm tra, không phải mặc định cố định của mọi Project. API tạo scene nếu bỏ các trường này mặc định `seedance-2.0`, `16:9`, `10`.
- `Candidates per Scene` ở đầu Project áp dụng cho Generate All Ready. `Candidates` bên cạnh từng scene chỉ áp dụng cho Regenerate riêng scene đó. Ví dụ ô trên cùng x1, ô scene x4 thì Generate All vẫn tạo một video cho mỗi scene đủ điều kiện; Regenerate scene đó tạo bốn video.
- Muốn bảy scene đủ điều kiện tạo bốn video/scene, chọn x4 ở đầu Project: tổng 28 task. Scene đang queued/processing sẽ được bỏ qua; Ready=0 thì Generate All không tạo thêm cho các scene đang chạy. Hộp xác nhận hiển thị số task trước khi gửi.

### Lệnh chạy trên máy hiện tại

Trong CMD, khi đã kích hoạt `.venv` và đứng tại `C:\dola\video-rendering`:

```bat
python run_server.py --host 127.0.0.1 --port 8000
```

Nếu chưa kích hoạt môi trường:

```bat
cd /d C:\dola\video-rendering
C:\dola\.venv\Scripts\python.exe run_server.py --host 127.0.0.1 --port 8000
```

Mở `http://127.0.0.1:8000`. Sau khi thay code Python cần restart server; thay UI thì Ctrl+F5. Dùng launcher này trên Windows, không thêm `--reload`.

## Môi trường và giãn lượt browser (2026-10-07)

- Workspace mới: `E:\video-rendering`, Python 3.11 trong `.venv` ngay trong project. Dependencies runtime/dev và Patchright Chromium đã cài. `start_server.ps1` dùng venv trong project.
- Google nhanh / nhiều account: mặc định 5 phiên song song, giới hạn 1–5 ở UI/API và worker; mỗi lượt bắt đầu cách nhau ít nhất 0,5 giây. Submit không giữ start lock trong lúc chờ hoàn tất.
- BrowserQueue vẫn tối đa 10 sessions, FIFO và chia sẻ giữa process; timestamp admission trong SQLite giãn các lượt khởi động ít nhất 0,5 giây, kể cả Generate All, bulk import và lượt kiểm tra video. Không giữ transaction SQLite trong lúc sleep.
- `config.PROXY` mặc định rỗng; `.env.local` cũng đặt `DOLA_PROXY=`.
- Kiểm tra: 66 tests bulk/queue/scheduler pass; bổ sung kiểm tra model API default 5 và reject >5. Integration process-shared queue/recovery/quota pass. Browser workflow 40 Scenes pass với bundled Chromium channel; headless shell bị renderer crash nên dùng launch override trong RAM, không sửa test UI. Không chạy generation Dola thật hoặc restart server người dùng.

## Chế độ 2 request/account (2026-10-07)

- Tab Accounts có nút “Cho phép 2 request đồng thời / account”, mặc định tắt. PATCH `/api/admin/account-request-mode` nhận `{ "enabled": true/false }`, yêu cầu admin auth. Trạng thái được lưu trong `pool_usage.db`, bảng additive `scheduler_settings`, giữ qua restart.
- Bật chế độ cho phép cùng account giữ tối đa 2 video chưa hoàn tất. Sau khi Dola xác nhận request đầu, Chrome đóng và request thứ hai có thể được gửi vào chat mới, không chờ video đầu xong. Video Tasks và Projects cùng áp dụng vì dùng chung scheduler.
- Profile/browser account lock, BrowserQueue tối đa 10 và giãn lượt 0,5 giây, quota/API key limits, retry/ETA/check/download/selection pipeline giữ nguyên. Không mở đồng thời hai Chrome dùng cùng persistent profile; hai generation chạy đồng thời ở Dola.
- Quota vẫn 2 theo cơ chế reset hiện có. Assignment chưa dispatch giữ quota slot; ledger claim idempotent, không tăng quota khi polling/recovery. UI Accounts hiển thị số request đang chạy và readiness khi còn slot thứ hai.
- Tắt chế độ không hủy các request đã nhận; chặn assignment mới cho account còn request. Restart/Check now/Stop quản lý được cả hai tasks, hoàn tất hoặc Stop một task vẫn giữ reservation của task còn lại. Legacy recovery phase=ready vẫn không tự giữ account.
- Validation offline: 9 tests mới cho capacity/quota/restart/toggle/auth/profile lock, 62 scheduler/queue tests và 78 account/bulk/storage/review tests pass. Browser toggle bật/reload/tắt, workflow 40 Scenes, Production UX 40x3=120 candidates pass; process integration pass. Tests dùng temporary stores/mock generation, chưa xác nhận Dola thật có chấp nhận hai generation/account; không restart server người dùng.
- Chạy: `.\.venv\Scripts\python.exe -m unittest test_account_request_mode -q` và `.\.venv\Scripts\python.exe test_account_request_mode_ui.py`.


### Project chapters (2026-10-08)

- A project can contain chapters. In Projects, open a project and choose **+ Create Chapter**; open the chapter to import scenes, import scene info, generate all ready scenes, and review/select outputs for that chapter only.
- All chapters share the parent project's Reference Library. Uploading from any chapter stores the image once under the parent project. Reference names/aliases resolve against that shared library; chapters may each use Scene 01 without collisions.
- Existing scenes, tasks and selected outputs remain attached to their original project. Project-level Generate All covers its direct scenes only, not chapter scenes. Existing separate projects are not automatically combined.
- Shared reference replacement/removal affects linked scenes across chapters; existing task reference snapshots retain their history.
- Implementation: projects.parent_project_id (one chapter level), schema v7 with pre-upgrade backup; chapter creation/list endpoints at /api/admin/projects/{project_id}/chapters. Scene and generation endpoints keep using the selected chapter's ID. Assets resolve to the parent project ID.
- Restart the server and refresh the browser to apply. Offline coverage: test_project_chapters.py and test_project_chapters_ui.py.


### Project account selection and domain Dispatch (2026-10-08)

- Project / chapter toolbar: **Run Accounts (optional)** opens an account checklist with email/name/domain search. No selections means all eligible accounts; otherwise only saved account UUIDs can be assigned. Chapters inherit the parent project setting. This does not reserve those accounts exclusively for one project.
- Dispatch, login, quota, cooldown and concurrency rules still apply. When selected accounts are unavailable, tasks wait rather than using an unselected account. Already submitted videos retain their account for monitoring; unsubmitted assignments are rechecked.
- Schema v8 adds project_account_allowlist. GET/PUT /api/admin/projects/{project_id}/accounts reads/writes the parent project's list. Deleted/unavailable account UUIDs remain restricted until explicitly removed from the checklist.
- Accounts lists sort by created_at descending. Non-Gmail groups email domains case-insensitively. Each domain has a filter, readiness count and its own Dispatch toggle. Gmail and all Non-Gmail master toggles remain. Missing/invalid emails appear under Unknown domain.
- Restart the server and refresh the browser to apply. Offline validation: 42 account/scheduler/chapter/migration tests passed; browser UI verified account selection/save/clear, search, newest-first ordering, and isolated domain Dispatch. No live generation was performed for these checks.


### Chapter-specific Run Accounts and domain tabs

- Run Accounts now saves a chapter-specific override when **Use parent project accounts** is unchecked. Checked means inherit the parent project selection (the default for existing chapters). A chapter override with no checked accounts explicitly allows all eligible accounts; it does not inherit a restricted parent list. Changes do not modify sibling chapters or the parent.
- The account picker opens with domain tabs only. Click @domain to show its accounts, search within that domain, and Select visible as needed. Selections persist across tab switches and Save applies the whole selection. Clear selection clears every domain.
- Schema v9 adds project_account_overrides to distinguish inheritance from an explicit all-accounts choice. Shared reference images remain project-wide. Restart server and refresh UI to apply.


### Current behavior update (2026-10-09)

This section supersedes earlier retry and browser spacing descriptions.

- Run from `C:\dola\video-rendering` with the virtual environment activated: `python run_server.py --host 127.0.0.1 --port 8000`. Without activation: `C:\dola\.venv\Scripts\python.exe run_server.py --host 127.0.0.1 --port 8000`. Restart the server after backend updates and refresh the browser with Ctrl+F5.
- Project-generated task names include scene number and chapter/project name, for example `scene1_chapter1+2`.
- Video Tasks **Try again** for failed/stopped scene tasks requeues the SAME task ID in place. It retains batch position, creation time, scene/chapter link, original prompt/configuration/reference snapshot. It clears the old account/chat and dispatch checkpoints for a fresh submission. Old errors are stored in `tasks.retry_history` and shown under Retry history. This does not create another candidate or use newly edited scene inputs.
- Generate All and Try again use the shared BrowserQueue: at most 10 managed Playwright sessions, with video sessions starting at least 20 seconds apart. Requests can wait in the queue; 10 is the browser capacity, not the number of remotely generating videos. Result-check browser sessions also use the video interval. Non-video session spacing remains 0.5 seconds, subject to the shared FIFO and capacity.
- Closing the submission browser manually stops that task instead of automatically reopening it. Expired login sessions are handled by releasing the unusable account and selecting another eligible account.
- Explicit copyright/policy refusals stop the task even after an earlier generation confirmation. Recognized terminal codes include 710082022 and 710092007, the Japanese copyright refusal, and the exact Japanese content refusal below. Preserve the original error text; do not automatically open a new chat to retry these refusals.
- A completed ordinary confirmation/question response without an actual terminal refusal remains retryable; requests such as Confirm with Generate are not classified as copyright rejection.
- On a terminal policy refusal without a video/result URL, remove that task/account's local usage claim and subtract one local used slot, once. Claims older than 24 hours are not subtracted from later usage. This does not change Dola's own credit accounting or retroactively repair already stopped tasks.
- Open chat resolves the saved conversation for the exact task, including when one account has two tasks; tasks without a saved conversation cannot open an arbitrary chat.
- Chapters share parent references and may inherit or override Run Accounts. Account selection uses domain tabs; account lists show newest accounts first, with domain-specific Dispatch controls.
- `convert_darius_chapters.py` is a guarded one-time migration for the original four Darius/Garen projects; it is not part of startup and should not be rerun after conversion.
- Local databases, SQLite backups, accounts, reference images and downloads are not uploaded to Git. A clone alone does not include runtime data.

Terminal content refusal: ご希望のコンテンツを生成できません。他の内容をお試しください。

### Domain Open Web queue (2026-10-09)

- Each account domain section (including Gmail) has Open Web cả domain. It queues all accounts in that domain through the existing Open Web launch path, preserving profiles and assigned proxies. Existing Open Web sessions are excluded; repeated clicks deduplicate pending accounts.
- New launches start at least 5 seconds apart. At most 10 Open Web sessions may coexist, counting manual openings and starting sessions. Pending accounts wait until a window closes. Busy/unavailable accounts are skipped without aborting the remaining queue. This does not solve captcha automatically.
- The queue runs in memory on the backend, survives dashboard reloads/tab changes, and stops on server shutdown. Accounts shows active, pending, opened and skipped counts, plus Dừng mở thêm to cancel pending openings without closing existing windows. Restarting the backend clears the queue.
- Admin endpoints: POST `/api/admin/accounts/open-web-domain` with `{domain}`, and POST `/api/admin/accounts/open-web-domain/stop`. Validation: virtual-clock tests cover 23 accounts, 5-second spacing, the 10-window cap, release on close, deduplication, error handling and stop; domain API auth/scope and browser button/layout acceptance passed. No real browsers/accounts were opened by tests. Restart backend and Ctrl+F5 to apply.

### Compact Accounts layout (2026-10-09)

- Above the table, Account theo proxy shows the total assigned account count for every saved proxy (including zero), the current machine network, and any missing connection definitions. Counts cover the whole pool regardless of Gmail/domain filters and refresh after connection changes; no extra API or credentials are exposed.
- Account rows keep Open Web visible and move Verify, Reset quota, Retry login and Delete into an accessible floating action menu. Escape, outside clicks and scrolling close the menu; Escape returns focus to its trigger.
- Account type is shown below the email, and last-used time below quota. Long emails truncate with the full value available on hover. Connection dropdowns fit their cells, dates use two predictable lines, and desktop rows are 52 pixels tall in the 22-account browser fixture.
- Accounts uses a wider desktop container and a fixed column layout without horizontal table scrolling. At widths of 860 pixels or less, rows become compact cards with all controls visible. The mobile header wraps and navigation can scroll independently.
- Validation: `test_accounts_layout_ui.py` passed with 22 temporary accounts at viewport widths 390, 820, 900, 1024, 1280 and 1440 pixels, checking overflow, row height, proxy controls, Open Web and action-menu keyboard handling. Account connection/proxy management and request-mode browser regressions passed. No live account profiles or generation were used. Frontend changes apply with Ctrl+F5.

### Automatic proxy distribution (2026-10-09)

- Accounts toolbar has **Tự chia proxy**. It previews distribution across all saved usable proxies, never selecting **Mạng hiện tại** for an eligible account. Scope is the whole account pool, independent of the current Gmail/domain tab. Click **Áp dụng** to save.
- Account counts are balanced as evenly as busy reservations allow. Busy browsers/login jobs/unfinished videos keep their existing connection and are listed as skipped; their existing proxy assignments count toward balancing. A busy account already using the machine network stays unchanged until it is idle and the user runs distribution again.
- Existing balanced routes are preserved where possible to avoid unnecessary IP changes. The backend rechecks busy state on Apply, and writes the batch atomically. Preview does not save; repeated distribution with unchanged state is idempotent. Missing proxies produces an error instead of using direct access.
- Admin API: POST `/api/admin/account-connections/distribute`, with optional `?preview=true`. Validation: 21 management/routing tests and browser acceptance passed, including even distribution, proxy-only selection, preserved busy accounts, preview/apply recheck and repeated-run stability. Tests use temporary settings; no real account routing was redistributed automatically. Restart backend and Ctrl+F5 to apply.

### Proxy management UI (2026-10-09)

- Accounts toolbar now has **Quản lý proxy**. Paste one or many lines as `host:port:username:password | ID: provider-id`, then click **Thêm proxy**. ID is optional; blank lines and exact duplicate proxies are skipped. Hostnames/IPv4 and HTTP proxies with authentication are supported by this paste format.
- Duplicate results now identify every skipped line, its proxy label/endpoint, and whether it matches a saved proxy or an earlier line in the same paste. No username/password is returned. Validation: 16 management/routing tests and browser duplicate-detail acceptance passed.
- Validate the whole paste before one atomic save. Invalid lines report their line number without quoting credentials; nothing from that batch is saved. Reusing an existing ID with different settings, or an endpoint/username with a different password, is rejected instead of silently changing existing account routes.
- The manager lists proxy labels/endpoints and accounts using them. Unused proxies can be deleted; assigned proxies are protected until their accounts change connection. Deleted-account mappings do not prevent removal. **Mạng hiện tại** stays available independently of the proxy list.
- New imports immediately populate individual account dropdowns, bulk **Gán kết nối**, and the Add/Google import forms. Passwords are cleared from the paste box after success and never returned by management/list APIs; settings remain only in ignored `.connections.local.json`.
- Admin APIs: GET `/api/admin/proxies`, POST `/api/admin/proxies/import`, DELETE `/api/admin/proxies/{identifier}`. Account list reads the routing file once per request for larger account/proxy collections.
- Validation: 45 offline management/routing/bulk/scheduler/relay/browser-lifecycle tests passed. Browser acceptance passed multiple-proxy paste, atomic error handling, duplicate skipping, selecting a newly imported proxy for an existing account, viewing assigned accounts and guarded/unused deletion. No real proxy records/accounts were changed by tests and no live login/generation was submitted. Restart backend and Ctrl+F5 to apply.

### Authenticated proxy with the Dola30 extension (2026-10-09)

Ghi chú bàn giao: đã sửa lỗi Open Web sau khi chuyển account từ mạng hiện tại sang proxy có username/password. Đổi kết nối trực tiếp bằng dropdown trong cột **Kết nối** (tự lưu), hoặc tick nhiều account rồi **Gán kết nối**. Đóng browser lỗi, restart backend bằng launcher trong workspace `F:\video-rendering`, Ctrl+F5 rồi mở lại Open Web để nạp bản sửa. Không cần xóa profile hay database. Cấu hình proxy và mapping account chỉ có trên máy local; khi clone code ở máy khác cần chuyển riêng `.connections.local.json` cùng dữ liệu runtime, không đưa file này lên Git.

- Reproduced `ERR_INVALID_AUTH_CREDENTIALS` in a temporary Chromium profile only when the Dola30 debugger/Fetch interception was attached. The same upstream proxy credentials worked with ordinary Chromium and aiohttp.
- Authenticated account browsers now connect through a per-context loopback relay (`proxy_bridge.py`). It forwards traffic only to the account's chosen upstream proxy and supplies upstream authentication independently of Chrome Fetch interception. The relay listens only on 127.0.0.1, closes with its browser and is recreated during extension startup recovery; no direct fallback is introduced. Proxy-free/unauthenticated routes do not need a relay. The extension itself remains unchanged.
- Open Web closes/unregisters its browser after navigation failure; focusing a closed/empty context removes the stale entry and allows a fresh opening instead of a spurious 409.
- Validation: 21 offline routing/relay/browser-lifecycle tests passed, along with both extension duration configuration suites. A temporary browser with the real purchased proxy and Dola30 enabled returned HTTP 200 from the IP check (egress 222.254.99.4) and the public Dola chat page. No existing account profile, Dola login or generation was used; the user's server was not restarted. Restart the backend to apply.

### Per-account connections (2026-10-09)

- Accounts supports manual selection followed by **Gán kết nối**. Available choices on this machine are **Mạng hiện tại (không proxy)** and **Proxy 30686**. Existing accounts default to the current machine network; this includes WARP if the machine uses it. No accounts were automatically assigned to the purchased proxy.
- Each account also has a dropdown directly in the **Kết nối** column; selecting a different connection saves immediately without ticking the row or opening a dialog. Busy accounts have this selector disabled. Browser acceptance verifies individual switching, reload persistence and the disabled state for an active login job.
- Add Account, Google bulk import and Retry have a connection selector. Bulk import applies the selected connection to each new account before login begins. The Accounts table shows each account's saved connection; choices persist across reload/restart.
- Persistent browser launches (Google login, imported sessions, Verify, Open Web, generation and scheduled checks) and video HTTP downloads use the assigned account connection. Captcha image downloads use the browser context request client. These account paths use per-account routing instead of the global `DOLA_PROXY` value. Unrelated unaffiliated reference downloads keep their existing configuration.
- PATCH `/api/admin/account-connections` is admin-authenticated and applies a whole batch after validating every account. Busy browsers, login jobs and unfinished video reservations block connection changes. Missing assigned proxy definitions or malformed local settings stop routing instead of falling back to direct access.
- Proxy credentials and account assignments live only in ignored `.connections.local.json`; browser/API/UI responses expose connection IDs and labels, not proxy credentials. Keep this private file with runtime backups when moving machines. No SQLite migration is required.
- Validation: 42 offline tests passed across connections, Google bulk limits/timing, account mode/UUIDs/chat, queue integration and recovery. Browser acceptance passed batch assignment, reload persistence, separate account routes and proxy selection in Google import; account-mode UI and the 40-scene Project workflow also passed. All use temporary stores/mocks; no real Google login or Dola generation was submitted. Restart the backend and Ctrl+F5 to apply.

### Google bulk import timing update (2026-10-09)

- Google nhanh / nhiều account now defaults to 10 concurrent imports and accepts 1–10 in UI/API. The worker also caps concurrency at 10.
- Import starts are spaced at least 20 seconds apart: the first can start immediately, then the second after 20 seconds, and so on. Up to 10 unfinished imports may coexist; later accounts wait for capacity and the start interval. This supersedes earlier 5-session/0.5-second bulk-import notes.
- Offline validation: all 6 `test_bulk_accounts` tests passed, covering the API limit, maximum 10 overlapping slow submissions, start spacing, smaller concurrency, and failure handling. No live Google login was performed. Restart backend and Ctrl+F5 to apply.

### Environment setup on F: (2026-10-09)

- Current workspace: `F:\video-rendering`; local virtual environment: `F:\video-rendering\.venv` using Python 3.11.0 (`py -3.11`). Earlier workspace paths above describe other machines.
- Installed `requirements.txt`, `requirements-dev.txt` and Patchright Chromium (including headless shell and bundled FFmpeg). `pip check` passed.
- Start manually from this workspace: `.\.venv\Scripts\python.exe run_server.py --host 127.0.0.1 --port 8000`; dashboard: `http://127.0.0.1:8000/`. No activation or global `python` command is required.
- Browser fixture `test_project_workflow_ui.py` passed the complete 40-scene workflow with zero live Dola generations.
- Offline backend selection ran 262 tests: 260 passed, two migration tests errored because their expectations predate schema v9 (`test_production_ux` expects v6; `test_asset_library` expects a project without `parent_project_id`). Their failed assertions also caused Windows temporary SQLite cleanup errors. Tests were not modified during environment setup.
- This checkout had no `.env.local`, runtime databases or `accounts/`. Existing defaults apply; account login and any runtime data transfer are still needed to generate real videos. No production server was started and no live generation was submitted.
