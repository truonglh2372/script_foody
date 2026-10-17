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
| `AQD_FOODY_CLIP` | `1` = cài CLIP và lọc ảnh như script_google_map | tắt |
| `AQD_FOODY_DELAY` | giây giữa 2 request tới Foody | `2` |
| `AQD_FOODY_MAX_PHOTOS` | ảnh tối đa / quán | `30` |
| `AQD_FOODY_ROOT` | thư mục cài | `~/script_foody`, `C:\script_foody` |

Dừng: tạo file `foody_data/STOP`. Log: `foody_data/fetch.log`.

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

Luật ảnh: chỉ giữ món ăn, đồ uống, menu, không gian, bảng hiệu (`common/photo_filter.py` lấy từ script_google_map). Không cài CLIP thì album “Tổng hợp” bị bỏ.

Lịch sự với Foody: ≥2 s/request + ngẫu nhiên, gặp 429/403/“System Busy” thì chờ 30 s → 10 phút; lỗi 3 lần thì bỏ quán.

## Đẩy lên web (mặc định DRY-RUN)
```bash
.venv/bin/python common/foody_upload.py            # chỉ xem trước, ghi foody_data/_upload_preview.jsonl
.venv/bin/python common/foody_upload.py --apply    # gửi thật
```
Chỉ dùng endpoint import mới: truonglh2372/an_uong_quanh_day#683 (chống trùng + cờ `duplicate` trỏ tới quán bị trùng) và #682 (cột nguồn). Xem quán Foody trên tab admin «Nguồn Foody» (#686). Endpoint chưa có thì `--apply` dừng, không gửi gì. Đăng nhập admin dùng `common/aqd_api.py` như script_google_map (`creds.env` / `.aqd_session`, không commit).

Dữ liệu Foody xoá thủ công từ tab admin «Nguồn Foody» (truonglh2372/an_uong_quanh_day#686).
