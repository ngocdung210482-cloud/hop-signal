"""
"Kế hoạch chờ" — lập kế hoạch TRƯỚC cú quét, từ heatmap đang dùng.

Bản 2 (28/11/2026) — để đo xác suất: giá hay quay đầu ở cụm nào?
Mỗi lần đổi map, mỗi phía (short trên / long dưới) lập 3 kiểu điểm vào:
  • gan : cụm gần giá nhất
  • lon : cụm lớn nhất (nhiều triệu USD nhất) trong các cụm của phía đó
  • xa  : mép xa của "dải dày" (các cụm liền nhau cách ≤ KHOANG_NOI % và đủ lớn)
Cả 3 kiểu dùng chung cách tính:
  • Cắt lỗ: qua cụm kế tiếp sau điểm vào (nếu cách ≤ 1,5%) thêm 0,1%; không có thì điểm vào ± 0,6%.
  • Chốt lời: cụm gần giá nhất ở phía đối diện, lùi 0,15% về phía giá.

Kế hoạch KHÔNG bị hủy khi đổi map: chờ khớp tối đa 24 giờ, giữ tối đa 48 giờ.
Ghi vào nhat_ky_ke_hoach_v2.csv: có chạm không, giá đến gần điểm vào bao nhiêu %,
sau khi chạm thì quét sâu qua điểm vào bao nhiêu %, thắng/thua.
Bot chỉ gửi ntfy "chạm điểm vào" cho kế hoạch R ≥ R_TOI_THIEU. Bot KHÔNG tự vào lệnh.
"""
import datetime as dt
import os

DIR = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(DIR, "nhat_ky_ke_hoach_v2.csv")

VN = dt.timedelta(hours=7)
KHOANG_NOI = 0.7          # % khoảng cách tối đa giữa 2 cụm liền nhau trong một dải
TY_LE_DU_LON = 0.5        # cụm sau phải ≥ 50% cụm lớn nhất của dải mới tính vào dải
SL_XA_TOI_DA = 1.5        # % — cụm kế tiếp xa hơn mức này thì không dùng làm mốc SL
SL_DEM = 0.1              # % đệm thêm sau mốc SL
SL_MAC_DINH = 0.6         # % khi không có cụm kế tiếp
TP_LUI = 0.15             # % lùi TP về phía giá (chốt trước cụm)
R_TOI_THIEU = 1.5
HET_HAN_CHO = dt.timedelta(hours=24)
HET_HAN_GIU = dt.timedelta(hours=48)
TEN_KIEU = {"gan": "cụm gần", "lon": "cụm lớn", "xa": "mép dải"}


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


def _mot_ke_hoach(phia, kieu, ds, i_vao, doi, dai=None):
    dau = 1 if phia == "short" else -1
    vao = ds[i_vao][0]
    sau = ds[i_vao + 1] if i_vao + 1 < len(ds) else None
    if sau and abs(sau[0] / vao - 1) * 100 <= SL_XA_TOI_DA:
        sl = sau[0] * (1 + dau * SL_DEM / 100)
    else:
        sl = vao * (1 + dau * SL_MAC_DINH / 100)
    tp = doi[0][0] * (1 + dau * TP_LUI / 100)
    rui_ro, loi = abs(sl - vao), abs(vao - tp)
    if rui_ro <= 0:
        return None
    r = loi / rui_ro
    dai = dai or [ds[i_vao]]
    return {"phia": phia, "kieu": kieu, "vao": round(vao), "sl": round(sl), "tp": round(tp),
            "r": round(r, 1), "trieu": round(ds[i_vao][1], 1),
            "dai": [round(x[0]) for x in dai], "tien_dai": round(sum(x[1] for x in dai), 1),
            "du_r": r >= R_TOI_THIEU}


def lap(cfg, gia):
    """Trả về danh sách kế hoạch (tối đa 3 kiểu × 2 phía), hoặc [] nếu thiếu số tiền."""
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
        # dải dày
        dai = [ds[0]]
        for c in ds[1:]:
            khoang = abs(c[0] / dai[-1][0] - 1) * 100
            if khoang <= KHOANG_NOI and c[1] >= TY_LE_DU_LON * max(x[1] for x in dai):
                dai.append(c)
            else:
                break
        i_lon = max(range(len(ds)), key=lambda i: ds[i][1])
        for kieu, i in (("gan", 0), ("lon", i_lon), ("xa", len(dai) - 1)):
            p = _mot_ke_hoach(phia, kieu, ds, i, doi, dai if kieu == "xa" else None)
            if p:
                out.append(p)
    return out


def _ghi(k, kq, t_kq):
    moi = not os.path.exists(CSV)
    short = k["phia"] == "short"
    # giá đến gần điểm vào bao nhiêu % (âm = còn thiếu) — chỉ có ý nghĩa khi chưa chạm
    gan = k.get("gan_nhat")
    thieu = "" if gan is None or k.get("t_khop") else \
        round((gan / k["vao"] - 1) * 100 * (1 if short else -1), 2)
    cuc = k.get("cuc")
    sau = "" if cuc is None else round((cuc / k["vao"] - 1) * 100 * (1 if short else -1), 2)
    with open(CSV, "a", encoding="utf-8") as f:
        if moi:
            f.write("lap_luc_vn,phia,kieu,vao,trieu_usd,sl,tp,R,dai_cum,gia_luc_lap,"
                    "khop_luc_vn,thieu_phan_tram,quet_sau_qua_vao_phan_tram,ket_qua,ket_qua_luc_vn\n")
        f.write(f"{_vn(_t(k['t_lap']))},{k['phia']},{k.get('kieu', 'xa')},{k['vao']},"
                f"{k.get('trieu', '')},{k['sl']},{k['tp']},{k['r']},"
                f"\"{'/'.join(str(x) for x in k['dai'])}\",{k['gia_lap']},"
                f"{_vn(_t(k['t_khop'])) if k.get('t_khop') else ''},{thieu},{sau},{kq},"
                f"{_vn(t_kq) if t_kq else ''}\n")


def khi_doi_muc(cfg, state, gia, t_now, ntfy):
    """Gọi khi muc.json vừa đổi: giữ kế hoạch cũ (theo dõi tiếp), lập thêm kế hoạch mới."""
    con = list(state.get("ke_hoach", []))
    kh = lap(cfg, gia)
    if not kh:
        state["ke_hoach"] = con
        return ["Kế hoạch chờ: thiếu số tiền các cụm trong muc.json → chưa lập được."]
    dong = []
    for phia in ("short", "long"):
        nhom = [p for p in kh if p["phia"] == phia]
        if not nhom:
            continue
        ten = "SHORT" if phia == "short" else "LONG"
        dong.append(f"{'🔴' if ten == 'SHORT' else '🟢'} {ten} (TP {_f(nhom[0]['tp'])}):")
        for p in nhom:
            dong.append(f"   • {TEN_KIEU[p['kieu']]} {_f(p['vao'])} ({p['trieu']}M), "
                        f"SL {_f(p['sl'])} → {p['r']}R" + ("" if p["du_r"] else " — chỉ ghi nhật ký"))
    for p in kh:
        p.update({"tt": "cho", "t_lap": t_now.isoformat(), "gia_lap": round(gia)})
        con.append(p)
    state["ke_hoach"] = con
    ntfy("📋 Kế hoạch chờ BTC", f"Giá {_f(gia)} lúc {_vn(t_now)}.\n" + "\n".join(dong)
         + "\nBot báo khi giá chạm điểm vào (kế hoạch ≥1,5R). Vẫn tự xem nến trước khi vào.",
         2, ["clipboard"])
    return ["Kế hoạch chờ:"] + dong


def theo_doi(state, b5, t_now, ntfy):
    """Theo dõi bằng nến 5m đã đóng: chạm điểm vào → báo; sau đó ghi TP/SL."""
    con = []
    da_bao = set()          # tránh báo trùng nhiều kế hoạch cùng một mức trong một lần chạy
    for k in state.get("ke_hoach", []):
        short = k["phia"] == "short"
        ten = "SHORT" if short else "LONG"
        moc = _t(k.get("t_xet") or k["t_lap"])
        xong = False
        for b in b5:
            if b["t"] < moc:
                continue
            if k["tt"] == "cho":
                cuc_nen = b["h"] if short else b["l"]
                g = k.get("gan_nhat")
                k["gan_nhat"] = cuc_nen if g is None else (max(g, cuc_nen) if short else min(g, cuc_nen))
                if (b["h"] >= k["vao"]) if short else (b["l"] <= k["vao"]):
                    k["tt"] = "khop"
                    k["t_khop"] = b["t"].isoformat()
                    k["cuc"] = cuc_nen
                    khoa = (k["phia"], k["vao"])
                    if k.get("du_r") and khoa not in da_bao:
                        da_bao.add(khoa)
                        ntfy(f"🎯 Giá chạm điểm vào {ten} {_f(k['vao'])} ({TEN_KIEU.get(k.get('kieu', 'xa'))})",
                             f"Nến 5m lúc {_vn(b['t'])}. SL {_f(k['sl'])}, TP {_f(k['tp'])} ({k['r']}R).\n"
                             "Mở biểu đồ 1–5 phút: có râu từ chối/nến nuốt thì vào, đi thẳng qua thì bỏ.",
                             4, ["dart"])
                    if (b["h"] >= k["sl"]) if short else (b["l"] <= k["sl"]):
                        _ghi(k, "thua (cùng nến)", b["t"])
                        xong = True
                        break
                    continue
                if b["t"] - _t(k["t_lap"]) > HET_HAN_CHO:
                    _ghi(k, "không chạm (hết 24 giờ)", b["t"])
                    xong = True
                    break
            else:
                cuc_nen = b["h"] if short else b["l"]
                c = k.get("cuc")
                k["cuc"] = cuc_nen if c is None else (max(c, cuc_nen) if short else min(c, cuc_nen))
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
