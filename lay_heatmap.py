"""
Tự mở CoinGlass bằng trình duyệt ẩn (Playwright) trên GitHub Actions, đọc cụm thanh lý
(Model 1, Coin, 24 giờ) + tổng OI BTC, ghi vào muc.json. Không cần chụp ảnh.
Chỉ sửa: cum_long_duoi_gia/_trieu, cum_short_tren_gia/_trieu, oi_tong_btc, ngay_cap_nhat, ghi_chu.
Giữ nguyên "vung" (vùng bò/gấu do bạn tự vẽ).
"""
import datetime as dt
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

DIR = os.path.dirname(os.path.abspath(__file__))
MUC = os.path.join(DIR, "muc.json")
CO = os.path.join(DIR, "can_cap_nhat.json")
VN = dt.timedelta(hours=7)


def so(s):
    return float(s.replace(",", ""))


def doc_heatmap(page):
    page.goto("https://www.coinglass.com/LiquidationHeatMap/BTC?mode=coin", timeout=90000)
    try:
        page.get_by_role("button", name="Decline").click(timeout=5000)
    except Exception:  # noqa
        pass
    page.wait_for_function(
        "() => /Below · long liquidations[\\s\\S]*\\d+\\.\\dM/.test(document.body.innerText)", timeout=90000)
    page.wait_for_timeout(3000)
    txt = page.inner_text("body")
    i = txt.index("Key liquidation levels")
    j = txt.index("Market overview", i)
    khoi = txt[i:j]
    tren, duoi = khoi.split("Below · long liquidations")
    pat = re.compile(r"(\d{2},\d{3})\s+([\d.]+)M\s*([+-][\d.]+)%")
    short = [(so(a), float(b)) for a, b, _ in pat.findall(tren)]
    long_ = [(so(a), float(b)) for a, b, _ in pat.findall(duoi)]
    m = re.search(r"Shorts \(above price\)\s*[\d.]+[MB]\s*(\d+)%", txt[j:])
    ty = m.group(1) if m else "?"
    return short, long_, ty


TL_CSV = os.path.join(DIR, "nhat_ky_thanh_ly_thuc.csv")


def _usd(x):
    x = x.replace("$", "").replace(",", "")
    k = {"K": 1e3, "M": 1e6, "B": 1e9}.get(x[-1:], 1)
    return float(x[:-1] if k != 1 else x) * k


def doc_thanh_ly(page):
    """Thanh lý THẬT của BTC (tất cả sàn) 1h/4h/12h/24h, long/short, USD — bảng Total Liquidations."""
    page.goto("https://www.coinglass.com/liquidations", timeout=90000)
    page.wait_for_function("() => /24h Short/.test(document.body.innerText)", timeout=60000)
    tien = r"\$[\d.,]+[KMB]?"
    pat = re.compile(r"\bBTC\s+(?:BTC\s+)?\$[\d.,]+\s+[-+]?[\d.]+%\s+((?:" + tien + r"\s+){7}" + tien + ")")
    for _ in range(20):
        txt = page.inner_text("body")
        k = txt.find("24h Short")
        m = pat.search(txt, k if k >= 0 else 0)
        if m:
            v = [_usd(x) for x in re.findall(tien, m.group(1))]
            return dict(zip(["1h_long", "1h_short", "4h_long", "4h_short", "12h_long", "12h_short",
                             "24h_long", "24h_short"], v))
        page.wait_for_timeout(1500)
    return None


def ghi_thanh_ly(tl, ly_do):
    moi = not os.path.exists(TL_CSV)
    now = dt.datetime.utcnow() + VN
    with open(TL_CSV, "a", encoding="utf-8") as f:
        if moi:
            f.write("luc_vn,ly_do,1h_long_trieu,1h_short_trieu,4h_long_trieu,4h_short_trieu,"
                    "12h_long_trieu,12h_short_trieu,24h_long_trieu,24h_short_trieu\n")
        f.write(f"{now:%H:%M %d/%m},{ly_do},"
                + ",".join(f"{tl[k] / 1e6:.2f}" for k in ["1h_long", "1h_short", "4h_long", "4h_short",
                                                          "12h_long", "12h_short", "24h_long", "24h_short"])
                + "\n")


def doc_oi(page):
    page.goto("https://www.coinglass.com/open-interest/BTC", timeout=90000)
    page.wait_for_function("() => /All\\s+[\\d.]+K BTC/.test(document.body.innerText)", timeout=60000)
    m = re.search(r"All\s+([\d.]+)K BTC", page.inner_text("body"))
    return round(float(m.group(1)) * 1000) if m else None


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
                          viewport={"width": 1280, "height": 1600})
        short, long_, ty = doc_heatmap(page)
        try:
            tl = doc_thanh_ly(page)
        except Exception as e:  # noqa
            print("Không đọc được thanh lý thật:", e)
            tl = None
        try:
            oi = doc_oi(page)
        except Exception as e:  # noqa
            print("Không đọc được OI:", e)
            oi = None
        b.close()
    print("short:", short)
    print("long:", long_)
    print("short %:", ty, "| OI BTC:", oi)
    print("thanh lý thật BTC:", tl)
    if tl:
        ghi_thanh_ly(tl, "có cờ bot (vừa quét cụm)" if os.path.exists(CO) else "định kỳ")
    if len(short) < 2 or len(long_) < 2:
        sys.exit("Đọc được quá ít cụm — không sửa muc.json")
    short.sort(key=lambda x: x[0])
    long_.sort(key=lambda x: -x[0])
    cfg = json.load(open(MUC, encoding="utf-8"))
    cu = (cfg.get("cum_short_tren_gia"), cfg.get("cum_long_duoi_gia"))
    cfg["cum_short_tren_gia"] = [round(x[0]) for x in short]
    cfg["cum_short_tren_trieu"] = [x[1] for x in short]
    cfg["cum_long_duoi_gia"] = [round(x[0]) for x in long_]
    cfg["cum_long_duoi_trieu"] = [x[1] for x in long_]
    if oi:
        cfg["oi_tong_btc"] = oi
    if cu == (cfg["cum_short_tren_gia"], cfg["cum_long_duoi_gia"]) and not os.path.exists(CO):
        print("Cụm không đổi — bỏ qua")
        return
    now = dt.datetime.utcnow() + VN
    cfg["ngay_cap_nhat"] = now.date().isoformat()
    cfg["ghi_chu"] = (f"Cụm lấy từ CoinGlass Model 1, Coin, 24 giờ ({now:%H:%M} ngày {now:%d/%m}, "
                      f"bot tự cập nhật). Short chiếm {ty}%.")
    json.dump(cfg, open(MUC, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if os.path.exists(CO):
        os.remove(CO)
    print("Đã cập nhật muc.json")


if __name__ == "__main__":
    main()
