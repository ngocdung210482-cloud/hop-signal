"""
"Kế hoạch chờ" — lập kế hoạch TRƯỚC cú quét, từ heatmap đang dùng.

Ý tưởng: giá thường quét hết một DẢI CỤM DÀY rồi dừng ở mép xa của dải,
vì sau đó là khoảng trống (không còn thanh khoản để ăn).

Mỗi phía (short phía trên / long phía dưới):
  • Dải dày: bắt đầu từ cụm gần giá nhất, nối tiếp các cụm cách nhau ≤ KHOANG_NOI %
    và đủ lớn (≥ TY_LE_DU_LON × cụm lớn nhất trong dải).
  • Điểm vào: cụm ở mép xa của dải dày.
  • Cắt lỗ: qua cụm kế tiếp sau dải (nếu cách mép ≤ 1,5%) thêm 0,1%; không có thì mép + 0,6%.
  • Chốt lời: cụm gần giá nhất ở phía đối diện, lùi 0,15% về phía giá.
  Chỉ lập kế hoạch nếu R ≥ R_TOI_THIEU.

Bot gửi ntfy khi lập kế hoạch và khi giá chạm điểm vào; ghi kết quả vào nhat_ky_ke_hoach.csv.
Bot KHÔNG tự vào lệnh.
"""
import datetime as dt
import os

DIR = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(DIR, "nhat_ky_ke_hoach.csv")

VN = dt.timedelta(hours=7)
KHOANG_NOI = 0.7          # % khoảng cách tối đa giữa 2 cụm liền nhau trong một dải
TY_LE_DU_LON = 0.5        # cụm sau phải ≥ 50% cụm lớn nhất của dải mới tính vào dải
SL_XA_TOI_DA = 1.5        # % — cụm kế tiếp sau dải xa hơn mức này thì không dùng làm mốc SL
SL_DEM = 0.1              # % đệm thêm sau mốc SL
SL_MAC_DINH = 0.6         # % khi không có cụm kế tiếp
TP_LUI = 0.15             # % lùi TP về phía giá (chốt trước cụm)
R_TOI_THIEU = 1.5
HET_HAN_CHO = dt.timedelta(hours=24)
HET_HAN_GIU = dt.timedelta(hours=48)


def _t(s):
    return dt.datetime.fromisoformat(s)


def _vn(t):
    return (t + VN).strftime("%H:%M %d/%m")


def _f(x):
    return f"{x:,.0f}".replace(",", ".")


def _cum(cfg, phia):
    """Danh sách (giá, triệu USD) của một phía, gần giá trước."""
    k = "cum_short_tren_gia" if phia == "short" else "cum_long_duoi_gia"
    muc = cfg.get(k, [])
    tien = cfg.get(k.replace("gia", "trieu"), [])
    if len(tien) != len(muc):
        return None
    ds = list(zip(muc, tien))
    return sorted(ds, key=lambda x: x[0], reverse=(phia == "long"))


def lap(cfg, gia):
    """Trả về danh sách kế hoạch (tối đa 2), hoặc [] nếu thiếu dữ liệu số tiền."""
    out = []
    for phia in ("short", "long"):
        ds = _cum(cfg, phia)
        doi = _cum(cfg, "long" if phia == "short" else "short")
        if not ds or not doi:
            continue
        ds = [x for x in ds if (x[0] > gia if phia == "short" else x[0] < gia)]
        doi = [x for x in doi if (x[0] < gia if phia == "short" else x[0] > gia)]
        if not ds or not doi:
            continue
        dai = [ds[0]]
        for c in ds[1:]:
            khoang = abs(c[0] / dai[-1][0] - 1) * 100
            if khoang <= KHOANG_NOI and c[1] >= TY_LE_DU_LON * max(x[1] for x in dai):
                dai.append(c)
            else:
                break
        vao = dai[-1][0]
        sau = ds[len(dai)] if len(ds) > len(dai) else None
        dau = 1 if phia == "short" else -1
        if sau and abs(sau[0] / vao - 1) * 100 <= SL_XA_TOI_DA:
            sl = sau[0] * (1 + dau * SL_DEM / 100)
        else:
            sl = vao * (1 + dau * SL_MAC_DINH / 100)
        tp = doi[0][0] * (1 + dau * TP_LUI / 100)
        rui_ro, loi = abs(sl - vao), abs(vao - tp)
        if rui_ro <= 0:
            continue
        r = loi / rui_ro
        out.append({"phia": phia, "vao": round(vao), "sl": round(sl), "tp": round(tp),
                    "r": round(r, 1), "dai": [round(x[0]) for x in dai],
                    "tien_dai": round(sum(x[1] for x in dai), 1),
                    "du_r": r >= R_TOI_THIEU})
    return out


def _ghi(k, kq, t_kq):
    moi = not os.path.exists(CSV)
    with open(CSV, "a", encoding="utf-8") as f:
        if moi:
            f.write("lap_luc_vn,phia,vao,sl,tp,R,dai_cum,trieu_usd_dai,gia_luc_lap,"
                    "khop_luc_vn,ket_qua,ket_qua_luc_vn\n")
        f.write(f"{_vn(_t(k['t_lap']))},{k['phia']},{k['vao']},{k['sl']},{k['tp']},{k['r']},"
                f"\"{'/'.join(str(x) for x in k['dai'])}\",{k['tien_dai']},{k['gia_lap']},"
                f"{_vn(_t(k['t_khop'])) if k.get('t_khop') else ''},{kq},"
                f"{_vn(t_kq) if t_kq else ''}\n")


def khi_doi_muc(cfg, state, gia, t_now, ntfy):
    """Gọi khi muc.json vừa đổi: đóng kế hoạch chưa khớp, lập kế hoạch mới, gửi ntfy."""
    con = []
    for k in state.get("ke_hoach", []):
        if k["tt"] == "cho":
            _ghi(k, "không khớp (đổi map)", t_now)
        else:
            con.append(k)                  # đang giữ lệnh → theo dõi tiếp tới TP/SL
    kh = lap(cfg, gia)
    if not kh:
        state["ke_hoach"] = con
        return ["Kế hoạch chờ: thiếu số tiền các cụm trong muc.json → chưa lập được."]
    dong = []
    for p in kh:
        ten = "SHORT" if p["phia"] == "short" else "LONG"
        dai = " · ".join(_f(x) for x in p["dai"])
        dong.append(f"{'🔴' if ten == 'SHORT' else '🟢'} {ten}: vào {_f(p['vao'])}, SL {_f(p['sl'])}, "
                    f"TP {_f(p['tp'])} → {p['r']}R"
                    + ("" if p["du_r"] else " (R thấp, chỉ ghi nhật ký)")
                    + f"\n   dải dày {dai} ({p['tien_dai']}M)")
        if p["du_r"]:
            p.update({"tt": "cho", "t_lap": t_now.isoformat(), "gia_lap": round(gia)})
            con.append(p)
    state["ke_hoach"] = con
    ntfy("📋 Kế hoạch chờ BTC", f"Giá {_f(gia)} lúc {_vn(t_now)}.\n" + "\n".join(dong)
         + "\nBot báo khi giá chạm điểm vào. Vẫn tự xem nến trước khi vào.", 3, ["clipboard"])
    return ["Kế hoạch chờ:"] + dong


def theo_doi(state, b5, t_now, ntfy):
    """Theo dõi bằng nến 5m đã đóng: chạm điểm vào → báo; sau đó ghi TP/SL."""
    con = []
    for k in state.get("ke_hoach", []):
        short = k["phia"] == "short"
        ten = "SHORT" if short else "LONG"
        moc = _t(k.get("t_xet") or k["t_lap"])
        xong = False
        for b in b5:
            if b["t"] < moc:
                continue
            if k["tt"] == "cho":
                if (b["h"] >= k["vao"]) if short else (b["l"] <= k["vao"]):
                    k["tt"] = "khop"
                    k["t_khop"] = b["t"].isoformat()
                    ntfy(f"🎯 Giá chạm điểm vào kế hoạch {ten} {_f(k['vao'])}",
                         f"Nến 5m lúc {_vn(b['t'])}. SL {_f(k['sl'])}, TP {_f(k['tp'])} ({k['r']}R).\n"
                         "Mở biểu đồ 1–5 phút: có râu từ chối/nến nuốt thì vào, đi thẳng qua thì bỏ.",
                         4, ["dart"])
                    # cùng nến chạm luôn SL → coi là thua
                    if (b["h"] >= k["sl"]) if short else (b["l"] <= k["sl"]):
                        _ghi(k, "thua (cùng nến)", b["t"])
                        xong = True
                        break
                    continue
                if b["t"] - _t(k["t_lap"]) > HET_HAN_CHO:
                    _ghi(k, "không khớp (hết 24 giờ)", b["t"])
                    xong = True
                    break
            else:
                cham_sl = (b["h"] >= k["sl"]) if short else (b["l"] <= k["sl"])
                cham_tp = (b["l"] <= k["tp"]) if short else (b["h"] >= k["tp"])
                if cham_sl or cham_tp:
                    kq = "không rõ (cả hai cùng nến)" if cham_sl and cham_tp else \
                        ("thua" if cham_sl else "thắng")
                    _ghi(k, kq, b["t"])
                    xong = True
                    break
                if b["t"] - _t(k["t_khop"]) > HET_HAN_GIU:
                    _ghi(k, "hết 48 giờ chưa TP/SL", b["t"])
                    xong = True
                    break
        if not xong:
            if b5:
                k["t_xet"] = (b5[-1]["t"] + dt.timedelta(minutes=5)).isoformat()
            con.append(k)
    state["ke_hoach"] = con
