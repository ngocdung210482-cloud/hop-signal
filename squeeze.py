"""
Theo dõi "squeeze" (phe kẹt bị ép đóng lệnh) — CHỈ GHI NHẬT KÝ, KHÔNG GỬI NTFY.

Squeeze lên  (short bị ép): trong ≤3 giờ giá tăng ≥1,5% mà OI (quy ra BTC) giảm.
Squeeze xuống (long bị ép):  trong ≤3 giờ giá giảm ≥1,5% mà OI (quy ra BTC) giảm.

Sau đỉnh (hoặc đáy) của cú squeeze, xem cái nào xảy ra trước:
  • "quay đầu": giá đi ngược lại quá 50% cú squeeze
  • "đi tiếp":  giá vượt đỉnh (hoặc thủng đáy) của cú squeeze
Ghi kết quả trong 4 giờ và 12 giờ vào nhat_ky_squeeze.csv,
và tỷ lệ tổng hợp vào tong_ket_squeeze.txt.

OI tính theo BTC (OI USD của OKX chia cho giá), để giá tăng không làm OI "tăng giả".
"""
import datetime as dt
import os

DIR = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(DIR, "nhat_ky_squeeze.csv")
TONG_KET = os.path.join(DIR, "tong_ket_squeeze.txt")

VN = dt.timedelta(hours=7)
NEN = dt.timedelta(minutes=15)
CUA_SO = dt.timedelta(hours=3)          # cú squeeze diễn ra trong tối đa 3 giờ
GIU_MAU_OI = dt.timedelta(hours=4)
BIEN_DO_MIN = 1.5                      # % giá tối thiểu
OI_GIAM_MIN = -0.3                     # % OI (BTC) tối thiểu phải giảm
CHO_DINH = dt.timedelta(hours=3)       # sau khi phát hiện, đỉnh còn được nâng tiếp trong 3 giờ
MOC_4H = dt.timedelta(hours=4)
MOC_12H = dt.timedelta(hours=12)

TEN = {"len": "Squeeze LÊN (short bị ép)", "xuong": "Squeeze XUỐNG (long bị ép)"}


def _t(s):
    return dt.datetime.fromisoformat(s)


def _vn(t):
    return (t + VN).strftime("%H:%M %d/%m")


def ghi_mau_oi(state, t_now, nguon, oi, gia):
    """Lưu mẫu OI (BTC) mỗi lần chạy (khoảng 5 phút/lần)."""
    if oi is None:
        return
    oi_btc = oi                                       # lay_oi đã quy ra BTC
    # mẫu cũ theo kiểu USD (trước khi sửa) bị bỏ để không lẫn đơn vị
    mau = [m for m in state.get("sq_oi", []) if t_now - _t(m[0]) <= GIU_MAU_OI and len(m) == 4]
    mau.append([t_now.isoformat(), oi_btc, nguon, "btc"])
    state["sq_oi"] = mau


def _oi_luc(mau, t, nguon):
    """Mẫu OI gần thời điểm t nhất (lệch ≤10 phút), cùng nguồn."""
    best = None
    for m in mau:
        if m[2] != nguon:
            continue
        lech = abs((_t(m[0]) - t).total_seconds())
        if lech <= 600 and (best is None or lech < best[0]):
            best = (lech, m[1])
    return None if best is None else best[1]


def phat_hien(state, bars, t_now):
    mau = state.get("sq_oi", [])
    if not mau:
        return
    nguon = mau[-1][2]
    w = [b for b in bars if b["t"] + NEN > t_now - CUA_SO]
    if len(w) < 3:
        return
    ds = state.setdefault("sq_su_kien", [])
    for huong in ("len", "xuong"):
        if huong == "len":
            i0 = min(range(len(w)), key=lambda i: w[i]["l"])
            sau = w[i0 + 1:]
            if not sau:
                continue
            b1 = max(sau, key=lambda b: b["h"])
            goc, dinh = w[i0]["l"], b1["h"]
            bien_do = (dinh / goc - 1) * 100
        else:
            i0 = max(range(len(w)), key=lambda i: w[i]["h"])
            sau = w[i0 + 1:]
            if not sau:
                continue
            b1 = min(sau, key=lambda b: b["l"])
            goc, dinh = w[i0]["h"], b1["l"]
            bien_do = (1 - dinh / goc) * 100
        if bien_do < BIEN_DO_MIN:
            continue
        oi0 = _oi_luc(mau, w[i0]["t"] + NEN, nguon)
        oi1 = _oi_luc(mau, b1["t"] + NEN, nguon)
        if oi0 is None or oi1 is None:
            continue
        oi_pct = (oi1 / oi0 - 1) * 100
        if oi_pct > OI_GIAM_MIN:
            continue
        # cú này đã được ghi rồi (bắt đầu trước đỉnh của sự kiện cùng hướng gần nhất) → bỏ
        cuoi = state.get("sq_dinh_cuoi", {}).get(huong)
        if cuoi and w[i0]["t"] <= _t(cuoi):
            continue
        if any(e["huong"] == huong for e in ds):
            continue
        ds.append({"huong": huong, "goc": goc, "t_goc": w[i0]["t"].isoformat(),
                   "dinh": dinh, "t_dinh": b1["t"].isoformat(), "oi_pct": round(oi_pct, 2),
                   "t_phat_hien": t_now.isoformat(), "ghi_chu": ""})


def _danh_gia(e, bars):
    """Trả về (kết quả, thời điểm) hoặc (None, None) nếu chưa ngã ngũ. Có thể nâng đỉnh."""
    len_ = e["huong"] == "len"
    han_nang = _t(e["t_phat_hien"]) + CHO_DINH
    for b in bars:
        if b["t"] <= _t(e["t_dinh"]):
            continue
        if b["t"] > _t(e["t_dinh"]) + MOC_12H:
            break
        giua = e["dinh"] - 0.5 * (e["dinh"] - e["goc"])
        vuot = b["h"] > e["dinh"] if len_ else b["l"] < e["dinh"]
        hoi = b["l"] <= giua if len_ else b["h"] >= giua
        if vuot and not hoi and b["t"] < han_nang:
            e["dinh"] = b["h"] if len_ else b["l"]       # cú squeeze còn đang chạy → nâng đỉnh
            e["t_dinh"] = b["t"].isoformat()
            continue
        if vuot and hoi:
            return "không rõ (cả hai trong 1 nến)", b["t"]
        if hoi:
            return "quay đầu", b["t"]
        if vuot:
            return "đi tiếp", b["t"]
    return None, None


def _ghi_csv(e, kq, t_kq):
    moi = not os.path.exists(CSV)
    t_dinh = _t(e["t_dinh"])
    gio = None if t_kq is None else (t_kq + NEN - t_dinh).total_seconds() / 3600
    kq4 = kq if gio is not None and gio <= 4 else "chưa rõ"
    kq12 = kq if gio is not None else "đi ngang"
    bien_do = abs(e["dinh"] / e["goc"] - 1) * 100
    with open(CSV, "a", encoding="utf-8") as f:
        if moi:
            f.write("gio_dinh_vn,huong,gia_goc,gia_dinh,bien_do_pct,oi_btc_pct,"
                    "ket_qua_4h,ket_qua_12h,sau_bao_nhieu_gio,ghi_chu\n")
        f.write(f"{_vn(t_dinh)},{e['huong']},{e['goc']:.0f},{e['dinh']:.0f},{bien_do:.2f},"
                f"{e['oi_pct']},{kq4},{kq12},{'' if gio is None else f'{gio:.1f}'},"
                f"\"{e.get('ghi_chu', '')}\"\n")


def _tong_ket():
    if not os.path.exists(CSV):
        return
    with open(CSV, encoding="utf-8") as f:
        dong = [l.rstrip("\n").split(",") for l in f.readlines()[1:] if l.strip()]
    out = [f"Tổng kết squeeze — cập nhật {_vn(dt.datetime.utcnow())} (giờ VN)", ""]
    for huong in ("len", "xuong"):
        d = [x for x in dong if x[1] == huong]
        out.append(f"{TEN[huong]}: {len(d)} lần")
        if not d:
            out.append("")
            continue
        for moc, cot in (("4 giờ", 6), ("12 giờ", 7)):
            qd = sum(1 for x in d if x[cot] == "quay đầu")
            dt_ = sum(1 for x in d if x[cot] == "đi tiếp")
            khac = len(d) - qd - dt_
            out.append(f"  Trong {moc}: quay đầu {qd} ({qd / len(d) * 100:.0f}%), "
                       f"đi tiếp {dt_} ({dt_ / len(d) * 100:.0f}%), chưa rõ/đi ngang {khac}")
        out.append("")
    out.append("Cần khoảng 20–30 lần mỗi hướng thì tỷ lệ mới đáng tin.")
    with open(TONG_KET, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def _them_tay(state, bars):
    """Sự kiện nhập tay trong squeeze_them.json (mỗi sự kiện chỉ nạp 1 lần)."""
    p = os.path.join(DIR, "squeeze_them.json")
    if not os.path.exists(p):
        return
    import json
    with open(p, encoding="utf-8") as f:
        ds_tay = json.load(f)
    da = state.setdefault("sq_da_nap", [])
    for e in ds_tay:
        khoa = e["huong"] + e["t_dinh"]
        if khoa in da:
            continue
        e = dict(e)
        # chỉnh lại giá gốc/đỉnh theo nến thật nếu có dữ liệu
        a, b_ = _t(e["t_goc"]) - dt.timedelta(hours=1), _t(e["t_dinh"]) + dt.timedelta(minutes=30)
        w = [x for x in bars if a <= x["t"] <= b_]
        if len(w) >= 3:
            len_ = e["huong"] == "len"
            i0 = (min if len_ else max)(range(len(w)), key=lambda i: w[i]["l"] if len_ else w[i]["h"])
            sau = w[i0 + 1:] or w[i0:]
            b1 = (max if len_ else min)(sau, key=lambda x: x["h"] if len_ else x["l"])
            e["goc"] = w[i0]["l"] if len_ else w[i0]["h"]
            e["t_goc"] = w[i0]["t"].isoformat()
            e["dinh"] = b1["h"] if len_ else b1["l"]
            e["t_dinh"] = b1["t"].isoformat()
        e.setdefault("t_phat_hien", e["t_dinh"])
        state.setdefault("sq_su_kien", []).append(e)
        da.append(khoa)


def cap_nhat(state, bars, t_now):
    """Gọi mỗi lần bot chạy. bars: nến 15m đã đóng (~25 giờ gần nhất)."""
    _them_tay(state, bars)
    phat_hien(state, bars, t_now)
    con = []
    xong = False
    for e in state.get("sq_su_kien", []):
        kq, t_kq = _danh_gia(e, bars)
        state.setdefault("sq_dinh_cuoi", {})[e["huong"]] = e["t_dinh"]
        het_han = t_now >= _t(e["t_dinh"]) + MOC_12H + NEN
        if kq is not None or het_han:
            _ghi_csv(e, kq or "đi ngang", t_kq)
            xong = True
        else:
            con.append(e)
    state["sq_su_kien"] = con
    if xong or not os.path.exists(TONG_KET):
        _tong_ket()
