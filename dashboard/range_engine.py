"""
Range Scalp Engine — CryptoDesk
Phát hiện coin đang sideway + signal khi giá chạm biên range
"""
from dashboard.indicators import prepare
from dashboard.binance    import fetch_klines, fetch_funding_rate, fetch_oi_change, fetch_btc_context

import numpy as np

def smart_round(v):
    if v == 0: return 0
    from math import floor, log10
    mag = floor(log10(abs(v)))
    digits = max(2, 2 - mag)
    return round(v, digits)

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _detect_range(df_h4, lookback=20):
    """
    Tự động phát hiện range từ swing H4.
    Trả về (range_high, range_low, range_pct, is_ranging)
    """
    recent = df_h4.iloc[-lookback:]
    highs  = recent["high"].astype(float)
    lows   = recent["low"].astype(float)

    range_high = float(highs.max())
    range_low  = float(lows.min())
    range_pct  = (range_high - range_low) / range_low * 100 if range_low > 0 else 0

    # ATR trung bình
    atr_mean  = float(recent["atr"].mean()) if "atr" in recent.columns else 0
    atr_last  = float(df_h4["atr"].iloc[-1]) if "atr" in df_h4.columns else 0
    atr_ratio = atr_last / atr_mean if atr_mean > 0 else 1

    # Coin được coi là ranging nếu:
    # 1. Range % đủ nhỏ (< 25% — không phải trend mạnh)
    # 2. ATR không đang spike
    # 3. Giá không cách range_high/low quá xa
    price = float(df_h4["close"].iloc[-1])
    price_in_range = range_low <= price <= range_high

    is_ranging = (
        range_pct >= 2.0          # range đủ rộng để có lãi
        and range_pct <= 30.0     # không phải swing quá lớn
        and atr_ratio < 1.8       # ATR không spike bất thường
        and price_in_range
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

    df_d1 = prepare(fetch_klines(symbol, "1d",  60, force_futures=ff))
    df_h4 = prepare(fetch_klines(symbol, "4h", 120, force_futures=ff))
    df_h1 = prepare(fetch_klines(symbol, "1h",  80, force_futures=ff))

    for df in [df_d1, df_h4, df_h1]:
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
        range_high, range_low, range_pct, is_ranging, atr_ratio = _detect_range(df_h4)
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
    range_size   = range_high - range_low
    zone_pct     = 0.15   # 15% biên trên/dưới = entry zone
    bottom_zone  = range_low  + range_size * zone_pct
    top_zone     = range_high - range_size * zone_pct

    at_bottom = price <= bottom_zone
    at_top    = price >= top_zone

    if at_bottom:
        direction = "LONG"
    elif at_top:
        direction = "SHORT"
    else:
        # Giữa range — không vào
        mid_pct = (price - range_low) / range_size * 100
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
            "warnings":   ["Giá đang giữa range ({:.0f}% từ đáy) — chờ về biên".format(mid_pct)],
            "conditions": ["Range {:.1f}%".format(range_pct)],
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
        "btc_context": btc_ctx,
    }
