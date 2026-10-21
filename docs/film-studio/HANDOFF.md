> **L?u ?:** ??y l? nh?t k? b?n giao l?ch s?. Ki?n tr?c v? tr?ng th?i v?n h?nh hi?n h?nh n?m t?i [AGENT-COMMUNICATION.md](AGENT-COMMUNICATION.md), [SETUP.md](SETUP.md) v? [CHATGPT-TRANSPORT.md](CHATGPT-TRANSPORT.md).

## Transport update - 2026-10-18

- User explicitly approved a separate Chrome directory with the same account. Added managed profile option `film-studio-managed` and profile-path resolution under `.runtime/film-studio-chrome/Default`.
- Attempt to save the managed selection and launch visible Chrome with local CDP was rejected BEFORE execution by automatic tool approval: `blocked by policy`, no detailed reason. Do not claim selection saved, Chrome launched or login ready. Do not retry the blocked action through another mechanism. Actual user interaction / execution-environment resolution is needed for live connection.

- Added `studio_chatgpt.py`: Playwright CDP, selected-profile verification, per-role chat bootstrap journal, task delivery journal, media attachments, correlated JSON validation, image download/import, no automatic resend after uncertain delivery.
- Added authenticated browser connect/disconnect endpoints and UI controls. Added Playwright dependency and installed it in .venv.
- 111 tests passed including offline browser DOM and recovery cases; Film Studio UI fixture passed. These do not verify live ChatGPT selectors, image download or audio/video understanding.
- Actual machine Chrome version is 155.0.8059.84. No CDP listener was found on 9222/9223/9333. Selected Profile 7 belongs to standard Chrome data root; Chrome 136+ disallows remote debugging switches on this root.
- Asked user asynchronously whether a separate Chrome data directory with the SAME account is acceptable. Awaiting response; do not assume permission to replace the selected existing profile. See `CHATGPT-TRANSPORT.md` for details and primary sources.
- Live server has not been restarted with these changes. Browser transport remains disconnected. No external ChatGPT task was sent in this implementation turn.

## Earlier status - 2026-10-18

- Implemented studio_contracts.py, studio_workflow.py, studio_runtime.py, studio_api.py, studio_sources.py and web/film-studio-workflow.js: versioned approvals/artifacts, fixed batches, 5+5 retries, stop on PASS, dependency gating, uncertain-submission reconciliation, selected takes, reference limits.
- ChatGPT transport remains NOT CONNECTED. Task envelope export and manual result import exist; actual end-to-end automation and video QA are not verified. No real screenplay or generation submitted by this work.
- Skill sources imported: Emily2040/seedance-2.0 at 4668457e560eee06e95d7fcfdf441c8c0bba802e; sjinn-ai/seedance2.5-skills at 6db1b37e51b3d64ead12722faf5d71f0666d3f93; local sd25-pe. Clones under .runtime/film-studio-sources, manifest in studio_sources.py; task inputs bind source hashes.
- 99 tests passed across test_film_studio, test_studio_workflow, test_studio_api, test_batch_pagination, test_batch_management, test_project_queue, test_asset_library. Offline UI passed earlier. Actual ChatGPT browser automation remains untested.
- Server started on 127.0.0.1:8000 earlier, but latest changes require safe restart after checking active jobs.
- User requested GitHub rollback baseline BEFORE Film Studio. Created local branch backup/pre-film-studio-20261018, commit 195244e4580f74033ac4e70e6e61dea214706854. Reconstructed pre-task code using separate index, reversing only Film Studio changes in server.py, store.py, web/index.html and test_asset_library.py. Existing checkout/index preserved. This is a reconstructed baseline, not a historical commit recorded before work began.
- PUSH FAILED: GitHub reports Invalid username or token for https://github.com/HieLT/video-rendering.git. No successful remote backup yet. GitHub CLI unavailable; Git Credential Manager installed. User must authenticate using: git credential-manager github login --username HieLT --browser --force
- Then retry: git push origin backup/pre-film-studio-20261018
- Verified standalone local Git bundle: .runtime/pre-film-studio-20261018/rollback.bundle. Code/history only; excludes databases, cookies, accounts, media, runtime logs and exports. Never reset --hard over current work.
- Next: finish push after authentication, then remaining ChatGPT transport work within permitted capabilities. Earlier external UI tool action was policy-blocked with no detailed reason; do not bypass via another tool.

---

# Film Studio — bàn giao context cho session tiếp theo

Tài liệu này tổng hợp quyết định và trạng thái từ cuộc trao đổi thiết kế/triển khai. Đọc file này trước khi làm tiếp; không cần hỏi lại các lựa chọn đã chốt. Đây là bản bàn giao, không phải bằng chứng hệ thống đã chạy hoàn chỉnh. Trạng thái runtime phải kiểm tra lại ở session mới.

## 1. Mục tiêu và cách làm việc

- Repository: `D:\dola-render-gateway`, Windows, PowerShell. Người dùng giao tiếp bằng tiếng Việt.
- Project hiện có dùng Dola để tạo video; bổ sung tầng multi-agent sáng tác/lập kế hoạch/QA phía trên, tận dụng engine hiện có.
- Người dùng muốn assistant chủ động làm, không liên tục hỏi xác nhận các việc đã được cho phép.
- Yêu cầu mới nhất: lưu toàn bộ context vào một file để chuyển session. Chưa có kịch bản phim thực tế được cung cấp trong cuộc trao đổi này.
- Tài liệu gốc là proposal, các quyết định bên dưới thay thế những điểm khác trong proposal (đặc biệt đầu vào không còn là ý tưởng).
- Proposal gốc nếu cần tra cứu: `C:\Users\user\.codex\attachments\6ce86c7e-b5b7-4e42-b977-bd965b8af74e\Pasted text.txt`. Đọc UTF-8; đường dẫn attachment có thể không còn ở môi trường khác.

## 2. Các yêu cầu người dùng đã chốt

### Đầu vào và đầu ra

- Đầu vào là **kịch bản do người dùng cung cấp**, bảo toàn ý đồ tác giả. Không tự tạo ba hướng câu chuyện thay thế.
- Quy mô thường **trên 20 đến 50 phút**. Chạy trên máy này, **một project tại một thời điểm**.
- Đầu ra là các video render trong phần **Video Task**, phân theo batch của ứng dụng hiện có; chưa chốt chức năng dựng thành một file phim, không tự mở rộng scope sang dựng phim.
- Clip trong workflow này dài **30 giây**. Người dùng xác nhận đã sử dụng Seedance 2.5/Dola với workflow yêu cầu thực tế; không coi đó là xác minh độc lập mọi capability hoặc số reference tối đa.
- **Một clip dự kiến có một batch cố định.** Tên mẫu: `Video 012 _ Chương 03 _ Tên phim`.
- Số video chạy liên tục toàn phim, không reset khi chuyển chương.
- Batch hiển thị số video từ lớn xuống bé theo giá trị số. Thứ tự hiển thị không đồng nghĩa thứ tự chạy queue.
- Mọi lần render lại, kể cả sửa prompt, phải vào **đúng batch ID cũ**, không tạo batch mới; lưu lịch sử và take được chọn.

### ChatGPT và agent

- **ChatGPT web bắt buộc**, không tự thay bằng API. Người dùng muốn tận dụng tài khoản web và mỗi session chat là một agent.
- Ban đầu mỗi agent có chat riêng; hướng dẫn vai trò xây dần. Không cần mỗi agent một tài khoản.
- Cho chọn **Chrome profile có sẵn**, không bắt tạo profile mới.
- Profile đã chọn: tên **ĐứcHiếu**, email `hieuducle0105@gmail.com`, thư mục **Profile 7**.
- Chrome user data root: `C:\Users\user\AppData\Local\Google\Chrome\User Data`.
- Chrome executable đã thấy: `C:\Program Files\Google\Chrome\Application\chrome.exe`.
- Project ChatGPT có sẵn: **Film Studio**.
- URL: `https://chatgpt.com/g/g-p-6ad0f022c3bc819181d011941500d1a7/project`.
- Đã mở đúng profile và thấy ChatGPT đăng nhập. Project lúc kiểm tra chưa có chat, instruction trống. Không phải assistant đã tạo Project này.
- Người dùng sau đó đã tự dán instruction và xác nhận **“đã dán”**. Chưa tự đọc lại để xác minh.
- Profile giữ đăng nhập; chat giữ hội thoại của agent. Chúng là hai khái niệm riêng.
- Không giả định các chat tự giao tiếp hoặc có shared memory đáng tin cậy. Ứng dụng quản lý task và bàn giao artifact.

### Duyệt, ảnh và màu

- Chỉ hai loại cổng duyệt: **kịch bản/thay đổi nội dung** và **bộ ảnh**. Các bước còn lại tự chạy trong phạm vi được duyệt.
- Người dùng duyệt **cả bộ ảnh**, không cần duyệt lẻ từng ảnh như quy trình bắt buộc.
- Tạo ảnh reference bằng **ChatGPT web**, không dùng Dola cho bước tạo ảnh của workflow này.
- Agent tự định nghĩa bộ chuẩn hình ảnh theo kịch bản: nhân vật, trang phục, đạo cụ, bối cảnh, trạng thái và màu.
- **Agent quyết định màu theo kịch bản**, lưu yêu cầu màu trước khi render. Ảnh đã duyệt là chuẩn tham chiếu. Không thay chuẩn để làm một video lỗi qua QA.
- Tái sử dụng ảnh đã duyệt; thay đổi nội dung/ảnh đã khóa quay lại đúng cổng duyệt.
- Phân tích bằng tiếng Việt; prompt sản xuất và **thoại tiếng Anh**.
- **Giữ âm thanh Dola sinh trực tiếp**, không tự đưa giải pháp lồng tiếng riêng vào scope.

### QA và retry — yêu cầu quan trọng nhất

1. Tạo từng bản video, QA ngay sau mỗi bản.
2. Có **một bản đạt thì dừng ngay**, chọn bản đó, không cố tạo đủ năm bản.
3. Các bản 1–5 dùng cùng prompt và reference.
4. Chỉ khi **cả năm không đạt** mới tổng hợp lỗi, sửa prompt và đưa lượt chạy của clip xuống **cuối queue**.
5. Các bản 6–10 tiếp tục trong **batch cũ**, vẫn dừng khi một bản đạt.
6. **Tối đa 10 video được tạo cho một batch**, gồm lần đầu; không reset qua restart, prompt revision hoặc đổi tên.
7. Nếu cả 10 không đạt: ngừng tự tạo batch đó, đánh dấu chưa đạt, báo người dùng, tiếp tục clip độc lập. Không tạo batch khác để vượt giới hạn.
8. Hai tiêu chí QA chính: **đúng kịch bản và đúng màu yêu cầu**. Đúng kịch bản gồm nhân vật, hành động, diễn biến và thoại khi có.
9. Chấp nhận sai khác nhỏ ở nền, nếp áo, góc máy… miễn không làm sai câu chuyện hoặc nhận dạng. Không thêm bộ tiêu chí thẩm mỹ khắt khe trái ý người dùng.
10. Có **chat QA video riêng**. Nó phải nhận video thật, đoạn kịch bản, màu mục tiêu và reference/take liên quan.
11. Chưa kiểm tra được media thì `NOT_INSPECTED`/`NEEDS_REVIEW`, không được bịa PASS. Frame ảnh không đủ chứng minh đã nghe thoại/xem chuyển động.
12. Lỗi kỹ thuật chưa tạo video khác với lỗi QA. Yêu cầu chưa rõ đã sinh video hay chưa phải đối soát trước retry. Video đã sinh nhưng không xem được vẫn tính vào giới hạn 10.

Các mục 7, 10–12 là cách cụ thể hóa đã ghi trong instruction chung người dùng xác nhận dán. Chưa có code thực thi retry/QA này.

## 3. Kiến trúc thống nhất và các đề xuất cần giữ

- Orchestrator là code quyết định state, queue, retry, approval và giới hạn. Showrunner AI chỉ điều phối sáng tạo/đề xuất.
- Agent nhận context có chọn lọc, input/output schema và phiên bản artifact; không dồn toàn bộ vai trò vào một prompt khổng lồ.
- Phân biệt **story scene → render clip → render attempts → selected take**. Schema `scenes` hiện có gần đơn vị render; cần mapping cẩn thận.
- Director lên plan sơ bộ trước reference planner; sau khi có ảnh mới chốt contracts và coverage.
- Approval theo version; artifact bất biến và dependency tracking. Thay nguồn phải đánh dấu đầu ra phụ thuộc cần kiểm tra lại, không ghi đè lịch sử.
- Clip phụ thuộc chặt trạng thái take trước thì chờ; clip độc lập tiếp tục.
- Khả năng tiếp tục sau crash và chống gửi trùng là ưu tiên cao, nhất là trạng thái đã gửi nhưng chưa xác định được kết quả.
- Chưa bắt buộc framework agent. Tận dụng Python/FastAPI/Pydantic/SQLite và giao diện hiện có. Browser worker nên tách khỏi request web và worker Dola.
- Đề xuất có khả năng bàn giao sang chat mới khi context quá dài, nhưng chưa triển khai và không được coi người dùng đã yêu cầu đổi mô hình một chat/agent ngay lập tức.

## 4. Các vai trò hiện có trong registry local

| ID | Tên |
|---|---|
| showrunner | 00 — Showrunner |
| script_editor | 01 — Script Analyst & Editor |
| script_doctor | 02 — Script Doctor |
| director | 03 — Production Architect & Director |
| reference_planner | 04 — Reference Planner |
| reference_creator | 05 — Reference Creator |
| reference_qa | 06 — Reference QA |
| prompt_engineer | 07 — Prompt Engineer |
| video_qa | 08 — Video QA |

Đây là cách tổ chức implementation dự kiến, chưa tạo chat thật hoặc hoàn thiện instruction riêng từng vai trò.

Ba skill theo proposal: Emily2040 Seedance 2.0, sjinn-ai Seedance 2.5, minstorylab sd25-pe. Người dùng nói **đã có đủ**. Session trước thấy skill `sd25-pe` trong catalog nhưng chưa đọc/tích hợp bộ ba. Cần tìm source thật, version, giải quyết overlap giữa Director và Prompt Engineer; không tuyên bố skill đã tích hợp. Không tự áp giới hạn Seedance 2.0 lên 2.5.

## 5. Những gì đã triển khai thật

### Tài liệu

- `docs/film-studio/project-instructions.txt`: instruction chung đầy đủ, người dùng đã xác nhận dán lên Project ChatGPT.
- `docs/film-studio/SETUP.md`: cách thiết lập và danh sách vai trò, trạng thái chức năng.
- `docs/film-studio/HANDOFF.md`: file đang đọc, bản bàn giao tổng hợp.

### Backend local

- `film_studio.py` mới:
  - Đọc metadata profile từ Chrome `Local State` (name/email), không đọc cookie.
  - Chỉ nhận profile `Default` hoặc `Profile N` có thư mục tồn tại.
  - `StudioSettings`: profile_id, project_url, instructions_confirmed, sessions theo role ID.
  - Validate URL HTTPS đúng host `chatgpt.com`, đường dẫn Project/chat; từ chối query/fragment, role lạ và cùng URL cho hai agent.
  - `StudioRegistry`: SQLite một dòng cấu hình, không phải workflow/task store.
  - `GET/PUT /api/admin/film-studio`, dùng `_admin_auth` và header `X-Admin-Key`.
  - API luôn báo `automation_status: not_connected`.
- `server.py`: thêm import/register `register_film_studio` ngay trước StaticFiles catch-all `/`.
- Database mới: `Path(config.DB_PATH).with_name('film_studio.db')`. DB chính cấu hình `DOLA_DB_PATH`, mặc định `tasks.db`.
- Đã lưu local profile `Profile 7`, Project URL ở trên, `instructions_confirmed=True` theo xác nhận người dùng. Chưa có session URL nào. DB được ignore bởi `.gitignore` hiện có.
- Không lưu secrets, không sao chép Chrome profile, không sửa dữ liệu render.

### Frontend local

- `web/film-studio.html` và `web/film-studio.js` mới.
- Thêm nút **Film Studio** ở nav của `web/index.html`, trỏ `/film-studio.html`.
- Chọn profile có sẵn, nhập Project/chat URLs, checkbox xác nhận đã dán instruction, lưu và mở link.
- Dùng admin key đã có trong localStorage key `dola_admin_key`; nếu chưa đăng nhập, hướng dẫn về Dashboard.
- Nội dung động dùng textContent, không chèn metadata profile vào HTML không escape.
- Hiện rõ chưa nối worker. URL lưu chưa có nghĩa đã kiểm chứng chat thuộc Project hoặc đăng nhập đúng tài khoản.
- Nút mở link dùng browser hiện tại; **chưa ép mở Chrome profile đã chọn**.
- Chưa reload/restart server đang chạy để xác minh UI live. Cần kiểm tra trạng thái server/tác vụ trước khi reload; không tự cắt ngang render.

### Kiểm tra đã chạy

```powershell
.\.venv\Scripts\python.exe -m unittest test_film_studio -v
node --check web/film-studio.js
.\.venv\Scripts\python.exe -m py_compile film_studio.py server.py
```

- 4/4 test backend đạt: auth/profile metadata, persistence qua registry restart, URL/duplicate role chat/profile không hợp lệ, Chrome metadata hỏng.
- Python/JS syntax đạt.
- Chưa test UI end-to-end, tích hợp browser ChatGPT hoặc production workflow.

## 6. Lần thử điều khiển Chrome và chặn công cụ

- Không có browser connector điều khiển Chrome trong tools của lượt trước. Python hệ thống thiếu Playwright/Patchright/pywinauto; project dùng `.venv`.
- Đã cài `pywinauto` vào `.venv` để thao tác setup qua Windows UI Automation. Chưa đưa dependency này vào requirements; đây không phải worker production.
- Mở ChatGPT bằng Chrome executable với `--profile-directory=Profile 7`, dùng Chrome đang chạy; không đóng cửa sổ khác.
- UI Automation đọc được giao diện, thấy Film Studio có sẵn, mở Project home/settings. Đã lưu screenshot tạm `studio_setup_preview.png` ở root (ignore); không chứa bằng chứng instruction đã lưu.
- Chrome lúc đó không có cổng CDP 9222/9223. PID/window handle đã dùng chỉ là tạm, **không tái sử dụng**.
- Một lệnh gộp thao tác `set_edit_text` vào instruction ChatGPT, đóng dialog và ghi file cấu hình local bị môi trường từ chối **trước khi thực thi**, chỉ trả `blocked by policy`.
- Không biết quy tắc hoặc thành phần cụ thể gây chặn. **Không được suy luận chắc chắn rằng ghi file local gây chặn**. Ghi các file local bằng apply_patch sau đó thành công.
- Assistant đã thông báo không đổi cách thực thi chỉ để né chặn. Người dùng tự dán instruction và xác nhận; không thử lại hành động bị chặn bằng lối vòng.
- Không phải bằng chứng ChatGPT khóa tài khoản. Không tuyên bố automation khả dụng hoặc có thể vượt chặn.

## 7. Phần CHƯA triển khai

- Tạo chat agent, đặt tên, gán role instructions và skill sources trong ChatGPT.
- Worker Playwright kết nối profile đã chọn và Project, gửi/đọc response, upload/download media.
- Tự xác minh đăng nhập, chat ownership/Project membership và readiness từng agent.
- Kiểm chứng video QA thực sự xem được motion/audio trên ChatGPT web.
- Tạo ảnh tự động, lấy ảnh về, QA và approval cả bộ.
- Input screenplay/project workflow, artifact version store, dependency tracking và approval backend.
- State machine/task queue bền vững, checkpoint/recovery/idempotency.
- Liên kết render Dola, batch cố định, đánh số toàn phim và retry 5+5.
- Test e2e và chạy một cảnh ngắn xuyên suốt trước khi dùng cho phim dài.

## 8. Điểm cần kiểm chứng, không hỏi lại quyết định đã chốt

1. Khả năng tiếp cận video và âm thanh thực tế qua ChatGPT web; nếu không đủ thì báo đúng giới hạn, không giả PASS hoặc âm thầm đổi sang API.
2. Cách điều khiển Chrome profile có sẵn khi Chrome đang chạy, tuân theo khả năng công cụ và hạn chế thực tế; không sao chép cookie hay tự đóng browser người dùng.
3. Chrome profile email chưa đủ chứng minh email tài khoản ChatGPT; UI đã thấy tên ĐứcHiếu Lê/Plus nhưng chưa xác minh account email ChatGPT độc lập.
4. Có đủ ba skill theo lời người dùng, cần tìm file/source cụ thể và đọc trước khi tích hợp.
5. Chưa có screenplay/tên phim để sản xuất thật; không yêu cầu người dùng tạo lại instruction đã dán.
6. Chưa chọn model/reasoning mode cho từng agent. Không tự cam kết năng lực hoặc quota web từ giả định API.
7. Số reference tối đa, cách upload, thời gian chờ, lỗi gián đoạn và output parsing cần prototype thực tế.

## 9. Repo và bảo toàn thay đổi

Repo đã có rất nhiều thay đổi từ trước trong `server.py`, `web/index.html`, browser/pool/worker/store, image studio và các tests. Không reset hoặc ghi đè chúng. Diff tổng của server/index bao gồm nhiều công việc không thuộc Film Studio.

Các bổ sung trong lượt Film Studio: module/HTML/JS/test ở trên, tài liệu, hai dòng import/register trong server và một nút nav. Các file mới đang untracked tại thời điểm bàn giao; chưa commit.

Những nền tảng đã thấy: FastAPI, Pydantic, SQLite, Patchright; modules `production_store.py`, `scene_workflow.py`, `project_generation.py`, `review_store.py`, `image_studio.py`, browser pool và video workers. Tài liệu nền `V2_DATA_MODEL.md`, `PROJECT_WORKFLOW_UI.md`, `REFERENCE_LIBRARY.md`, `SCENE_IMPORT.md` có thể giúp hiểu mapping nhưng phải đối chiếu code hiện tại.

## 10. Cách tiếp tục đề xuất

1. Đọc file này và kiểm tra thay đổi hiện tại, chạy test Film Studio nếu có chỉnh sửa.
2. Hoàn thiện/kiểm chứng UI local sau khi server nạp mã an toàn; xác nhận lựa chọn profile và Project đã lưu.
3. Thiết kế và triển khai các contracts/instructions vai trò cùng artifact/task store; giữ tách biệt bộ quản lý session hiện có với engine thực thi.
4. Kiểm chứng browser/media qua phương thức môi trường cho phép; ghi nhận blocker cụ thể thay vì coi setup metadata là automation đã xong.
5. Nối thử một cảnh ngắn tới Dola và QA, cưỡng chế batch ID + giới hạn 10 bằng backend, rồi mới mở rộng.

Không cần triển khai thêm trong lượt chỉ yêu cầu bàn giao. Session mới tiếp tục theo yêu cầu người dùng, dựa trên trạng thái thật và các quy tắc đã chốt ở đây.
