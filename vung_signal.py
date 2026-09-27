#!/usr/bin/env python3
"""
Cảnh báo GIÁ GẦN VÙNG cung/cầu khung 1H, 4H, ngày — qua ntfy.
Vàng (XAU/USD, Twelve Data), BTC, ETH (Binance). Chạy 15 phút/lần trên GitHub Actions.

Cách vẽ vùng (giống indicator TradingView "Cảnh báo gần vùng 1H-4H-D"):
  • Vùng tại đỉnh/đáy sóng (5 nến mỗi bên) của từng khung.
    Vùng cung: từ râu cao nhất xuống đỉnh thân nến. Vùng cầu: từ râu thấp nhất lên đáy thân nến.
  • Mở rộng phủ khoảng trống giá (imbalance) mà cú chạy mạnh để lại ngay sau đỉnh/đáy.
  • Chỉ công nhận khi giá đã chạy khỏi vùng ≥ 3 ATR. Vùng trùng nhau cùng khung → gộp.
  • Vùng bị phá khi đóng cửa vượt cạnh xa quá 0,2 ATR.
Báo khi giá cách cạnh gần của vùng ≤ 0,5 ATR (ATR của khung vùng). Mỗi lần tiếp cận báo 1 lần;
giá phải rời vùng ≥ 1,5 ATR mới báo lại.

Biến môi trường: TWELVE_KEY, NTFY_TOPIC.
Chạy thử: python vung_signal.py --test   (gửi tin thử + danh sách vùng gần giá, không ghi trạng thái)
Chạy offline với dữ liệu mẫu: python vung_signal.py --offline FILE.csv.gz  (in kết quả, không gửi)
"""
import datetime as dt
import gzip
import csv
import json
import os
import sys

import requests

# ═════════════ CẤU HÌNH ═════════════
SYMBOLS = [
    ("VÀNG", "twelve", "XAU/USD"),
    ("BTC", "binance", "BTCUSDT"),
    ("ETH", "binance", "ETHUSDT"),
]
H1_BARS = 5000          # số nến 1H lấy về (≈ 7 tháng) → tự gộp thành 4H và ngày

Z_PIV = 5               # đỉnh/đáy tạo vùng: số nến mỗi bên
DEPART = 3.0            # giá phải chạy khỏi vùng ≥ x ATR mới công nhận
GAP_BARS = 5            # số nến tìm khoảng trống giá sau đỉnh/đáy
MAX_WIDTH = 4.0         # độ rộng vùng tối đa (x ATR)
T_PIV = 3               # đỉnh/đáy để xác định xu hướng
NEAR = 0.5              # báo khi giá cách vùng ≤ x ATR
LEAVE = 1.5             # giá phải rời vùng ≥ x ATR mới báo lại
BREAK = 0.2             # vùng bị phá khi đóng cửa vượt cạnh xa x ATR
ATR_LEN = 14
COOLDOWN_H = {"1h": 6, "4h": 12, "1d": 24}   # cùng một vùng không báo lại trong x giờ

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vung_state.json")
TF_NAMES = {"1h": "1H", "4h": "4H", "1d": "Ngày"}


# ═════════════ DỮ LIỆU ═════════════
def fetch_twelve(symbol, key):
    r = requests.get("https://api.twelvedata.com/time_series", timeout=40, params={
        "symbol": symbol, "interval": "1h", "outputsize": H1_BARS, "timezone": "UTC", "apikey": key})
    j = r.json()
    if j.get("status") != "ok":
        raise RuntimeError("Twelve Data: " + str(j.get("message", j))[:300])
    bars = [{"t": dt.datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S"),
             "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])}
            for v in j["values"]]
    bars.sort(key=lambda b: b["t"])
    return bars


def fetch_binance(symbol):
    bars, end = [], None
    while len(bars) < H1_BARS:
        params = {"symbol": symbol, "interval": "1h", "limit": 1000}
        if end is not None:
            params["endTime"] = end
        r = requests.get("https://data-api.binance.vision/api/v3/klines", params=params, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        bars = [{"t": dt.datetime.fromtimestamp(k[0] / 1000, dt.timezone.utc).replace(tzinfo=None),
                 "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in rows] + bars
        end = rows[0][0] - 1
        if len(rows) < 1000:
            break
    seen, out = set(), []
    for b in sorted(bars, key=lambda b: b["t"]):
        if b["t"] not in seen:
            seen.add(b["t"])
            out.append(b)
    return out[-H1_BARS:]


def load_csv_1m(path):
    """Dữ liệu 1 phút trong repo (data/aggflow) → gộp thành 1H, để chạy thử offline."""
    bars = {}
    with gzip.open(path, "rt") as f:
        for row in csv.DictReader(f):
            t = dt.datetime.strptime(row["t"], "%Y-%m-%d %H:%M:%S").replace(minute=0)
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            if t not in bars:
                bars[t] = {"t": t, "o": o, "h": h, "l": l, "c": c}
            else:
                b = bars[t]
                b["h"], b["l"], b["c"] = max(b["h"], h), min(b["l"], l), c
    return [bars[t] for t in sorted(bars)]


def resample(bars, hours):
    """Gộp nến 1H thành 4H hoặc ngày (mốc giờ UTC)."""
    out = {}
    for b in bars:
        if hours == 24:
            k = b["t"].replace(hour=0)
        else:
            k = b["t"].replace(hour=(b["t"].hour // hours) * hours)
        if k not in out:
            out[k] = dict(b, t=k)
        else:
            x = out[k]
            x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
    return [out[k] for k in sorted(out)]


def atr_series(bars, n=ATR_LEN):
    out, prev, trs = [None] * len(bars), None, []
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        if prev is None:
            trs.append(tr)
            if len(trs) == n:
                prev = sum(trs) / n
                out[i] = prev
        else:
            prev = (prev * (n - 1) + tr) / n
            out[i] = prev
    return out


def is_ph(bars, p, n):
    h = bars[p]["h"]
    return all(bars[k]["h"] < h for k in range(p - n, p)) and all(bars[k]["h"] <= h for k in range(p + 1, p + n + 1))


def is_pl(bars, p, n):
    l = bars[p]["l"]
    return all(bars[k]["l"] > l for k in range(p - n, p)) and all(bars[k]["l"] >= l for k in range(p + 1, p + n + 1))


def fmt(x):
    return f"{x:,.2f}"


# ═════════════ VÙNG + XU HƯỚNG CHO MỘT KHUNG ═════════════
def analyze(bars, closed_until):
    """bars: nến của một khung (nến cuối có thể đang chạy).
    closed_until: số nến đã đóng cửa (nến >= chỉ số này đang chạy, chỉ dùng để đo khoảng cách).
    Trả về: danh sách lần tiếp cận [(chỉ số nến, vùng)], vùng còn sống, xu hướng."""
    n = len(bars)
    atr = atr_series(bars)
    zones, events = [], []
    lh = ll = None
    trend = 0
    for i in range(n):
        b = bars[i]
        closed = i < closed_until
        a = atr[i - 1] if i > 0 and atr[i - 1] else atr[i]
        # xu hướng theo sóng
        if closed:
            p = i - T_PIV
            if p - T_PIV >= 0:
                if is_ph(bars, p, T_PIV):
                    lh = bars[p]["h"]
                if is_pl(bars, p, T_PIV):
                    ll = bars[p]["l"]
            if lh is not None and b["c"] > lh:
                trend = 1
            if ll is not None and b["c"] < ll:
                trend = -1
        if a is None:
            continue
        # theo dõi vùng
        for z in list(zones):
            sd, nr, fr = z["sd"], z["near"], z["far"]
            if closed and sd * (b["c"] - fr) < -BREAK * a:
                zones.remove(z)
                continue
            ext = b["l"] if sd == 1 else b["h"]
            fav = b["h"] if sd == 1 else b["l"]
            if not z["active"]:
                if sd * (fav - nr) >= DEPART * z["a"]:
                    z["active"], z["armed"] = True, True
                continue
            dist = sd * (ext - nr)
            if not z["armed"]:
                if dist > LEAVE * z["a"]:
                    z["armed"] = True
            elif dist <= NEAR * z["a"]:
                z["armed"] = False
                events.append((i, dict(z)))
        # tạo vùng mới khi đỉnh/đáy được xác nhận (chỉ từ nến đã đóng)
        p = i - Z_PIV
        if closed and p - Z_PIV >= 0 and atr[p]:
            ap = atr[i]
            for sd in (-1, 1):
                if sd == -1 and not is_ph(bars, p, Z_PIV):
                    continue
                if sd == 1 and not is_pl(bars, p, Z_PIV):
                    continue
                pb = bars[p]
                if sd == -1:
                    fr, nr = pb["h"], max(pb["o"], pb["c"])
                    for j in range(p, min(p + GAP_BARS - 1, i - 1)):
                        if bars[j + 2]["h"] < bars[j]["l"] and bars[j + 2]["h"] < nr:
                            nr = bars[j + 2]["h"]
                    nr = max(nr, fr - MAX_WIDTH * ap)
                    nr = min(nr, fr - 0.2 * ap)
                else:
                    fr, nr = pb["l"], min(pb["o"], pb["c"])
                    for j in range(p, min(p + GAP_BARS - 1, i - 1)):
                        if bars[j + 2]["l"] > bars[j]["h"] and bars[j + 2]["l"] > nr:
                            nr = bars[j + 2]["l"]
                    nr = min(nr, fr + MAX_WIDTH * ap)
                    nr = max(nr, fr + 0.2 * ap)
                top, bot = (nr, fr) if sd == 1 else (fr, nr)
                merged = False
                for z in zones:
                    if z["sd"] == sd:
                        zt, zb = (z["near"], z["far"]) if sd == 1 else (z["far"], z["near"])
                        if not (bot > zt or top < zb):
                            nt, nb = max(top, zt), min(bot, zb)
                            z["near"], z["far"] = (nt, nb) if sd == 1 else (nb, nt)
                            merged = True
                            break
                if not merged:
                    # vùng mới có thể đã được giá chạy khỏi đủ xa ngay trong các nến xác nhận
                    fav = max(bars[k]["h"] for k in range(p + 1, i + 1)) if sd == 1 else min(bars[k]["l"] for k in range(p + 1, i + 1))
                    act = sd * (fav - nr) >= DEPART * ap
                    zones.append({"sd": sd, "near": nr, "far": fr, "a": ap, "active": act, "armed": act,
                                  "born": b["t"]})
    return events, zones, trend


def trend_name(x):
    return {1: "tăng 🟢", -1: "giảm 🔴", 0: "chưa rõ ⚪"}[x]


def process(name, h1):
    """h1: nến 1H (nến cuối có thể đang chạy). Trả về (tin mới, tóm tắt)."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    frames = {"1h": (h1, 1), "4h": (resample(h1, 4), 4), "1d": (resample(h1, 24), 24)}
    results, trends = {}, {}
    for tf, (bars, hrs) in frames.items():
        closed = sum(1 for b in bars if b["t"] + dt.timedelta(hours=hrs) <= now)
        ev, zones, tr = analyze(bars, closed)
        results[tf] = (bars, ev, zones)
        trends[tf] = tr
    price = h1[-1]["c"]
    tr_txt = f"Xu hướng: 1H {trend_name(trends['1h'])}, 4H {trend_name(trends['4h'])}, ngày {trend_name(trends['1d'])}"
    alerts = []
    for tf, (bars, ev, zones) in results.items():
        for i, z in ev:
            sd = z["sd"]
            top, bot = (z["near"], z["far"]) if sd == 1 else (z["far"], z["near"])
            kind = "CẦU (hỗ trợ) → chờ tín hiệu MUA" if sd == 1 else "CUNG (kháng cự) → chờ tín hiệu BÁN"
            aligned = sd == trends["1h"]
            title = f"{'🟢' if sd == 1 else '🔴'} {name} gần vùng {'CẦU' if sd == 1 else 'CUNG'} {TF_NAMES[tf]}" + (" · thuận xu hướng 1H" if aligned else "")
            msg = (f"Giá {fmt(price)} gần vùng {kind}\nVùng {TF_NAMES[tf]}: {fmt(bot)} – {fmt(top)}\n{tr_txt}\n"
                   f"Gợi ý: xem pin bar / đáy-đỉnh đôi / engulfing tại vùng trước khi vào lệnh.")
            key = f"{name}|{tf}|{sd}|{round(z['far'], 2)}"
            alerts.append({"t": bars[i]["t"], "ago": len(bars) - 1 - i, "tf": tf, "key": key, "title": title, "msg": msg, "prio": 4 if aligned else 3})
    # tóm tắt: vùng đã công nhận gần giá nhất mỗi phía
    lines = [f"Giá {fmt(price)}", tr_txt]
    for tf, (bars, ev, zones) in results.items():
        act = [z for z in zones if z["active"]]
        up = sorted([z for z in act if z["sd"] == -1 and z["near"] > price], key=lambda z: z["near"])[:1]
        dn = sorted([z for z in act if z["sd"] == 1 and z["near"] < price], key=lambda z: -z["near"])[:1]
        for z in up + dn:
            top, bot = (z["near"], z["far"]) if z["sd"] == 1 else (z["far"], z["near"])
            lines.append(f" • {TF_NAMES[tf]} {'cung' if z['sd'] == -1 else 'cầu'} {fmt(bot)} – {fmt(top)}")
    return alerts, "\n".join(lines)


# ═════════════ GỬI + TRẠNG THÁI ═════════════
def ntfy(topic, title, message, priority=3, tags=None):
    r = requests.post("https://ntfy.sh/", timeout=20, json={
        "topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []})
    r.raise_for_status()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    if "--offline" in sys.argv:
        path = sys.argv[sys.argv.index("--offline") + 1]
        h1 = load_csv_1m(path)
        alerts, summary = process_offline(h1)
        print(summary)
        print(f"{len(alerts)} lần tiếp cận vùng trong dữ liệu:")
        for a in alerts[-15:]:
            print(a["t"], "|", a["title"])
        return

    test = "--test" in sys.argv
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    key = os.environ.get("TWELVE_KEY", "").strip()
    if not topic:
        sys.exit("Thiếu NTFY_TOPIC")
    state = load_state()
    sent = state.get("sent", {})
    first_run = not sent and not state.get("init")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    errors = []

    for name, source, symbol in SYMBOLS:
        try:
            h1 = fetch_twelve(symbol, key) if source == "twelve" else fetch_binance(symbol)
            if len(h1) < 500:
                raise RuntimeError(f"chỉ lấy được {len(h1)} nến 1H")
            alerts, summary = process(name, h1)
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[{name}] LỖI: {e}")
            continue
        # chỉ xét lần tiếp cận ở nến đang chạy hoặc nến vừa đóng của mỗi khung
        recent = [a for a in alerts if a["ago"] <= 1]
        print(f"[{name}] {len(h1)} nến 1H, {len(alerts)} lần tiếp cận trong lịch sử, {len(recent)} gần đây")
        if test or first_run:
            ntfy(topic, f"📍 {name}: vùng 1H/4H/ngày gần giá", summary, 2, ["round_pushpin"])
        for a in recent:
            last = sent.get(a["key"])
            if last and dt.datetime.fromisoformat(last) >= now - dt.timedelta(hours=COOLDOWN_H[a["tf"]]):
                continue
            if not (test or first_run):
                ntfy(topic, a["title"], a["msg"] + f"\n🕐 {(now + dt.timedelta(hours=7)):%H:%M %d/%m} giờ VN", a["prio"], ["dart"])
                print("  → đã gửi:", a["title"])
            sent[a["key"]] = now.isoformat()

    # dọn các khóa cũ hơn 30 ngày
    sent = {k: v for k, v in sent.items() if dt.datetime.fromisoformat(v) >= now - dt.timedelta(days=30)}
    if test:
        ntfy(topic, "✅ Cảnh báo gần vùng đã kết nối", "Tin thử từ GitHub. Bạn sẽ nhận báo khi Vàng, BTC, ETH tới gần vùng cung/cầu khung 1H, 4H, ngày.", 3, ["white_check_mark"])
    if errors:
        ntfy(topic, "❗ Lỗi cảnh báo vùng", "\n".join(errors), 3, ["x"])
    if not test:
        state["sent"] = sent
        state["init"] = True
        save_state(state)


def process_offline(h1):
    """Như process() nhưng coi toàn bộ nến là đã đóng cửa (dữ liệu quá khứ)."""
    global dt
    real = dt.datetime

    class Fake(real):
        @classmethod
        def now(cls, tz=None):
            t = h1[-1]["t"] + dt.timedelta(days=2)
            return t.replace(tzinfo=tz) if tz else t
    dt.datetime = Fake
    try:
        return process("BTC (thử)", h1)
    finally:
        dt.datetime = real


if __name__ == "__main__":
    main()
