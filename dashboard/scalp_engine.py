"""dashboard/scalp_engine.py — Scalp M15/H1 Strategy Engine.

Strategy: Scalp (15–120 phút hold)
─────────────────────────────────────────────────────
Tầng 1 — H1 Bias    : H1 EMA9/21 xác định hướng ngắn hạn
Tầng 2 — M15 Confirm: M15 EMA cross + RSI + volume spike
Tầng 3 — M5  Entry  : M5 nến xác nhận, entry chính xác

SL:  ATR M15 × 1.2, giới hạn 0.8–1.5%
TP1: Swing high/low M15 gần nhất (30 nến), R:R ≥ 1.5
TP2: Fib Extension 1.272 sóng M15

Đặc điểm:
- Entry nhanh, dùng EMA9/21 thay MA34/89
- RSI xác nhận không overbought/oversold khi entry
- Volume spike xác nhận breakout thật
- Noise nhiều hơn H4/H1 → cần M15+M5 đồng thuận
"""
import math
from datetime import datetime

from core.binance import (fetch_klines, fetch_funding_rate,
                           fetch_oi_change, fetch_btc_context)
from core.indicators import (prepare, ma_slope, find_swing_points,
                              classify_structure, fib_retracement,
                              fib_extension, calc_atr_context)
from core.utils import sanitize, smart_round


def scalp_analyze(symbol: str, cfg: dict) -> dict:
    """Phân tích theo strategy Scalp M15/H1."""
    ff = bool(cfg.get("force_futures", False))

    # Fetch: H1 (bias) + M15 (confirm) + M5 (entry)
    df_h1  = prepare(fetch_klines(symbol, "1h",  100, force_futures=ff))
    df_m15 = prepare(fetch_klines(symbol, "15m", 150, force_futures=ff))
    df_m5  = prepare(fetch_klines(symbol, "5m",  100, force_futures=ff))

    for df in [df_h1, df_m15, df_m5]:
        if len(df) < 20:
            raise ValueError(f"Không đủ data cho {symbol}")

    price    = float(df_m5["close"].iloc[-1])
    row_h1   = df_h1.iloc[-1]
    prev_h1  = df_h1.iloc[-2]
    row_m15  = df_m15.iloc[-1]
    prev_m15 = df_m15.iloc[-2]
    row_m5   = df_m5.iloc[-1]
    prev_m5  = df_m5.iloc[-2]

    # Market data
    funding   = fetch_funding_rate(symbol)
    oi_change = fetch_oi_change(symbol)
    btc_ctx   = fetch_btc_context()
    atr_m15   = float(df_m15["atr"].iloc[-1])
    atr_m5    = float(df_m5["atr"].iloc[-1])

    # ATR context dùng M15 làm base
    atr_avg_m15  = float(df_m15["atr"].iloc[-60:].mean()) if len(df_m15) >= 60 else atr_m15
    atr_ratio    = round(atr_m15 / atr_avg_m15, 2) if atr_avg_m15 else 1.0
    if atr_ratio < 0.5:
        atr_state, atr_note, atr_adj = "COMPRESS", "ATR M15 thấp — thị trường nén, chờ breakout", -1
    elif atr_ratio > 2.0:
        atr_state, atr_note, atr_adj = "EXPAND", "ATR M15 quá cao — volatility lớn, SL dễ quét", -1
    else:
        atr_state, atr_note, atr_adj = "NORMAL", "", 0

    # ────────────────────────────────────────
    # TẦNG 1 — H1 Bias (dùng EMA9/21)
    # ────────────────────────────────────────
    ema9_h1  = float(row_h1["ema9"])
    ema21_h1 = float(row_h1["ema21"])
    rsi_h1   = float(row_h1["rsi"])

    h1_ema_bull  = ema9_h1 > ema21_h1           # EMA9 trên EMA21 → bullish
    h1_ema_cross_up  = (float(prev_h1["ema9"]) <= float(prev_h1["ema21"])
                        and ema9_h1 > ema21_h1)  # vừa cross up
    h1_ema_cross_dn  = (float(prev_h1["ema9"]) >= float(prev_h1["ema21"])
                        and ema9_h1 < ema21_h1)  # vừa cross down

    h1_price_above_ema21 = price > ema21_h1
    slope_ema9_h1 = ma_slope(df_h1["ema9"], n=3)

    if h1_ema_bull and h1_price_above_ema21:
        h1_bias = "LONG"
    elif not h1_ema_bull and not h1_price_above_ema21:
        h1_bias = "SHORT"
    else:
        h1_bias = "NEUTRAL"

    # ────────────────────────────────────────
    # TẦNG 2 — M15 Confirmation
    # ────────────────────────────────────────
    ema9_m15   = float(row_m15["ema9"])
    ema21_m15  = float(row_m15["ema21"])
    rsi_m15    = float(row_m15["rsi"])
    vol_m15    = float(row_m15["vol_ratio"])

    m15_ema_bull     = ema9_m15 > ema21_m15
    m15_ema_cross_up = (float(prev_m15["ema9"]) <= float(prev_m15["ema21"])
                        and ema9_m15 > ema21_m15)
    m15_ema_cross_dn = (float(prev_m15["ema9"]) >= float(prev_m15["ema21"])
                        and ema9_m15 < ema21_m15)
    m15_vol_spike    = vol_m15 > 1.5
    m15_bullish      = row_m15["close"] > row_m15["open"]
    m15_bearish      = row_m15["close"] < row_m15["open"]

    # RSI filter: tránh entry khi overbought/oversold
    rsi_ok_long  = 40 <= rsi_m15 <= 70   # không quá OB
    rsi_ok_short = 30 <= rsi_m15 <= 60   # không quá OS
    rsi_ob       = rsi_m15 > 75
    rsi_os       = rsi_m15 < 25

    # Swing M15 gần nhất (30 nến = 7.5h)
    df_m15_recent = df_m15.iloc[-30:]
    highs_m15, lows_m15 = find_swing_points(df_m15_recent, lookback=2)
    swing_highs_m15 = sorted([v for _, v in highs_m15], reverse=True)
    swing_lows_m15  = sorted([v for _, v in lows_m15])
    recent_m15_high = float(df_m15["high"].iloc[-30:].max())
    recent_m15_low  = float(df_m15["low"].iloc[-30:].min())

    m15_structure = classify_structure(
        *find_swing_points(df_m15.iloc[-40:], lookback=3)
    )

    # Fib M15 retracement
    fib_m15_ret = fib_retracement(recent_m15_high, recent_m15_low)
    f382 = fib_m15_ret.get("0.382", price)
    f618 = fib_m15_ret.get("0.618", price)
    in_fib_m15 = min(f382, f618) * 0.998 <= price <= max(f382, f618) * 1.002

    # ────────────────────────────────────────
    # TẦNG 3 — M5 Entry Confirmation
    # ────────────────────────────────────────
    ema9_m5   = float(row_m5["ema9"])
    ema21_m5  = float(row_m5["ema21"])
    rsi_m5    = float(row_m5["rsi"])
    vol_m5    = float(row_m5["vol_ratio"])

    m5_ema_bull  = ema9_m5 > ema21_m5
    m5_bullish   = row_m5["close"] > row_m5["open"]
    m5_bearish   = row_m5["close"] < row_m5["open"]
    m5_vol_ok    = vol_m5 > 1.2

    def get_m5_status(direction):
        if direction == "LONG":
            if m5_ema_bull and m5_bullish and m5_vol_ok and not rsi_ob:
                return "CONFIRMED", "✅ M5 xác nhận LONG — EMA bull, nến xanh, vol tốt"
            elif rsi_ob:
                return "OVERBOUGHT", "⚠️ RSI M5 overbought — chờ RSI hạ xuống dưới 70"
            elif not m5_ema_bull:
                return "PULLBACK", "⏳ M5 EMA chưa bull — chờ EMA9 vượt EMA21 M5"
            else:
                return "FORMING", "⏳ M5 chưa rõ — theo dõi thêm 1–2 nến M5"
        else:
            if not m5_ema_bull and m5_bearish and m5_vol_ok and not rsi_os:
                return "CONFIRMED", "✅ M5 xác nhận SHORT — EMA bear, nến đỏ, vol tốt"
            elif rsi_os:
                return "OVERSOLD", "⚠️ RSI M5 oversold — chờ RSI hồi lên trên 30"
            elif m5_ema_bull:
                return "PULLBACK", "⏳ M5 EMA chưa bear — chờ EMA9 dưới EMA21 M5"
            else:
                return "FORMING", "⏳ M5 chưa rõ — theo dõi thêm 1–2 nến M5"

    m5_status, m5_note = get_m5_status(h1_bias)

    # ────────────────────────────────────────
    # DIRECTION & SCORING
    # ────────────────────────────────────────
    warnings = []

    if h1_bias == "NEUTRAL":
        direction  = "WAIT"
        confidence = "LOW"
        conditions = ["H1 EMA9/21 chưa rõ hướng — chờ EMA cross"]
        score = 0
    else:
        direction  = h1_bias
        conditions = []

        if direction == "LONG":
            if h1_ema_bull:           conditions.append("H1 EMA9 > EMA21 — bias LONG")
            if h1_ema_cross_up:       conditions.append("KEY: EMA9 vừa cross EMA21 H1 ↑")
            if slope_ema9_h1 == "UP": conditions.append("EMA9 H1 slope ↑ — momentum tăng")
            if m15_ema_bull:          conditions.append("M15 EMA9 > EMA21 — xác nhận LONG")
            if m15_ema_cross_up:      conditions.append("KEY: EMA9 vừa cross EMA21 M15 ↑")
            if m15_bullish and m15_vol_spike: conditions.append(f"M15 nến xanh vol {vol_m15:.1f}x — breakout mạnh")
            if rsi_ok_long:           conditions.append(f"RSI M15 {rsi_m15:.0f} — vùng an toàn (40–70)")
            if m15_structure == "UPTREND": conditions.append("M15 cấu trúc UPTREND")
            if in_fib_m15:            conditions.append("M15 trong Fib 0.382–0.618 — vùng pullback tốt")
            if rsi_ob:
                conditions = [c for c in conditions if "RSI" not in c]
                warnings.append(f"⚠️ RSI M15 {rsi_m15:.0f} — overbought, rủi ro reversal")
        else:  # SHORT
            if not h1_ema_bull:        conditions.append("H1 EMA9 < EMA21 — bias SHORT")
            if h1_ema_cross_dn:        conditions.append("KEY: EMA9 vừa cross EMA21 H1 ↓")
            if slope_ema9_h1 == "DOWN":conditions.append("EMA9 H1 slope ↓ — momentum giảm")
            if not m15_ema_bull:       conditions.append("M15 EMA9 < EMA21 — xác nhận SHORT")
            if m15_ema_cross_dn:       conditions.append("KEY: EMA9 vừa cross EMA21 M15 ↓")
            if m15_bearish and m15_vol_spike: conditions.append(f"M15 nến đỏ vol {vol_m15:.1f}x — breakdown mạnh")
            if rsi_ok_short:           conditions.append(f"RSI M15 {rsi_m15:.0f} — vùng an toàn (30–60)")
            if m15_structure == "DOWNTREND": conditions.append("M15 cấu trúc DOWNTREND")
            if in_fib_m15:             conditions.append("M15 trong Fib 0.382–0.618 — vùng retest tốt")
            if rsi_os:
                conditions = [c for c in conditions if "RSI" not in c]
                warnings.append(f"⚠️ RSI M15 {rsi_m15:.0f} — oversold, rủi ro bounce")

        score = len(conditions)
        confidence = "HIGH" if score >= 4 else "MEDIUM" if score >= 2 else "LOW"

    # Funding / ATR adj
    def _interp_funding(funding, direction):
        w, adj = [], 0
        if funding is None: return w, adj
        if direction == "LONG":
            if funding > 0.05: w.append(f"⚠️ Funding {funding:+.4f}% — Long overcrowded"); adj -= 1
            elif funding < -0.03: w.append(f"✅ Funding {funding:+.4f}% — có lợi LONG")
        elif direction == "SHORT":
            if funding < -0.05: w.append(f"⚠️ Funding {funding:+.4f}% — Short overcrowded"); adj -= 1
            elif funding > 0.03: w.append(f"✅ Funding {funding:+.4f}% — có lợi SHORT")
        return w, adj

    funding_warns, funding_adj = _interp_funding(funding, direction)
    atr_warns = [f"⚠️ {atr_note}"] if atr_note else []

    all_warnings = warnings + atr_warns + funding_warns
    if direction == "LONG"  and btc_ctx["sentiment"] in ("RISK_OFF", "DUMP"):
        all_warnings.insert(0, f"⚠️ BTC: {btc_ctx['note']}")
    elif direction == "SHORT" and btc_ctx["sentiment"] == "RISK_ON":
        all_warnings.insert(0, f"⚠️ BTC: {btc_ctx['note']}")

    total_adj = funding_adj + atr_adj
    if total_adj <= -2 and confidence != "LOW":
        confidence = "LOW"
        all_warnings.append("⚠️ Confidence hạ LOW do funding/volatility bất lợi")
    elif total_adj == -1 and confidence == "HIGH":
        confidence = "MEDIUM"

    # ────────────────────────────────────────
    # SL / TP — ATR M15, swing M15 gần nhất
    # ────────────────────────────────────────
    def _tp1_long(entry, swings, ema9, ema21, atr):
        mn, mx = entry * 1.008, entry * 1.06   # 0.8–6%
        cands = [h for h in swings if mn < h < mx]
        if cands: return smart_round(min(cands))
        for ma in [ema9, ema21]:
            if mn < ma < mx: return smart_round(ma)
        return smart_round(entry + atr * 2.0)

    def _tp1_short(entry, swings, ema9, ema21, atr):
        mx, mn = entry * 0.992, entry * 0.94
        cands = [l for l in swings if mn < l < mx]
        if cands: return smart_round(max(cands))
        for ma in [ema9, ema21]:
            if mn < ma < mx: return smart_round(ma)
        return smart_round(entry - atr * 2.0)

    fib_ext_long  = fib_extension(recent_m15_low,  recent_m15_high, price)
    fib_ext_short = fib_extension(recent_m15_high, recent_m15_low,  price)

    def _tp2(entry, tp1, fib_ext, direction):
        f127 = fib_ext.get("1.272", 0)
        f162 = fib_ext.get("1.618", 0)
        if direction == "LONG":
            if f127 > tp1 * 1.003 and f127 < entry * 1.20: return smart_round(f127)
            if f162 > tp1 * 1.003 and f162 < entry * 1.25: return smart_round(f162)
            return smart_round(tp1 + (tp1 - entry))
        else:
            if 0 < f127 < tp1 * 0.997 and f127 > entry * 0.80: return smart_round(f127)
            if 0 < f162 < tp1 * 0.997 and f162 > entry * 0.75: return smart_round(f162)
            return smart_round(tp1 - (entry - tp1))

    if direction == "LONG" or (direction == "WAIT" and h1_bias == "LONG"):
        entry    = price
        # SL: dưới swing low M15 gần nhất + buffer nhỏ, tối đa 1.5%
        sl_struct = recent_m15_low - atr_m15 * 0.3
        sl_price  = smart_round(min(entry * 0.992, max(sl_struct, entry * 0.985)))
        tp1 = _tp1_long(entry, swing_highs_m15, ema9_m15, ema21_m15, atr_m15)
        tp2 = _tp2(entry, tp1, fib_ext_long, "LONG")

    elif direction == "SHORT" or (direction == "WAIT" and h1_bias == "SHORT"):
        entry    = price
        sl_struct = recent_m15_high + atr_m15 * 0.3
        sl_price  = smart_round(max(entry * 1.008, min(sl_struct, entry * 1.015)))
        tp1 = _tp1_short(entry, swing_lows_m15, ema9_m15, ema21_m15, atr_m15)
        tp2 = _tp2(entry, tp1, fib_ext_short, "SHORT")

    else:
        entry = sl_price = tp1 = tp2 = price

    if direction == "LONG"  and tp2 <= tp1: tp2 = smart_round(tp1 + (tp1 - entry))
    if direction == "SHORT" and tp2 >= tp1: tp2 = smart_round(tp1 - (entry - tp1))

    sl_pct  = round(abs(entry - sl_price) / entry * 100, 2) if entry != sl_price else 0
    tp1_pct = round(abs(tp1 - entry) / entry * 100, 2)      if entry != tp1 else 0
    rr      = round(tp1_pct / sl_pct, 2)                    if sl_pct > 0 else 0

    if direction in ("LONG", "SHORT") and rr < 1.0:
        all_warnings.append(f"❌ R:R {rr} < 1.0 — chờ M15 pullback về EMA")
        direction  = "WAIT"
        confidence = "LOW"

    # ────────────────────────────────────────
    # ENTRY CHECKLIST & VERDICT
    # ────────────────────────────────────────
    def build_checklist(direction, m5_status, rr, rsi_m15, funding, oi_change, btc_ctx, confidence):
        checks = []

        if confidence == "HIGH":
            checks.append({"ok": True,  "text": "Confidence HIGH — H1+M15+M5 đồng thuận"})
        elif confidence == "MEDIUM":
            checks.append({"ok": None,  "text": "Confidence MEDIUM — chờ thêm 1–2 nến M15"})
        else:
            checks.append({"ok": False, "text": "Confidence LOW — tín hiệu yếu, không vào"})

        if m5_status == "CONFIRMED":
            checks.append({"ok": True,  "text": "M5 xác nhận entry — vào được ngay"})
        elif m5_status in ("OVERBOUGHT", "OVERSOLD"):
            checks.append({"ok": False, "text": f"RSI M5 cực đoan ({m5_status}) — không entry"})
        elif m5_status == "PULLBACK":
            checks.append({"ok": None,  "text": "M5 EMA chưa sẵn — chờ nến M5 tiếp theo"})
        else:
            checks.append({"ok": None,  "text": "M5 đang hình thành — theo dõi thêm"})

        if direction == "LONG":
            if rsi_m15 > 70:   checks.append({"ok": False, "text": f"RSI M15 {rsi_m15:.0f} — overbought, chờ RSI hạ xuống 60–65"})
            elif rsi_m15 >= 40:checks.append({"ok": True,  "text": f"RSI M15 {rsi_m15:.0f} — vùng an toàn cho LONG"})
            else:              checks.append({"ok": None,  "text": f"RSI M15 {rsi_m15:.0f} — hơi thấp, momentum yếu"})
        else:
            if rsi_m15 < 30:   checks.append({"ok": False, "text": f"RSI M15 {rsi_m15:.0f} — oversold, chờ RSI hồi về 35–40"})
            elif rsi_m15 <= 60:checks.append({"ok": True,  "text": f"RSI M15 {rsi_m15:.0f} — vùng an toàn cho SHORT"})
            else:              checks.append({"ok": None,  "text": f"RSI M15 {rsi_m15:.0f} — hơi cao, momentum yếu"})

        if rr >= 2.0:   checks.append({"ok": True,  "text": f"R:R 1:{rr} ≥ 1:2 — tốt"})
        elif rr >= 1.5: checks.append({"ok": True,  "text": f"R:R 1:{rr} ≥ 1:1.5 — chấp nhận"})
        elif rr >= 1.0: checks.append({"ok": None,  "text": f"R:R 1:{rr} — thấp, cân nhắc"})
        else:           checks.append({"ok": False, "text": f"R:R 1:{rr} < 1:1 — không vào"})

        if funding is not None:
            if direction == "LONG":
                if funding < -0.01:   checks.append({"ok": True,  "text": f"Funding {funding:+.4f}% âm — tốt cho LONG"})
                elif funding > 0.05:  checks.append({"ok": False, "text": f"Funding {funding:+.4f}% cao — chờ giảm"})
                else:                 checks.append({"ok": None,  "text": f"Funding {funding:+.4f}% trung tính"})
            else:
                if funding > 0.01:    checks.append({"ok": True,  "text": f"Funding {funding:+.4f}% dương — tốt cho SHORT"})
                elif funding < -0.05: checks.append({"ok": False, "text": f"Funding {funding:+.4f}% âm sâu — chờ tăng"})
                else:                 checks.append({"ok": None,  "text": f"Funding {funding:+.4f}% trung tính"})

        sentiment = btc_ctx.get("sentiment", "NEUTRAL")
        btc_chg   = btc_ctx.get("chg_24h", 0) or 0
        if sentiment == "RISK_ON" and direction == "LONG":
            checks.append({"ok": True,  "text": "BTC BULL — thị trường thuận cho LONG"})
        elif sentiment in ("RISK_OFF", "DUMP") and direction == "SHORT":
            checks.append({"ok": True,  "text": f"BTC giảm ({btc_chg:+.1f}%) — SHORT theo thị trường"})
        elif sentiment in ("RISK_OFF", "DUMP") and direction == "LONG":
            checks.append({"ok": False, "text": f"BTC BEAR ({btc_chg:+.1f}%) — không LONG scalp"})
        else:
            checks.append({"ok": None,  "text": f"BTC sideways ({btc_chg:+.1f}%) — xét tín hiệu mã riêng"})

        ok_c   = sum(1 for c in checks if c["ok"] is True)
        fail_c = sum(1 for c in checks if c["ok"] is False)

        if fail_c >= 2:         verdict = "NO"
        elif ok_c >= 4:         verdict = "GO"
        else:                   verdict = "WAIT"

        if confidence == "LOW": verdict = "NO" if fail_c >= 1 else "WAIT"
        elif confidence == "MEDIUM":
            if verdict == "GO": verdict = "WAIT"
        if m5_status in ("FORMING", "OVERBOUGHT", "OVERSOLD") and verdict == "GO":
            verdict = "WAIT"

        return checks, verdict

    entry_checklist, entry_verdict = build_checklist(
        direction, m5_status, rr, rsi_m15, funding, oi_change, btc_ctx, confidence
    )
    if direction == "WAIT": entry_verdict = "WAIT"

    # ── Chart candles — dùng M15 ──
    chart_df = df_m15.tail(80).reset_index()
    candles  = [{"t": int(r["open_time"].timestamp() * 1000),
                  "o": smart_round(r["open"]),  "h": smart_round(r["high"]),
                  "l": smart_round(r["low"]),   "c": smart_round(r["close"]),
                  "v": round(r["volume"], 2),
                  "ma34":  smart_round(r["ema9"]),    # slot ma34 = EMA9 cho scalp
                  "ma89":  smart_round(r["ema21"]),   # slot ma89 = EMA21 cho scalp
                  "ma200": smart_round(r["ma34"]),    # slot ma200 = MA34 H1 context
                  "vol_ratio": round(r["vol_ratio"], 2)}
                 for _, r in chart_df.iterrows()]

    return sanitize({
        "symbol":        symbol,
        "strategy":      "SCALP",
        "price":         smart_round(price),
        "direction":     direction,
        "confidence":    confidence,
        "score":         int(score),
        "conditions":    conditions,
        "warnings":      all_warnings,
        "no_trade_zone": False,
        "entry":         smart_round(entry),
        "entry_now":     smart_round(entry),
        "entry_opt":     None,
        "entry_opt_label": None,
        "entry_opt_rr":  None,
        "sl":            sl_price,
        "tp1":           tp1,
        "tp2":           tp2,
        "sl_pct":        sl_pct,
        "tp1_pct":       tp1_pct,
        "rr":            rr,
        "market": {
            "funding":     round(funding, 4) if funding is not None else None,
            "funding_pct": f"{funding:+.4f}%" if funding is not None else "N/A",
            "oi_change":   oi_change,
            "oi_str":      f"{oi_change:+.2f}%" if oi_change is not None else "N/A",
            "atr_ratio":   atr_ratio,
            "atr_state":   atr_state,
            "atr_note":    atr_note,
        },
        "btc_context": btc_ctx,
        "d1":  {"bias": h1_bias, "structure": "", "notes":
                [f"H1 EMA9 {'>' if h1_ema_bull else '<'} EMA21 — {'BULL' if h1_ema_bull else 'BEAR'}",
                 f"RSI H1: {rsi_h1:.0f}"]},
        "h4":  {"bias": h1_bias,
                "above_ma34": h1_ema_bull, "above_ma89": h1_price_above_ema21,
                "crossed_ma34": h1_ema_cross_up or h1_ema_cross_dn,
                "slope_ma34": slope_ema9_h1, "slope_ma89": "—", "slope_ma200": "—",
                "ma34": smart_round(ema9_h1), "ma89": smart_round(ema21_h1), "ma200": smart_round(ema21_h1),
                "structure": m15_structure, "notes": []},
        "h1":  {"fib_zone": "0.382-0.618",
                "fib_zone_price": f"{smart_round(f618)} – {smart_round(f382)}",
                "vol_ratio": round(vol_m15, 2), "h1_bullish": m15_bullish, "breakout": False,
                "rsi_m15": round(rsi_m15, 1), "rsi_m5": round(rsi_m5, 1)},
        "fib_ret":   fib_m15_ret,
        "fib_ext":   fib_ext_long if direction != "SHORT" else fib_ext_short,
        "swing_high": smart_round(recent_m15_high),
        "swing_low":  smart_round(recent_m15_low),
        "candles":    candles,
        "timestamp":  datetime.now().isoformat(),
        "h1_status":       m5_status,
        "h1_status_note":  m5_note,
        "entry_checklist": entry_checklist,
        "entry_verdict":   entry_verdict,
        "d1_bias":  h1_bias,
        "h4_bias":  h1_bias,
    })
