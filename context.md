# Context dự án Video Rendering

Cập nhật: 25/09/2026. Tài liệu mô tả bản custom tại commit `bb8ea94`.

## Tổng quan

Dịch vụ tạo video qua Dola, dùng Python/FastAPI, SQLite và Patchright để điều khiển Chromium. Dashboard là HTML/CSS/JavaScript thuần, không cần build frontend.

Luồng chính: Dashboard/API → lưu task → chọn tài khoản trong pool → mở profile Chromium → upload ảnh, chọn model/thời lượng, điền prompt → gửi tạo video → theo dõi kết quả → tải MP4 về `downloads/` → trả URL.

## Nhánh Git đang làm việc

- Repo: https://github.com/HieLT/video-rendering
- Chỉ phát triển trên nhánh `adding-more-scene`; `main` là nguồn cập nhật gốc.
- `adding-more-scene` đã được push lên GitHub ở `bb8ea94`.
- Đã gộp main đến `14c89c0` (`start/end ui`).
- Lần kiểm tra remote gần nhất thấy main có commit mới `2eeeb88`, **chưa được gộp hoặc đánh giá trong bản hiện tại**. Trạng thái này có thể thay đổi sau ngày viết tài liệu.
- Khi cập nhật main, giữ đầy đủ chức năng gốc và các phần custom bên dưới; đối chiếu code Git thực tế, không tự thay thế tính năng gốc bằng luồng suy đoán.

## Các chức năng hiện tại

### Tạo và quản lý video

- Model: Seedance 2.0 (Fast) và Seedance 2.5; thời lượng yêu cầu 10/15/30 giây.
- `Name (optional)`: đặt tên như `scene 2`, lưu vào SQLite và hiển thị cạnh Task ID. Tên không được đưa vào prompt gửi Dola.
- `Image mode`: ảnh tham chiếu thông thường hoặc Start / End.
- Start / End có hai ô ảnh và preview riêng, yêu cầu đủ hai ảnh; gửi ảnh mở đầu trước, ảnh kết thúc sau, cùng `start_end=true`.
- Backend Start / End hiện bổ sung chỉ dẫn vào prompt dựa trên thứ tự ảnh. Không bảo đảm chính xác từng khung hình đầu/cuối, không đồng nghĩa gán vai trò native `first_frame`/`last_frame` trong request.
- `All Video Tasks`: trả toàn bộ task chưa bị xóa khỏi danh sách, không giới hạn 50/200.
- Các thao tác tùy trạng thái gồm Open chat, Continue, Stop, Delete. Stop dừng theo dõi cục bộ, không hủy quá trình tạo trên Dola. Delete ẩn bản ghi và giữ file video.
- Có khôi phục một số tác vụ sau restart dựa trên account/conversation ID đã lưu. Token ảnh upload và job quản trị nằm trong RAM, không bền qua restart.

### Try 30s — chức năng custom

- Nằm cạnh Generate Video, có bước chọn tài khoản.
- Lấy ảnh theo Image mode đang chọn, upload ảnh, điền prompt, chọn model/tỷ lệ rồi thử chọn 30s.
- Với Start / End, dùng cùng chỉ dẫn prompt như luồng tạo video.
- Không nhấn Enter để gửi, không bấm tạo video, không tạo bản ghi generation task.
- Báo kết quả trên dashboard; giữ Chrome mở cả khi chọn thất bại để người dùng kiểm tra.
- Chỉ kết thúc phiên khi người dùng đóng Chrome; dừng server cũng có thể kết thúc phiên.
- Khóa tài khoản trong thời gian kiểm tra để tránh dùng chung profile với tác vụ khác.

### Tài khoản, API key và chạy đồng thời

- Profile riêng trong `accounts/<account>`; hỗ trợ các luồng Google, Facebook và nhập cookie.
- Pool quản lý khóa tài khoản, quota, credit và cooldown; mặc định tối đa 5 tác vụ tạo video đồng thời toàn hệ thống.
- Mỗi tài khoản chỉ có một hoạt động giữ khóa tại một thời điểm. API key có giới hạn riêng.
- Dashboard tự polling accounts/jobs; các dòng GET lặp trong terminal không có nghĩa đang tạo nhiều video.

### Option 30s

Extension `extensions/dola30/` can thiệp phản hồi cấu hình/skill của Dola để thêm option 30s, rồi giao diện Dola render option đó. Đây không chỉ là thêm một nút HTML.

Hiển thị hoặc click được 30s không chứng minh server Dola chấp nhận hay tạo video đủ 30 giây. Khi chẩn đoán cần phân biệt cấu hình trên dashboard, lựa chọn thực tế trong Chrome và kết quả video. Không chỉ dựa vào thời lượng lưu trong task.

## Các file quan trọng

| File | Vai trò |
| --- | --- |
| `server.py` | FastAPI, xác thực, API task/admin, upload ảnh, phục hồi task và phiên Try 30s |
| `store.py` | SQLite task/API key, migration schema; lưu cả `name` và `start_end` |
| `browser_pool.py` | Chọn tài khoản, khóa profile, quota/credit, điều phối |
| `browser.py` | Mở persistent Chromium profile, proxy và extension |
| `video_worker_ui.py` | Luồng tạo video qua giao diện, upload ảnh, polling |
| `video_worker.py` | Các hàm protocol/polling, tải video và phân loại lỗi |
| `try_30s.py` | Chuẩn bị bản nháp và thử thời lượng, giữ Chrome mở |
| `media.py` | Kiểm tra URL và tải ảnh tham chiếu |
| `account_import.py`, `add_account.py` | Nhập/đăng nhập tài khoản |
| `web/index.html` | Toàn bộ dashboard |
| `config.py` | Đọc biến môi trường và `.env.local` |

Dữ liệu local: `tasks.db`, `pool_usage.db`, `accounts/`, `downloads/`, `diagnostics/` và log. Không đưa profile, cookie, mật khẩu hay dữ liệu chẩn đoán nhạy cảm lên Git. Khi commit nên chỉ định file code cụ thể thay vì `git add .`.

## Cách chạy trên máy hiện tại

Thư mục dự án: `C:\dola\video-rendering`.
Môi trường Python đã có: `C:\dola\.venv` (nằm ngoài thư mục dự án).

### CMD

```cmd
cd /d C:\dola\video-rendering
C:\dola\.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
```

### PowerShell

```powershell
Set-Location C:\dola\video-rendering
& C:\dola\.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
```

- Dashboard: http://127.0.0.1:8000/ — **không phải `/web`** như README cũ.
- Health: http://127.0.0.1:8000/health
- API docs: http://127.0.0.1:8000/docs
- Giữ terminal chạy, nhấn Ctrl+C để dừng.
- Sau khi sửa Python: restart server. Sau khi sửa dashboard: Ctrl+F5 trình duyệt.
- Phải chạy từ thư mục dự án để tìm `server.py`, `web/`, profile và database đúng chỗ.

### Cấu hình proxy

`config.py` mặc định dùng `http://127.0.0.1:7890` nếu không có cấu hình. Không có proxy hoạt động ở đó sẽ gây `ERR_PROXY_CONNECTION_FAILED`.

Để bỏ proxy được chỉ định bởi ứng dụng, đặt trong `.env.local` tại thư mục dự án:

```dotenv
DOLA_PROXY=
DOLA_MAX_CONCURRENCY=5
```

Biến môi trường của terminal ưu tiên hơn `.env.local`. Nếu trước đó đã đặt proxy, xóa biến trước khi restart:

```cmd
set DOLA_PROXY=
```

Hoặc trong PowerShell:

```powershell
Remove-Item Env:DOLA_PROXY -ErrorAction SilentlyContinue
```

Nếu cần proxy thì đặt URL hoạt động vào `DOLA_PROXY`. Khả năng truy cập Dola/tính năng tạo video còn phụ thuộc mạng và tài khoản. Extension dùng cửa sổ Chromium hiện ra, kể cả khi cấu hình headless được bật.

### Nếu cần cài lại dependencies

```cmd
C:\dola\.venv\Scripts\python.exe -m pip install -r requirements.txt
C:\dola\.venv\Scripts\python.exe -m patchright install chromium
```

## Kiểm tra cục bộ

Từ thư mục dự án:

```cmd
C:\dola\.venv\Scripts\python.exe -m unittest test_merge_compatibility test_try_30s -v
```

Các test này kiểm tra tích hợp tên scene/start-end, danh sách không giới hạn và vòng đời Try 30s bằng mock; không tạo video thật. Lần kiểm tra khi merge `bb8ea94`: 5 test pass, JavaScript hợp lệ. Chưa xác nhận end-to-end tạo video trên Dola sau merge.

Không chạy toàn bộ `test_*.py` một cách mặc định: một số script từ main là thử nghiệm live, có thể mở profile, gửi request và tạo video thật.
