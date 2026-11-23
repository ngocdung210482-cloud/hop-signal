"""
Bot "Quét thanh lý tại vùng" — BTC, báo qua ntfy.

Mỗi lần chạy (khoảng 5 phút/lần trên GitHub Actions):
  1. Lấy nến 15 phút BTCUSDT và Open Interest (OI) tự động.
  2. Đọc các mức trong muc.json (vùng bò/gấu 4H, cụm thanh lý do bạn gửi ảnh).
  3. Báo khi:
     - giá vào vùng / vùng bị thủng
     - giá quét qua cụm thanh lý rồi nến 15m đóng lại bên trong (quét + rút râu)
     - giá xuyên hẳn qua cụm (không phản ứng)
     - OI giảm mạnh trong 1 giờ (phe kẹt bị xả)
     - ĐỦ ĐIỀU KIỆN: quét cụm sát vùng + OI giảm → kèm cắt lỗ, mục tiêu, khối lượng gợi ý
     - nhắc chụp heatmap mới
Bot KHÔNG tự vào lệnh. Xác nhận nến 5 phút là việc của bạn.

Biến môi trường: NTFY_TOPIC
Tham số: --test  (gửi tin thử + tóm tắt, ghi ket_qua_chay.txt)
"""
import datetime as dt
import json
import os
import sys

import requests

DIR = os.path.dirname(os.path.abspath(__file__))
MUC = os.path.join(DIR, "muc.json")
STATE = os.path.join(DIR, "quet_state.json")
LOG = os.path.join(DIR, "nhat_ky_tin_hieu.csv")
DIAG = os.path.join(DIR, "ket_qua_chay.txt")

VN = dt.timedelta(hours=7)
NEN = dt.timedelta(minutes=15)
SO_NEN_CHO_RUT = 4          # trong 4 nến 15m (1 giờ) phải đóng lại bên trong, quá thì coi là xuyên
COOLDOWN_VUNG = dt.timedelta(hours=4)
COOLDOWN_OI = dt.timedelta(hours=2)
CUA_SO_KET_HOP = dt.timedelta(minutes=60)
UA = {"User-Agent": "quet-signal/1.0"}

diag = []


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def gio_vn(t):
    return (t + VN).strftime("%H:%M %d/%m")


def fmt(x):
    return f"{x:,.0f}".replace(",", ".")


# ═════════════ DỮ LIỆU ═════════════
def _bars_binance(url):
    r = requests.get(url, params={"symbol": "BTCUSDT", "interval": "15m", "limit": 100},
                     timeout=20, headers=UA)
    r.raise_for_status()
    out = []
    for k in r.json():
        out.append({"t": dt.datetime.utcfromtimestamp(k[0] / 1000), "o": float(k[1]),
                    "h": float(k[2]), "l": float(k[3]), "c": float(k[4])})
    return out


def _bars_okx():
    r = requests.get("https://www.okx.com/api/v5/market/candles",
                     params={"instId": "BTC-USDT-SWAP", "bar": "15m", "limit": 100},
                     timeout=20, headers=UA)
    r.raise_for_status()
    d = r.json()["data"]
    out = []
    for k in reversed(d):
        out.append({"t": dt.datetime.utcfromtimestamp(int(k[0]) / 1000), "o": float(k[1]),
                    "h": float(k[2]), "l": float(k[3]), "c": float(k[4])})
    return out


def lay_nen():
    nguon = [
        ("Binance Futures", lambda: _bars_binance("https://fapi.binance.com/fapi/v1/klines")),
        ("OKX Futures", _bars_okx),
        ("Binance Spot", lambda: _bars_binance("https://data-api.binance.vision/api/v3/klines")),
    ]
    for ten, f in nguon:
        try:
            bars = f()
            t_now = now_utc()
            bars = [b for b in bars if b["t"] + NEN <= t_now]   # chỉ nến đã đóng
            if len(bars) >= 10:
                diag.append(f"Nến 15m: lấy từ {ten} — OK ({len(bars)} nến)")
                return ten, bars
            diag.append(f"Nến 15m: {ten} trả về quá ít dữ liệu")
        except Exception as e:  # noqa
            diag.append(f"Nến 15m: {ten} lỗi — {str(e)[:120]}")
    raise RuntimeError("Không lấy được nến từ nguồn nào")


def _oi_binance():
    r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                     params={"symbol": "BTCUSDT", "period": "5m", "limit": 13}, timeout=20, headers=UA)
    r.raise_for_status()
    d = r.json()
    vals = [float(x["sumOpenInterest"]) for x in d]
    return vals[-1], (vals[-1] / vals[0] - 1) * 100


def _oi_okx():
    r = requests.get("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
                     params={"ccy": "BTC", "period": "5m"}, timeout=20, headers=UA)
    r.raise_for_status()
    d = r.json()["data"]          # mới nhất trước: [ts, oi(USD), vol]
    now_v = float(d[0][1])
    hour_v = float(d[12][1])
    return now_v, (now_v / hour_v - 1) * 100


def lay_oi(state):
    """Trả về (nguồn, oi, % thay đổi 1 giờ) hoặc (None, None, None)."""
    for ten, f in (("Binance", _oi_binance), ("OKX", _oi_okx)):
        try:
            oi, ch = f()
            diag.append(f"OI: lấy từ {ten} — OK, 1 giờ thay đổi {ch:+.2f}%")
            return ten, oi, ch
        except Exception as e:  # noqa
            diag.append(f"OI: {ten} lỗi — {str(e)[:120]}")
    # dự phòng: Hyperliquid (chỉ có giá trị hiện tại, tự lưu mẫu để tính 1 giờ)
    try:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"},
                          timeout=20, headers=UA)
        r.raise_for_status()
        meta, ctxs = r.json()
        i = [a["name"] for a in meta["universe"]].index("BTC")
        oi = float(ctxs[i]["openInterest"])
        t = now_utc()
        mau = [m for m in state.get("oi_mau", []) if m[2] == "Hyperliquid"]
        mau.append([t.isoformat(), oi, "Hyperliquid"])
        mau = [m for m in mau if t - dt.datetime.fromisoformat(m[0]) <= dt.timedelta(minutes=90)]
        state["oi_mau"] = mau
        cu = [m for m in mau if t - dt.datetime.fromisoformat(m[0]) >= dt.timedelta(minutes=55)]
        ch = (oi / cu[-1][1] - 1) * 100 if cu else None
        diag.append("OI: lấy từ Hyperliquid (dự phòng) — "
                    + (f"1 giờ thay đổi {ch:+.2f}%" if ch is not None else "đang gom mẫu, cần ~1 giờ"))
        return "Hyperliquid", oi, ch
    except Exception as e:  # noqa
        diag.append(f"OI: Hyperliquid lỗi — {str(e)[:120]}")
    return None, None, None


# ═════════════ NTFY, TRẠNG THÁI, NHẬT KÝ ═════════════
def ntfy(topic, title, message, priority=3, tags=None):
    if not topic:
        print("[ntfy giả lập]", title, "|", message.replace("\n", " / "))
        return
    r = requests.post("https://ntfy.sh/", timeout=20, json={
        "topic": topic, "title": title, "message": message,
        "priority": priority, "tags": tags or []})
    r.raise_for_status()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def ghi_nhat_ky(t, loai, muc, gia, oi_ch, chi_tiet):
    moi = not os.path.exists(LOG)
    with open(LOG, "a", encoding="utf-8") as f:
        if moi:
            f.write("gio_vn,loai,muc,gia,oi_1h_pct,chi_tiet\n")
        oi_s = "" if oi_ch is None else f"{oi_ch:.2f}"
        f.write(f"{gio_vn(t)},{loai},{muc},{gia:.0f},{oi_s},\"{chi_tiet}\"\n")


# ═════════════ LOGIC ═════════════
def vung_gan(cfg, phia, L):
    """Vùng cùng phía (long↔bò, short↔gấu) chứa hoặc sát mức L."""
    tol = cfg.get("cum_gan_vung_phan_tram", 1.0) / 100
    loai = "bo" if phia == "long" else "gau"
    for v in cfg.get("vung", []):
        if v["loai"] != loai:
            continue
        if v["tu"] * (1 - tol) <= L <= v["den"] * (1 + tol):
            return v
    return None


def ke_hoach(cfg, phia, cuc_tri, gia):
    """Gợi ý cắt lỗ, mục tiêu, khối lượng (tài khoản cent: 1 lot, giá chạy 1 USD = 1 USC)."""
    rui_ro = cfg.get("so_du_usc", 0) * cfg.get("rui_ro_phan_tram", 2) / 100
    if phia == "long":
        sl = cuc_tri * 0.999
        dist = gia - sl
        tps = sorted(x for x in cfg.get("cum_short_tren_gia", []) if x > gia)
        tp = tps[0] if tps else None
    else:
        sl = cuc_tri * 1.001
        dist = sl - gia
        tps = sorted((x for x in cfg.get("cum_long_duoi_gia", []) if x < gia), reverse=True)
        tp = tps[0] if tps else None
    if dist <= 0:
        return "Giá đã về sát râu quét, không tính được cắt lỗ."
    lot = rui_ro / dist if rui_ro else 0
    dong = [f"Giá tham khảo: {fmt(gia)}",
            f"Cắt lỗ gợi ý: {fmt(sl)} (cách {fmt(dist)})"]
    if tp:
        rr = abs(tp - gia) / dist
        dong.append(f"Mục tiêu: cụm {fmt(tp)} → {rr:.1f}R" + ("" if rr >= 2 else "  ⚠️ CHƯA ĐẠT 2R → bỏ"))
    else:
        dong.append("Mục tiêu: chưa có cụm phía bên kia trong muc.json")
    if lot:
        dong.append(f"Khối lượng: ~{lot:.2f} lot (rủi ro {fmt(rui_ro)} USC)")
    dong.append("👉 Chờ nến 5m đóng vượt đỉnh/đáy nhỏ rồi mới vào, tính lại theo giá thật.")
    return "\n".join(dong)


def khoi_tao_muc(cfg, state, gia):
    lv = state.setdefault("muc", {})
    canh_bao = []
    keys = set()
    for L in cfg.get("cum_long_duoi_gia", []):
        k = f"long:{L}"
        keys.add(k)
        if k not in lv:
            lv[k] = {"tt": "chua"} if L < gia else {"tt": "sai"}
            if L >= gia:
                canh_bao.append(f"Cụm long {fmt(L)} đang nằm TRÊN giá — bỏ qua, cần cập nhật")
    for L in cfg.get("cum_short_tren_gia", []):
        k = f"short:{L}"
        keys.add(k)
        if k not in lv:
            lv[k] = {"tt": "chua"} if L > gia else {"tt": "sai"}
            if L <= gia:
                canh_bao.append(f"Cụm short {fmt(L)} đang nằm DƯỚI giá — bỏ qua, cần cập nhật")
    for k in list(lv):
        if k not in keys:
            del lv[k]
    vs = state.setdefault("vung", {})
    vkeys = set()
    for v in cfg.get("vung", []):
        k = f"{v['loai']}:{v['tu']}-{v['den']}"
        vkeys.add(k)
        vs.setdefault(k, {"trong": v["tu"] <= gia <= v["den"], "bao": None, "thung": False})
    for k in list(vs):
        if k not in vkeys:
            del vs[k]
    return canh_bao


def xu_ly_nen(cfg, state, b, oi_ch, gui):
    t = b["t"]
    # ── vùng ──
    for v in cfg.get("vung", []):
        k = f"{v['loai']}:{v['tu']}-{v['den']}"
        s = state["vung"][k]
        if s.get("thung"):
            continue
        cham = b["l"] <= v["den"] and b["h"] >= v["tu"]
        if cham and not s["trong"]:
            last = s.get("bao")
            if not last or t - dt.datetime.fromisoformat(last) >= COOLDOWN_VUNG:
                gui("vao_vung", f"📍 BTC vào {v['ten']} {fmt(v['tu'])}–{fmt(v['den'])}",
                    f"Nến 15m lúc {gio_vn(t)}: thấp {fmt(b['l'])}, cao {fmt(b['h'])}, đóng {fmt(b['c'])}.\n"
                    f"Mở biểu đồ chuẩn bị, CHƯA vào lệnh — chờ quét cụm + OI giảm + xác nhận 5m.",
                    3, ["round_pushpin"], v["ten"], b["c"])
                s["bao"] = t.isoformat()
            s["trong"] = True
        elif not cham:
            s["trong"] = False
        thung = (v["loai"] == "bo" and b["c"] < v["tu"] * 0.998) or \
                (v["loai"] == "gau" and b["c"] > v["den"] * 1.002)
        if thung:
            s["thung"] = True
            gui("vung_thung", f"❌ {v['ten']} bị thủng",
                f"Nến 15m đóng {fmt(b['c'])} lúc {gio_vn(t)} — vùng mất tác dụng.\n"
                f"Không đánh vùng này nữa. Gửi biểu đồ mới để cập nhật vùng.", 3, ["x"], v["ten"], b["c"])

    # ── cụm thanh lý ──
    for k, s in state["muc"].items():
        phia, L = k.split(":")
        L = float(L)
        if s["tt"] in ("sai", "da_quet", "thung"):
            continue
        qua = b["l"] < L if phia == "long" else b["h"] > L
        ve = b["c"] >= L if phia == "long" else b["c"] <= L
        cuc = b["l"] if phia == "long" else b["h"]
        if s["tt"] == "chua" and qua:
            if ve:
                _quet(cfg, state, k, phia, L, cuc, b, oi_ch, gui)
            else:
                s.update(tt="duoi", t=t.isoformat(), cuc=cuc, so_nen=1)
        elif s["tt"] == "duoi":
            s["cuc"] = min(s["cuc"], b["l"]) if phia == "long" else max(s["cuc"], b["h"])
            s["so_nen"] += 1
            if ve:
                _quet(cfg, state, k, phia, L, s["cuc"], b, oi_ch, gui)
            elif s["so_nen"] >= SO_NEN_CHO_RUT:
                s["tt"] = "thung"
                gui("xuyen_cum", f"↘️ Giá xuyên qua cụm {phia} {fmt(L)}" if phia == "long"
                    else f"↗️ Giá xuyên qua cụm {phia} {fmt(L)}",
                    f"Đã 1 giờ mà nến 15m chưa đóng lại được bên kia mức (đóng {fmt(b['c'])}).\n"
                    f"Không có phản ứng → KHÔNG vào. Chờ cụm tiếp theo.\n📸 Nên chụp heatmap mới.",
                    3, ["warning"], L, b["c"])


def _quet(cfg, state, k, phia, L, cuc, b, oi_ch, gui):
    k = f"{phia}:{int(L) if L.is_integer() else L}"
    state["muc"][k] = {"tt": "da_quet", "t": b["t"].isoformat(), "cuc": cuc}
    v = vung_gan(cfg, phia, L)
    quet = {"phia": phia, "muc": L, "cuc": cuc, "t": b["t"].isoformat(),
            "vung": v["ten"] if v else None, "da_ket_hop": False}
    state.setdefault("quet_gan", []).append(quet)
    huong = "LONG" if phia == "long" else "SHORT"
    oi_txt = "chưa có số OI" if oi_ch is None else f"OI 1 giờ: {oi_ch:+.2f}%"
    if v and oi_ch is not None and oi_ch <= -cfg.get("oi_xac_nhan_phan_tram", 1.0):
        _du_dieu_kien(cfg, quet, b["c"], oi_ch, gui)
        return
    vung_txt = f"Sát {v['ten']} ✅" if v else "Không nằm sát vùng nào → chỉ tham khảo"
    gui("quet_cum", f"🧹 Quét cụm {phia} {fmt(L)} rồi rút râu ({huong}?)",
        f"Râu chạm {fmt(cuc)}, nến 15m đóng {fmt(b['c'])} lúc {gio_vn(b['t'])}.\n"
        f"{vung_txt}. {oi_txt}.\n"
        + ("Chờ OI giảm thêm để đủ điều kiện, hoặc tự xem OI.\n" if v else "")
        + "📸 Cụm này đã hết — nên chụp heatmap mới.",
        4, ["broom"], L, b["c"])


def _du_dieu_kien(cfg, quet, gia, oi_ch, gui):
    quet["da_ket_hop"] = True
    huong = "LONG" if quet["phia"] == "long" else "SHORT"
    t = dt.datetime.fromisoformat(quet["t"])
    gui("du_dieu_kien", f"🚨 ĐỦ ĐIỀU KIỆN {huong} BTC — mở biểu đồ 5m",
        f"Quét cụm {fmt(quet['muc'])} (râu {fmt(quet['cuc'])}) lúc {gio_vn(t)}, sát {quet['vung']}.\n"
        f"OI 1 giờ: {oi_ch:+.2f}% (phe kẹt đã bị xả).\n"
        + ke_hoach(cfg, quet["phia"], quet["cuc"], gia),
        5, ["rotating_light"], quet["muc"], gia)


def main():
    test = "--test" in sys.argv
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    cfg = load_json(MUC, {})
    state = load_json(STATE, {})
    t_now = now_utc()
    so_tin = [0]

    def gui(loai, title, msg, prio, tags, muc, gia):
        ntfy(topic, title, msg, prio, tags)
        ghi_nhat_ky(t_now, loai, muc, gia, oi_ch, title)
        so_tin[0] += 1

    nguon_nen, bars = lay_nen()
    gia = bars[-1]["c"]
    nguon_oi, oi, oi_ch = lay_oi(state)
    canh_bao = khoi_tao_muc(cfg, state, gia)

    last = state.get("nen_cuoi")
    if last is None:
        moi = []                                   # lần đầu: chỉ lấy mốc, không báo lịch sử
    else:
        last_t = dt.datetime.fromisoformat(last)
        moi = [b for b in bars if b["t"] > last_t]
    for b in moi:
        xu_ly_nen(cfg, state, b, oi_ch, gui)
    state["nen_cuoi"] = bars[-1]["t"].isoformat()

    # ── quét gần đây + OI giảm sau đó → đủ điều kiện ──
    con = []
    for q in state.get("quet_gan", []):
        if t_now - dt.datetime.fromisoformat(q["t"]) > CUA_SO_KET_HOP:
            continue
        con.append(q)
        if not q["da_ket_hop"] and q["vung"] and oi_ch is not None \
                and oi_ch <= -cfg.get("oi_xac_nhan_phan_tram", 1.0):
            _du_dieu_kien(cfg, q, gia, oi_ch, gui)
    state["quet_gan"] = con

    # ── OI xả mạnh ──
    if oi_ch is not None and oi_ch <= -cfg.get("oi_xa_manh_phan_tram", 2.0):
        last_oi = state.get("bao_oi")
        if not last_oi or t_now - dt.datetime.fromisoformat(last_oi) >= COOLDOWN_OI:
            gui("oi_xa", f"📉 OI BTC giảm mạnh {oi_ch:+.2f}% trong 1 giờ",
                f"Giá {fmt(gia)} lúc {gio_vn(t_now)}. Phe kẹt vừa bị cắt hàng loạt.\n"
                f"Nếu giá đang ở vùng/cụm thì mở biểu đồ xem; nếu giữa 2 vùng thì đứng ngoài.",
                3, ["chart_with_downwards_trend"], "OI", gia)
            state["bao_oi"] = t_now.isoformat()

    # ── nhắc chụp heatmap mỗi ngày (sau 8h sáng VN) ──
    vn = t_now + VN
    hom_nay = vn.date().isoformat()
    try:
        cu = dt.date.fromisoformat(cfg.get("ngay_cap_nhat", "2000-01-01"))
    except ValueError:
        cu = dt.date(2000, 1, 1)
    if vn.hour >= 8 and state.get("nhac_heatmap") != hom_nay and (vn.date() - cu).days >= 1:
        ntfy(topic, "📸 Nhắc: chụp heatmap mới",
             f"Các mức đang dùng từ ngày {cu:%d/%m}. Chụp danh sách cụm thanh lý (CoinGlass, "
             f"Model 1 hoặc 2) gửi Claude để cập nhật bot.", 2, ["camera"])
        state["nhac_heatmap"] = hom_nay

    if test:
        dong = [f"Giá BTC: {fmt(gia)} (nguồn {nguon_nen})",
                "OI 1 giờ: " + ("chưa có" if oi_ch is None else f"{oi_ch:+.2f}% (nguồn {nguon_oi})"),
                "", "Vùng:"]
        for v in cfg.get("vung", []):
            s = state["vung"][f"{v['loai']}:{v['tu']}-{v['den']}"]
            dong.append(f"• {v['ten']} {fmt(v['tu'])}–{fmt(v['den'])}"
                        + (" (đã thủng)" if s.get("thung") else ""))
        ten_tt = {"chua": "đang canh", "duoi": "đang ở bên kia, chờ rút", "da_quet": "đã quét",
                  "thung": "đã xuyên", "sai": "sai phía, bỏ qua"}
        dong.append("Cụm đang canh:")
        for k, s in state["muc"].items():
            phia, L = k.split(":")
            dong.append(f"• {phia} {fmt(float(L))}: {ten_tt[s['tt']]}")
        dong += canh_bao
        ntfy(topic, "✅ Bot quét thanh lý đã chạy", "\n".join(dong), 3, ["white_check_mark"])
        with open(DIAG, "w", encoding="utf-8") as f:
            f.write(f"Chạy lúc {gio_vn(t_now)} (giờ VN)\n" + "\n".join(diag) + "\n\n"
                    + "\n".join(dong) + "\n")

    save_json(STATE, state)
    print("\n".join(diag))
    print(f"Giá {gia:.0f} | OI 1h {oi_ch} | số tin đã gửi {so_tin[0]}")


def chay_an_toan():
    """Lỗi thì báo ntfy tối đa 6 giờ/lần, không làm workflow đỏ liên tục."""
    try:
        main()
    except Exception as e:  # noqa
        print("LỖI:", e)
        print("\n".join(diag))
        state = load_json(STATE, {})
        last = state.get("bao_loi")
        t = now_utc()
        if not last or t - dt.datetime.fromisoformat(last) >= dt.timedelta(hours=6):
            try:
                ntfy(os.environ.get("NTFY_TOPIC", "").strip(), "❗ Bot quét thanh lý gặp lỗi",
                     f"{str(e)[:200]}\n" + "\n".join(diag[-4:]), 3, ["x"])
            except Exception:  # noqa
                pass
            state["bao_loi"] = t.isoformat()
            save_json(STATE, state)
        if "--test" in sys.argv:
            with open(DIAG, "w", encoding="utf-8") as f:
                f.write(f"LỖI: {e}\n" + "\n".join(diag) + "\n")


if __name__ == "__main__":
    chay_an_toan()
