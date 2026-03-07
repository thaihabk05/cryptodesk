"""
Range Scalp Engine v2 — CryptoDesk
Logic: Chỉ range scalp khi D1+H4 KHÔNG downtrend mạnh + coin thực sự sideway
"""
from core.indicators import prepare
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

    if down_count >= 3:
        return "DOWNTREND"
    elif up_count >= 3:
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


def _detect_range(df_h1, df_m15, df_h4, lookback_h1=24, lookback_m15=32):
    """
    Detect range THẬT — coin phải sideway với biên lặp đi lặp lại.
    Phân biệt:
      ✅ ARB range: giá đảo giữa 0.1015–0.1055 nhiều lần trong 2 ngày
      ❌ PTB bounce: giá downtrend rồi bounce 1 lần, không lặp lại
    """
    # ── H4 ATR spike check ──
    h4_recent    = df_h4.iloc[-20:]
    h4_atr_mean  = float(h4_recent["atr"].mean()) if "atr" in h4_recent.columns else 0
    h4_atr_last  = float(df_h4["atr"].iloc[-1])   if "atr" in df_h4.columns else 0
    h4_atr_ratio = h4_atr_last / h4_atr_mean if h4_atr_mean > 0 else 1

    if h4_atr_ratio > 1.8:
        return 0, 0, 0, False, h4_atr_ratio, 0, 0

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

    # ── Drift check: close đầu vs cuối không trending ──
    drift_pct = abs(float(h1_closes.iloc[-1]) - float(h1_closes.iloc[0])) / float(h1_closes.iloc[0]) * 100

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

    is_ranging = (
        range_pct >= 1.5            # đủ rộng để có lãi
        and range_pct <= 8.0        # không phải swing lớn (BTR 26% bị block ở đây)
        and atr_ratio < 1.6         # H1 ATR không spike
        and drift_pct < 4.0         # không có momentum rõ (PTB 18% bị block ở đây)
        and price_in_range
        and h4_atr_ratio < 1.8
        and not is_bimodal          # không có 2 price regime riêng biệt (BTR bị block ở đây)
        and top_touches >= 2        # đã chạm đỉnh range >= 2 lần
        and bottom_touches >= 2     # đã chạm đáy range >= 2 lần
    )

    return range_high, range_low, round(range_pct, 2), is_ranging, round(atr_ratio, 2), top_touches, bottom_touches


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


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

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

    # ══════════════════════════════════════════
    # FILTER 1: D1 trend — không Long khi D1 downtrend
    # ══════════════════════════════════════════
    d1_bias = _get_trend_bias(df_d1, "D1")
    h4_bias = _get_trend_bias(df_h4, "H4")

    trend_block    = False
    trend_block_msg = ""

    if d1_bias == "DOWNTREND" and h4_bias == "DOWNTREND":
        # Cả D1 và H4 đều downtrend → chỉ cho SHORT, block LONG hoàn toàn
        trend_block     = True
        trend_block_dir = "LONG"
        trend_block_msg = "D1+H4 đều DOWNTREND — chỉ xem xét SHORT tại đỉnh range"
    elif d1_bias == "DOWNTREND":
        trend_block     = True
        trend_block_dir = "LONG"
        trend_block_msg = "D1 DOWNTREND — không Long range (countertrend)"
    elif d1_bias == "UPTREND" and h4_bias == "UPTREND":
        trend_block     = True
        trend_block_dir = "SHORT"
        trend_block_msg = "D1+H4 UPTREND — không Short range"

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
    else:
        range_high, range_low, range_pct, is_ranging, atr_ratio, top_touches, bottom_touches = _detect_range(df_h1, df_m15, df_h4)
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

    if pump_block:
        direction  = "WAIT"
        confidence = "LOW"
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

    entry_verdict = "GO" if (confidence in ("HIGH", "MEDIUM") and direction in ("LONG", "SHORT") and reversal_ok) else "WAIT"

    return {
        "symbol":          symbol,
        "strategy":        "RANGE_SCALP",
        "direction":       direction,
        "confidence":      confidence,
        "price":           smart_round(price),
        "entry":           smart_round(price),
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
        "timeframe_note":  "Range: H1 (1 ngày) | Entry: M15 (4h) | Trend: D1+H4",
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
        "btc_context": btc_ctx,
    }
