"""dashboard/swing_h1_engine.py — Swing H1 Strategy Engine.

Strategy: Swing H1 (4–24h hold)
─────────────────────────────────────────────────────
Tầng 1 — H4 Bias   : H4 xác định hướng tổng (thay cho D1)
Tầng 2 — H1 Confirm: H1 cross MA34/MA89, cấu trúc sóng H1
Tầng 3 — M15 Entry : M15 pullback về MA34 H1 hoặc Fib H1

SL:   ATR H1 × 1.5, giới hạn 2–3%
TP1:  Swing high/low H1 gần nhất (20 nến)
TP2:  Fib Extension 1.272 sóng H1

So với H4/D1: phát hiện setup nhanh hơn, R:R tương đương
nhưng noise nhiều hơn → cần H1 confirm rõ.
"""
import math
from datetime import datetime

from core.binance import (fetch_klines, fetch_funding_rate,
                           fetch_oi_change, fetch_btc_context)
from core.indicators import (prepare, ma_slope, find_swing_points,
                              classify_structure, fib_retracement,
                              fib_extension, is_no_trade_zone, calc_atr_context)
from core.utils import sanitize, smart_round


def swing_h1_analyze(symbol: str, cfg: dict) -> dict:
    """Phân tích theo strategy Swing H1."""
    ff = bool(cfg.get("force_futures", False))

    # Fetch: H4 (bias) + H1 (confirm + entry) + M15 (zone tinh chỉnh)
    df_h4  = prepare(fetch_klines(symbol, "4h",  200, force_futures=ff))
    df_h1  = prepare(fetch_klines(symbol, "1h",  200, force_futures=ff))
    df_m15 = prepare(fetch_klines(symbol, "15m", 100, force_futures=ff))

    for df in [df_h4, df_h1, df_m15]:
        if len(df) < 10:
            raise ValueError(f"Không đủ data cho {symbol}")

    price   = float(df_h1["close"].iloc[-1])
    row_h4  = df_h4.iloc[-1]
    prev_h4 = df_h4.iloc[-2]
    row_h1  = df_h1.iloc[-1]
    prev_h1 = df_h1.iloc[-2]
    row_m15 = df_m15.iloc[-1]

    # Market data
    funding   = fetch_funding_rate(symbol)
    oi_change = fetch_oi_change(symbol)
    btc_ctx   = fetch_btc_context()
    atr_ctx   = calc_atr_context(df_h4, df_h4)  # dùng H4 làm base
    atr_h1    = float(df_h1["atr"].iloc[-1])
    atr_m15   = float(df_m15["atr"].iloc[-1])

    # ────────────────────────────────────────
    # TẦNG 1 — H4 Bias (thay cho D1)
    # ────────────────────────────────────────
    h4_above_ma34 = float(row_h4["close"]) > float(row_h4["ma34"])
    h4_above_ma89 = float(row_h4["close"]) > float(row_h4["ma89"])
    h4_x_ma34_up  = prev_h4["close"] <= prev_h4["ma34"] and row_h4["close"] > row_h4["ma34"]
    h4_x_ma34_dn  = prev_h4["close"] >= prev_h4["ma34"] and row_h4["close"] < row_h4["ma34"]

    if h4_above_ma34 and h4_above_ma89:
        h4_bias = "LONG"
    elif not h4_above_ma34 and not h4_above_ma89:
        h4_bias = "SHORT"
    else:
        h4_bias = "NEUTRAL"

    slope_h4_ma34 = ma_slope(df_h4["ma34"])
    slope_h4_ma89 = ma_slope(df_h4["ma89"])
    highs_h4, lows_h4 = find_swing_points(df_h4, lookback=5)
    h4_structure  = classify_structure(highs_h4, lows_h4)

    # ────────────────────────────────────────
    # TẦNG 2 — H1 Confirmation (thay cho H4)
    # ────────────────────────────────────────
    h1_above_ma34 = float(row_h1["close"]) > float(row_h1["ma34"])
    h1_above_ma89 = float(row_h1["close"]) > float(row_h1["ma89"])
    h1_x_ma34_up  = prev_h1["close"] <= prev_h1["ma34"] and row_h1["close"] > row_h1["ma34"]
    h1_x_ma34_dn  = prev_h1["close"] >= prev_h1["ma34"] and row_h1["close"] < row_h1["ma34"]
    h1_x_ma89_up  = prev_h1["close"] <= prev_h1["ma89"] and row_h1["close"] > row_h1["ma89"]

    slope_h1_ma34 = ma_slope(df_h1["ma34"])
    slope_h1_ma89 = ma_slope(df_h1["ma89"])

    highs_h1, lows_h1 = find_swing_points(df_h1, lookback=3)
    h1_structure  = classify_structure(highs_h1, lows_h1)

    h1_bullish = row_h1["close"] > row_h1["open"]
    h1_bearish = row_h1["close"] < row_h1["open"]
    vol_ratio  = float(row_h1["vol_ratio"])
    vol_confirm = vol_ratio > 1.3

    # Swing H1 gần đây — dùng cho TP1
    df_h1_recent = df_h1.iloc[-20:]
    highs_h1_rec, lows_h1_rec = find_swing_points(df_h1_recent, lookback=2)
    swing_highs_h1 = sorted([v for _, v in highs_h1_rec], reverse=True)
    swing_lows_h1  = sorted([v for _, v in lows_h1_rec])

    recent_h1_high = float(df_h1["high"].iloc[-20:].max())
    recent_h1_low  = float(df_h1["low"].iloc[-20:].min())

    # ────────────────────────────────────────
    # TẦNG 3 — M15 Entry Zone
    # ────────────────────────────────────────
    m15_above_ma34 = float(row_m15["close"]) > float(row_m15["ma34"])
    m15_bullish    = row_m15["close"] > row_m15["open"]
    m15_bearish    = row_m15["close"] < row_m15["open"]
    m15_vol_ratio  = float(row_m15["vol_ratio"])

    # Fib H1 — vùng pullback để entry
    fib_h1_ret = fib_retracement(recent_h1_high, recent_h1_low)
    f05_h1 = fib_h1_ret.get("0.500", price)
    f618_h1 = fib_h1_ret.get("0.618", price)
    in_fib_h1 = min(f618_h1, f05_h1) * 0.998 <= price <= max(f618_h1, f05_h1) * 1.002

    no_trade, no_trade_detail = is_no_trade_zone(price, row_h1)  # check H1 thay H4

    # ── M15 status ──
    def get_m15_status(direction):
        if direction == "LONG":
            if m15_above_ma34 and m15_bullish and m15_vol_ratio > 1.2:
                return "CONFIRMED", "✅ M15 đang LONG — momentum ngắn tốt, entry zone rõ"
            elif not m15_above_ma34:
                return "PULLBACK", "⏳ M15 đang pullback về MA34 H1 — chờ nến M15 xanh xác nhận"
            else:
                return "FORMING", "⏳ M15 chưa rõ — theo dõi thêm 1–2 nến M15"
        else:
            if not m15_above_ma34 and m15_bearish and m15_vol_ratio > 1.2:
                return "CONFIRMED", "✅ M15 đang SHORT — momentum ngắn tốt, entry zone rõ"
            elif m15_above_ma34:
                return "PULLBACK", "⏳ M15 đang retest MA34 H1 — chờ nến M15 đỏ xác nhận"
            else:
                return "FORMING", "⏳ M15 chưa rõ — theo dõi thêm 1–2 nến M15"

    m15_status, m15_note = get_m15_status(h4_bias)

    # ────────────────────────────────────────
    # DIRECTION & SCORING
    # ────────────────────────────────────────
    warnings = []
    if no_trade:
        warnings.append(f"⚠️ Giá kẹt trong vùng MA H1 — {no_trade_detail}")

    conditions = []

    if h4_bias == "NEUTRAL" or no_trade:
        direction  = "WAIT"
        confidence = "LOW"
        score      = 0
    else:
        direction = h4_bias  # H4 quyết định hướng

        if h4_bias == "LONG":
            if h4_above_ma34:     conditions.append("H4 trên MA34 — bias LONG")
            if h4_above_ma89:     conditions.append("H4 trên MA89 — trend mạnh")
            if h4_x_ma34_up:      conditions.append("KEY: Vừa vượt MA34 H4 ↑")
            if h4_structure == "UPTREND": conditions.append("H4 cấu trúc UPTREND (HH HL)")
            if h1_above_ma34:     conditions.append("H1 trên MA34 — xác nhận LONG")
            if h1_x_ma34_up:      conditions.append("KEY: Vừa vượt MA34 H1 ↑")
            if h1_x_ma89_up:      conditions.append("KEY: Vừa vượt MA89 H1 ↑")
            if h1_structure == "UPTREND": conditions.append("H1 cấu trúc UPTREND")
            if h1_bullish and vol_confirm: conditions.append(f"H1 nến xanh xác nhận, vol {vol_ratio:.1f}x")
            if in_fib_h1:         conditions.append("H1 trong vùng Fib 0.5-0.618")
            if slope_h1_ma34 == "UP": conditions.append("MA34 H1 slope ↑")
        else:  # SHORT
            if not h4_above_ma34: conditions.append("H4 dưới MA34 — bias SHORT")
            if not h4_above_ma89: conditions.append("H4 dưới MA89 — trend mạnh")
            if h4_x_ma34_dn:      conditions.append("KEY: Vừa break MA34 H4 ↓")
            if h4_structure == "DOWNTREND": conditions.append("H4 cấu trúc DOWNTREND (LH LL)")
            if not h1_above_ma34: conditions.append("H1 dưới MA34 — xác nhận SHORT")
            if h1_x_ma34_dn:      conditions.append("KEY: Vừa break MA34 H1 ↓")
            if h1_structure == "DOWNTREND": conditions.append("H1 cấu trúc DOWNTREND")
            if h1_bearish and vol_confirm: conditions.append(f"H1 nến đỏ xác nhận, vol {vol_ratio:.1f}x")
            if in_fib_h1:         conditions.append("H1 trong vùng Fib 0.5-0.618")
            if slope_h1_ma34 == "DOWN": conditions.append("MA34 H1 slope ↓")

        score = len(conditions)
        confidence = "HIGH" if score >= 4 else "MEDIUM" if score >= 2 else "LOW"

    # Funding / ATR adjustments
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
    atr_warns = []
    if atr_ctx["atr_state"] in ("COMPRESS", "EXPAND"):
        atr_warns.append(f"⚠️ {atr_ctx['atr_note']}")

    all_warnings = warnings + atr_warns + funding_warns
    if direction == "LONG" and btc_ctx["sentiment"] in ("RISK_OFF", "DUMP"):
        all_warnings.insert(0, f"⚠️ Market: {btc_ctx['note']}")
    elif direction == "SHORT" and btc_ctx["sentiment"] in ("RISK_ON",):
        all_warnings.insert(0, f"⚠️ Market: {btc_ctx['note']}")

    total_adj = funding_adj + atr_ctx["score_adj"]
    if total_adj <= -2 and confidence != "LOW":
        confidence = "LOW"
        all_warnings.append("⚠️ Confidence hạ LOW do funding/volatility bất lợi")
    elif total_adj == -1 and confidence == "HIGH":
        confidence = "MEDIUM"

    # ────────────────────────────────────────
    # SL / TP — dựa ATR H1, swing H1 gần nhất
    # ────────────────────────────────────────
    ma34_h1 = float(row_h1["ma34"])
    ma89_h1 = float(row_h1["ma89"])
    ma34_h4 = float(row_h4["ma34"])

    # TP1: swing high/low H1 gần nhất trong 2–10%
    def _tp1_long(entry, swings, ma34, ma89, atr):
        mn, mx = entry * 1.015, entry * 1.10
        cands = [h for h in swings if mn < h < mx]
        if cands: return smart_round(min(cands))
        for ma in [ma34, ma89]:
            if mn < ma < mx: return smart_round(ma)
        return smart_round(max(entry * 1.02, entry + atr * 2.5))

    def _tp1_short(entry, swings, ma34, ma89, atr):
        mx, mn = entry * 0.985, entry * 0.90
        cands = [l for l in swings if mn < l < mx]
        if cands: return smart_round(max(cands))
        for ma in [ma34, ma89]:
            if mn < ma < mx: return smart_round(ma)
        return smart_round(min(entry * 0.98, entry - atr * 2.5))

    # TP2: Fib Ext H1
    fib_ext_h1_long  = fib_extension(recent_h1_low, recent_h1_high, price)
    fib_ext_h1_short = fib_extension(recent_h1_high, recent_h1_low, price)

    def _tp2_long(entry, tp1, fib_ext):
        f127 = fib_ext.get("1.272", 0)
        f162 = fib_ext.get("1.618", 0)
        if f127 > tp1 * 1.005 and f127 < entry * 1.30: return smart_round(f127)
        if f162 > tp1 * 1.005 and f162 < entry * 1.40: return smart_round(f162)
        return smart_round(tp1 + (tp1 - entry))

    def _tp2_short(entry, tp1, fib_ext):
        f127 = fib_ext.get("1.272", 0)
        f162 = fib_ext.get("1.618", 0)
        if 0 < f127 < tp1 * 0.995 and f127 > entry * 0.70: return smart_round(f127)
        if 0 < f162 < tp1 * 0.995 and f162 > entry * 0.60: return smart_round(f162)
        return smart_round(tp1 - (entry - tp1))

    if direction == "LONG" or (direction == "WAIT" and h4_bias == "LONG"):
        entry    = price
        # SL: dưới swing low H1 gần nhất + buffer ATR × 0.5, giới hạn 2–3%
        sl_struct = recent_h1_low - atr_h1 * 0.5
        sl_price  = smart_round(min(entry * 0.98, max(sl_struct, entry * 0.97)))
        tp1 = _tp1_long(entry, swing_highs_h1, ma34_h1, ma89_h1, atr_h1)
        tp2 = _tp2_long(entry, tp1, fib_ext_h1_long)

    elif direction == "SHORT" or (direction == "WAIT" and h4_bias == "SHORT"):
        entry    = price
        sl_struct = recent_h1_high + atr_h1 * 0.5
        sl_price  = smart_round(max(entry * 1.02, min(sl_struct, entry * 1.03)))
        tp1 = _tp1_short(entry, swing_lows_h1, ma34_h1, ma89_h1, atr_h1)
        tp2 = _tp2_short(entry, tp1, fib_ext_h1_short)

    else:
        entry = sl_price = tp1 = tp2 = price

    # Clamp TP2
    if direction == "LONG" and tp2 <= tp1:
        tp2 = smart_round(tp1 + (tp1 - entry))
    if direction == "SHORT" and tp2 >= tp1:
        tp2 = smart_round(tp1 - (entry - tp1))

    sl_pct  = round(abs(entry - sl_price) / entry * 100, 2) if entry != sl_price else 0
    tp1_pct = round(abs(tp1 - entry) / entry * 100, 2)      if entry != tp1 else 0
    rr      = round(tp1_pct / sl_pct, 2)                    if sl_pct > 0 else 0

    # Drop nếu R:R < 1
    if direction in ("LONG", "SHORT") and rr < 1.0:
        all_warnings.append(f"❌ R:R {rr} < 1.0 — chờ H1 pullback về MA")
        direction  = "WAIT"
        confidence = "LOW"

    # ────────────────────────────────────────
    # ENTRY CHECKLIST & VERDICT
    # ────────────────────────────────────────
    def build_checklist(direction, m15_status, rr, funding, oi_change, btc_ctx, no_trade, confidence):
        checks = []

        if confidence == "HIGH":
            checks.append({"ok": True,  "text": "Confidence HIGH — tín hiệu H4+H1 đủ mạnh"})
        elif confidence == "MEDIUM":
            checks.append({"ok": None,  "text": "Confidence MEDIUM — chờ thêm 1–2 nến H1 xác nhận"})
        else:
            checks.append({"ok": False, "text": "Confidence LOW — tín hiệu yếu, không vào"})

        if m15_status == "CONFIRMED":
            checks.append({"ok": True,  "text": "M15 xác nhận entry zone — vào được"})
        elif m15_status == "PULLBACK":
            checks.append({"ok": None,  "text": "M15 đang pullback — chờ nến M15 xác nhận"})
        else:
            checks.append({"ok": None,  "text": "M15 chưa rõ — theo dõi thêm"})

        if not no_trade:
            checks.append({"ok": True,  "text": "H1 nằm ngoài vùng kẹt MA — tín hiệu rõ"})
        else:
            checks.append({"ok": None,  "text": "H1 kẹt giữa MA34-MA89 — chờ thoát ra"})

        if rr >= 2.0:
            checks.append({"ok": True,  "text": f"R:R 1:{rr} ≥ 1:2 — tốt"})
        elif rr >= 1.5:
            checks.append({"ok": True,  "text": f"R:R 1:{rr} ≥ 1:1.5 — chấp nhận"})
        elif rr >= 1.0:
            checks.append({"ok": None,  "text": f"R:R 1:{rr} — thấp, cân nhắc chờ entry tốt hơn"})
        else:
            checks.append({"ok": False, "text": f"R:R 1:{rr} < 1:1 — không vào"})

        if funding is not None:
            dir_vn = direction
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
            checks.append({"ok": True,  "text": f"BTC BULL — thị trường thuận cho LONG"})
        elif sentiment in ("RISK_OFF", "DUMP") and direction == "SHORT":
            checks.append({"ok": True,  "text": f"BTC giảm ({btc_chg:+.1f}%) — SHORT theo thị trường"})
        elif sentiment in ("RISK_OFF", "DUMP") and direction == "LONG":
            checks.append({"ok": False, "text": f"BTC BEAR ({btc_chg:+.1f}%) — không LONG"})
        else:
            checks.append({"ok": None,  "text": f"BTC sideways ({btc_chg:+.1f}%) — xét tín hiệu mã riêng"})

        if oi_change is not None:
            if direction == "LONG":
                if oi_change > 5:    checks.append({"ok": True,  "text": f"OI +{oi_change}% — dòng tiền vào, hỗ trợ LONG"})
                elif oi_change < -5: checks.append({"ok": False, "text": f"OI {oi_change}% — vị thế đóng, chờ ổn định"})
                else:                checks.append({"ok": None,  "text": f"OI {oi_change:+.1f}% — chưa rõ xu hướng"})
            else:
                if oi_change < -5:   checks.append({"ok": True,  "text": f"OI {oi_change}% — Long đóng, hỗ trợ SHORT"})
                elif oi_change > 5:  checks.append({"ok": False, "text": f"OI +{oi_change}% — Long vào mạnh, rủi ro SHORT"})
                else:                checks.append({"ok": None,  "text": f"OI {oi_change:+.1f}% — chưa rõ xu hướng"})

        ok_c   = sum(1 for c in checks if c["ok"] is True)
        fail_c = sum(1 for c in checks if c["ok"] is False)
        if fail_c >= 2:          verdict = "NO"
        elif ok_c >= 4:          verdict = "GO"
        else:                    verdict = "WAIT"

        if confidence == "LOW":  verdict = "NO" if fail_c >= 1 else "WAIT"
        elif confidence == "MEDIUM":
            if verdict == "GO":  verdict = "WAIT"
        if m15_status == "FORMING" and verdict == "GO":
            verdict = "WAIT"

        return checks, verdict

    entry_checklist, entry_verdict = build_checklist(
        direction, m15_status, rr, funding, oi_change, btc_ctx, no_trade, confidence
    )

    if confidence == "LOW" and entry_verdict == "GO":
        entry_verdict = "WAIT"
    if direction == "WAIT":
        entry_verdict = "NO" if rr < 1.0 else "WAIT"

    # ── Chart candles (H1 thay vì H4) ──
    chart_df = df_h1.tail(80).reset_index()
    candles  = [{"t": int(r["open_time"].timestamp() * 1000),
                  "o": smart_round(r["open"]),  "h": smart_round(r["high"]),
                  "l": smart_round(r["low"]),   "c": smart_round(r["close"]),
                  "v": round(r["volume"], 2),
                  "ma34": smart_round(r["ma34"]), "ma89": smart_round(r["ma89"]),
                  "ma200": smart_round(r["ma200"]),
                  "vol_ratio": round(r["vol_ratio"], 2)}
                 for _, r in chart_df.iterrows()]

    return sanitize({
        "symbol":        symbol,
        "strategy":      "SWING_H1",
        "price":         smart_round(price),
        "direction":     direction,
        "confidence":    confidence,
        "score":         int(score),
        "conditions":    conditions,
        "warnings":      all_warnings,
        "no_trade_zone": bool(no_trade),
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
            "atr_ratio":   atr_ctx["atr_ratio"],
            "atr_state":   atr_ctx["atr_state"],
            "atr_note":    atr_ctx["atr_note"],
        },
        "btc_context": btc_ctx,
        "d1":  {"bias": h4_bias, "structure": h4_structure, "notes": []},  # d1 slot = H4 bias trong strategy này
        "h4":  {"bias": h4_bias, "structure": h4_structure,
                "above_ma34": h4_above_ma34, "above_ma89": h4_above_ma89,
                "crossed_ma34": h4_x_ma34_up, "slope_ma34": slope_h4_ma34,
                "slope_ma89": slope_h4_ma89, "slope_ma200": "—",
                "ma34": smart_round(row_h4["ma34"]),
                "ma89": smart_round(row_h4["ma89"]),
                "ma200": smart_round(row_h4["ma200"])},
        "h1":  {"fib_zone": "0.5-0.618", "fib_zone_price": f"{smart_round(f618_h1)} – {smart_round(f05_h1)}",
                "vol_ratio": round(vol_ratio, 2), "h1_bullish": h1_bullish, "breakout": False},
        "fib_ret":   fib_h1_ret,
        "fib_ext":   fib_ext_h1_long if direction != "SHORT" else fib_ext_h1_short,
        "swing_high": smart_round(recent_h1_high),
        "swing_low":  smart_round(recent_h1_low),
        "candles":    candles,
        "timestamp":  datetime.now().isoformat(),
        "h1_status":       m15_status,
        "h1_status_note":  m15_note,
        "entry_checklist": entry_checklist,
        "entry_verdict":   entry_verdict,
        "d1_bias":  h4_bias,
        "h4_bias":  h4_bias,
    })
