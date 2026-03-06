"""
Range Scalp Engine — CryptoDesk
Phát hiện coin đang sideway + signal khi giá chạm biên range
"""
from core.indicators import prepare
from core.binance    import fetch_klines, fetch_funding_rate, fetch_oi_change, fetch_btc_context
from core.utils      import smart_round

import numpy as np

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _detect_range(df_h1, df_m15, df_h4, lookback_h1=48, lookback_m15=60):
    """
    Detect range dùng H1 (biên range) + M15 (entry precision).
    - H1 lookback 48 nến = 2 ngày → xác định vùng tích lũy gần nhất
    - M15 lookback 60 nến = 15 giờ → tìm swing high/low precision cho entry
    - H4 chỉ dùng để check macro context (coin có đang trending không)
    """
    # ── Macro check từ H4 — nếu H4 đang trend mạnh thì không range scalp ──
    h4_recent = df_h4.iloc[-20:]
    h4_atr_mean = float(h4_recent["atr"].mean()) if "atr" in h4_recent.columns else 0
    h4_atr_last = float(df_h4["atr"].iloc[-1])   if "atr" in df_h4.columns else 0
    h4_atr_ratio = h4_atr_last / h4_atr_mean if h4_atr_mean > 0 else 1

    # H4 ATR đang spike mạnh → coin đang breakout, không range
    if h4_atr_ratio > 2.0:
        price = float(df_h1["close"].iloc[-1])
        return 0, 0, 0, False, h4_atr_ratio

    # ── Range biên từ H1 (2 ngày gần nhất) ──
    h1_recent  = df_h1.iloc[-lookback_h1:]
    h1_highs   = h1_recent["high"].astype(float)
    h1_lows    = h1_recent["low"].astype(float)

    # Loại bỏ outlier spike (top/bottom 5%)
    h1_high_90 = float(h1_highs.quantile(0.95))
    h1_low_10  = float(h1_lows.quantile(0.05))

    range_high = h1_high_90
    range_low  = h1_low_10
    range_pct  = (range_high - range_low) / range_low * 100 if range_low > 0 else 0

    price = float(df_h1["close"].iloc[-1])

    # ── Entry zone precision từ M15 (15 giờ gần nhất) ──
    m15_recent    = df_m15.iloc[-lookback_m15:]
    m15_high      = float(m15_recent["high"].max())
    m15_low       = float(m15_recent["low"].min())

    # ATR H1 để check volatility
    h1_atr_mean = float(h1_recent["atr"].mean()) if "atr" in h1_recent.columns else 0
    h1_atr_last = float(df_h1["atr"].iloc[-1])   if "atr" in df_h1.columns else 0
    atr_ratio   = h1_atr_last / h1_atr_mean if h1_atr_mean > 0 else 1

    price_in_range = range_low <= price <= range_high

    is_ranging = (
        range_pct >= 1.5          # H1 range đủ rộng (scalp cần ít nhất 1.5%)
        and range_pct <= 20.0     # không phải swing quá lớn
        and atr_ratio < 1.8       # H1 ATR không spike
        and price_in_range
        and h4_atr_ratio < 2.0    # H4 không đang breakout
    )

    return range_high, range_low, round(range_pct, 2), is_ranging, round(atr_ratio, 2)


def _candle_reversal(df_h1, direction):
    """
    Kiểm tra nến đảo chiều H1 gần nhất.
    direction: 'LONG' → tìm bullish reversal | 'SHORT' → bearish reversal
    """
    if len(df_h1) < 3:
        return False, "Không đủ data H1"

    c1 = df_h1.iloc[-1]   # nến vừa đóng
    c2 = df_h1.iloc[-2]

    o1, h1, l1, cl1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, cl2 = float(c2["open"]), float(c2["close"])
    body1  = abs(cl1 - o1)
    candle_range1 = h1 - l1

    if candle_range1 == 0:
        return False, "Nến H1 không hợp lệ"

    body_ratio = body1 / candle_range1

    if direction == "LONG":
        # Bullish: hammer (lower wick dài) hoặc bullish engulf
        lower_wick = o1 - l1 if cl1 >= o1 else cl1 - l1
        wick_ratio = lower_wick / candle_range1
        is_hammer  = wick_ratio >= 0.5 and cl1 >= o1
        is_engulf  = cl1 > o2 and o1 < cl2 and cl2 < o2  # bullish engulfing
        if is_hammer:
            return True, "Hammer H1 (lower wick {:.0f}%)".format(wick_ratio * 100)
        if is_engulf:
            return True, "Bullish Engulfing H1"
        # Nến xanh đơn giản với body > 40%
        if cl1 > o1 and body_ratio > 0.4:
            return True, "Nến xanh H1 body {:.0f}%".format(body_ratio * 100)
        return False, "Chưa có nến đảo chiều bullish H1"

    else:  # SHORT
        upper_wick = h1 - o1 if cl1 <= o1 else h1 - cl1
        wick_ratio = upper_wick / candle_range1
        is_star    = wick_ratio >= 0.5 and cl1 <= o1
        is_engulf  = cl1 < o2 and o1 > cl2 and cl2 > o2
        if is_star:
            return True, "Shooting Star H1 (upper wick {:.0f}%)".format(wick_ratio * 100)
        if is_engulf:
            return True, "Bearish Engulfing H1"
        if cl1 < o1 and body_ratio > 0.4:
            return True, "Nến đỏ H1 body {:.0f}%".format(body_ratio * 100)
        return False, "Chưa có nến đảo chiều bearish H1"


def _btc_allows(btc_ctx, direction):
    """BTC context filter cho range scalp — chặt hơn trend mode."""
    if not btc_ctx:
        return True, ""
    sentiment = btc_ctx.get("sentiment", "NEUTRAL")
    note      = btc_ctx.get("note", "")

    if direction == "LONG":
        if sentiment in ("DUMP", "RISK_OFF"):
            return False, "BTC đang {} — không Long range".format(sentiment)
    if direction == "SHORT":
        if sentiment in ("PUMP", "RISK_ON"):
            return False, "BTC đang {} — không Short range".format(sentiment)
    return True, ""


# ─────────────────────────────────────────────
# Main analyze function
# ─────────────────────────────────────────────

def range_analyze(symbol: str, cfg: dict) -> dict:
    ff = bool(cfg.get("force_futures", False))

    df_d1  = prepare(fetch_klines(symbol, "1d",  60, force_futures=ff))
    df_h4  = prepare(fetch_klines(symbol, "4h",  80, force_futures=ff))
    df_h1  = prepare(fetch_klines(symbol, "1h", 120, force_futures=ff))
    df_m15 = prepare(fetch_klines(symbol, "15m", 96, force_futures=ff))

    for df in [df_d1, df_h4, df_h1, df_m15]:
        if len(df) < 10:
            raise ValueError(f"Không đủ data cho {symbol}")

    price   = float(df_h1["close"].iloc[-1])
    funding = fetch_funding_rate(symbol)
    oi_change = fetch_oi_change(symbol)
    btc_ctx   = fetch_btc_context()

    # ── Override range từ config nếu anh set tay ──
    override = cfg.get("range_override", {}).get(symbol, {})
    if override.get("range_high") and override.get("range_low"):
        range_high = float(override["range_high"])
        range_low  = float(override["range_low"])
        range_pct  = (range_high - range_low) / range_low * 100
        is_ranging = range_low <= price <= range_high
        atr_ratio  = 1.0
        range_source = "manual"
    else:
        range_high, range_low, range_pct, is_ranging, atr_ratio = _detect_range(df_h1, df_m15, df_h4)
        range_source = "auto"

    # ── Không phải ranging → WAIT ──
    if not is_ranging:
        return {
            "symbol":     symbol,
            "strategy":   "RANGE_SCALP",
            "direction":  "WAIT",
            "confidence": "LOW",
            "price":      smart_round(price),
            "range_high": smart_round(range_high),
            "range_low":  smart_round(range_low),
            "range_pct":  range_pct,
            "range_source": range_source,
            "warnings":   ["Coin không đang trong range rõ ràng (range {:.1f}%, ATR ratio {:.1f}x)".format(range_pct, atr_ratio)],
            "conditions": [],
        }

    # ── Xác định vị trí trong range ──
    # Dùng M15 để tính entry zone chính xác hơn (swing M15 gần nhất)
    range_size = range_high - range_low

    # Entry zone: 20% biên (H1 range thường hẹp hơn H4 nên cần zone rộng hơn chút)
    zone_pct    = 0.20
    bottom_zone = range_low  + range_size * zone_pct
    top_zone    = range_high - range_size * zone_pct

    # Precision: dùng M15 swing low/high trong 4 giờ gần nhất làm entry trigger
    m15_4h      = df_m15.iloc[-16:]   # 16 nến M15 = 4 tiếng
    m15_entry_long  = float(m15_4h["low"].min())    # swing low M15 gần nhất
    m15_entry_short = float(m15_4h["high"].max())   # swing high M15 gần nhất

    at_bottom = price <= bottom_zone
    at_top    = price >= top_zone

    if at_bottom:
        direction = "LONG"
    elif at_top:
        direction = "SHORT"
    else:
        # Giữa range — không vào
        mid_pct     = (price - range_low) / range_size * 100
        dist_to_long  = round((price - bottom_zone) / price * 100, 1)
        dist_to_short = round((top_zone - price)    / price * 100, 1)
        return {
            "symbol":        symbol,
            "strategy":      "RANGE_SCALP",
            "direction":     "WAIT",
            "confidence":    "LOW",
            "price":         smart_round(price),
            "range_high":    smart_round(range_high),
            "range_low":     smart_round(range_low),
            "range_pct":     range_pct,
            "range_source":  range_source,
            "bottom_zone":   smart_round(bottom_zone),
            "top_zone":      smart_round(top_zone),
            "m15_entry_long":  smart_round(m15_entry_long),
            "m15_entry_short": smart_round(m15_entry_short),
            "dist_to_long":  dist_to_long,
            "dist_to_short": dist_to_short,
            "warnings":      ["Giá đang giữa range ({:.0f}% từ đáy) — cách vùng Long -{:.1f}%, cách vùng Short +{:.1f}%".format(mid_pct, dist_to_long, dist_to_short)],
            "conditions":    ["H1 Range {:.1f}%".format(range_pct)],
        }

    # ── BTC context filter ──
    btc_ok, btc_warn = _btc_allows(btc_ctx, direction)
    if not btc_ok:
        return {
            "symbol":     symbol,
            "strategy":   "RANGE_SCALP",
            "direction":  "WAIT",
            "confidence": "LOW",
            "price":      smart_round(price),
            "range_high": smart_round(range_high),
            "range_low":  smart_round(range_low),
            "range_pct":  range_pct,
            "range_source": range_source,
            "warnings":   [btc_warn],
            "conditions": [],
        }

    # ── Funding filter ──
    warnings   = []
    conditions = []

    if funding is not None:
        if direction == "LONG"  and funding > 0.05:
            warnings.append("⚠️ Funding cao +{:.3f}% — Long range rủi ro bị xả".format(funding))
        if direction == "SHORT" and funding < -0.05:
            warnings.append("⚠️ Funding âm {:.3f}% — Short range cẩn thận".format(funding))

    # ── Nến đảo chiều H1 ──
    reversal_ok, reversal_note = _candle_reversal(df_h1, direction)

    # ── 7-day pump exhaustion ──
    pump_block = False
    if len(df_d1) >= 8:
        price_7d = float(df_d1["close"].iloc[-8])
        chg_7d   = (price - price_7d) / price_7d * 100 if price_7d > 0 else 0
        if direction == "LONG" and chg_7d > 50:
            pump_block = True
            warnings.insert(0, "🚫 PUMP EXHAUSTION +{:.1f}% / 7d — không Long đuổi".format(chg_7d))

    # ── SL / TP ──
    if direction == "LONG":
        sl    = smart_round(range_low  * 0.992)   # dưới đáy range 0.8%
        tp1   = smart_round(range_high * 0.990)   # gần đỉnh range
        tp2   = smart_round(range_high * 1.010)   # breakout target
    else:
        sl    = smart_round(range_high * 1.008)
        tp1   = smart_round(range_low  * 1.010)
        tp2   = smart_round(range_low  * 0.990)

    sl_pct  = round(abs(price - sl)  / price * 100, 2)
    tp1_pct = round(abs(tp1  - price) / price * 100, 2)
    rr      = round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0

    # ── Confidence & score ──
    score = 0
    if reversal_ok:            score += 2; conditions.append(reversal_note)
    if rr >= 1.5:              score += 2; conditions.append("R:R {:.1f}".format(rr))
    if range_pct >= 3:         score += 1; conditions.append("Range {:.1f}%".format(range_pct))
    if oi_change and abs(oi_change) < 5: score += 1; conditions.append("OI ổn định")
    if funding is not None and abs(funding) < 0.03: score += 1; conditions.append("Funding neutral")

    if pump_block:
        direction  = "WAIT"
        confidence = "LOW"
    elif score >= 5 and reversal_ok and rr >= 1.5:
        confidence = "HIGH"
    elif score >= 3:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    if rr < 1.5 and direction != "WAIT":
        direction  = "WAIT"
        confidence = "LOW"
        warnings.append("❌ R:R {:.2f} < 1.5 — không đủ".format(rr))

    # ── Entry verdict ──
    entry_verdict = "GO" if (confidence in ("HIGH","MEDIUM") and direction in ("LONG","SHORT") and reversal_ok) else "WAIT"

    pos_label = "đáy range" if direction == "LONG" else "đỉnh range"

    return {
        "symbol":        symbol,
        "strategy":      "RANGE_SCALP",
        "direction":     direction,
        "confidence":    confidence,
        "price":         smart_round(price),
        "entry":         smart_round(price),
        "sl":            sl,
        "sl_pct":        sl_pct,
        "tp1":           tp1,
        "tp1_pct":       tp1_pct,
        "tp2":           tp2,
        "rr":            rr,
        "score":         score,
        "entry_verdict": entry_verdict,
        "range_high":    smart_round(range_high),
        "range_low":     smart_round(range_low),
        "range_pct":     range_pct,
        "range_source":  range_source,
        "position_in_range": pos_label,
        "reversal_ok":   reversal_ok,
        "reversal_note": reversal_note,
        "warnings":      warnings,
        "conditions":    conditions,
        "market": {
            "funding":     round(funding, 4) if funding is not None else None,
            "funding_pct": "{:+.4f}%".format(funding) if funding is not None else "N/A",
            "oi_change":   oi_change,
            "oi_str":      "{:+.2f}%".format(oi_change) if oi_change is not None else "N/A",
            "atr_ratio":   atr_ratio,
        },
        "btc_context":     btc_ctx,
        "bottom_zone":    smart_round(bottom_zone),
        "top_zone":       smart_round(top_zone),
        "m15_entry_long":  smart_round(m15_entry_long),
        "m15_entry_short": smart_round(m15_entry_short),
        "timeframe_note": "Range: H1 (2 ngày) | Entry zone: M15 (4 giờ) | Macro: H4",
    }
