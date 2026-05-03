"""
Range Scalp Engine v2 — CryptoDesk
Logic: Chỉ range scalp khi D1+H4 KHÔNG downtrend mạnh + coin thực sự sideway
"""
from core.indicators import prepare, detect_exhaustion_short
from core.binance    import fetch_klines, fetch_funding_rate, fetch_oi_change, fetch_btc_context
from core.utils      import smart_round

# ─────────────────────────────────────────────
# Helper: Trend bias từ MA
# ─────────────────────────────────────────────

def _get_trend_bias(df, label=""):
    """
    Đọc D1 hoặc H4 bias dựa trên MA34/MA89/MA200 và slope.
    Trả về: 'DOWNTREND', 'UPTREND', 'NEUTRAL'
    """
    if len(df) < 10:
        return "NEUTRAL"

    row   = df.iloc[-1]
    price = float(row["close"])

    ma34  = float(row.get("ma34",  0) or 0)
    ma89  = float(row.get("ma89",  0) or 0)
    ma200 = float(row.get("ma200", 0) or 0)

    # Slope MA34 trong 5 nến
    if len(df) >= 6 and ma34 > 0:
        ma34_prev = float(df.iloc[-6].get("ma34", ma34) or ma34)
        slope34   = (ma34 - ma34_prev) / ma34_prev * 100
    else:
        slope34 = 0

    # Đếm điều kiện downtrend
    down_count = 0
    up_count   = 0

    if ma34 > 0 and price < ma34:  down_count += 1
    else:                           up_count   += 1

    if ma89 > 0 and ma34 > 0 and ma34 < ma89:  down_count += 1
    else:                                        up_count   += 1

    if ma200 > 0 and price < ma200: down_count += 1
    else:                            up_count   += 1

    if slope34 < -0.3:  down_count += 1  # MA34 đang dốc xuống
    elif slope34 > 0.3: up_count   += 1

    # Ngưỡng 2/4: nhạy hơn, bắt trend sớm hơn
    # (3/4 trước đây bỏ sót KITE H4 chỉ có 2 điều kiện downtrend)
    if down_count >= 2:
        return "DOWNTREND"
    elif up_count >= 2:
        return "UPTREND"
    return "NEUTRAL"


def _count_range_touches(closes, highs, lows, range_high, range_low, touch_zone=0.18, min_gap=3):
    """
    Đếm số lần giá thực sự chạm biên range (không liên tiếp).
    Đây là điều kiện then chốt để phân biệt range tích lũy vs bounce/recovery.
    - touch_zone: 18% của range_size tính là "vào vùng biên"
    - min_gap: ít nhất 3 nến H1 giữa 2 lần chạm (tránh đếm cùng 1 run)
    """
    rng         = range_high - range_low
    top_zone    = range_high - rng * touch_zone
    bottom_zone = range_low  + rng * touch_zone

    top_touches = bottom_touches = 0
    last_top = last_bottom = -100

    for i in range(len(closes)):
        h = float(highs.iloc[i])
        l = float(lows.iloc[i])

        # Chạm đỉnh range: high của nến vào vùng top
        if h >= top_zone and i - last_top >= min_gap:
            top_touches += 1
            last_top = i

        # Chạm đáy range: low của nến vào vùng bottom
        if l <= bottom_zone and i - last_bottom >= min_gap:
            bottom_touches += 1
            last_bottom = i

    return top_touches, bottom_touches


def _is_bimodal(closes, n_bins=10, max_gap_pct=3.0):
    """
    Phát hiện giá có 2 cụm riêng biệt (bimodal distribution).
    Trường hợp điển hình: coin sideway cao → dump mạnh → sideway thấp.
    Nếu bimodal → KHÔNG phải range tích lũy, là 2 price regime khác nhau.
    
    BTR: sideway 0.18 → dump → sideway 0.15 → gap 20% → BLOCK
    ARB: sideway đều trong 0.10–0.105 → gap ~0% → PASS
    """
    import numpy as np
    arr = np.array([float(x) for x in closes])
    if len(arr) < 8:
        return False, 0

    mn, mx = arr.min(), arr.max()
    if mx == mn:
        return False, 0

    bins   = np.linspace(mn, mx, n_bins + 1)
    counts, _ = np.histogram(arr, bins=bins)
    bin_width  = (mx - mn) / n_bins

    # Tìm 2 bin có frequency cao nhất
    top2_idx = sorted(counts.argsort()[-2:])
    gap_bins = abs(top2_idx[1] - top2_idx[0]) - 1  # bins trống ở giữa
    gap_pct  = gap_bins * bin_width / mn * 100 if mn > 0 else 0

    # Bimodal nếu 2 cụm cách nhau > max_gap_pct
    return gap_pct > max_gap_pct, round(gap_pct, 2)


def _detect_range(df_h1, df_m15, df_h4, lookback_h1=120, lookback_m15=32):
    """
    Detect range THẬT — dùng H1 5 ngày (120 nến) để thấy range tích lũy dài hạn.
    Phân biệt:
      ✅ ARB range 12%: 0.098–0.110, touch 6+ lần trong 2 tuần → PASS
      ❌ PTB bounce: dump 1 chiều, drift lớn → BLOCK
      ❌ BTR dump: bimodal 2 regime → BLOCK
    """
    # ── H4 ATR spike check ──
    h4_recent    = df_h4.iloc[-20:]
    h4_atr_mean  = float(h4_recent["atr"].mean()) if "atr" in h4_recent.columns else 0
    h4_atr_last  = float(df_h4["atr"].iloc[-1])   if "atr" in df_h4.columns else 0
    h4_atr_ratio = h4_atr_last / h4_atr_mean if h4_atr_mean > 0 else 1

    if h4_atr_ratio > 1.8:
        return 0, 0, 0, False, h4_atr_ratio, 0, 0, False, 0.0

    # ── Range biên từ H1 (24 nến = 1 ngày) ──
    h1_recent = df_h1.iloc[-lookback_h1:]
    h1_highs  = h1_recent["high"].astype(float)
    h1_lows   = h1_recent["low"].astype(float)
    h1_closes = h1_recent["close"].astype(float)

    range_high = float(h1_highs.quantile(0.92))
    range_low  = float(h1_lows.quantile(0.08))
    range_pct  = (range_high - range_low) / range_low * 100 if range_low > 0 else 0

    price = float(df_h1["close"].iloc[-1])

    # ── ATR H1 ──
    h1_atr_mean = float(h1_recent["atr"].mean()) if "atr" in h1_recent.columns else 0
    h1_atr_last = float(df_h1["atr"].iloc[-1])   if "atr" in df_h1.columns else 0
    atr_ratio   = h1_atr_last / h1_atr_mean if h1_atr_mean > 0 else 1

    # ── Drift check: linear regression slope để detect downtrend kéo dài ──
    # Ví dụ KITE: 0.223 → 0.201 trong 5 ngày — close[0] vs close[-1] có thể miss
    # Linear regression slope cho kết quả chính xác hơn
    import numpy as _np
    closes_arr  = _np.array([float(x) for x in h1_closes])
    n_pts       = len(closes_arr)
    x_arr       = _np.arange(n_pts)
    slope_coef  = _np.polyfit(x_arr, closes_arr, 1)[0]       # USD/nến
    slope_pct   = abs(slope_coef) / closes_arr.mean() * 100  # % mỗi nến
    total_drift = slope_pct * n_pts                           # tổng drift % ước tính

    # drift_pct: tổng % dịch chuyển ước tính theo linear regression
    drift_pct   = total_drift

    # Cũng tính H1 MA34 slope (10 nến vs 35 nến trước) để phát hiện downtrend kéo dài
    h1_ma34_now  = float(h1_closes.iloc[-10:].mean())   # MA gần nhất (approx)
    h1_ma34_prev = float(h1_closes.iloc[-44:-10].mean()) if n_pts >= 44 else float(h1_closes.iloc[:max(1,n_pts//3)].mean())
    ma34_slope_pct = (h1_ma34_now - h1_ma34_prev) / h1_ma34_prev * 100 if h1_ma34_prev > 0 else 0

    # ── Bimodal check: loại trường hợp có 2 price regime khác nhau ──
    # BTR: dump từ 0.18 → 0.15 tạo 2 cụm tách biệt → BLOCK
    # ARB: sideway đều trong 1 vùng → không bimodal → PASS
    is_bimodal, bimodal_gap = _is_bimodal(h1_closes)

    # ── Touch count: phải chạm biên 2+ lần mỗi phía ──
    # Đây là filter quan trọng nhất: loại PTB (bounce 1 lần), giữ ARB (lặp nhiều lần)
    top_touches, bottom_touches = _count_range_touches(
        h1_closes, h1_highs, h1_lows, range_high, range_low
    )

    price_in_range = range_low <= price <= range_high

    # Range rộng >= 10% (kiểu ARB): tích lũy dài hạn, chỉ cần touch >= 1
    # Range hẹp  <  10%: tích lũy ngắn hạn, cần touch >= 2
    min_touches = 1 if range_pct >= 10.0 else 2

    # MA34 H1 đang dốc mạnh → coin đang trend, không phải range
    ma34_trending = abs(ma34_slope_pct) > 4.0   # > 4% drift MA34 trong lookback = trend rõ

    is_ranging = (
        range_pct >= 1.5            # đủ rộng để có lãi
        and range_pct <= 15.0       # nới lên 15% để bắt ARB-type range (~12%)
        and atr_ratio < 1.8         # H1 ATR không spike quá mạnh
        and drift_pct < 5.0         # linear regression drift < 5%
        and not ma34_trending       # MA34 H1 không đang trend mạnh (KITE bị catch ở đây)
        and price_in_range
        and h4_atr_ratio < 1.8
        and not is_bimodal          # không có 2 price regime riêng biệt
        and top_touches >= min_touches
        and bottom_touches >= min_touches
    )

    return range_high, range_low, round(range_pct, 2), is_ranging, round(atr_ratio, 2), top_touches, bottom_touches, is_bimodal, bimodal_gap


def _candle_reversal(df_h1, direction):
    """Nến đảo chiều H1 — chặt hơn: chỉ chấp nhận nến rõ ràng."""
    if len(df_h1) < 3:
        return False, "Không đủ data H1"

    c1 = df_h1.iloc[-1]
    c2 = df_h1.iloc[-2]

    o1, h1_high, l1, cl1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, cl2 = float(c2["open"]), float(c2["close"])
    body1         = abs(cl1 - o1)
    candle_range1 = h1_high - l1

    if candle_range1 == 0:
        return False, "Nến H1 không hợp lệ"

    body_ratio = body1 / candle_range1

    if direction == "LONG":
        lower_wick = (o1 - l1) if cl1 >= o1 else (cl1 - l1)
        wick_ratio = lower_wick / candle_range1
        # Hammer rõ ràng: wick dưới ≥ 55% và nến xanh
        if wick_ratio >= 0.55 and cl1 >= o1:
            return True, "Hammer H1 (wick dưới {:.0f}%)".format(wick_ratio * 100)
        # Bullish engulfing
        if cl1 > o2 and o1 <= cl2 and cl2 <= o2 and body_ratio > 0.5:
            return True, "Bullish Engulfing H1"
        return False, "Chưa có nến đảo chiều bullish rõ ràng"
    else:
        upper_wick = (h1_high - o1) if cl1 <= o1 else (h1_high - cl1)
        wick_ratio = upper_wick / candle_range1
        if wick_ratio >= 0.55 and cl1 <= o1:
            return True, "Shooting Star H1 (wick trên {:.0f}%)".format(wick_ratio * 100)
        if cl1 < o2 and o1 >= cl2 and cl2 >= o2 and body_ratio > 0.5:
            return True, "Bearish Engulfing H1"
        return False, "Chưa có nến đảo chiều bearish rõ ràng"


def _btc_allows(btc_ctx, direction):
    if not btc_ctx:
        return True, ""
    sentiment = btc_ctx.get("sentiment", "NEUTRAL")
    if direction == "LONG"  and sentiment in ("DUMP", "RISK_OFF"):
        return False, "BTC {} — không Long range".format(sentiment)
    if direction == "SHORT" and sentiment in ("PUMP", "RISK_ON"):
        return False, "BTC {} — không Short range".format(sentiment)
    return True, ""


def _btc_pump_blocks_range(btc_ctx, direction):
    """
    Fix #1: Block Range mode khi BTC đang pump/dump mạnh.
    - BTC pump > 2%/24h → block LONG (altcoin chạy theo trend, không range)
    - BTC dump > 2%/24h → block LONG (downtrend mạnh, đừng bắt đáy)
    - SHORT khi BTC pump/dump mạnh vẫn OK (thuận trend ngắn hạn)
    Case 18/3: BTC pump → 29 loss LONG → cần hard block.
    """
    if not btc_ctx:
        return False, ""

    chg_24h   = btc_ctx.get("chg_24h") or 0
    sentiment = btc_ctx.get("sentiment", "NEUTRAL")

    # Chỉ block LONG khi BTC pump mạnh
    if direction == "LONG":
        if chg_24h > 2.0:
            return True, f"BTC pump +{chg_24h:.1f}% / 24h — thị trường trending, range LONG không hiệu quả"
        if chg_24h < -2.0:
            return True, f"BTC dump {chg_24h:.1f}% / 24h — tránh Long khi BTC đang downtrend"

    # Block SHORT khi BTC dump quá mạnh (< -3%) + RISK_OFF — altcoin sẽ dump theo, không range
    if direction == "SHORT" and chg_24h < -3.0 and sentiment in ("DUMP", "RISK_OFF"):
        return True, f"BTC DUMP mạnh {chg_24h:.1f}% — altcoin dump tự do, range Short không có đáy"

    return False, ""


def _coin_btc_correlation(df_h4_coin, btc_ctx):
    """
    Fix #2: Check correlation coin vs BTC trong 4H gần nhất.
    Nếu BTC tăng mạnh mà coin không tăng theo → coin underperform → không Long.
    Case: BTC +2%/4h, BERA -0.5% → BERA yếu, Long sẽ thua.
    """
    if not btc_ctx or len(df_h4_coin) < 3:
        return "NEUTRAL", 0, 0

    # Coin chg 4h (so nến hiện tại vs nến 4 tiếng trước)
    coin_now  = float(df_h4_coin["close"].iloc[-1])
    coin_4h   = float(df_h4_coin["close"].iloc[-2]) if len(df_h4_coin) >= 2 else coin_now
    coin_chg_4h = round((coin_now - coin_4h) / coin_4h * 100, 2) if coin_4h > 0 else 0

    # BTC chg 4h từ context (dùng chg_24h / 6 để ước tính 4h)
    btc_chg_24h = btc_ctx.get("chg_24h") or 0
    btc_chg_4h  = round(btc_chg_24h / 6, 2)  # ước tính

    # Tính relative strength: coin vs BTC
    # Nếu btc_chg_4h > 1% mà coin_chg_4h < 0.3% → underperform
    underperform_long  = btc_chg_4h >  1.0 and coin_chg_4h <  0.3
    underperform_short = btc_chg_4h < -1.0 and coin_chg_4h > -0.3

    if underperform_long:
        return "UNDERPERFORM_LONG",  coin_chg_4h, btc_chg_4h
    if underperform_short:
        return "UNDERPERFORM_SHORT", coin_chg_4h, btc_chg_4h

    # Coin mạnh hơn BTC đáng kể → có momentum riêng
    if coin_chg_4h > btc_chg_4h + 1.5:
        return "OUTPERFORM_BULL",    coin_chg_4h, btc_chg_4h
    if coin_chg_4h < btc_chg_4h - 1.5:
        return "OUTPERFORM_BEAR",    coin_chg_4h, btc_chg_4h

    return "NEUTRAL", coin_chg_4h, btc_chg_4h


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────


def _fibo_levels(entry, sl, direction, range_high, range_low):
    """
    Tính các mức TP dựa trên Fibonacci Extension + Range target.
    Dùng swing (SL → Entry) làm base leg để project extension.

    Levels:
      TP1 = Fibo 1.0  (100% của move SL→Entry)  — an toàn
      TP2 = Fibo 1.618 (Golden ratio extension)  — target lý tưởng
      TP3 = Fibo 2.618 (nếu breakout range)       — aggressive
      Range TP = đỉnh/đáy range × 0.985          — target trong range
    """
    base = abs(entry - sl)   # khoảng cách SL→Entry = 1 leg

    if direction == "LONG":
        tp_f100  = round(entry + base * 1.0,   8)
        tp_f1618 = round(entry + base * 1.618, 8)
        tp_f2618 = round(entry + base * 2.618, 8)
        tp_range = round(range_high * 0.985,   8)   # sát đỉnh range
        tp_range2= round(range_high,            8)   # đỉnh range (aggressive)
    else:
        tp_f100  = round(entry - base * 1.0,   8)
        tp_f1618 = round(entry - base * 1.618, 8)
        tp_f2618 = round(entry - base * 2.618, 8)
        tp_range = round(range_low * 1.015,    8)   # sát đáy range
        tp_range2= round(range_low,             8)

    sl_pct  = round(abs(entry - sl)   / entry * 100, 2)
    rr100   = round(abs(tp_f100 - entry) / abs(entry - sl), 2) if sl > 0 else 0
    rr1618  = round(abs(tp_f1618- entry) / abs(entry - sl), 2) if sl > 0 else 0
    rr_range= round(abs(tp_range- entry) / abs(entry - sl), 2) if sl > 0 else 0

    return {
        "tp_fibo_100":  tp_f100,   "rr_fibo_100":  rr100,
        "tp_fibo_1618": tp_f1618,  "rr_fibo_1618": rr1618,
        "tp_fibo_2618": tp_f2618,
        "tp_range":     tp_range,  "rr_range":     rr_range,
        "tp_range_full":tp_range2,
        "fibo_note":    "TP1=Fibo1.0 (an toàn) | TP2=Fibo1.618 (lý tưởng) | TP3=Fibo2.618 (breakout)",
    }


def _market_structure(df_h1, df_h4, df_d1, symbol, force_futures=False):
    """
    Phân tích cấu trúc thị trường để xác định Range vs Breakout/Trend.
    Trả về dict với EMA alignment, pullback depth, higher/lower highs.
    """
    # ── EMA alignment từ H1 ──
    row_h1   = df_h1.iloc[-1]
    price    = float(row_h1["close"])
    ema7     = float(df_h1["close"].rolling(7,  min_periods=1).mean().iloc[-1])
    ema34    = float(df_h1["close"].rolling(34, min_periods=1).mean().iloc[-1])
    ema89    = float(df_h1["close"].rolling(89, min_periods=1).mean().iloc[-1])
    ema200   = float(df_h1["close"].rolling(200,min_periods=1).mean().iloc[-1])

    # EMA slopes (% change trong 5 nến)
    ema7_prev  = float(df_h1["close"].rolling(7,  min_periods=1).mean().iloc[-6]) if len(df_h1) >= 7  else ema7
    ema34_prev = float(df_h1["close"].rolling(34, min_periods=1).mean().iloc[-6]) if len(df_h1) >= 35 else ema34
    ema7_slope  = round((ema7  - ema7_prev)  / ema7_prev  * 100, 3) if ema7_prev  > 0 else 0
    ema34_slope = round((ema34 - ema34_prev) / ema34_prev * 100, 3) if ema34_prev > 0 else 0

    # EMA alignment score
    # Bullish: price > ema7 > ema34 > ema89
    # Bearish: price < ema7 < ema34 < ema89
    bull_pts = sum([price > ema7, ema7 > ema34, ema34 > ema89, ema7_slope > 0.1, ema34_slope > 0.05])
    bear_pts = sum([price < ema7, ema7 < ema34, ema34 < ema89, ema7_slope < -0.1, ema34_slope < -0.05])

    if bull_pts >= 4:
        ema_align = "BULLISH"
        ema_note  = "EMA7>EMA34>EMA89 dốc lên — uptrend, tránh Short"
    elif bear_pts >= 4:
        ema_align = "BEARISH"
        ema_note  = "EMA7<EMA34<EMA89 dốc xuống — downtrend, tránh Long"
    elif bull_pts >= 2 and ema7_slope > 0:
        ema_align = "WEAK_BULL"
        ema_note  = "EMAs đang sắp xếp tăng nhẹ — range chuyển sang uptrend"
    elif bear_pts >= 2 and ema7_slope < 0:
        ema_align = "WEAK_BEAR"
        ema_note  = "EMAs đang sắp xếp giảm nhẹ — range chuyển sang downtrend"
    else:
        ema_align = "FLAT"
        ema_note  = "EMA7≈EMA34 flat — sideway range thật, Long/Short đều xét được"

    # ── Pullback depth: đo pullback từ đỉnh gần nhất (20 nến) ──
    recent_20   = df_h1.iloc[-20:]
    swing_high  = float(recent_20["high"].max())
    swing_low   = float(recent_20["low"].min())
    current_low = float(df_h1["low"].iloc[-1])

    # Pullback từ đỉnh = (swing_high - current_price) / swing_high
    pullback_from_high = round((swing_high - price) / swing_high * 100, 2)

    # Kiểm tra EMA34 có bị phá không trong pullback
    ema34_breached = price < ema34

    # ── Higher High / Lower High (10 nến vs 10 nến trước) ──
    prev_10_high = float(df_h1.iloc[-20:-10]["high"].max()) if len(df_h1) >= 20 else swing_high
    curr_10_high = float(df_h1.iloc[-10:]["high"].max())
    prev_10_low  = float(df_h1.iloc[-20:-10]["low"].min())  if len(df_h1) >= 20 else swing_low
    curr_10_low  = float(df_h1.iloc[-10:]["low"].min())

    hh = curr_10_high > prev_10_high * 1.005   # Higher High (tăng > 0.5%)
    lh = curr_10_high < prev_10_high * 0.995   # Lower High (giảm > 0.5%)
    hl = curr_10_low  > prev_10_low  * 1.005   # Higher Low
    ll = curr_10_low  < prev_10_low  * 0.995   # Lower Low

    if hh and hl:
        hh_pattern = "HH+HL"
        hh_note    = "Higher High + Higher Low — uptrend rõ, Short nguy hiểm"
    elif lh and ll:
        hh_pattern = "LH+LL"
        hh_note    = "Lower High + Lower Low — downtrend rõ, Long nguy hiểm"
    elif hh:
        hh_pattern = "HH"
        hh_note    = "Higher High — áp lực mua mạnh"
    elif lh:
        hh_pattern = "LH"
        hh_note    = "Lower High — áp lực bán"
    else:
        hh_pattern = "EQUAL"
        hh_note    = "Đỉnh ngang — sideway range"

    # ── Tổng hợp verdict: nên LONG, SHORT hay Range cả 2 ──
    short_risks = 0
    long_risks  = 0

    if ema_align in ("BULLISH", "WEAK_BULL"):   short_risks += 2
    if ema_align in ("BEARISH", "WEAK_BEAR"):   long_risks  += 2
    if hh_pattern in ("HH+HL", "HH"):           short_risks += 1
    if hh_pattern in ("LH+LL", "LH"):           long_risks  += 1
    if pullback_from_high < 2.0:                short_risks += 1  # đang gần đỉnh, dễ reversal
    if not ema34_breached and ema_align == "BULLISH": short_risks += 1  # giá vẫn trên EMA34

    if short_risks >= 3:
        verdict     = "LONG_ONLY"
        verdict_note= "Cấu trúc UPTREND — chỉ Long pullback, KHÔNG Short đỉnh"
        verdict_col = "ok"
    elif long_risks >= 3:
        verdict     = "SHORT_ONLY"
        verdict_note= "Cấu trúc DOWNTREND — chỉ Short hồi, KHÔNG Long đáy"
        verdict_col = "danger"
    else:
        verdict     = "BOTH_OK"
        verdict_note= "Cấu trúc RANGE — có thể Long đáy + Short đỉnh"
        verdict_col = "info"

    return {
        "ema7":    round(ema7,  8),
        "ema34":   round(ema34, 8),
        "ema89":   round(ema89, 8),
        "ema200":  round(ema200,8),
        "ema7_slope":    ema7_slope,
        "ema34_slope":   ema34_slope,
        "ema_align":     ema_align,
        "ema_note":      ema_note,
        "pullback_from_high": pullback_from_high,
        "swing_high":    round(swing_high, 8),
        "ema34_breached":ema34_breached,
        "hh_pattern":    hh_pattern,
        "hh_note":       hh_note,
        "bull_pts":      bull_pts,
        "bear_pts":      bear_pts,
        "verdict":       verdict,
        "verdict_note":  verdict_note,
        "verdict_col":   verdict_col,
    }


def _btc_volume_trend(n_candles=8):
    """
    Phân tích BTC volume H1 để xác định trend momentum.
    So sánh volume nến xanh vs đỏ trong n nến gần nhất.
    """
    try:
        df = fetch_klines("BTCUSDT", "1h", n_candles + 4, force_futures=False)
        recent = df.iloc[-n_candles:]
        closes = recent["close"].astype(float)
        opens  = recent["open"].astype(float)
        vols   = recent["volume"].astype(float)

        bull_vol = float(vols[closes >= opens].sum())
        bear_vol = float(vols[closes <  opens].sum())
        total    = bull_vol + bear_vol
        bull_pct = round(bull_vol / total * 100, 1) if total > 0 else 50

        # Volume trend: so nửa đầu vs nửa sau
        half = n_candles // 2
        vol_early = float(vols.iloc[:half].mean())
        vol_late  = float(vols.iloc[half:].mean())
        vol_slope = round((vol_late - vol_early) / vol_early * 100, 1) if vol_early > 0 else 0

        # BTC price change trong n nến
        btc_chg = round((float(closes.iloc[-1]) - float(closes.iloc[0])) / float(closes.iloc[0]) * 100, 2)

        if bull_pct >= 65 and btc_chg > 0.5:
            vol_bias  = "BULL_DOMINANT"
            vol_note  = f"BTC volume xanh {bull_pct}% — momentum mua, tránh Short altcoin"
        elif bull_pct <= 35 and btc_chg < -0.5:
            vol_bias  = "BEAR_DOMINANT"
            vol_note  = f"BTC volume đỏ {100-bull_pct:.0f}% — momentum bán, tránh Long altcoin"
        elif vol_slope > 20 and btc_chg > 0:
            vol_bias  = "RISING_BULL"
            vol_note  = f"BTC volume tăng +{vol_slope}% & giá tăng — breakout đang xảy ra"
        elif vol_slope > 20 and btc_chg < 0:
            vol_bias  = "RISING_BEAR"
            vol_note  = f"BTC volume tăng +{vol_slope}% & giá giảm — selling pressure mạnh"
        else:
            vol_bias  = "NEUTRAL"
            vol_note  = f"BTC volume cân bằng ({bull_pct}% xanh) — không có bias rõ"

        return {
            "bull_vol_pct":  bull_pct,
            "bear_vol_pct":  round(100 - bull_pct, 1),
            "vol_slope":     vol_slope,
            "btc_chg_8h":    btc_chg,
            "vol_bias":      vol_bias,
            "vol_note":      vol_note,
            "n_candles":     n_candles,
        }
    except Exception as e:
        return {"vol_bias": "UNKNOWN", "vol_note": str(e)[:60],
                "bull_vol_pct": 50, "bear_vol_pct": 50, "vol_slope": 0, "btc_chg_8h": 0}


def range_analyze(symbol: str, cfg: dict) -> dict:
    ff = bool(cfg.get("force_futures", False))

    df_d1  = prepare(fetch_klines(symbol, "1d",  30, force_futures=ff))
    df_h4  = prepare(fetch_klines(symbol, "4h",  60, force_futures=ff))
    df_h1  = prepare(fetch_klines(symbol, "1h",  72, force_futures=ff))
    df_m15 = prepare(fetch_klines(symbol, "15m", 64, force_futures=ff))

    for df in [df_d1, df_h4, df_h1, df_m15]:
        if len(df) < 10:
            raise ValueError(f"Không đủ data cho {symbol}")

    price     = float(df_h1["close"].iloc[-1])
    funding   = fetch_funding_rate(symbol)
    oi_change = fetch_oi_change(symbol)
    btc_ctx   = fetch_btc_context()

    # ── Market structure + BTC volume analysis ──
    ms        = _market_structure(df_h1, df_h4, df_d1, symbol)
    btc_vol   = _btc_volume_trend(n_candles=8)

    # ── Fix #1: BTC pump/dump mạnh → block Range mode ──
    btc_pump_block, btc_pump_msg = _btc_pump_blocks_range(btc_ctx, "LONG")  # check cho LONG trước

    # ── Fix #2: Coin correlation vs BTC ──
    corr_status, coin_chg_4h, btc_chg_4h = _coin_btc_correlation(df_h4, btc_ctx)

    # ══════════════════════════════════════════
    # FILTER 1: D1 trend — không Long khi D1 downtrend
    # ══════════════════════════════════════════
    d1_bias = _get_trend_bias(df_d1, "D1")
    h4_bias = _get_trend_bias(df_h4, "H4")

    trend_block    = False
    trend_block_msg = ""

    if d1_bias == "DOWNTREND" and h4_bias == "DOWNTREND":
        trend_block     = True
        trend_block_dir = "LONG"
        trend_block_msg = "D1+H4 đều DOWNTREND — chỉ xem xét SHORT tại đỉnh range"
    elif d1_bias == "DOWNTREND":
        trend_block     = True
        trend_block_dir = "LONG"
        trend_block_msg = "D1 DOWNTREND — không Long range (countertrend)"
    elif h4_bias == "DOWNTREND":
        # H4 downtrend đủ để block LONG range (KITE case)
        trend_block     = True
        trend_block_dir = "LONG"
        trend_block_msg = "H4 DOWNTREND — không Long range, chỉ Short tại đỉnh"
    elif d1_bias == "UPTREND" and h4_bias == "UPTREND":
        trend_block     = True
        trend_block_dir = "SHORT"
        trend_block_msg = "D1+H4 UPTREND — không Short range"
    elif h4_bias == "UPTREND":
        trend_block     = True
        trend_block_dir = "SHORT"
        trend_block_msg = "H4 UPTREND — không Short range"

    # ══════════════════════════════════════════
    # FILTER 2: Override range tay
    # ══════════════════════════════════════════
    override = cfg.get("range_override", {}).get(symbol, {})
    if override.get("range_high") and override.get("range_low"):
        range_high   = float(override["range_high"])
        range_low    = float(override["range_low"])
        range_pct    = (range_high - range_low) / range_low * 100
        is_ranging   = range_low <= price <= range_high
        atr_ratio      = 1.0
        range_source   = "manual"
        top_touches    = 99  # manual override — assume valid
        bottom_touches = 99
        is_bimodal     = False  # manual = user đã xác nhận range
        bimodal_gap    = 0.0
    else:
        range_high, range_low, range_pct, is_ranging, atr_ratio, top_touches, bottom_touches, is_bimodal, bimodal_gap = _detect_range(df_h1, df_m15, df_h4)
        range_source = "auto"

    # ══════════════════════════════════════════
    # Không ranging → WAIT
    # ══════════════════════════════════════════
    if not is_ranging:
        warn = "Coin không sideway (range {:.1f}%, ATR H1 {:.1f}x, drift quá lớn hoặc H4 đang breakout)".format(
            range_pct, atr_ratio)
        return {
            "symbol": symbol, "strategy": "RANGE_SCALP", "direction": "WAIT",
            "confidence": "LOW", "price": smart_round(price),
            "range_high": smart_round(range_high), "range_low": smart_round(range_low),
            "range_pct": range_pct, "range_source": range_source,
            "d1_bias": d1_bias, "h4_bias": h4_bias,
            "top_touches": top_touches, "bottom_touches": bottom_touches,
            "is_bimodal": is_bimodal, "bimodal_gap": bimodal_gap,
            "market_structure": ms if "ms" in dir() else None,
            "btc_volume": btc_vol if "btc_vol" in dir() else None,
            "warnings": [warn], "conditions": [],
        }

    # ══════════════════════════════════════════
    # Vị trí trong range
    # ══════════════════════════════════════════
    range_size  = range_high - range_low
    zone_pct    = 0.20
    bottom_zone = range_low  + range_size * zone_pct
    top_zone    = range_high - range_size * zone_pct

    m15_4h          = df_m15.iloc[-16:]
    m15_entry_long  = float(m15_4h["low"].min())
    m15_entry_short = float(m15_4h["high"].max())

    at_bottom = price <= bottom_zone
    at_top    = price >= top_zone

    if at_bottom:
        direction = "LONG"
    elif at_top:
        direction = "SHORT"
    else:
        mid_pct       = (price - range_low) / range_size * 100
        dist_to_long  = round((price - bottom_zone) / price * 100, 1)
        dist_to_short = round((top_zone - price) / price * 100, 1)
        return {
            "symbol": symbol, "strategy": "RANGE_SCALP", "direction": "WAIT",
            "confidence": "LOW", "price": smart_round(price),
            "range_high": smart_round(range_high), "range_low": smart_round(range_low),
            "range_pct": range_pct, "range_source": range_source,
            "d1_bias": d1_bias, "h4_bias": h4_bias,
            "bottom_zone": smart_round(bottom_zone), "top_zone": smart_round(top_zone),
            "m15_entry_long": smart_round(m15_entry_long),
            "m15_entry_short": smart_round(m15_entry_short),
            "dist_to_long": dist_to_long, "dist_to_short": dist_to_short,
            "warnings": ["Giá giữa range ({:.0f}% từ đáy) — cách Long -{:.1f}%, cách Short +{:.1f}%".format(
                mid_pct, dist_to_long, dist_to_short)],
            "conditions": ["H1 Range {:.1f}%".format(range_pct)],
        }

    # ══════════════════════════════════════════
    # FILTER 2b: BTC pump block — thêm sau override, trước trend block
    # ══════════════════════════════════════════
    btc_pump_block_dir, btc_pump_msg_dir = _btc_pump_blocks_range(btc_ctx, direction)
    if btc_pump_block_dir:
        return {
            "symbol": symbol, "strategy": "RANGE_SCALP", "direction": "WAIT",
            "confidence": "LOW", "price": smart_round(price),
            "range_high": smart_round(range_high), "range_low": smart_round(range_low),
            "range_pct": range_pct, "range_source": range_source,
            "d1_bias": d1_bias, "h4_bias": h4_bias,
            "top_touches": top_touches, "bottom_touches": bottom_touches,
            "is_bimodal": is_bimodal, "bimodal_gap": bimodal_gap,
            "market_structure": ms, "btc_volume": btc_vol,
            "warnings": [f"🚫 {btc_pump_msg_dir}"],
            "conditions": [],
        }

    # ══════════════════════════════════════════
    # FILTER 3: Trend block
    # ══════════════════════════════════════════
    warnings   = []
    conditions = []

    if trend_block and trend_block_dir == direction:
        # Hard block — không cho vào lệnh ngược trend D1
        warnings.append("🚫 " + trend_block_msg)
        return {
            "symbol": symbol, "strategy": "RANGE_SCALP", "direction": "WAIT",
            "confidence": "LOW", "price": smart_round(price),
            "range_high": smart_round(range_high), "range_low": smart_round(range_low),
            "range_pct": range_pct, "range_source": range_source,
            "d1_bias": d1_bias, "h4_bias": h4_bias,
            "market_structure": ms if "ms" in dir() else None,
            "btc_volume": btc_vol if "btc_vol" in dir() else None,
            "warnings": warnings, "conditions": [],
        }
    elif trend_block and trend_block_dir != direction:
        # Cho phép nhưng warn
        warnings.append("⚠️ " + trend_block_msg)

    # ══════════════════════════════════════════
    # FILTER 4: BTC context
    # ══════════════════════════════════════════
    btc_ok, btc_warn = _btc_allows(btc_ctx, direction)
    if not btc_ok:
        return {
            "symbol": symbol, "strategy": "RANGE_SCALP", "direction": "WAIT",
            "confidence": "LOW", "price": smart_round(price),
            "range_high": smart_round(range_high), "range_low": smart_round(range_low),
            "range_pct": range_pct, "range_source": range_source,
            "d1_bias": d1_bias, "h4_bias": h4_bias,
            "market_structure": ms if "ms" in dir() else None,
            "btc_volume": btc_vol if "btc_vol" in dir() else None,
            "warnings": [btc_warn], "conditions": [],
        }

    # ══════════════════════════════════════════
    # FILTER 5: Funding
    # ══════════════════════════════════════════
    if funding is not None:
        if direction == "LONG"  and funding > 0.05:
            warnings.append("⚠️ Funding +{:.3f}% cao — Long rủi ro bị xả".format(funding))
        if direction == "SHORT" and funding < -0.05:
            warnings.append("⚠️ Funding {:.3f}% âm — Short cẩn thận".format(funding))

    # ══════════════════════════════════════════
    # FILTER 5b: Coin correlation với BTC
    # ══════════════════════════════════════════
    if corr_status == "UNDERPERFORM_LONG" and direction == "LONG":
        warnings.append(
            f"⚠️ Coin yếu hơn BTC: coin {coin_chg_4h:+.1f}% vs BTC {btc_chg_4h:+.1f}%/4h — coin đang underperform"
        )
        # Không block cứng nhưng giảm score
    elif corr_status == "UNDERPERFORM_SHORT" and direction == "SHORT":
        warnings.append(
            f"⚠️ Coin mạnh hơn BTC khi BTC dump: coin {coin_chg_4h:+.1f}% vs BTC {btc_chg_4h:+.1f}%/4h"
        )
    elif corr_status == "OUTPERFORM_BULL" and direction == "LONG":
        conditions.append(f"Coin outperform BTC ({coin_chg_4h:+.1f}% vs {btc_chg_4h:+.1f}%)")
    elif corr_status == "OUTPERFORM_BEAR" and direction == "SHORT":
        conditions.append(f"Coin lead xuống ({coin_chg_4h:+.1f}% vs BTC {btc_chg_4h:+.1f}%)")

    # ══════════════════════════════════════════
    # FILTER 6: 7-day pump exhaustion (chỉ block LONG)
    # ══════════════════════════════════════════
    pump_block = False
    if len(df_d1) >= 8:
        price_7d = float(df_d1["close"].iloc[-8])
        chg_7d   = (price - price_7d) / price_7d * 100 if price_7d > 0 else 0
        if direction == "LONG" and chg_7d > 40:
            pump_block = True
            warnings.insert(0, "🚫 PUMP +{:.1f}% / 7d — không Long đuổi".format(chg_7d))

    # ══════════════════════════════════════════
    # SL / TP — nằm TRONG range, không vượt ra ngoài
    # ══════════════════════════════════════════
    if direction == "LONG":
        sl   = smart_round(range_low  * 0.993)      # dưới đáy range 0.7%
        tp1  = smart_round(range_high * 0.982)      # 1.8% dưới đỉnh range (trong range)
        tp2  = smart_round(range_high * 0.995)      # sát đỉnh range
    else:
        sl   = smart_round(range_high * 1.007)
        tp1  = smart_round(range_low  * 1.018)
        tp2  = smart_round(range_low  * 1.005)

    sl_pct  = round(abs(price - sl)  / price * 100, 2)
    tp1_pct = round(abs(tp1 - price) / price * 100, 2)
    rr      = round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0

    # ══════════════════════════════════════════
    # Score & Confidence
    # ══════════════════════════════════════════
    reversal_ok, reversal_note = _candle_reversal(df_h1, direction)

    score = 0
    if reversal_ok:                          score += 2; conditions.append(reversal_note)
    if rr >= 1.5:                            score += 2; conditions.append("R:R {:.1f}".format(rr))
    if range_pct >= 2.5:                     score += 1; conditions.append("Range {:.1f}%".format(range_pct))
    if d1_bias == "NEUTRAL":                 score += 1; conditions.append("D1 Neutral")
    if h4_bias == "NEUTRAL":                 score += 1; conditions.append("H4 Neutral")
    if oi_change is not None and abs(oi_change) < 5: score += 1; conditions.append("OI ổn định")
    if funding is not None and abs(funding) < 0.02:  score += 1; conditions.append("Funding neutral")
    # Bonus/penalty từ coin correlation
    if corr_status in ("OUTPERFORM_BULL",) and direction == "LONG":   score += 1; conditions.append("Coin outperform BTC")
    if corr_status in ("OUTPERFORM_BEAR",) and direction == "SHORT":  score += 1; conditions.append("Coin lead downside")
    if corr_status == "UNDERPERFORM_LONG"  and direction == "LONG":   score -= 1  # penalty
    if corr_status == "UNDERPERFORM_SHORT" and direction == "SHORT":  score -= 1

    # SHORT funding booster (backtest 2026-04-30): funding > +0.05% là pattern thắng mạnh
    # CRCL: funding +0.16%, OI +1.06% → +3.57R. Setup `fund_pos_oi_up` WR=90%.
    funding_short_boost = False
    if direction == "SHORT" and funding is not None:
        if funding > 0.10:
            score += 2
            conditions.append("🔥 Funding {:+.4f}% — longs trả phí CỰC cao".format(funding))
            funding_short_boost = True
        elif funding > 0.05:
            score += 1
            conditions.append("🎯 Funding {:+.4f}% — longs overcrowded".format(funding))
            funding_short_boost = True

    # SHORT exhaustion detector (verify 2026-04-30: precision 71%, recall 20% — booster phụ trợ)
    # Chỉ áp khi engine đang ra SHORT để tránh spurious LONG-LOSS (UB/SIREN).
    exhaustion_short_triggered = False
    if direction == "SHORT":
        ex_ok, ex_note = detect_exhaustion_short(df_h1)
        if ex_ok:
            score += 2
            conditions.append("🔻 " + ex_note)
            exhaustion_short_triggered = True

    # PATCH K: LONG funding hard block (data-driven, backtest 168h)
    # LONG funding < -0.01% WR=26%, sumR=-6.94R — block trước khi xét confidence
    funding_block_long = (direction == "LONG" and funding is not None
                          and isinstance(funding, (int, float)) and funding < -0.01)

    if pump_block:
        direction  = "WAIT"
        confidence = "LOW"
    elif funding_block_long:
        direction  = "WAIT"
        confidence = "LOW"
        warnings.append(f"🚫 BLOCK LONG — funding {funding:+.4f}% < -0.01% (backtest WR=26%)")
    elif rr < 1.5:
        direction  = "WAIT"
        confidence = "LOW"
        warnings.append("❌ R:R {:.2f} < 1.5".format(rr))
    elif score >= 6 and reversal_ok:
        confidence = "HIGH"
    elif score >= 4:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    # SHORT funding-spike upgrade: funding +0.10% + reversal_ok → ép HIGH
    if (direction == "SHORT" and funding is not None
            and funding > 0.10 and reversal_ok and score >= 4
            and confidence == "MEDIUM"):
        confidence = "HIGH"
        warnings.append("🚀 Auto-upgrade HIGH: SHORT + funding cực cao + reversal candle (case CRCL)")
    # SHORT exhaustion upgrade: detector trigger + score đủ → ép HIGH
    if (direction == "SHORT" and exhaustion_short_triggered
            and score >= 5 and confidence == "MEDIUM"):
        confidence = "HIGH"
        warnings.append("🔻 Auto-upgrade HIGH: SHORT + exhaustion candle (verify 71% precision)")

    entry_verdict = "GO" if (confidence in ("HIGH", "MEDIUM") and direction in ("LONG", "SHORT") and reversal_ok) else "WAIT"

    # ── Entry optimal cho RANGE_SCALP ──
    # LONG: ưu tiên entry tại đáy range (range_low * 1.005) hoặc gần range_low
    # SHORT: ưu tiên entry tại đỉnh range (range_high * 0.995) hoặc gần range_high
    # Chỉ propose nếu khác price ≥ 0.3% (tránh trigger noise)
    entry_opt = None
    entry_opt_label = None
    entry_opt_rr = None
    if direction == "LONG" and range_low and range_low < price * 0.997:
        entry_opt = smart_round(range_low * 1.005)
        entry_opt_label = "Range bottom"
    elif direction == "SHORT" and range_high and range_high > price * 1.003:
        entry_opt = smart_round(range_high * 0.995)
        entry_opt_label = "Range top"
    if entry_opt is not None and sl is not None and tp1 is not None:
        try:
            opt_sl_pct  = abs(entry_opt - sl) / entry_opt * 100
            opt_tp1_pct = abs(tp1 - entry_opt) / entry_opt * 100
            entry_opt_rr = round(opt_tp1_pct / opt_sl_pct, 2) if opt_sl_pct > 0 else None
        except Exception:
            entry_opt_rr = None

    return {
        "symbol":          symbol,
        "strategy":        "RANGE_SCALP",
        "direction":       direction,
        "confidence":      confidence,
        "price":           smart_round(price),
        "entry":           smart_round(price),
        "entry_opt":       entry_opt,
        "entry_opt_label": entry_opt_label,
        "entry_opt_rr":    entry_opt_rr,
        "sl":              sl,
        "sl_pct":          sl_pct,
        "tp1":             tp1,
        "tp1_pct":         tp1_pct,
        "tp2":             tp2,
        "rr":              rr,
        "score":           score,
        "entry_verdict":   entry_verdict,
        "range_high":      smart_round(range_high),
        "range_low":       smart_round(range_low),
        "range_pct":       range_pct,
        "range_source":    range_source,
        "d1_bias":         d1_bias,
        "h4_bias":         h4_bias,
        "position_in_range": "đáy range" if direction == "LONG" else "đỉnh range",
        "reversal_ok":     reversal_ok,
        "reversal_note":   reversal_note,
        "warnings":        warnings,
        "conditions":      conditions,
        "bottom_zone":     smart_round(bottom_zone),
        "top_zone":        smart_round(top_zone),
        "m15_entry_long":  smart_round(m15_entry_long),
        "m15_entry_short": smart_round(m15_entry_short),
        "timeframe_note":  "Range: H1 (5 ngày) | Entry: M15 (4h) | Trend: D1+H4",
        "fibo": _fibo_levels(smart_round(price), sl, direction, range_high, range_low),
        "top_touches":    top_touches,
        "bottom_touches": bottom_touches,
        "is_bimodal":     is_bimodal,
        "bimodal_gap":    bimodal_gap,
        "market": {
            "funding":     round(funding, 4) if funding is not None else None,
            "funding_pct": "{:+.4f}%".format(funding) if funding is not None else "N/A",
            "oi_change":   oi_change,
            "oi_str":      "{:+.2f}%".format(oi_change) if oi_change is not None else "N/A",
            "atr_ratio":   atr_ratio,
        },
        "btc_context":    btc_ctx,
        "market_structure": ms,
        "btc_volume":    btc_vol,
        "corr_status":   corr_status,
        "coin_chg_4h":   coin_chg_4h,
        "btc_chg_4h":    btc_chg_4h,
    }
