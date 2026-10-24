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
| `AQD_FOODY_MAX_PHOTOS` | ảnh tối đa / quán, **chỉ** ảnh thực đơn rồi ảnh món (an_uong_quanh_day#1082) | `5` |
| `AQD_MAX_REVIEWS` | review tối đa / quán, lật trang review đến khi đủ hoặc hết (#1083) | `50` |
| `AQD_FOODY_LIST_RETRIES` / `AQD_FOODY_LIST_BACKOFF` / `AQD_FOODY_LIST_BACKOFF_MAX` | trang danh sách trống: số lần thử lại, giây chờ đầu (gấp đôi mỗi lần), trần (#1080) | `5` / `30` / `600` |
| `AQD_FOODY_LIST_SKIP_STREAK` / `AQD_FOODY_LIST_SKIP_PAUSE` | bỏ qua liền ngần này trang thì nghỉ ngần này giây (#1080) | `5` / `1800` |
| `AQD_FOODY_GALLERY_TRIES` / `AQD_FOODY_GALLERY_BACKOFF` / `AQD_FOODY_GALLERY_BACKOFF_MAX` | album ảnh lỗi: số lần thử lại, giây chờ đầu, trần (#1081) | `5` / `900` / `21600` |
| `AQD_FOODY_GALLERY_RETRY_PER_PAGE` | số quán chờ thử lại ảnh xử lý trước mỗi trang danh sách (#1081) | `3` |
| `AQD_FOODY_ROOT` | thư mục cài | `~/script_foody`, `C:\script_foody` |

Dừng: tạo file `foody_data/STOP`. Log: `foody_data/fetch.log`.

## Heartbeat cho báo cáo Slack hằng ngày (an_uong_quanh_day#1021)
`common/heartbeat.py` gửi `POST {AQD_BASE}/v1/ingest/heartbeats` để báo cáo Slack 08:00 biết máy này còn cào, đã lấy bao nhiêu quán / ảnh / món và có bị Foody chặn không.
- **Bật:** đặt `AQD_INGEST_TOKEN` trong môi trường hoặc thêm dòng `AQD_INGEST_TOKEN=<token>` vào `<AQD_ROOT>/creds.env` (cùng file chứa tài khoản admin). Token lấy ở Admin → Ingest clients (`/admin/ingest_clients`), mỗi máy một token, không bao giờ được in ra log. Không có token thì heartbeat không làm gì.
- **Gửi gì:** `script_foody`, tên máy (`CRAWLER_HOSTNAME` hoặc hostname), trang đang cào, số quán / ảnh giữ lại / món menu / lỗi / lần bị giới hạn **kể từ heartbeat trước** (kèm `window_seconds`), chỗ trống ổ đĩa, trạng thái (`blocked` khi ổ đầy, `idle` khi hết danh sách hoặc dừng) và ghi chú vấn đề (`rate_limited` khi Foody trả 429/403/503 hoặc «System Busy», `disk_full`).
- **Định dạng:** đúng hợp đồng của bảng `crawler_heartbeats` (web #1024) và endpoint web #1036 (`docs/runbooks/daily-ops-report.md`): `crawler`, `host`, `status` (`running` / `idle` / `blocked` / `error`), `activity`, `range_label`, `window_seconds`, `processed_count`, `photo_count`, `dish_count`, `error_count`, `rate_limited_count`, `disk_free_mb`, `note`. Trả về 201; 422 `invalid_heartbeat` hoặc 409 thì bỏ body đó, không gửi lại.
- Tối đa 1 lần mỗi `AQD_HEARTBEAT_EVERY_MIN` phút (mặc định 15); gửi ngay khi dừng hoặc ổ đầy. Số đếm cộng dồn ở `foody_data/heartbeat_state.json`; gửi lỗi thì gửi lại đúng body + `Idempotency-Key` cũ. Lỗi heartbeat không bao giờ làm dừng việc cào. `AQD_HEARTBEAT=0` để tắt.
- Test offline: `python3 tests/test_heartbeat.py`.

## Gọi thẳng server gốc, bỏ qua Cloudflare (an_uong_quanh_day#1058)
Đường Viettel → Cloudflare SG hay mất gói nên gọi API qua Cloudflare chậm. Thêm **một dòng** vào `creds.env` (hoặc biến môi trường) để gọi thẳng server gốc:

```
AQD_ORIGIN_IP=<IP server gốc, chủ dự án đưa>
```

- Giống `curl --resolve`: chỉ việc tra DNS của host trong `AQD_BASE` được thay bằng IP đó. URL, SNI, header `Host` và việc kiểm tra chứng chỉ vẫn dùng tên `anuongquanhday.com`, nên chứng chỉ phải khớp tên (server gốc có sẵn chứng chỉ Let's Encrypt cho tên này). **Không bao giờ tắt kiểm tra TLS**; token / đăng nhập admin không đổi.
- Chỉ pin đúng host của `AQD_BASE`; Google, Foody, CDN ảnh... vẫn tra DNS bình thường.
- `AQD_BASE` phải là `https://...` và `AQD_ORIGIN_IP` phải là đúng một địa chỉ IP, không thì client admin báo lỗi ngay (`OriginPinError`) còn heartbeat tự tắt. Để trống / không có dòng này = như cũ (qua Cloudflare).
- **Bỏ:** xoá dòng đó rồi khởi động lại crawler. Chạy lại installer và chọn ghi đè `creds.env` sẽ mất dòng này (khi đó crawler lại đi qua Cloudflare, vẫn chạy được); thêm lại sau khi cài.
- Kiểm tra trên máy: `curl -s -o /dev/null -w '%{http_code} %{time_total}\n' --resolve anuongquanhday.com:443:<IP> https://anuongquanhday.com/up` phải ra `200`.
- Test offline: `python3 tests/test_origin_pin.py` (server TLS thật ở 127.0.0.1 với CA tạm; phần `requests` cần `pip install -r requirements.txt`).

## Lấy những gì
Nguồn: danh sách `https://www.foody.vn/ho-chi-minh/o-dau` (JSON công khai trang này dùng), không đăng nhập, không vượt captcha.
Mỗi quán → `foody_data/<foody_id>/place.json`:
- `source=foody`, `source_id`, `source_url`, `fetched_at`
- tên, địa chỉ, quận, khu vực, toạ độ `lat/lng`, SĐT (`phones`), website, giá min/max
- loại hình (`categories`), ẩm thực (`cuisines`), đối tượng khách
- điểm Foody (thang 10), số bình luận, điểm từng tiêu chí, tối đa `AQD_MAX_REVIEWS` (50) bình luận mới nhất (khoá `reviews_sample`)
- giờ mở cửa từng ngày (`opening_hours`) + `opening_periods` `{weekday 1=T2..7=CN, opens_at, closes_at}` (giống script_google_map)
- menu công khai (trang `/thuc-don`, nếu quán có)
- ảnh: **chỉ** album Thực đơn rồi Món ăn (đồ ăn/đồ uống), tổng tối đa `AQD_FOODY_MAX_PHOTOS` (5); mỗi ảnh có `source_id` (id ảnh Foody), `source_url`, `album`, `keep`. Lưu ở `photos/`.

Luật ảnh (an_uong_quanh_day#1082, thay luật #855): chỉ lấy ảnh **thực đơn trước**, rồi lấp bằng ảnh **món ăn/đồ uống**, tổng tối đa 5/quán. Không lấy ảnh không gian, người, “Tổng hợp”. Không có model local; API vẫn tự phân loại ảnh đã upload.

Lịch sự với Foody: ≥2 s/request + ngẫu nhiên, gặp 429/403/“System Busy” thì chờ 30 s → 10 phút; lỗi 3 lần thì bỏ quán.

## Đẩy lên web (mặc định DRY-RUN)
```bash
.venv/bin/python common/foody_upload.py            # chỉ xem trước, ghi foody_data/_upload_preview.jsonl
.venv/bin/python common/foody_upload.py --apply    # gửi thật
```
Chỉ dùng endpoint import mới: truonglh2372/an_uong_quanh_day#683 (chống trùng + cờ `duplicate` trỏ tới quán bị trùng) và #682 (cột nguồn). Xem quán Foody trên tab admin «Nguồn Foody» (#686). Endpoint chưa có thì `--apply` dừng, không gửi gì. Đăng nhập admin dùng `common/aqd_api.py` như script_google_map (`creds.env` / `.aqd_session`, không commit).

Dữ liệu Foody xoá thủ công từ tab admin «Nguồn Foody» (truonglh2372/an_uong_quanh_day#686).

## Danh sách trống, album ảnh lỗi, review (an_uong_quanh_day#1080, #1081, #1083)
- **Danh sách (#1080):** trang trống hoặc không có `Total` mà vẫn **dưới** tổng đã biết (`_state.json` `total`) = Foody đang chặn, không phải hết danh sách: thử lại đúng trang đó với backoff, hết lượt thì bỏ qua sang trang sau (ghi `skipped_pages`). Chỉ quay về trang 1 khi đã qua tổng đã biết. Trang đang cào luôn được lưu vào `_state.json`.
- **Album ảnh lỗi (#1081):** album thực đơn/món ăn trả 503/«System Busy»/không phải JSON, hoặc trả rỗng trong khi trang quán nói có ảnh → quán **không** được đánh dấu xong, vào hàng `gallery_retry` của `_state.json` (thử lại sau 15 phút, 30, 60... tối đa 6 giờ). Hết 5 lượt thì sang `gallery_failed`, vẫn chưa xong nên vòng danh sách sau lấy lại.
- **Review (#1083):** lật `/__get/Review/ResLoadMore` (10/lần, mới nhất trước) đến khi đủ `AQD_MAX_REVIEWS` hoặc hết; trang review lỗi thì giữ phần đã đọc, không chặn quán. Pusher gửi review theo lô 20. Không tự lấy lại review cho quán cũ.
- Test offline: `python3 tests/test_list_and_gallery.py`.

## Lấy lại ảnh cho quán đã lên với 0 ảnh (an_uong_quanh_day#1081) — CHUẨN BỊ SẴN, CHƯA CHẠY
Trước #1081, quán có album ảnh lỗi vẫn bị đánh dấu xong và lên server **không có ảnh**. `scripts/replay_missing_photos.py` đưa các quán đó vào hàng `gallery_retry` để chính crawler lấy lại (đúng luật #1082: thực đơn trước, rồi món, tối đa 5) và pusher chỉ gửi ảnh còn thiếu vào **nhà hàng đã có** (không tạo quán mới).

Danh sách id (ảnh chụp lúc 2026-10-24 20:48 ICT, tab: `foody_id restaurant_id ảnh_thực_đơn ảnh_món tổng_ảnh`):
| File | Máy | Số quán |
|---|---|---|
| `scripts/replay_ids/foody_mac.tsv` | Mac (`~/script_foody/foody_data`) | 353 |
| `scripts/replay_ids/foody_admin.tsv` | Admin (`C:\script_foody\foody_data`) | 336 |

(Không tính 167 + 128 quán chỉ có ảnh không gian/“Tổng hợp”: theo luật #1082 không lấy nữa.)

Chạy trên **đúng máy** của danh sách, bằng python của `.venv`:
```bash
# 1. (tuỳ chọn) dựng lại danh sách từ dữ liệu hiện tại — chỉ đọc
python scripts/replay_missing_photos.py list --out scripts/replay_ids/foody_mac.tsv
# 2. chạy thử (mặc định): in ra sẽ xếp hàng bao nhiêu quán, không ghi gì
python scripts/replay_missing_photos.py enqueue --ids-file scripts/replay_ids/foody_mac.tsv
# 3. thật: DỪNG crawler trước (tạo foody_data/STOP, chờ tiến trình thoát, xoá STOP), rồi
python scripts/replay_missing_photos.py enqueue --ids-file scripts/replay_ids/foody_mac.tsv --apply [--limit 50]
# 4. khởi động lại crawler như thường; mỗi trang danh sách nó xử lý AQD_FOODY_GALLERY_RETRY_PER_PAGE quán trong hàng
```
`--apply` từ chối nếu `foody_fetch.py` đang chạy hoặc `_state.json` vừa đổi trong 120 giây, và lưu bản sao `_state.json.pre-replay-<giờ>` trước khi ghi. Hoàn tác: dừng crawler, chép bản sao đó đè lại `_state.json`.
Test offline: `python3 tests/test_replay_missing_photos.py`.
