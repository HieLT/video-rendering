# Khảo sát tạo ảnh Dola không có watermark hiển thị

Ngày kiểm tra: 2026-10-12 (Asia/Saigon).

## Kết quả thực nghiệm

Đã tạo thành công **một ảnh mới** bằng tài khoản hiện có trong dự án, qua giao diện
Create Images, model **Seedream 5.0 Pro**, ratio **16:9**, không chọn style/template.
Không cần extension để tạo ảnh hoặc lấy bản raw.

Prompt thử: một ấm trà gốm đỏ trên bàn gỗ sáng cạnh cửa sổ, cây xanh bên trái,
ánh sáng buổi sáng, bố cục ngang.

| Trường trong response | File bằng chứng | Kết quả |
| --- | --- | --- |
| `image.image_ori.url` | `diagnostics/image_research/image_ori.png` | Có chữ Dola AI ở góc phải dưới |
| `image.image_ori_raw.url` | `diagnostics/image_research/image_ori_raw.png` | Không có chữ Dola AI |
| `image.image_thumb.url` | `diagnostics/image_research/image_thumb.png` | Bản thumbnail riêng |

Hai ảnh đầy đủ đều có kích thước thực tế **2848 × 1600**. So sánh RGB bằng Pillow:
chỉ khác nhau trong bounding box `(2442, 1422, 2771, 1525)` (tọa độ phải/dưới
exclusive), đúng vùng watermark. Mọi pixel ngoài vùng đó giống hệt nhau.
Kết quả máy đọc được nằm tại `diagnostics/image_research/comparison.json`.

Metadata `image_ori` và `image_ori_raw` khai báo 2732 × 1534, khác kích thước file
thực tế. `placeholder` khai báo 2848 × 1600; `gen_params.ratio` là `89:50`, dù
đã chọn 16:9 ở UI. Khi lưu ảnh nên đọc kích thước thực tế từ file, giữ riêng
`requested_ratio` thay vì buộc ảnh phải khớp tuyệt đối 16/9.

Kết luận chỉ áp dụng cho **watermark nhìn thấy** trong mẫu đã thử. Chưa kiểm tra
watermark vô hình, metadata provenance, mọi tài khoản/model, hoặc ảnh chỉnh sửa.

## Đường dẫn dữ liệu đã xác nhận

Response `POST /im/chain/single`:

```text
downlink_body
  .pull_singe_chain_downlink_body
  .messages[]                         # user_type == 2
  .content_block[]                    # block_type == 2074
  .content.creation_block.creations[] # type == 1
  .image.image_ori_raw.url
```

`message.content` còn có thể chứa cùng các block dưới dạng chuỗi JSON. Chỉ chọn
một biểu diễn hoặc deduplicate theo `creation.id` để không tải trùng.
Mẫu kết quả có `image.status == 2` và một creation. Không được giả định mọi
request chỉ trả một ảnh; cũng không quét toàn response để lấy URL đầu tiên vì
có thể lấy ảnh input, avatar, icon hoặc ảnh của lượt cũ.

Trong mẫu:

- `image_ori`: URL có template `ppe_i_dld_wm_16_marc_b_c`.
- `image_preview`: URL có template `ppe_i_pre_wm_16_marc_b_3`.
- `image_ori_raw`: URL có template `ppe_image_raw_marc_b_3`.

Đây là **URL HTTPS trực tiếp**, không phải Base64 như `main_url` của video.
Mỗi URL có chữ ký riêng. Dùng nguyên URL raw server cung cấp; không thay chuỗi
`wm`, bỏ query hoặc tự ghép URL. Tải về kho local thay vì dùng signed URL làm
đường dẫn bền vững.

## Create Images: các lựa chọn quan sát được

Entry UI có `data-testid="skill_bar_button_3"`.
Ratio button có `data-testid="chat_input_action_image_creation_ratio"`.
Editor dùng `[contenteditable="true"]`; nút gửi là `chat_input_send_button`.

Cấu hình hiện tại đến từ `/alice/slot/action_bar_v3/get_item_conf`.
Item tạo ảnh quan sát được: `1064442771335185`, `action_bar_key=coco_instruction`.
Các template lồng nhau là JSON-encoded strings, cần parse nhiều lớp.

| Lựa chọn | Key | Option ID / option key quan sát được |
| --- | --- | --- |
| Model | `model` | `5` / `Seedream 5.0 Pro`; `3` / `Seedream 4.5` |
| Ratio | `image_creation_ratio` | `1` / `1:1`; `2` / `2:3`; `3` / `3:4`; `4` / `4:3`; `5` / `9:16`; `6` / `16:9` |
| Style | `style` | Portrait, landscape, anime, 3D, cyberpunk, oil painting, watercolor, flat illustration, children's illustration, pixel art, colored pencil, ink wash, ink print |

UI tài khoản thử dùng tiếng Nhật: Seedream 5.0 Pro hiển thị `Seedream 5.0プロ`.
Nhãn mô tả ghi dùng 4 lần credit. Đây là mô tả UI, không phải phép đo số credit
thực tế bị trừ. Cấu hình/model có thể khác giữa tài khoản và thay đổi theo thời gian;
nên đọc cấu hình hoặc xác nhận lựa chọn trên UI thay vì hardcode nhãn dịch.

Đã thử chọn ratio 9:16 ở UI trong một lần khảo sát payload, nhưng request gửi
được chủ động abort tại `/chat/completion`, không tạo ảnh thứ hai. Không giữ được
payload completion sạch cho lần thử này; **chưa xác minh hợp đồng request direct
API cho model/ratio/style**. Option IDs ở trên là cấu hình UI, không đủ để kết luận
cách serialize request generation.

## Liên hệ với code hiện tại

- `extensions/dola30/service-worker-v2.js`, `findImageOriRawUrls()` đã tìm
  `image_ori_raw.url` trong object/chuỗi JSON. Đây là đúng field cho ảnh sạch ở mẫu.
- `dola_client.py`, `_build_image_body()` hiện dùng ability 16 và chèn ratio/style
  vào text, chưa có chọn model như UI. Không coi code cũ này là giao thức đã kiểm chứng.
- `_poll_image()` hiện ưu tiên `image_raw`/`image_ori`, bỏ sót `image_ori_raw`,
  và đọc cấu trúc messages khác response live đã quan sát. Nếu tái sử dụng cần sửa
  cả parser response lẫn cách chọn URL.
- `video_worker.POLL_JS` chỉ nhận creation type 2 và kiểm tra xác nhận video;
  dùng nguyên cho ảnh có thể báo `missing_confirmation` ngay khi ảnh đã hoàn thành.
- `account_connections.http_proxy()` và `browser.launch_account_context()` có thể
  tái sử dụng để giữ routing theo tài khoản.

## Hướng triển khai tính năng

1. Worker ảnh riêng dùng browser/account queue hiện có: vào Create Images,
   chọn model/ratio/style, đọc lại lựa chọn, gửi prompt một lần và lưu conversation ID.
2. Poll conversation bằng parser ảnh riêng, giới hạn assistant message sau lượt gửi.
   Phân biệt ảnh đang tạo, lỗi/rejection/quota và ảnh có raw URL sẵn sàng.
3. Với mỗi creation ảnh, giữ `creation.id`, lấy đúng `image_ori_raw.url`. Nếu thiếu,
   tiếp tục chờ đến timeout hoặc báo `raw_image_unavailable`; không fallback âm thầm
   sang `image_ori` trong chế độ yêu cầu không watermark.
4. Download qua kết nối tài khoản, kiểm tra HTTP/content type và decode được ảnh,
   xác định kích thước/format thật, lưu file local cùng model, ratio yêu cầu và nguồn URL.
5. Tích hợp API/task ảnh và UI chọn model/ratio/style; có thể đưa output vào image
   library làm reference cho video. Tách trạng thái/usage ảnh khỏi quota video.
6. Test parser với content JSON string, nhiều creations, lượt cũ, ảnh tham chiếu,
   thiếu raw, output lỗi và URL hết hạn. Thử thêm Seedream 4.5 và ảnh edit/reference
   trước khi tuyên bố hỗ trợ các nhánh đó.

Hiện tại đây là **kết quả nghiên cứu đã có bằng chứng live**, chưa tích hợp worker,
API hoặc màn hình tạo ảnh vào ứng dụng. Các file chẩn đoán chứa URL có chữ ký và
dữ liệu tài khoản; thư mục `diagnostics/` đã được gitignore.
