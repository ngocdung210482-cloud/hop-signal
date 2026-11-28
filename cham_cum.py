"""
"Chạm cụm" — mỗi lần giá chạm một cụm thanh lý, đo:
  • MAE: giá đi QUÁ cụm tối đa bao nhiêu % (để chọn cắt lỗ)
  • MFE: giá quay đầu tối đa bao nhiêu % (để chọn chốt lời)
  • Với từng mức chốt lời 0,5 / 1 / 1,5 / 2 / 3 %: trước khi chạm mức đó, giá đã đi ngược bao nhiêu %
    → sau này chọn cặp SL/TP bất kỳ là tính được thắng/thua:
       thắng nếu  mae_truoc_tp_X < SL   (và cột đó có số, tức là đã chạm TP).
Giả định lệnh limit đặt ĐÚNG giá cụm. Theo dõi 12 giờ rồi ghi nhat_ky_cham_cum.csv.
Nến 15m: nếu trong cùng 1 nến vừa chạm TP vừa đi ngược, tính theo hướng bất lợi (thận trọng).
Chỉ ghi nhật ký, không gửi ntfy.
"""
import datetime as dt
import os

import dot_quet

DIR = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(DIR, "nhat_ky_cham_cum.csv")
VN = dt.timedelta(hours=7)
THEO_DOI = dt.timedelta(hours=12)
MUC_TP = (0.5, 1.0, 1.5, 2.0, 3.0)


def _vn(t):
    return (t + VN).strftime("%H:%M %d/%m")


def _ghi(c):
    moi = not os.path.exists(CSV)
    with open(CSV, "a", encoding="utf-8") as f:
        if moi:
            f.write("cham_luc_vn,phien,phia,cum,trieu_usd,xu_huong_4h,thuan_nguoc,kl_so_voi_tb_24h,"
                    "mae_pct,mfe_pct,"
                    + ",".join(f"mae_truoc_tp_{x}" for x in MUC_TP) + "\n")
        f.write(f"{_vn(dt.datetime.fromisoformat(c['t']))},{c['phien']},{c['phia']},{c['muc']},{c['trieu']},"
                f"{c['xu_huong']},{c['thuan']},{c['kl_tl']},{round(c['mae'], 2)},{round(c['mfe'], 2)},"
                + ",".join("" if c["tp"][str(x)] is None else str(round(c["tp"][str(x)], 2)) for x in MUC_TP)
                + "\n")


def _cap_nhat_mot(c, b):
    """Cập nhật MAE/MFE với 1 nến. Lệnh long tại cụm long (giá rơi xuống), short tại cụm short."""
    L = c["muc"]
    long_ = c["phia"] == "long"
    nguoc = (L - b["l"]) / L * 100 if long_ else (b["h"] - L) / L * 100
    thuan = (b["h"] - L) / L * 100 if long_ else (L - b["l"]) / L * 100
    c["mae"] = max(c["mae"], nguoc)               # thận trọng: tính phần đi ngược của nến trước
    for x in MUC_TP:
        if c["tp"][str(x)] is None and thuan >= x:
            c["tp"][str(x)] = c["mae"]
    c["mfe"] = max(c["mfe"], thuan)


def cap_nhat(state, cfg, bars, b4):
    """bars: nến 15m đã đóng (cũ → mới). Gọi mỗi lần bot chạy."""
    moc = state.get("cc_nen")
    state["cc_nen"] = bars[-1]["t"].isoformat()
    if moc is None:
        return
    moc = dt.datetime.fromisoformat(moc)
    vs = [b.get("v", 0) for b in bars[-96:] if b.get("v")]
    tb = sum(vs) / len(vs) if vs else None
    dang = state.setdefault("cc_dang", [])
    da = state.setdefault("cc_da", {})
    for i, b in enumerate(bars):
        if b["t"] <= moc or i == 0:
            continue
        truoc = bars[i - 1]["c"]
        # theo dõi các lần chạm đang mở
        for c in dang:
            if b["t"] > dt.datetime.fromisoformat(c["t"]):
                _cap_nhat_mot(c, b)
        # lần chạm mới
        for phia, k in (("long", "cum_long_duoi_gia"), ("short", "cum_short_tren_gia")):
            tien = cfg.get(k.replace("gia", "trieu"), [])
            for j, L in enumerate(cfg.get(k, [])):
                dung_phia = truoc > L if phia == "long" else truoc < L
                cham = b["l"] <= L if phia == "long" else b["h"] >= L
                khoa = f"{phia}:{L}"
                if not (dung_phia and cham):
                    continue
                if khoa in da and b["t"] - dt.datetime.fromisoformat(da[khoa]) < THEO_DOI:
                    continue
                da[khoa] = b["t"].isoformat()
                xh = dot_quet.xu_huong_4h([x for x in b4 if x["t"] + dt.timedelta(hours=4) <= b["t"]] or b4)
                thuan = "chưa rõ" if xh == "chưa rõ" else \
                    ("thuận" if (phia == "long") == (xh == "tăng") else "ngược")
                c = {"phia": phia, "muc": L, "trieu": tien[j] if j < len(tien) else "",
                     "t": b["t"].isoformat(), "phien": dot_quet.phien(b["t"]), "xu_huong": xh,
                     "thuan": thuan, "kl_tl": round(b.get("v", 0) / tb, 1) if tb and b.get("v") else "",
                     "mae": 0.0, "mfe": 0.0, "tp": {str(x): None for x in MUC_TP}}
                # nến chạm: phần đi ngược tính đủ, phần quay đầu chỉ tính tới giá đóng (thận trọng)
                nguoc = (L - b["l"]) / L * 100 if phia == "long" else (b["h"] - L) / L * 100
                c["mae"] = max(0.0, nguoc)
                thuan_dong = (b["c"] - L) / L * 100 if phia == "long" else (L - b["c"]) / L * 100
                c["mfe"] = max(0.0, thuan_dong)
                for x in MUC_TP:
                    if thuan_dong >= x:
                        c["tp"][str(x)] = c["mae"]
                dang.append(c)
    # kết thúc các lần chạm đã đủ 12 giờ
    con = []
    for c in dang:
        if bars[-1]["t"] - dt.datetime.fromisoformat(c["t"]) >= THEO_DOI:
            _ghi(c)
        else:
            con.append(c)
    state["cc_dang"] = con
    for k in [k for k, v in da.items() if bars[-1]["t"] - dt.datetime.fromisoformat(v) > dt.timedelta(hours=48)]:
        del da[k]
