"""
"Đợt quét" — đo xem giá quét hết bao nhiêu triệu USD cụm thanh lý thì quay đầu.

Ý tưởng (của bạn, 28/11/2026): giá quét cụm rồi quay đầu. Cần biết theo từng phiên
(Á / Âu / Mỹ) phải quét bao nhiêu tiền thì quay đầu, quay đầu thuận hay ngược xu hướng 4H,
và quay đầu đi được bao xa.

Cách đo:
  • Chia đường giá thành các "nhịp" (zigzag): một nhịp kết thúc khi giá bật ngược ≥ QUAY_DAU %
    từ đỉnh/đáy của nhịp.
  • Trong mỗi nhịp, cộng số triệu USD của các cụm thanh lý (trong muc.json lúc đó) mà giá
    đi xuyên qua: nhịp lên ăn cụm short, nhịp xuống ăn cụm long.
  • Khi nhịp kết thúc (giá đã quay đầu 1%): ghi phiên lúc đạt đỉnh/đáy, tổng tiền đã quét,
    xu hướng 4H lúc đó → quay đầu thuận hay ngược.
  • Khi nhịp quay đầu kết thúc tiếp: ghi thêm cú quay đầu đi được bao xa (%).
Ghi vào nhat_ky_dot_quet.csv. Gửi 1 tin ntfy nhẹ khi phát hiện quay đầu.
"""
import datetime as dt
import os

DIR = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(DIR, "nhat_ky_dot_quet.csv")
VN = dt.timedelta(hours=7)
QUAY_DAU = 1.0            # % bật ngược từ đỉnh/đáy thì tính là quay đầu


def _vn(t):
    return (t + VN).strftime("%H:%M %d/%m")


def _f(x):
    return f"{x:,.0f}".replace(",", ".")


def phien(t):
    """Phiên theo giờ VN lúc giá đạt đỉnh/đáy."""
    h = (t + VN).hour
    if 7 <= h < 14:
        return "Á"
    if 14 <= h < 19:
        return "Âu"
    if h >= 19 or h < 3:
        return "Mỹ"
    return "Ngoài giờ (3h–7h)"


def xu_huong_4h(b4):
    """Luật của bạn: tăng khi chưa thủng đáy 4H gần nhất, giảm khi chưa vượt đỉnh 4H gần nhất.
    Đỉnh/đáy = nến có râu cao/thấp hơn 2 nến mỗi bên. Đổi xu hướng khi nến 4H ĐÓNG vượt đỉnh / thủng đáy."""
    xh, dinh, day = None, None, None
    for i in range(len(b4)):
        if i >= 4:
            j = i - 2
            if all(b4[j]["h"] >= b4[k]["h"] for k in (j - 2, j - 1, j + 1, j + 2)):
                dinh = b4[j]["h"]
            if all(b4[j]["l"] <= b4[k]["l"] for k in (j - 2, j - 1, j + 1, j + 2)):
                day = b4[j]["l"]
        c = b4[i]["c"]
        if dinh is not None and c > dinh:
            xh = "tăng"
        elif day is not None and c < day:
            xh = "giảm"
    return xh or "chưa rõ"


def _cum_tren_duong(cfg, huong, tu, den):
    """Các cụm (giá, triệu) nằm giữa tu và den theo hướng nhịp."""
    if huong == "len":
        muc, tien = cfg.get("cum_short_tren_gia", []), cfg.get("cum_short_tren_trieu", [])
        ds = [(m, t) for m, t in zip(muc, tien) if tu < m <= den]
    else:
        muc, tien = cfg.get("cum_long_duoi_gia", []), cfg.get("cum_long_duoi_trieu", [])
        ds = [(m, t) for m, t in zip(muc, tien) if den <= m < tu]
    return ds


def _them_cum(nhip, cfg, tu, den):
    da = {round(c[0]) for c in nhip["cum"]}
    for m, t in _cum_tren_duong(cfg, nhip["huong"], tu, den):
        if round(m) not in da:
            nhip["cum"].append([round(m), t])
            da.add(round(m))


def _oi_nhip(n, cfg):
    """(% thay đổi OI trong nhịp, ước tính triệu USD vị thế thay đổi trên toàn thị trường)."""
    a, b = n.get("oi_tu"), n.get("oi_cuc")
    if not a or not b or a[1] != b[1] or not a[0]:
        return None, None
    pct = (b[0] / a[0] - 1) * 100
    tong = cfg.get("oi_tong_btc")
    usd = pct / 100 * tong * n["cuc"] / 1e6 if tong else None
    return pct, usd


def _oi_txt(n, cfg):
    pct, usd = _oi_nhip(n, cfg)
    if pct is None:
        return ""
    return f" OI {pct:+.2f}%" + (f" (~{usd:+,.0f}M USD toàn thị trường)" if usd is not None else "") + "."


def ghi_taker(state, data, t_now):
    """Lưu khối lượng mua/bán chủ động 5 phút (OKX, USD) của 48 giờ gần nhất: {ts_ms: [bán, mua]}."""
    tk = state.setdefault("dq_taker", {})
    for x in data:
        tk[str(x[0])] = [float(x[1]), float(x[2])]
    moc = (t_now - dt.timedelta(hours=48) - dt.datetime(1970, 1, 1)).total_seconds() * 1000
    for k in [k for k in tk if int(k) < moc]:
        del tk[k]


def _taker_nhip(state, n):
    """(mua chủ động %, CVD triệu USD) từ đầu nhịp tới đỉnh/đáy — đo lệnh MỚI chủ động đẩy giá."""
    if not n.get("t_tu"):
        return None, None
    a = (dt.datetime.fromisoformat(n["t_tu"]) - dt.datetime(1970, 1, 1)).total_seconds() * 1000
    b = (dt.datetime.fromisoformat(n["t_cuc"]) - dt.datetime(1970, 1, 1)).total_seconds() * 1000 + 15 * 60000
    ban = mua = 0.0
    for k, (s_, m_) in state.get("dq_taker", {}).items():
        if a <= int(k) < b:
            ban += s_
            mua += m_
    if mua + ban <= 0:
        return None, None
    return mua / (mua + ban) * 100, (mua - ban) / 1e6


def _kl_bat_thuong(n, bars):
    """Khối lượng trung bình mỗi nến 15m trong nhịp ÷ trung bình 24 giờ (>2 = bất thường, có thể có tin)."""
    if not n.get("nen_cuc") or not n.get("vol_cuc"):
        return None
    vs = [b.get("v", 0) for b in bars[-96:] if b.get("v")]
    if not vs:
        return None
    return (n["vol_cuc"] / n["nen_cuc"]) / (sum(vs) / len(vs))


def _ghi(n, di_xa, cfg=None):
    moi = not os.path.exists(CSV)
    pct, usd = _oi_nhip(n, cfg or {})
    tong = round(sum(c[1] for c in n["cum"]), 1)
    quay = "xuống" if n["huong"] == "len" else "lên"
    thuan = "chưa rõ" if n["xu_huong"] == "chưa rõ" else \
        ("thuận" if (quay == "lên") == (n["xu_huong"] == "tăng") else "ngược (sóng hồi)")
    with open(CSV, "a", encoding="utf-8") as f:
        if moi:
            f.write("dinh_day_luc_vn,phien,nhip,gia_bat_dau,dinh_day,nhip_dai_pct,so_cum_quet,"
                    "tong_trieu_usd_quet,cac_cum,oi_thay_doi_pct,oi_uoc_tinh_trieu_usd,"
                    "mua_chu_dong_pct,cvd_trieu_usd_okx,kl_so_voi_tb_24h,"
                    "xu_huong_4h,quay_dau,quay_dau_di_xa_pct\n")
        f.write(f"{_vn(dt.datetime.fromisoformat(n['t_cuc']))},{n['phien']},"
                f"{'lên' if n['huong'] == 'len' else 'xuống'},{round(n['tu'])},{round(n['cuc'])},"
                f"{round(abs(n['cuc'] / n['tu'] - 1) * 100, 2)},{len(n['cum'])},{tong},"
                f"\"{' '.join(f'{c[0]}:{c[1]}M' for c in n['cum'])}\","
                f"{'' if pct is None else round(pct, 2)},{'' if usd is None else round(usd)},"
                f"{n.get('mua_pct', '')},{n.get('cvd', '')},{n.get('kl_tl', '')},{n['xu_huong']},"
                f"{quay} {thuan},{di_xa}\n")


def cap_nhat(state, cfg, bars, b4, ntfy, oi=None, nguon_oi=None):
    """bars: nến 15m đã đóng (cũ → mới). b4: nến 4H đã đóng. oi: OI hiện tại (BTC, 1 sàn).
    Gọi mỗi lần bot chạy."""
    oi_mau = [oi, nguon_oi] if oi else None
    moc = state.get("dq_nen")
    moi = [b for b in bars if moc is None or b["t"] > dt.datetime.fromisoformat(moc)]
    if not moi:
        return None
    state["dq_nen"] = bars[-1]["t"].isoformat()
    n = state.get("dq_nhip")
    if n is None:                       # lần đầu: bắt đầu từ nến hiện tại, chưa biết hướng
        b = moi[-1]
        state["dq_nhip"] = {"huong": None, "hi": b["h"], "lo": b["l"],
                            "t_hi": b["t"].isoformat(), "t_lo": b["t"].isoformat()}
        return None
    tin = None
    for b in moi:
        if n["huong"] is None:          # chờ đủ 1% để biết nhịp đầu tiên
            if b["h"] > n["hi"]:
                n["hi"], n["t_hi"] = b["h"], b["t"].isoformat()
            if b["l"] < n["lo"]:
                n["lo"], n["t_lo"] = b["l"], b["t"].isoformat()
            if n["hi"] >= n["lo"] * (1 + QUAY_DAU / 100):
                len_truoc = n["t_lo"] < n["t_hi"]
                n = {"huong": "len" if len_truoc else "xuong",
                     "tu": n["lo"] if len_truoc else n["hi"],
                     "cuc": n["hi"] if len_truoc else n["lo"],
                     "t_cuc": n["t_hi"] if len_truoc else n["t_lo"], "cum": [], "cho_di_xa": None,
                     "oi_tu": None, "oi_cuc": oi_mau,
                     "t_tu": n["t_lo"] if len_truoc else n["t_hi"], "vol": 0.0, "nen": 0}
                _them_cum(n, cfg, n["tu"], n["cuc"])
            continue
        len_ = n["huong"] == "len"
        n["vol"] = n.get("vol", 0.0) + b.get("v", 0.0)
        n["nen"] = n.get("nen", 0) + 1
        cuc_moi = b["h"] if len_ else b["l"]
        if (cuc_moi > n["cuc"]) if len_ else (cuc_moi < n["cuc"]):
            _them_cum(n, cfg, n["cuc"], cuc_moi)
            n["cuc"], n["t_cuc"] = cuc_moi, b["t"].isoformat()
            n["oi_cuc"] = oi_mau
            n["vol_cuc"], n["nen_cuc"] = n["vol"], n["nen"]
        nguoc = b["l"] if len_ else b["h"]
        bat = (n["cuc"] - nguoc) / n["cuc"] * 100 if len_ else (nguoc - n["cuc"]) / n["cuc"] * 100
        if bat >= QUAY_DAU:
            # nhịp kết thúc: giá đã quay đầu
            t_cuc = dt.datetime.fromisoformat(n["t_cuc"])
            n["phien"] = phien(t_cuc)
            n["xu_huong"] = xu_huong_4h([x for x in b4 if x["t"] + dt.timedelta(hours=4) <= t_cuc]
                                        or b4)
            mp, cvd = _taker_nhip(state, n)
            n["mua_pct"] = "" if mp is None else round(mp)
            n["cvd"] = "" if cvd is None else round(cvd)
            tl = _kl_bat_thuong(n, bars)
            n["kl_tl"] = "" if tl is None else round(tl, 1)
            # nhịp trước (đang chờ biết quay đầu đi xa bao nhiêu) → nay đã biết = độ dài nhịp này
            if n.get("cho_di_xa"):
                _ghi(n["cho_di_xa"], round(abs(n["cuc"] / n["tu"] - 1) * 100, 2), cfg)
            truoc = {k: v for k, v in n.items() if k != "cho_di_xa"}
            tong = sum(c[1] for c in n["cum"])
            quay = "XUỐNG" if len_ else "LÊN"
            thuan = "chưa rõ xu hướng" if n["xu_huong"] == "chưa rõ" else \
                ("thuận xu hướng 4H" if (quay == "LÊN") == (n["xu_huong"] == "tăng") else "ngược xu hướng 4H (sóng hồi)")
            oi_txt = _oi_txt(n, cfg)
            if n["mua_pct"] != "":
                oi_txt += f" Mua chủ động {n['mua_pct']}%."
            if n["kl_tl"] != "" and n["kl_tl"] >= 2:
                oi_txt += f" ⚠️ Khối lượng gấp {n['kl_tl']} lần bình thường (có thể có tin)."
            tin = (f"🔄 BTC quay đầu {quay} sau khi quét {tong:.0f}M",
                   f"{'Đỉnh' if len_ else 'Đáy'} {_f(n['cuc'])} lúc {_vn(t_cuc)} (phiên {n['phien']}), "
                   f"nhịp {abs(n['cuc'] / n['tu'] - 1) * 100:.1f}%, quét {len(n['cum'])} cụm.{oi_txt}\n"
                   f"Quay đầu {thuan}. Chỉ ghi nhật ký, chưa phải tín hiệu vào lệnh.")
            n = {"huong": "xuong" if len_ else "len", "tu": n["cuc"], "cuc": nguoc,
                 "t_cuc": b["t"].isoformat(), "cum": [], "cho_di_xa": truoc,
                 "oi_tu": n.get("oi_cuc"), "oi_cuc": oi_mau,
                 "t_tu": n["t_cuc"], "vol": b.get("v", 0.0), "nen": 1,
                 "vol_cuc": b.get("v", 0.0), "nen_cuc": 1}
            _them_cum(n, cfg, n["tu"], n["cuc"])
    state["dq_nhip"] = n
    if tin:
        ntfy(tin[0], tin[1], 2, ["arrows_counterclockwise"])
    return tin
