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
| `AQD_FOODY_LIST_TIMEOUT_SKIP` / `AQD_FOODY_TIMEOUT_BACKOFF` | trang danh sách bị ConnectTimeout liên tiếp ngần này lần -> bỏ qua trang, ghi log + báo Slack (không crash); giây chờ giữa các lần timeout (danh sách, giá trị cuối lặp lại) | `6` / `60,120,300,600` |
| `AQD_FOODY_LIST_SKIP_STREAK` / `AQD_FOODY_LIST_SKIP_PAUSE` | bỏ qua liền ngần này trang thì nghỉ ngần này giây (#1080) | `5` / `1800` |
| `AQD_FOODY_GALLERY_TRIES` / `AQD_FOODY_GALLERY_BACKOFF` / `AQD_FOODY_GALLERY_BACKOFF_MAX` | album ảnh lỗi: số lần thử lại, giây chờ đầu, trần (#1081) | `5` / `900` / `21600` |
| `AQD_FOODY_GALLERY_RETRY_PER_PAGE` | số quán chờ thử lại ảnh xử lý trước mỗi trang danh sách (#1081) | `3` |
| `AQD_FOODY_ROOT` | thư mục cài | `~/script_foody`, `C:\script_foody` |
| `AQD_FOODY_CITIES` | tỉnh/thành để cào: `all` (mặc định) hoặc danh sách slug/id cách nhau bởi dấu phẩy (`ha-noi,da-nang`) | `all` |
| `AQD_FOODY_SKIP_CITIES` | slug/id bỏ qua trong lượt này (giữ nguyên bucket state, ví dụ `ho-chi-minh` khi HCM đã xong) | (trống) |
| `AQD_FOODY_SINGLE_CITY` | `1` = chỉ cào `AQD_FOODY_CITY` (giữ hành vi cũ một tỉnh) | tắt |
| `AQD_FOODY_VERIFY_CITIES` | `1` = probe Foody để bỏ slug chết, ghi `foody_data/_cities.json` | tắt |

Lát (district×cate) lấy `districtId` từ `/__get/Common/GetDistrictsByProvinceId` theo province (id toàn cục, không phải 1..N mỗi tỉnh). `AQD_FOODY_SKIP_CITIES` / `--skip-cities` bỏ qua tỉnh đã xong mà không xoá state.

Tiến độ multi-city nằm trong `_state.json` (`schema: 2`, bucket theo từng city). State HCM cũ được nâng cấp tự động, không mất `done`. Import 422 vĩnh viễn (`opening_periods_invalid`, `import_coordinates_out_of_range`, …) ghi vào `foody_data/_bad_import.json` và không POST lại.


Dừng: tạo file `foody_data/STOP`. Log: `foody_data/fetch.log`.

## Báo cáo Slack trực tiếp (an_uong_quanh_day#1388)
`common/slack_notify.py` gửi thẳng lên **một kênh Slack chung** qua Incoming Webhook (không qua server; heartbeat `/v1/ingest/heartbeats` đã bỏ):
- **Mỗi 4 giờ** (`AQD_REPORT_EVERY_H`): trạng thái 🟢 chạy / 🟡 kẹt / 🔴 lỗi / ⚫ dừng, vị trí (`foody page 533, parity even`), số liệu 4 giờ qua (quán, ảnh, review, upload ok/fail), lần thành công cuối, lỗi gần nhất + việc cần làm, giờ gửi báo cáo (UTC+7).
- **Bắt đầu / dừng** bình thường (file STOP, hết `--limit`, Ctrl+C).
- **Cảnh báo ngay** (🔴, nhắc `SLACK_ALERT_MENTION`): `crash` (exception thoát process), `disk_low` (ổ < `AQD_DISK_MIN_GB`, mặc định 5 GB), `foody_empty_pages` (`AQD_EMPTY_PAGES_ALERT`=10 trang list liên tiếp bị bỏ vì trống), `captcha`, `signin_required`.
- **Kẹt**: process vẫn chạy nhưng 0 quán/ảnh/review mới trong `AQD_STUCK_HOURS` (mặc định 3) giờ → 🟡.
- **Chống spam**: tối đa 1 cảnh báo / crawler / máy / mã lỗi / `AQD_ALERT_EVERY_H` (4) giờ, lưu ở `foody_data/slack_state.json` (giữ qua khởi động lại).
- Gửi lỗi (mạng, timeout, HTTP) chỉ ghi 1 dòng log `[slack] gửi Slack lỗi: <loại lỗi>` (không có URL) và **không làm dừng crawler**.

### Cấu hình webhook (mỗi máy, KHÔNG commit)
Cách khuyên dùng — file secret ngoài repo (Task Scheduler / launchd đọc được, không cần biến môi trường):
- Mac: `~/.config/aqd/slack_webhook` — `mkdir -p ~/.config/aqd && chmod 700 ~/.config/aqd`, tạo file rồi `chmod 600 ~/.config/aqd/slack_webhook`.
- Windows: `%APPDATA%\aqd\slack_webhook` (chỉ user hiện tại đọc được).

Nội dung (dòng `#` là ghi chú; chỉ một dòng URL cũng được):
```
SLACK_CRAWLER_WEBHOOK_URL=https://hooks.slack.com/services/...
AQD_HOST_LABEL=Admin
SLACK_ALERT_MENTION=U0123456789
```
- Biến môi trường cùng tên **thắng** file. `SLACK_CRAWLER_WEBHOOK_FILE` đổi đường dẫn file.
- `AQD_HOST_LABEL`: tên máy hiện trong tin (Admin / Mac mini / TRUONGLE); trống → hostname.
- `SLACK_ALERT_MENTION`: Slack user id (`U…`/`W…`) được nhắc ở tin 🔴; trống → không nhắc. Không bao giờ ghi cứng trong code.
- Thiếu webhook → log 1 dòng cảnh báo, crawler vẫn chạy, không gửi gì. `AQD_SLACK=0` để tắt hẳn.
- Không bao giờ in URL ra log; `slack_webhook*` có trong `.gitignore` phòng khi lỡ đặt trong repo.

### Watchdog (máy tắt / process chết)
Crawler chết thì không tự báo được; người xem thấy qua "Báo cáo lúc …" (quá 4h không có tin = có vấn đề). Watchdog của hệ điều hành chạy `common/watchdog.py` mỗi 15 phút: không có `foody_data/STOP` mà không thấy process `foody_fetch.py` → chạy lại crawler rồi báo Slack ⚫→🟢 (đã khởi động lại) hoặc ⚫ + nhắc (không khởi động lại được); khởi động lại >3 lần/ngày → 🔴. Log: `foody_data/watchdog.log`.
- Windows (Task Scheduler, cần đã cài chế độ chạy hết → có `Startup\aqd_foody.cmd`): `powershell -ExecutionPolicy Bypass -File windows\install_watchdog.ps1` · gỡ: thêm `-Uninstall`.
- Mac (launchd `aqd.watchdog.foody`, cần đã có `aqd.foody`): `sh mac/install_watchdog.sh` · gỡ: `sh mac/install_watchdog.sh --uninstall`.
- Dừng có chủ đích: tạo `foody_data/STOP` trước (watchdog thấy STOP thì không khởi động lại).

Test offline (chỉ server Slack giả ở 127.0.0.1): `python3 tests/test_slack_notify.py`.

### Tự dọn ổ (sau cảnh báo disk_low 04/11/2026)
Crawler không có giờ nghỉ cố định, nên job dọn chạy theo lịch: launchd `aqd.cleanup.foody` / Task Scheduler `aqd_cleanup_foody` gọi `common/disk_cleanup.py` mỗi 15 phút, script tự quyết:
- dọn định kỳ mỗi `AQD_CLEANUP_EVERY_H` giờ (mặc định 6), **hoặc** ngay khi ổ còn < `AQD_CLEANUP_MIN_FREE_GB` (mặc định 8 GB, cao hơn ngưỡng tự tạm dừng của crawler) — tối đa 1 lần/30 phút, giữ cache ít ngày hơn.
- Không dừng crawler và không đụng file STOP/STOP_ALL: chỉ xoá file crawler không còn dùng tới. Khi ổ dưới ngưỡng, crawler đã tự tạm dừng sẵn và tự chạy lại khi có chỗ.
- Chỉ xoá thứ tạo lại được: ảnh còn sót trong `foody_data/<fid>/photos/` của quán đã có trong `_cleaned.json` (server đã xác nhận `succeeded`); log `.log/.out/.err` không ghi thêm trong `AQD_CLEANUP_LOG_DAYS` (14) ngày → cắt về rỗng (log xoay `*.log.N` → xoá); log đang ghi > `AQD_CLEANUP_LOG_MAX_MB` (50) → giữ `AQD_CLEANUP_LOG_KEEP_MB` (5) MB cuối; `*.tmp/*.part` > 1 ngày.
- **Không bao giờ** xoá: json/jsonl dữ liệu, dữ liệu chưa đồng bộ, `crawler.env`, `creds.env`, `*.env`, file secret, `chrome-profile`/profile đăng nhập, venv, `.git`, `bak/`; không theo symlink ra ngoài root.
- Mỗi lần dọn thật: 1 dòng Slack `🧹 *foody @ <máy>* — tự dọn ổ (định kỳ): giải phóng 3,2 GB (1.234 file) · còn trống 11,5 GB`. Log: `foody_data/cleanup.log`.
- Cài: `sh mac/install_cleanup.sh` (macOS) · `windows\install_cleanup.ps1` (Windows); gỡ: `--uninstall` / `-Uninstall`. Xem trước, không xoá: `--dry-run` / `-DryRun`. Bộ cài 1 lệnh (`install_crawlers`) tự cài job này.
- Dọn 0 file mà ổ thấp → 1 cảnh báo / `AQD_CLEANUP_ALERT_H` (6) giờ kèm thứ đang chiếm ổ: `docker system df`, thư mục lớn nhất (`common/host_usage.py`, đo bằng `os.scandir`, **chạy được trên Windows**, không cần `du`; giới hạn `AQD_SPACE_SCAN_SECONDS`=90 s, `AQD_SPACE_SCAN_MAX_ENTRIES`=2.000.000 mục/thư mục, đo dở ghi `>=`), `pagefile.sys`/`hiberfil.sys`/`swapfile.sys` và process ngốn RAM nhất (`tasklist` / `ps`). Xem tay: `python common/host_usage.py C:\script_foody`.
- Test offline: `python3 tests/test_disk_cleanup.py` (có dry-run, vùng cấm, symlink, lịch, khoá, dòng Slack, installer launchd với `launchctl` giả).

## Gọi thẳng server gốc, bỏ qua Cloudflare (an_uong_quanh_day#1058)
Đường Viettel → Cloudflare SG hay mất gói nên gọi API qua Cloudflare chậm. Thêm **một dòng** vào `creds.env` (hoặc biến môi trường) để gọi thẳng server gốc:

```
AQD_ORIGIN_IP=<IP server gốc, chủ dự án đưa>
```

- Giống `curl --resolve`: chỉ việc tra DNS của host trong `AQD_BASE` được thay bằng IP đó. URL, SNI, header `Host` và việc kiểm tra chứng chỉ vẫn dùng tên `anuongquanhday.com`, nên chứng chỉ phải khớp tên (server gốc có sẵn chứng chỉ Let's Encrypt cho tên này). **Không bao giờ tắt kiểm tra TLS**; token / đăng nhập admin không đổi.
- Chỉ pin đúng host của `AQD_BASE`; Google, Foody, CDN ảnh... vẫn tra DNS bình thường.
- `AQD_BASE` phải là `https://...` và `AQD_ORIGIN_IP` phải là đúng một địa chỉ IP, không thì client admin báo lỗi ngay (`OriginPinError`). Để trống / không có dòng này = như cũ (qua Cloudflare).
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

## Toạ độ sai từ Foody (script_foody#18, an_uong_quanh_day#1452)
Dữ liệu trang Foody của một số quán sai (0,0; (1,2); lat = lng; mất số «1» đầu của vĩ độ như 0.7556 thay vì 10.7556). Crawler **không gửi** điểm nằm ngoài Việt Nam (lat 6–24, lng 101.5–118, cùng hộp với server): ghi vào `foody_data/_bad_coords.json` (kèm lý do, lat/lng, URL), bỏ qua khi quét backlog, và khi fetch lại mà điểm đã đúng thì tự gửi. Server trả 422 `import_coordinates_out_of_range` cũng được ghi vào file đó, không thử lại, không tính là lỗi.
Sửa quán đã lên server: `python scripts/repair_coordinates.py --ids-file scripts/replay_ids/foody_bad_coords_1452.tsv` (dry-run), thêm `--apply` để PATCH các dòng Foody đã trả điểm đúng. Dòng `[manual]` = Foody vẫn sai → đặt ghim tay trong admin.
Test: `python3 tests/test_coordinates.py`, `python3 tests/test_repair_coordinates.py`.
