# script_foody — lấy dữ liệu quán từ Foody (CHỈ ĐỂ TEST, sẽ xoá sau)

> Dữ liệu và ảnh Foody chỉ dùng để thử nghiệm nội bộ. Ảnh được tải **nguyên bản**:
> script **không** cắt, che hay xoá watermark Foody. Mọi bản ghi đều gắn `source=foody` để lọc trên tab admin «Nguồn Foody» (#686).

## Cài + chạy bằng 1 lệnh
**Mac**, dán vào Terminal:
```bash
curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_foody/main/mac/install.sh | bash
```
**Windows**, dán vào PowerShell:
```powershell
irm https://raw.githubusercontent.com/truonglh2372/script_foody/main/windows/install.ps1 | iex
```
Lệnh tự: tải code mới nhất, tạo `.venv`, cài thư viện, rồi chạy nền lấy **20 quán** (test). Chạy lại lệnh = cập nhật code, tiến độ giữ nguyên.

Biến tuỳ chọn (đặt trước lệnh, ví dụ `AQD_FOODY_LIMIT=0 bash` / `$env:AQD_FOODY_LIMIT=0;`):
| Biến | Ý nghĩa | Mặc định |
|---|---|---|
| `AQD_FOODY_LIMIT` | số quán lấy mỗi lần chạy, `0` = chạy hết (hết danh sách thì nghỉ 10 phút rồi quét lại) | `20` |
| `AQD_FOODY_DELAY` | giây giữa 2 request tới Foody | `2` |
| `AQD_FOODY_MAX_PHOTOS` | ảnh tối đa / quán | `30` |
| `AQD_FOODY_ROOT` | thư mục cài | `~/script_foody`, `C:\script_foody` |

Dừng: tạo file `foody_data/STOP`. Log: `foody_data/fetch.log`.

## Heartbeat cho báo cáo Slack hằng ngày (an_uong_quanh_day#1021)
`common/heartbeat.py` gửi `POST {AQD_BASE}/v1/ingest/heartbeats` để báo cáo Slack 08:00 biết máy này còn cào, đã lấy bao nhiêu quán / ảnh / món và có bị Foody chặn không.
- **Bật:** đặt `AQD_INGEST_TOKEN` trong môi trường hoặc thêm dòng `AQD_INGEST_TOKEN=<token>` vào `<AQD_ROOT>/creds.env` (cùng file chứa tài khoản admin). Token lấy ở Admin → Ingest clients (`/admin/ingest_clients`), mỗi máy một token, không bao giờ được in ra log. Không có token thì heartbeat không làm gì.
- **Gửi gì:** `script_foody`, tên máy (`CRAWLER_HOSTNAME` hoặc hostname), trang đang cào, số quán / ảnh giữ lại / món menu / lỗi / lần bị giới hạn **kể từ heartbeat trước** (kèm `window_seconds`), chỗ trống ổ đĩa, trạng thái (`blocked` khi ổ đầy, `idle` khi hết danh sách hoặc dừng) và ghi chú vấn đề (`rate_limited` khi Foody trả 429/403/503 hoặc «System Busy», `disk_full`).
- **Định dạng:** đúng hợp đồng của bảng `crawler_heartbeats` (web #1024) và endpoint web #1036 (`docs/runbooks/daily-ops-report.md`): `crawler`, `host`, `status` (`running` / `idle` / `blocked` / `error`), `activity`, `range_label`, `window_seconds`, `processed_count`, `photo_count`, `dish_count`, `error_count`, `rate_limited_count`, `disk_free_mb`, `note`. Trả về 201; 422 `invalid_heartbeat` hoặc 409 thì bỏ body đó, không gửi lại.
- Tối đa 1 lần mỗi `AQD_HEARTBEAT_EVERY_MIN` phút (mặc định 15); gửi ngay khi dừng hoặc ổ đầy. Số đếm cộng dồn ở `foody_data/heartbeat_state.json`; gửi lỗi thì gửi lại đúng body + `Idempotency-Key` cũ. Lỗi heartbeat không bao giờ làm dừng việc cào. `AQD_HEARTBEAT=0` để tắt.
- Test offline: `python3 tests/test_heartbeat.py`.

## Lấy những gì
Nguồn: danh sách `https://www.foody.vn/ho-chi-minh/o-dau` (JSON công khai trang này dùng), không đăng nhập, không vượt captcha.
Mỗi quán → `foody_data/<foody_id>/place.json`:
- `source=foody`, `source_id`, `source_url`, `fetched_at`
- tên, địa chỉ, quận, khu vực, toạ độ `lat/lng`, SĐT (`phones`), website, giá min/max
- loại hình (`categories`), ẩm thực (`cuisines`), đối tượng khách
- điểm Foody (thang 10), số bình luận, điểm từng tiêu chí, 2 bình luận mẫu
- giờ mở cửa từng ngày (`opening_hours`) + `opening_periods` `{weekday 1=T2..7=CN, opens_at, closes_at}` (giống script_google_map)
- menu công khai (trang `/thuc-don`, nếu quán có)
- ảnh: album Thực đơn → Món ăn → Không gian → Tổng hợp; mỗi ảnh có `source_id` (id ảnh Foody), `source_url`, `album`, `keep`. Ảnh `keep` lưu ở `photos/`.

Luật ảnh (#855): **không còn model local**. Mọi ảnh của album Thực đơn / Món ăn / Không gian được upload, API tự phân loại (ảnh khó → `unclassified` cho admin; chỉ ảnh menu đi vào flow đọc menu). Album “Tổng hợp” chưa upload vì API import chưa nhận ảnh không có `role`.

Lịch sự với Foody: ≥2 s/request + ngẫu nhiên, gặp 429/403/“System Busy” thì chờ 30 s → 10 phút; lỗi 3 lần thì bỏ quán.

## Đẩy lên web (mặc định DRY-RUN)
```bash
.venv/bin/python common/foody_upload.py            # chỉ xem trước, ghi foody_data/_upload_preview.jsonl
.venv/bin/python common/foody_upload.py --apply    # gửi thật
```
Chỉ dùng endpoint import mới: truonglh2372/an_uong_quanh_day#683 (chống trùng + cờ `duplicate` trỏ tới quán bị trùng) và #682 (cột nguồn). Xem quán Foody trên tab admin «Nguồn Foody» (#686). Endpoint chưa có thì `--apply` dừng, không gửi gì. Đăng nhập admin dùng `common/aqd_api.py` như script_google_map (`creds.env` / `.aqd_session`, không commit).

Dữ liệu Foody xoá thủ công từ tab admin «Nguồn Foody» (truonglh2372/an_uong_quanh_day#686).
