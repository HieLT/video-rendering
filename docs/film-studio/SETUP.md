# Thiết lập Project ChatGPT Film Studio

Project: `https://chatgpt.com/g/g-p-6ad0f022c3bc819181d011941500d1a7/project`

Tài khoản browser đang dùng cho worker: `hieuducle0105@gmail.com` qua Account List. Không sao chép cookie hoặc profile Chrome để tạo context khác.

## Chat theo vai trò

Mỗi vai trò cần một chat bền vững trong Project. Worker lưu URL theo role, tái dùng chat đó qua các task và chỉ bootstrap chat thiếu khi thật sự chưa có session. Danh sách đầy đủ và luồng giữa các chat nằm trong [AGENT-COMMUNICATION.md](AGENT-COMMUNICATION.md).

| Mã | Role ID | Tên chat |
|---|---|---|
| 00 | `showrunner` | Showrunner |
| 01 | `script_editor` | Script Analyst & Editor |
| 02 | `script_doctor` | Script Doctor |
| 03 | `director` | Production Architect & Director |
| 04 | `reference_planner` | Reference Planner |
| 05 | `reference_creator` | Reference Creator |
| 05a | `reference_selector` | Reference Candidate Selector |
| 06 | `reference_qa` | Reference QA |
| 07 | `prompt_engineer` | Prompt Engineer |
| 08 | `video_qa` | Video QA |

`reference_selector` và `reference_qa` là hai chat khác nhau. Selector chọn ứng viên trong năm ảnh; QA kiểm tra độc lập ảnh được chọn và quyết định có đưa nó vào bộ reference hay không.

## Instructions và skills

Dán [project-instructions.txt](project-instructions.txt) vào Project Instructions. Role instruction, JSON schema và artifact đầu vào được worker gửi theo task; không cần dán thủ công các prompt nhiệm vụ vào chat.

Skill `reference_selection` được snapshot tự động vào mỗi task Selector. Ba skill prompt được snapshot tự động vào task Prompt Engineer. Không dùng skill để tự thay đổi approval, queue hay nội dung kịch bản đã khóa.

## Trạng thái vận hành

- Worker có thể kết nối browser context của Account List và đã có session role trong Project.
- Run hiện tại đang tạm dừng vì UI ChatGPT chưa xác nhận media đính kèm trong composer. Đây là blocker transport, không phải kết quả QA.
- Không resume run cho đến khi preflight upload ảnh được kiểm chứng; nếu không agent sẽ nhận metadata thay vì ảnh.

Chi tiết transport và recovery: [CHATGPT-TRANSPORT.md](CHATGPT-TRANSPORT.md).
