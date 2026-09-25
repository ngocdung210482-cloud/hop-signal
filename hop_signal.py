#!/usr/bin/env python3
"""
Cảnh báo hộp cung/cầu qua ntfy — Vàng (XAU/USD), BTC, ETH, khung 1H.

Logic giống indicator TradingView "Backtest hộp nền v2":
  • Hộp: nền giằng co 3–5 nến ngay trước cú bứt phá (≥ 1,5 ATR, đóng cửa vượt nền).
    Cạnh kẻ ngang = giá mở cửa nến bứt phá; hộp kéo về phía ngược cú đi, trùm râu xa nhất của nền.
  • Xu hướng: tăng còn giữ khi chưa đóng cửa thủng đáy gần nhất; giảm còn giữ khi chưa đóng cửa vượt đỉnh gần nhất.
    Phá lần 1 → CÂN NHẮC (không vào lệnh). Phá tiếp cùng chiều → đảo chiều. Phá lại theo chiều cũ → tiếp tục.
  • Chấm 7 điểm; chỉ báo khi thuận xu hướng và hộp ≥ MIN_SCORE điểm.
  • Vào lệnh: nến đóng cửa ra ngoài hộp đúng chiều. SL ngoài râu. Chốt lời ngay trước hộp đối diện gần nhất.

Thông báo:
  1) Giá chạm hộp (đạt điều kiện)      2) Có nến đảo chiều = VÀO LỆNH      3) Xu hướng đổi trạng thái

Biến môi trường: TWELVE_KEY (mã API Twelve Data), NTFY_TOPIC (tên kênh ntfy).
Chạy thử: python hop_signal.py --test   (gửi 1 tin thử + tóm tắt hiện tại, không ghi trạng thái)
"""
import datetime as dt
import json
import math
import os
import sys

import requests

# ═════════════ CẤU HÌNH ═════════════
SYMBOLS = [
    # (tên hiển thị, nguồn, mã)
    ("VÀNG", "twelve", "XAU/USD"),
    ("BTC", "binance", "BTCUSDT"),
    ("ETH", "binance", "ETHUSDT"),
]
BARS = 1500              # số nến 1H lấy về mỗi lần

BASE_MIN, BASE_MAX = 3, 5  # nền giằng co 3–5 nến
BASE_RNG = 2.0             # biên độ cả nền ≤ x ATR
IMP_BARS = 1               # cú bứt phá gồm bao nhiêu nến
IMP_MULT = 1.5             # bứt phá ≥ x ATR
ZONE_MAX = 3.0             # bỏ hộp dày hơn x ATR
MAX_AGE = 300              # hộp hết hạn sau x nến
MAX_TOUCH = 3              # tối đa số lần chạm mỗi hộp
MAX_ZONES = 40

CONFIRM_BARS = 5           # chờ nến đảo chiều tối đa x nến
SL_BUF = 0.2               # SL ngoài hộp/râu x ATR
LEAVE_ATR = 0.5            # giá phải rời hộp ≥ x ATR mới tính lần chạm mới
TP_BUF = 0.1               # chốt lời trước hộp đối diện x ATR
MIN_RR = 1.0               # bỏ lệnh nếu lời < x R
NO_BOX_RR = 2.0            # không có hộp đối diện → chốt ở x R

SWING = 5                  # đỉnh/đáy gần nhất: số nến mỗi bên
ONLY_TREND = True
MIN_SCORE = 4

DEPART_MULT = 3.0          # 1. rời hộp mạnh ≥ x ATR
BOS_LOOK = 20              # 2. số nến trước nền tìm đỉnh/đáy cũ
HTF_MUL = 4                # 3. khung lớn = 4H (4 nến 1H)
MAX_TOUCH_NEW = 2          # 4. hộp còn mới khi lần chạm ≤ x
FLIP_LOOK = 150            # 5. tìm đổi vai trong x nến trước nền
VOL_MULT = 1.8             # 7. volume ≥ x lần trung bình 20 nến
WICK_PCT = 0.5             # 7. râu hấp thụ ≥ x thân nến

MAX_CATCHUP = 6            # nếu lỡ vài lần chạy, báo bù tối đa x nến gần nhất
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

CRIT_NAMES = ["Rời hộp mạnh", "Phá cấu trúc", "Đỉnh/đáy khung lớn", "Hộp còn mới",
              "Hợp lưu", "Thuận xu hướng", "Volume / hấp thụ"]


# ═════════════ LẤY DỮ LIỆU ═════════════
def _drop_open_bar(bars):
    """Bỏ nến đang chạy (chưa đóng cửa)."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return [b for b in bars if b["t"] + dt.timedelta(hours=1) <= now]


def fetch_twelve(symbol, key):
    r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params={
        "symbol": symbol, "interval": "1h", "outputsize": BARS, "timezone": "UTC", "apikey": key})
    j = r.json()
    if j.get("status") != "ok":
        raise RuntimeError("Twelve Data: " + str(j.get("message", j))[:300])
    bars = [{"t": dt.datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S"),
             "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"]),
             "v": float(v["volume"]) if v.get("volume") not in (None, "", "0") else None}
            for v in j["values"]]
    bars.sort(key=lambda b: b["t"])
    return _drop_open_bar(bars)


def fetch_binance(symbol):
    bars = []
    end = None
    # Binance trả tối đa 1000 nến/lần → lấy 2 lần
    while len(bars) < BARS:
        params = {"symbol": symbol, "interval": "1h", "limit": 1000}
        if end is not None:
            params["endTime"] = end
        r = requests.get("https://data-api.binance.vision/api/v3/klines", params=params, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        chunk = [{"t": dt.datetime.fromtimestamp(k[0] / 1000, dt.timezone.utc).replace(tzinfo=None),
                  "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
                 for k in rows]
        bars = chunk + bars
        end = rows[0][0] - 1
        if len(rows) < 1000:
            break
    seen, out = set(), []
    for b in sorted(bars, key=lambda b: b["t"]):
        if b["t"] not in seen:
            seen.add(b["t"])
            out.append(b)
    return _drop_open_bar(out[-BARS:])


# ═════════════ CHỈ BÁO PHỤ ═════════════
def atr_series(bars, n=14):
    out = [None] * len(bars)
    prev = None
    trs = []
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


def sma_vol(bars, n=20):
    out = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        vs = [bars[k]["v"] for k in range(i - n + 1, i + 1)]
        if all(v is not None for v in vs):
            out[i] = sum(vs) / n
    return out


def prev_week_hl(bars):
    """Đỉnh/đáy của tuần trước cho mỗi nến."""
    wk = {}
    for b in bars:
        key = b["t"].isocalendar()[:2]
        h, l = wk.get(key, (-1e18, 1e18))
        wk[key] = (max(h, b["h"]), min(l, b["l"]))
    keys = sorted(wk)
    prev = {keys[i]: wk[keys[i - 1]] for i in range(1, len(keys))}
    return [prev.get(b["t"].isocalendar()[:2], (None, None)) for b in bars]


def is_pivot_high(bars, p, n):
    h = bars[p]["h"]
    return all(bars[k]["h"] < h for k in range(p - n, p)) and all(bars[k]["h"] <= h for k in range(p + 1, p + n + 1))


def is_pivot_low(bars, p, n):
    l = bars[p]["l"]
    return all(bars[k]["l"] > l for k in range(p - n, p)) and all(bars[k]["l"] >= l for k in range(p + 1, p + n + 1))


def fmt(x):
    return f"{x:,.2f}"


# ═════════════ MÔ PHỎNG TOÀN BỘ LỊCH SỬ ═════════════
class Zone:
    def __init__(self, dem, top, bot, a0, born, prior):
        self.dem, self.top, self.bot, self.a0, self.born, self.prior = dem, top, bot, a0, born, prior
        self.c3 = self.c5 = self.c7 = False
        self.d5 = ""
        self.max_ext = 0.0
        self.bos = False
        self.touches = 0
        self.armed = False
        self.waiting = False
        self.wait_start = 0
        self.touch_ext = None
        self.in_trade = False
        self.entry = self.sl = self.risk = self.tp = self.rr = None
        self.tp_box = False
        self.crit = [False] * 7
        self.score = 0
        self.dead = False

    def kind(self):
        return "Hộp cầu (MUA)" if self.dem else "Hộp cung (BÁN)"


def simulate(name, bars):
    n = len(bars)
    atr = atr_series(bars)
    vavg = sma_vol(bars)
    has_vol = all(b["v"] is not None for b in bars[-50:])
    wkhl = prev_week_hl(bars)
    events = []           # (index, title, message, priority, tags)
    zones = []

    key_high = key_low = None
    st, prev_t, pend = 0, 0, 0
    start = max(FLIP_LOOK + 20, 60)

    def score_now(z, trend):
        aligned = (z.dem and trend == 1) or (not z.dem and trend == -1)
        c = [z.max_ext >= DEPART_MULT * z.a0, z.bos, z.c3, z.touches <= MAX_TOUCH_NEW, z.c5, aligned, z.c7]
        return c, sum(c), aligned

    def crit_txt(c, d5):
        return "\n".join(("✓ " if c[k] else "✗ ") + CRIT_NAMES[k] + (f" ({d5.strip()})" if k == 4 and c[k] and d5 else "")
                         for k in range(7))

    def band(sc):
        return "Mạnh" if sc >= 5 else "Trung bình" if sc >= 3 else "Yếu"

    def find_tp(z, i):
        best = None
        for q in zones:
            if q.dead or q.dem == z.dem:
                continue
            if z.dem and q.bot > z.entry:
                best = q.bot if best is None else min(best, q.bot)
            elif not z.dem and q.top < z.entry:
                best = q.top if best is None else max(best, q.top)
        z.tp_box = best is not None
        if best is None:
            z.tp = z.entry + NO_BOX_RR * z.risk if z.dem else z.entry - NO_BOX_RR * z.risk
        else:
            z.tp = best - TP_BUF * atr[i] if z.dem else best + TP_BUF * atr[i]
        z.rr = abs(z.tp - z.entry) / z.risk if z.risk > 0 else 0
        return z.risk > 0 and z.rr >= MIN_RR and ((z.tp > z.entry) if z.dem else (z.tp < z.entry))

    for i in range(start, n):
        b = bars[i]
        a = atr[i]
        if a is None:
            continue

        # ── 1. Xu hướng theo đỉnh/đáy gần nhất ──
        st_before = st
        p = i - SWING
        if p - SWING >= 0:
            if is_pivot_high(bars, p, SWING):
                key_high = bars[p]["h"]
            if is_pivot_low(bars, p, SWING):
                key_low = bars[p]["l"]
        broke_up_lvl = key_high if key_high is not None and b["c"] > key_high else None
        broke_dn_lvl = key_low if key_low is not None and b["c"] < key_low else None
        if broke_up_lvl is not None:
            key_high = None
            if st == -1:
                st, prev_t, pend = 0, -1, 1
                events.append((i, f"⚠️ {name}: vượt đỉnh gần nhất → CÂN NHẮC",
                               f"Đóng cửa {fmt(b['c'])} vượt đỉnh {fmt(broke_up_lvl)} (phá lần 1).\nXu hướng giảm tạm dừng — chưa vào lệnh bán.", 3, ["warning"]))
            elif st == 0:
                if prev_t == 1 or pend == 1:
                    resumed = prev_t == 1 and pend != 1
                    st, prev_t, pend = 1, 1, 0
                    events.append((i, f"🟢 {name}: {'xu hướng TĂNG tiếp tục' if resumed else 'ĐẢO CHIỀU TĂNG'}",
                                   f"Đóng cửa {fmt(b['c'])} vượt đỉnh {fmt(broke_up_lvl)}.\nChỉ tìm lệnh MUA ở hộp cầu.", 4, ["green_circle"]))
                else:
                    pend = 1
        if broke_dn_lvl is not None:
            key_low = None
            if st == 1:
                st, prev_t, pend = 0, 1, -1
                events.append((i, f"⚠️ {name}: thủng đáy gần nhất → CÂN NHẮC",
                               f"Đóng cửa {fmt(b['c'])} thủng đáy {fmt(broke_dn_lvl)} (phá lần 1).\nXu hướng tăng tạm dừng — chưa vào lệnh mua.", 3, ["warning"]))
            elif st == 0:
                if prev_t == -1 or pend == -1:
                    resumed = prev_t == -1 and pend != -1
                    st, prev_t, pend = -1, -1, 0
                    events.append((i, f"🔴 {name}: {'xu hướng GIẢM tiếp tục' if resumed else 'ĐẢO CHIỀU GIẢM'}",
                                   f"Đóng cửa {fmt(b['c'])} thủng đáy {fmt(broke_dn_lvl)}.\nChỉ tìm lệnh BÁN ở hộp cung.", 4, ["red_circle"]))
                else:
                    pend = -1
        trend_dir = st_before   # xu hướng tới hết nến trước (cho lúc chạm)
        trend_now = st          # xu hướng sau khi nến này đóng cửa (cho lúc vào lệnh)

        # ── 2. Tìm cú bứt phá và nền giằng co ──
        b0 = IMP_BARS
        decide = bars[i - (IMP_BARS - 1)]["o"]
        kb = 0
        bhi, blo = bars[i - b0]["h"], bars[i - b0]["l"]
        for j in range(max(BASE_MIN, BASE_MAX)):
            o = i - b0 - j
            nh, nl = max(bhi, bars[o]["h"]), min(blo, bars[o]["l"])
            if nh - nl <= BASE_RNG * a:
                bhi, blo, kb = nh, nl, j + 1
            else:
                break
        if kb >= BASE_MIN:
            move = b["c"] - decide
            for dem in ([False] if (move <= -IMP_MULT * a and b["c"] < blo) else []) + ([True] if (move >= IMP_MULT * a and b["c"] > bhi) else []):
                top, bot = (decide, blo) if dem else (bhi, decide)
                left = i - (b0 + kb - 1)
                if not (top > bot and top - bot <= ZONE_MAX * a):
                    continue
                if any(q.dem == dem and i - q.born <= BASE_MAX + IMP_BARS and q.top >= bot and q.bot <= top for q in zones):
                    continue
                tol = 0.25 * a
                prior_rng = [bars[k] for k in range(max(0, left - BOS_LOOK), left)]
                prior = (max(x["h"] for x in prior_rng) if dem else min(x["l"] for x in prior_rng)) if prior_rng else (1e18 if dem else -1e18)
                z = Zone(dem, top, bot, a, i, prior)
                w_rng = [bars[k] for k in range(max(0, left - 3 * HTF_MUL), left)]
                if w_rng:
                    ext = min(x["l"] for x in w_rng) if dem else max(x["h"] for x in w_rng)
                    z.c3 = bot <= ext if dem else top >= ext
                else:
                    z.c3 = True
                step = 0.5 * 10 ** (math.floor(math.log10(b["c"])) - 1)
                r_hit = math.floor((top + tol) / step) * step >= bot - tol
                wh, wl = wkhl[i]
                w_hit = wh is not None and ((bot - tol <= wh <= top + tol) or (bot - tol <= wl <= top + tol))
                f_hit = any((bars[k]["h"] >= bot and bars[k]["c"] < bot) if dem else (bars[k]["l"] <= top and bars[k]["c"] > top)
                            for k in range(max(0, left - FLIP_LOOK), left))
                z.c5 = r_hit or w_hit or f_hit
                z.d5 = ("số tròn " if r_hit else "") + ("đỉnh/đáy tuần " if w_hit else "") + ("đổi vai" if f_hit else "")
                if has_vol:
                    for k in range(left, i + 1):
                        if vavg[k] and bars[k]["v"] >= VOL_MULT * vavg[k]:
                            rng = bars[k]["h"] - bars[k]["l"]
                            wick = (min(bars[k]["o"], bars[k]["c"]) - bars[k]["l"]) if dem else (bars[k]["h"] - max(bars[k]["o"], bars[k]["c"]))
                            if k > i - IMP_BARS or (rng > 0 and wick >= WICK_PCT * rng):
                                z.c7 = True
                                break
                zones.append(z)
        if len(zones) > MAX_ZONES:
            for q in zones:
                if not q.in_trade and not q.waiting:
                    zones.remove(q)
                    break

        # ── 3. Theo dõi từng hộp ──
        for z in reversed(list(zones)):
            if z.born >= i:
                continue
            dem = z.dem
            if z.in_trade:
                hit_sl = b["l"] <= z.sl if dem else b["h"] >= z.sl
                hit_tp = b["h"] >= z.tp if dem else b["l"] <= z.tp
                if hit_sl:
                    z.dead = True
                elif hit_tp:
                    z.in_trade = False
                    z.armed = False
                    if z.touches >= MAX_TOUCH:
                        z.dead = True
            elif not z.waiting:
                if i - z.born > MAX_AGE:
                    z.dead = True
                elif (b["c"] < z.bot - SL_BUF * z.a0) if dem else (b["c"] > z.top + SL_BUF * z.a0):
                    z.dead = True
                elif not z.armed:
                    z.max_ext = max(z.max_ext, (b["h"] - z.top) if dem else (z.bot - b["l"]))
                    if (b["h"] > z.prior) if dem else (b["l"] < z.prior):
                        z.bos = True
                    if (b["l"] > z.top + LEAVE_ATR * z.a0) if dem else (b["h"] < z.bot - LEAVE_ATR * z.a0):
                        z.armed = True
                elif (b["l"] <= z.top) if dem else (b["h"] >= z.bot):
                    z.touches += 1
                    z.armed = False
                    c, sc, aligned = score_now(z, trend_dir)
                    if (ONLY_TREND and not aligned) or sc < MIN_SCORE:
                        pass
                    else:
                        z.waiting = True
                        z.wait_start = i
                        z.touch_ext = b["l"] if dem else b["h"]
                        events.append((i, f"👀 {name}: giá chạm {z.kind()} · {sc}/7 {band(sc)}",
                                       f"Hộp {fmt(z.bot)} – {fmt(z.top)} · lần chạm {z.touches}\n"
                                       f"Giá hiện tại {fmt(b['c'])}\n{crit_txt(c, z.d5)}\n"
                                       f"→ Chờ nến đóng cửa {'TRÊN ' + fmt(z.top) if dem else 'DƯỚI ' + fmt(z.bot)} để vào lệnh (tối đa {CONFIRM_BARS} nến).",
                                       3, ["eyes"]))
                else:
                    z.max_ext = max(z.max_ext, (b["h"] - z.top) if dem else (z.bot - b["l"]))
                    if (b["h"] > z.prior) if dem else (b["l"] < z.prior):
                        z.bos = True
            if z.waiting and not z.dead:
                z.touch_ext = min(z.touch_ext, b["l"]) if dem else max(z.touch_ext, b["h"])
                if (b["c"] < z.bot - SL_BUF * z.a0) if dem else (b["c"] > z.top + SL_BUF * z.a0):
                    z.dead = True
                    z.waiting = False
                elif ONLY_TREND and not ((dem and trend_now == 1) or (not dem and trend_now == -1)):
                    z.waiting = False
                elif (b["c"] > z.top) if dem else (b["c"] < z.bot):
                    c, sc, _ = score_now(z, trend_now)
                    z.waiting = False
                    z.entry = b["c"]
                    z.sl = (min(z.touch_ext, z.bot) - SL_BUF * z.a0) if dem else (max(z.touch_ext, z.top) + SL_BUF * z.a0)
                    z.risk = abs(z.entry - z.sl)
                    if sc >= MIN_SCORE and find_tp(z, i):
                        z.in_trade = True
                        z.crit, z.score = c, sc
                        events.append((i, f"{'🚀 ' + name + ': VÀO LỆNH MUA' if dem else '🔻 ' + name + ': VÀO LỆNH BÁN'} · {sc}/7",
                                       f"Vào {fmt(z.entry)}\nSL {fmt(z.sl)}\n"
                                       f"Chốt lời {fmt(z.tp)} ({z.rr:.1f}R, {'trước hộp đối diện' if z.tp_box else 'không có hộp phía trước'})\n"
                                       f"{z.kind()} {fmt(z.bot)} – {fmt(z.top)} · lần chạm {z.touches}\n{crit_txt(c, z.d5)}",
                                       5, ["rotating_light"]))
                elif i - z.wait_start >= CONFIRM_BARS:
                    z.waiting = False
                    if z.touches >= MAX_TOUCH:
                        z.dead = True
        zones = [z for z in zones if not z.dead]

    # Tóm tắt trạng thái hiện tại
    trend_txt = {1: "TĂNG 🟢", -1: "GIẢM 🔴", 0: "CÂN NHẮC / chưa rõ ⚠️"}[st]
    last = bars[-1]
    lines = [f"Giá {fmt(last['c'])} (nến {last['t']:%d/%m %H:%M} UTC)", f"Xu hướng: {trend_txt}"]
    if key_low is not None:
        lines.append(f"Đáy gần nhất: {fmt(key_low)}")
    if key_high is not None:
        lines.append(f"Đỉnh gần nhất: {fmt(key_high)}")
    near = sorted(zones, key=lambda z: min(abs(last["c"] - z.top), abs(last["c"] - z.bot)))[:4]
    if near:
        lines.append("Hộp gần giá:")
        for z in near:
            _, sc, _ = score_now(z, st)
            lines.append(f" • {z.kind()} {fmt(z.bot)} – {fmt(z.top)} · {sc}/7")
    summary = "\n".join(lines)
    return events, summary


# ═════════════ GỬI NTFY & TRẠNG THÁI ═════════════
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
    test = "--test" in sys.argv
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    key = os.environ.get("TWELVE_KEY", "").strip()
    if not topic:
        sys.exit("Thiếu NTFY_TOPIC")
    state = load_state()
    first_run = not state
    errors = []

    for name, source, symbol in SYMBOLS:
        try:
            if source == "twelve":
                if not key:
                    raise RuntimeError("thiếu TWELVE_KEY")
                bars = fetch_twelve(symbol, key)
            else:
                bars = fetch_binance(symbol)
            if len(bars) < 300:
                raise RuntimeError(f"chỉ lấy được {len(bars)} nến")
            events, summary = simulate(name, bars)
        except Exception as e:  # một mã lỗi không làm hỏng các mã khác
            errors.append(f"{name}: {e}")
            print(f"[{name}] LỖI: {e}")
            continue

        last_t = bars[-1]["t"]
        seen = state.get(symbol)
        seen_t = dt.datetime.fromisoformat(seen) if seen else None
        print(f"[{name}] {len(bars)} nến, nến cuối {last_t}, {len(events)} sự kiện trong lịch sử")

        if test or first_run or seen_t is None:
            ntfy(topic, f"📊 {name}: tóm tắt hiện tại", summary, 2, ["bar_chart"])
        else:
            cutoff_i = max(0, len(bars) - MAX_CATCHUP)
            new = [e for e in events if bars[e[0]]["t"] > seen_t and e[0] >= cutoff_i]
            for i, title, msg, prio, tags in new:
                ntfy(topic, title, f"{msg}\n🕐 Nến {bars[i]['t']:%d/%m %H:%M} UTC ({(bars[i]['t'] + dt.timedelta(hours=7)):%H:%M} giờ VN)", prio, tags)
                print(f"  → đã gửi: {title}")
        if not test:
            state[symbol] = last_t.isoformat()

    if test:
        ntfy(topic, "✅ Hệ thống hộp đã kết nối", "Tin thử từ GitHub. Bạn sẽ nhận thông báo Vàng, BTC, ETH tại đây.", 3, ["white_check_mark"])
    if errors:
        ntfy(topic, "❗ Lỗi hệ thống hộp", "\n".join(errors), 3, ["x"])
    if not test:
        save_state(state)


if __name__ == "__main__":
    main()
