# Vận hành ChatGPT web cho Film Studio

## Trạng thái hiện tại

Worker Playwright đã có khả năng kết nối profile Account List được chọn, tạo/khôi phục chat theo role, journal delivery, đọc JSON có `task_id` và làm việc với Image Studio/Dola qua backend. Kết nối hay URL chat không tự là bằng chứng task đã nhận đúng media.

Blocker hiện tại của run `film_c58b98aca1cd44848b89014a97e150b4`: ChatGPT web chưa hiển thị ảnh sau upload vào composer. Do đó worker phải chặn Send cho task QA có ảnh; nếu gửi tiếp, chatbot chỉ đọc metadata và sẽ trả `NOT_INSPECTED` vô ích. Run được tạm dừng cho đến khi xác minh và sửa được attachment UI.

## Quy tắc transport

- Mỗi role dùng một chat bền vững trong Project. Worker không tạo chat mới cho cùng role nếu URL session đã hợp lệ.
- Trước Send, journal `delivery=sending`, task marker và chat URL. Sau restart, đối soát marker thay vì gửi mù.
- Media được xem là đã đính kèm chỉ khi composer xác nhận đủ số tile/attachment cần thiết. `input.files` có dữ liệu nhưng UI không hiện tile không đủ điều kiện gửi.
- Chatbot chỉ nhận task sau khi preflight thành công. Nếu preflight thất bại, task giữ `not_sent`/retry có lỗi transport; không đổi thành FAIL ảnh.
- Không tự đăng nhập, vượt CAPTCHA, vượt quota hay đọc/copy cookie. Worker dùng context của Account List đã mở.
- Kết quả phải có chính xác `task_id` và `output` hợp schema. Response sai được lưu để chẩn đoán, không advance workflow.

## Reference Candidate Selector

Role `reference_selector` có chat riêng và nhận skill snapshot `reference_selection` trước task đầu tiên. Nó chỉ được gửi năm ảnh thực tế sau khi transport preflight xác nhận attachment. Kết quả PASS tạo task Reference QA độc lập; PASS của Selector không phải approval và không ghi ảnh vào bộ reference.

## Kiểm thử

- `test_studio_chatgpt.py`: journal/recovery/correlation.
- `test_studio_chatgpt_dom.py`: DOM fixture, composer và upload preflight.
- `test_studio_workflow.py`: artifact, retry năm ảnh, selector và QA.
- Browser test cục bộ không chứng minh selector ChatGPT web đang hoạt động; chỉ live preflight và một task thật có evidence mới xác minh được điều đó.
