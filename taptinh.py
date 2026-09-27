#!/usr/bin/env python3
"""
Nghiên cứu tập tính giá: biên độ ngày, theo thứ, theo giờ VN, ngày tin NFP.
Lấy dữ liệu 1H (Twelve Data cho vàng, Binance cho crypto), gửi kết quả qua ntfy.
Chạy: python taptinh.py            (cần TWELVE_KEY, NTFY_TOPIC)
"""
import datetime as dt
import os
import statistics as st
from collections import defaultdict

import requests

from vung_signal import fetch_twelve, fetch_binance, ntfy

SYMBOLS = [("VÀNG", "twelve", "XAU/USD"), ("BTC", "binance", "BTCUSDT"), ("ETH", "binance", "ETHUSDT")]
VN = dt.timedelta(hours=7)
THU = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]


def study(name, h1):
    bars = [dict(b, t=b["t"] + VN) for b in h1]
    days = defaultdict(list)
    for b in bars:
        days[b["t"].date()].append(b)
    full = max(len(v) for v in days.values())
    rows = []
    for d, v in sorted(days.items()):
        if len(v) < 0.8 * full:   # bỏ ngày thiếu nến (ngày nghỉ, ngày đầu/cuối)
            continue
        o, h, l, c = v[0]["o"], max(x["h"] for x in v), min(x["l"] for x in v), v[-1]["c"]
        rows.append({"d": d, "pct": 100 * (h - l) / o, "pts": h - l, "body": abs(c - o) / (h - l) if h > l else 0})
    pct = [r["pct"] for r in rows]
    pts = [r["pts"] for r in rows]
    q = st.quantiles(pct, n=20)
    qp = st.quantiles(pts, n=20)
    out = [f"{len(rows)} ngày ({rows[0]['d']:%d/%m/%Y} → {rows[-1]['d']:%d/%m/%Y}), giờ VN",
           f"Biên ngày TB {st.mean(pts):,.1f} giá ({st.mean(pct):.2f}%), trung vị {st.median(pts):,.1f} ({st.median(pct):.2f}%)",
           f"25% ngày < {qp[4]:,.1f} · 75% ngày < {qp[14]:,.1f} · 90% ngày < {qp[17]:,.1f}"]
    wd = defaultdict(list)
    for r in rows:
        wd[r["d"].weekday()].append(r["pct"])
    out.append("Theo thứ (%): " + ", ".join(f"{THU[k]} {st.mean(v):.2f}" for k, v in sorted(wd.items())))
    nfp = [r["pct"] for r in rows if r["d"].weekday() == 4 and r["d"].day <= 7]
    oth = [r["pct"] for r in rows if r["d"].weekday() == 4 and r["d"].day > 7]
    if nfp and oth:
        out.append(f"T6 đầu tháng (tin NFP): {st.mean(nfp):.2f}% ({len(nfp)} ngày) vs T6 khác {st.mean(oth):.2f}%")
    out.append(f"Ngày chạy một chiều (thân ≥ 60% biên): {100 * sum(r['body'] >= 0.6 for r in rows) / len(rows):.0f}%")
    c1 = c15 = n = 0
    for i in range(14, len(rows)):
        adr = st.mean(r["pct"] for r in rows[i - 14:i])
        n += 1
        c1 += rows[i]["pct"] >= adr
        c15 += rows[i]["pct"] >= 1.5 * adr
    if n:
        out.append(f"Ngày vượt ADR14: {100 * c1 / n:.0f}% · vượt 1,5×ADR: {100 * c15 / n:.0f}%")
    rng = [100 * (b["h"] - b["l"]) / b["o"] for b in bars]
    avg = st.mean(rng)
    hr = defaultdict(list)
    for b, r in zip(bars, rng):
        hr[b["t"].hour].append(r)
    prof = sorted(((h, st.mean(v), 100 * sum(x > 2 * avg for x in v) / len(v)) for h, v in hr.items()), key=lambda x: -x[1])
    out.append("Giờ VN biến động mạnh nhất (biên TB % · % nến đột biến):")
    out.append(", ".join(f"{h}h {m:.2f}·{p:.0f}%" for h, m, p in prof[:6]))
    out.append("Giờ yên nhất: " + ", ".join(f"{h}h {m:.2f}" for h, m, _ in prof[-5:]))
    return "\n".join(out)


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    key = os.environ.get("TWELVE_KEY", "").strip()
    for name, src, sym in SYMBOLS:
        try:
            h1 = fetch_twelve(sym, key) if src == "twelve" else fetch_binance(sym)
            txt = study(name, h1)
        except Exception as e:
            txt = f"Lỗi: {e}"
        print(f"===== {name} =====\n{txt}\n")
        if topic:
            ntfy(topic, f"📚 Tập tính {name}", txt, 3, ["books"])


if __name__ == "__main__":
    main()
