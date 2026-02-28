"""dashboard/engine.py — FAM Signal Engine. Chỉ sửa file này khi thay đổi logic Dashboard."""
import math
from datetime import datetime

from core.binance import (fetch_klines, fetch_funding_rate,
                           fetch_oi_change, fetch_btc_context)
from core.indicators import (prepare, ma_slope, find_swing_points,
                              classify_structure, fib_retracement,
                              fib_extension, is_no_trade_zone, calc_atr_context)
from core.utils import sanitize, smart_round


def _interpret_funding(funding, oi_change, direction):
    warnings, adj = [], 0
    if funding is None: return warnings, adj
    if direction == "LONG":
        if funding > 0.05:
            warnings.append(f"⚠️ Funding {funding:+.4f}% — Long overcrowded, rủi ro squeeze")
            adj -= 1
        elif funding < -0.03:
            warnings.append(f"✅ Funding {funding:+.4f}% — Short overcrowded, có lợi cho LONG")
    elif direction == "SHORT":
        if funding < -0.05:
            warnings.append(f"⚠️ Funding {funding:+.4f}% — Short overcrowded, rủi ro squeeze")
            adj -= 1
        elif funding > 0.03:
            warnings.append(f"✅ Funding {funding:+.4f}% — Long overcrowded, có lợi cho SHORT")
    return warnings, adj


def fam_analyze(symbol: str, cfg: dict) -> dict:
    # ── Fetch data ──
    ff = bool(cfg.get("force_futures", False))
    df_d1 = prepare(fetch_klines(symbol, "1d", 300, force_futures=ff))
    df_h4 = prepare(fetch_klines(symbol, "4h", 300, force_futures=ff))
    df_h1 = prepare(fetch_klines(symbol, "1h", 150, force_futures=ff))

    for df in [df_d1, df_h4, df_h1]:
        if len(df) < 10:
            raise ValueError(f"Không đủ data cho {symbol}")

    price    = float(df_h1["close"].iloc[-1])
    row_d1   = df_d1.iloc[-1]
    row_h4   = df_h4.iloc[-1]
    prev_h4  = df_h4.iloc[-2]
    row_h1   = df_h1.iloc[-1]

    # ── Fetch market data ──
    funding   = fetch_funding_rate(symbol)
    oi_change = fetch_oi_change(symbol)
    atr_ctx   = calc_atr_context(df_h4, df_d1)
    btc_ctx   = fetch_btc_context()
    atr_h1    = float(df_h1["atr"].iloc[-1])

    # ────────────────────────────────────────
    # TẦNG 1 — D1 Bias
    # ────────────────────────────────────────
    dist_ma34_d1  = (price - float(row_d1["ma34"])) / float(row_d1["ma34"]) * 100
    dist_ma89_d1  = (price - float(row_d1["ma89"])) / float(row_d1["ma89"]) * 100
    far_from_ma   = abs(dist_ma34_d1) > 8 or abs(dist_ma89_d1) > 8

    if price > row_d1["ma34"] and price > row_d1["ma89"]:
        d1_bias = "LONG"
    elif price < row_d1["ma34"] and price < row_d1["ma89"]:
        d1_bias = "SHORT"
    else:
        d1_bias = "NEUTRAL"

    highs_d1, lows_d1 = find_swing_points(df_d1, lookback=3)
    d1_structure = classify_structure(highs_d1, lows_d1)
    d1_notes = []
    if far_from_ma:
        d1_notes.append(f"D1 xa MA ({dist_ma34_d1:+.1f}%) — nguy cơ hồi về MA")
    if d1_structure == "UPTREND":
        d1_notes.append("D1 cấu trúc UPTREND (HH HL)")
    elif d1_structure == "DOWNTREND":
        d1_notes.append("D1 cấu trúc DOWNTREND (LH LL)")

    # ────────────────────────────────────────
    # TẦNG 2 — H4 Bias
    # ────────────────────────────────────────
    h4_above_ma34 = bool(row_h4["close"] > row_h4["ma34"])
    h4_above_ma89 = bool(row_h4["close"] > row_h4["ma89"])
    h4_x_ma34_up  = bool(prev_h4["close"] <= prev_h4["ma34"] and row_h4["close"] > row_h4["ma34"])
    h4_x_ma34_dn  = bool(prev_h4["close"] >= prev_h4["ma34"] and row_h4["close"] < row_h4["ma34"])

    if h4_above_ma34 and h4_above_ma89:   h4_bias = "LONG"
    elif not h4_above_ma34 and not h4_above_ma89: h4_bias = "SHORT"
    else:                                  h4_bias = "NEUTRAL"

    slope_ma34 = ma_slope(df_h4["ma34"])
    slope_ma89 = ma_slope(df_h4["ma89"])
    slope_ma200= ma_slope(df_h4["ma200"])

    highs_h4, lows_h4 = find_swing_points(df_h4, lookback=5)
    h4_structure = classify_structure(highs_h4, lows_h4)
    h4_notes = []
    if h4_above_ma34: h4_notes.append("H4 trên MA34, slope " + slope_ma34)
    if h4_above_ma89: h4_notes.append("H4 trên MA89, slope " + slope_ma89)
    if h4_x_ma34_up:  h4_notes.append("KEY: Vừa vượt MA34 H4 ↑")
    if h4_x_ma34_dn:  h4_notes.append("KEY: Vừa break MA34 H4 ↓")
    if h4_structure != "SIDEWAYS":
        h4_notes.append(f"H4 cấu trúc sóng {h4_structure} (HH HL)")

    # ────────────────────────────────────────
    # TẦNG 3 — H1 Confirmation
    # ────────────────────────────────────────
    no_trade, no_trade_detail = is_no_trade_zone(price, row_h4)

    recent_h = float(df_h1["high"].iloc[-60:].max())
    recent_l  = float(df_h1["low"].iloc[-60:].min())
    fib_ret   = fib_retracement(recent_h, recent_l)
    sh, sl_   = recent_h, recent_l
    fib_ext   = fib_extension(sl_, sh, recent_l)

    f05, f618 = fib_ret["0.500"], fib_ret["0.618"]
    fib_zone  = "0.5-0.618"
    in_fib    = min(f618, f05) * 0.998 <= price <= max(f618, f05) * 1.002
    fib_zone_price = f"{smart_round(f618)} – {smart_round(f05)}"

    h1_bullish      = bool(row_h1["close"] > row_h1["open"])
    h1_bearish      = bool(row_h1["close"] < row_h1["open"])
    h1_breakout     = bool(row_h1["close"] > recent_h * 0.998)
    vol_ratio       = float(row_h1["vol_ratio"])
    vol_confirm     = vol_ratio > 1.3

    # ── H1 Status — đánh giá momentum H1 cho entry decision ──
    h1_ma34_slope  = ma_slope(df_h1["ma34"], n=3)
    h1_above_ma7   = float(row_h1["close"]) > float(df_h1["close"].rolling(7, min_periods=1).mean().iloc[-1])
    h1_above_ma25  = float(row_h1["close"]) > float(df_h1["close"].rolling(25, min_periods=1).mean().iloc[-1])

    # Đếm nến đỏ/xanh 5 nến gần nhất H1
    last5_closes = df_h1["close"].iloc[-5:].values
    last5_opens  = df_h1["open"].iloc[-5:].values
    bear_count   = sum(1 for i in range(5) if last5_closes[i] < last5_opens[i])
    bull_count   = 5 - bear_count

    def get_h1_status(direction):
        if direction == "LONG":
            if h1_above_ma7 and h1_above_ma25 and h1_ma34_slope == "UP" and bull_count >= 3:
                return "CONFIRMED", "✅ H1 xác nhận LONG — momentum tốt, có thể xem xét entry"
            elif bear_count >= 4:
                return "COUNTER", "⚠️ H1 đang pullback mạnh ({} nến đỏ/5) — chờ đáy hình thành".format(bear_count)
            elif h1_ma34_slope == "DOWN" or not h1_above_ma25:
                return "PULLBACK", "⏳ H1 đang pullback về MA — chờ nến xanh xác nhận vùng support"
            else:
                return "FORMING", "⏳ H1 chưa rõ hướng — theo dõi thêm 1-2 nến"
        elif direction == "SHORT":
            if not h1_above_ma7 and not h1_above_ma25 and h1_ma34_slope == "DOWN" and bear_count >= 3:
                return "CONFIRMED", "✅ H1 xác nhận SHORT — momentum tốt, có thể xem xét entry"
            elif bull_count >= 4:
                return "COUNTER", "⚠️ H1 đang bounce mạnh ({} nến xanh/5) — chờ đỉnh hình thành".format(bull_count)
            elif h1_ma34_slope == "UP" or h1_above_ma25:
                return "PULLBACK", "⏳ H1 đang retest MA — chờ nến đỏ xác nhận vùng resistance"
            else:
                return "FORMING", "⏳ H1 chưa rõ hướng — theo dõi thêm 1-2 nến"
        return "NEUTRAL", "— Không có tín hiệu H1"

    h1_status, h1_status_note = get_h1_status(d1_bias)

    # Entry checklist tự động
    def build_entry_checklist(direction, h1_status, rr, funding, oi_change, btc_ctx, no_trade, confidence="LOW"):
        checks = []
        # 1. H1 confirmation
        if h1_status == "CONFIRMED":
            checks.append({"ok": True,  "text": "H1 momentum xác nhận hướng"})
        elif h1_status == "PULLBACK":
            checks.append({"ok": False, "text": "H1 đang pullback — chờ nến đóng cửa xác nhận"})
        elif h1_status == "COUNTER":
            checks.append({"ok": False, "text": "H1 counter-trend mạnh — không vào lúc này"})
        else:
            checks.append({"ok": None,  "text": "H1 chưa rõ — theo dõi thêm"})

        # 2. No-trade zone
        checks.append({"ok": not no_trade,
                        "text": "Không trong vùng lưng chừng MA34/89" if not no_trade
                                else "Đang trong no-trade zone — chờ thoát rõ hướng"})

        # 3. R:R
        if rr >= 2.0:
            checks.append({"ok": True,  "text": f"R:R {rr} ≥ 2.0 — xuất sắc"})
        elif rr >= 1.5:
            checks.append({"ok": True,  "text": f"R:R {rr} ≥ 1.5 — chấp nhận được"})
        else:
            checks.append({"ok": False, "text": f"R:R {rr} < 1.5 — cân nhắc kỹ hoặc chờ"})

        # 4. Funding
        if funding is not None:
            if direction == "LONG":
                if funding < 0:
                    checks.append({"ok": True,  "text": f"Funding {funding:+.4f}% âm — có lợi cho LONG"})
                elif funding > 0.05:
                    checks.append({"ok": False, "text": f"Funding {funding:+.4f}% cao — Long overcrowded"})
                else:
                    checks.append({"ok": None,  "text": f"Funding {funding:+.4f}% neutral"})
            else:
                if funding > 0:
                    checks.append({"ok": True,  "text": f"Funding {funding:+.4f}% dương — có lợi cho SHORT"})
                elif funding < -0.05:
                    checks.append({"ok": False, "text": f"Funding {funding:+.4f}% thấp — Short overcrowded"})
                else:
                    checks.append({"ok": None,  "text": f"Funding {funding:+.4f}% neutral"})

        # 5. BTC context
        if btc_ctx.get("sentiment") in ("RISK_ON",) and direction == "LONG":
            checks.append({"ok": True,  "text": "BTC trend BULL — thị trường thuận cho LONG"})
        elif btc_ctx.get("sentiment") in ("RISK_OFF", "DUMP") and direction == "LONG":
            checks.append({"ok": False, "text": f"BTC {btc_ctx.get('sentiment')} — thận trọng LONG altcoin"})
        elif btc_ctx.get("sentiment") in ("RISK_OFF", "DUMP") and direction == "SHORT":
            checks.append({"ok": True,  "text": "BTC downtrend — thuận cho SHORT"})
        else:
            checks.append({"ok": None,  "text": f"BTC {btc_ctx.get('sentiment','?')} — neutral"})

        # 6. OI
        if oi_change is not None:
            if direction == "LONG":
                if oi_change > 5:
                    checks.append({"ok": True,  "text": f"OI +{oi_change}% — tiền đang vào, Long có lợi"})
                elif oi_change < -5:
                    checks.append({"ok": False, "text": f"OI {oi_change}% giảm mạnh — tiền rút ra"})
                else:
                    checks.append({"ok": None,  "text": f"OI {oi_change:+.2f}% neutral"})
            else:  # SHORT
                if oi_change < -5:
                    checks.append({"ok": True,  "text": f"OI {oi_change}% — Long đang đóng, SHORT có lợi"})
                elif oi_change > 5:
                    checks.append({"ok": False, "text": f"OI +{oi_change}% tăng mạnh — Long đang vào, rủi ro squeeze SHORT"})
                else:
                    checks.append({"ok": None,  "text": f"OI {oi_change:+.2f}% neutral"})

        # Verdict
        ok_count   = sum(1 for c in checks if c["ok"] is True)
        fail_count = sum(1 for c in checks if c["ok"] is False)
        if fail_count >= 2:
            verdict = "NO"
        elif fail_count == 0 and ok_count >= 4:
            verdict = "GO"
        else:
            verdict = "WAIT"

        # Override theo confidence
        if confidence == "LOW":
            verdict = "NO" if fail_count >= 1 else "WAIT"
        elif confidence == "MEDIUM":
            if verdict == "GO": verdict = "WAIT"  # MEDIUM tối đa WAIT
        # H1 chưa rõ → không GO
        if h1_status in ("FORMING", "COUNTER"):
            if verdict == "GO": verdict = "WAIT"

        return checks, verdict

    entry_checklist, entry_verdict = build_entry_checklist(
        d1_bias, h1_status, 0, funding, oi_change, btc_ctx, no_trade, "LOW"
    )  # rr=0 placeholder, confidence chưa có — sẽ recalc sau

    # ────────────────────────────────────────
    # DIRECTION & SCORING
    # ────────────────────────────────────────
    warnings = []
    if no_trade:
        warnings.append(f"⚠️ Vùng lưng chừng — {no_trade_detail}")

    if d1_bias == "NEUTRAL" or h4_bias == "NEUTRAL" or no_trade:
        direction  = "WAIT"
        confidence = "LOW"
        conditions = warnings
        score = 0
    else:
        if d1_bias != h4_bias:
            direction  = "WAIT"
            confidence = "LOW"
            conditions = [f"D1 {d1_bias} vs H4 {h4_bias} — conflict timeframe"]
            score = 0
        else:
            direction = d1_bias
            conditions_long, conditions_short = [], []

            if d1_bias == "LONG":
                conditions_long.append("D1 bias LONG (trên MA34/89)")
            else:
                conditions_short.append("D1 bias SHORT (dưới MA34/89)")

            if h4_bias == "LONG":
                conditions_long += [n for n in h4_notes if "trên" in n or "KEY" in n or "UP" in n]
            else:
                conditions_short += [n for n in h4_notes if "dưới" in n or "KEY" in n or "DOWN" in n]

            if h4_structure == "UPTREND" and direction == "LONG":
                conditions_long.append("H4 cấu trúc sóng UPTREND (HH HL)")
            if h4_structure == "DOWNTREND" and direction == "SHORT":
                conditions_short.append("H4 cấu trúc sóng DOWNTREND (LH LL)")

            if direction == "LONG":
                if h1_bullish and vol_confirm:  conditions_long.append(f"H1 nến xanh xác nhận, vol {vol_ratio:.1f}x")
                if in_fib:                       conditions_long.append(f"H1 trong Fib {fib_zone}")
                if h4_x_ma34_up:                conditions_long.append("KEY: Vừa vượt MA34 H4 ↑")
                conditions = conditions_long
            else:
                if h1_bearish and vol_confirm:  conditions_short.append(f"H1 nến đỏ xác nhận, vol {vol_ratio:.1f}x")
                if in_fib:                       conditions_short.append(f"H1 trong Fib {fib_zone}")
                if h4_x_ma34_dn:                conditions_short.append("KEY: Vừa break MA34 H4 ↓")
                conditions = conditions_short

            score = len(conditions)
            confidence = "HIGH" if score >= 4 else "MEDIUM" if score >= 2 else "LOW"

    # ── ATR & Funding adjustments ──
    atr_warnings = []
    if atr_ctx["atr_state"] == "COMPRESS":
        atr_warnings.append(f"⚠️ {atr_ctx['atr_note']}")
    elif atr_ctx["atr_state"] == "EXPAND":
        atr_warnings.append(f"⚠️ {atr_ctx['atr_note']}")
    atr_score_adj = atr_ctx["score_adj"]

    funding_warnings, funding_score_adj = _interpret_funding(funding, oi_change, direction)
    all_warnings = warnings + atr_warnings + funding_warnings

    # BTC context warning
    if direction == "LONG" and btc_ctx["sentiment"] in ("RISK_OFF", "DUMP"):
        all_warnings.insert(0, f"⚠️ Market: {btc_ctx['note']}")
    elif direction == "SHORT" and btc_ctx["sentiment"] in ("RISK_ON", "PUMP"):
        all_warnings.insert(0, f"⚠️ Market: {btc_ctx['note']}")

    total_adj = funding_score_adj + atr_score_adj
    if total_adj <= -2 and confidence != "LOW":
        confidence = "LOW"
        all_warnings.append("⚠️ Confidence hạ xuống LOW do funding/volatility bất lợi")
    elif total_adj == -1 and confidence == "HIGH":
        confidence = "MEDIUM"

    # ────────────────────────────────────────
    # SL / TP
    # ────────────────────────────────────────
    recent_h1_high = float(df_h1["high"].iloc[-20:].max())
    recent_h1_low  = float(df_h1["low"].iloc[-20:].min())

    if direction == "LONG" or (direction == "WAIT" and h4_bias == "LONG"):
        entry    = price
        sl_struct = recent_h1_low - atr_h1 * 0.5
        sl_price  = smart_round(min(entry * 0.99, max(sl_struct, entry * 0.96)))

        tp1 = None
        for ma in [float(row_h4["ma34"]), float(row_h4["ma89"]), float(row_h4["ma200"])]:
            if ma > entry * 1.005:
                tp1 = smart_round(ma); break
        if tp1 is None: tp1 = smart_round(entry + atr_h1 * 3)
        tp2 = smart_round(entry + atr_h1 * 5)

    elif direction == "SHORT" or (direction == "WAIT" and h4_bias == "SHORT"):
        entry    = price
        sl_struct = recent_h1_high + atr_h1 * 0.5
        sl_price  = smart_round(max(entry * 1.01, min(sl_struct, entry * 1.04)))

        tp1 = None
        for ma in [float(row_h4["ma34"]), float(row_h4["ma89"]), float(row_h4["ma200"])]:
            if ma < entry * 0.995:
                tp1 = smart_round(ma); break
        if tp1 is None: tp1 = smart_round(entry - atr_h1 * 3)
        tp2 = smart_round(entry - atr_h1 * 5)
        if tp2 >= entry: tp2 = smart_round(entry - atr_h1 * 5)

    else:
        entry = sl_price = tp1 = tp2 = price

    sl_pct  = round(abs(entry - sl_price) / entry * 100, 2) if entry != sl_price else 0
    tp1_pct = round(abs(tp1 - entry) / entry * 100, 2)      if entry != tp1 else 0
    rr      = round(tp1_pct / sl_pct, 2)                    if sl_pct > 0 else 0

    # Update checklist với rr thực
    entry_checklist, entry_verdict = build_entry_checklist(
        direction, h1_status, rr, funding, oi_change, btc_ctx, no_trade, confidence
    )

    if direction in ("LONG", "SHORT") and rr < 1.0:
        all_warnings.append(f"❌ R:R {rr} < 1.0 — chờ pullback về MA")
        direction      = "WAIT"
        confidence     = "LOW"
        entry_verdict  = "NO"   # Override: R:R quá thấp → không vào

    # Override verdict nếu confidence LOW — không bao giờ GO với LOW
    if confidence == "LOW" and entry_verdict == "GO":
        entry_verdict = "WAIT"

    # ── Candles cho chart ──
    chart_df = df_h4.tail(80).reset_index()
    candles  = [{"t": int(r["open_time"].timestamp() * 1000),
                  "o": smart_round(r["open"]),  "h": smart_round(r["high"]),
                  "l": smart_round(r["low"]),   "c": smart_round(r["close"]),
                  "v": round(r["volume"], 2),
                  "ma34": smart_round(r["ma34"]), "ma89": smart_round(r["ma89"]),
                  "ma200": smart_round(r["ma200"]),
                  "vol_ratio": round(r["vol_ratio"], 2)}
                 for _, r in chart_df.iterrows()]

    result = {
        "symbol":       symbol,
        "price":        smart_round(price),
        "direction":    direction,
        "confidence":   confidence,
        "score":        int(score),
        "conditions":   conditions,
        "warnings":     all_warnings,
        "no_trade_zone": bool(no_trade),
        "entry":   smart_round(entry),
        "sl":      sl_price,
        "tp1":     tp1,
        "tp2":     tp2,
        "sl_pct":  sl_pct,
        "tp1_pct": tp1_pct,
        "rr":      rr,
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
        "d1": {"bias": d1_bias, "structure": d1_structure, "notes": d1_notes,
               "dist_ma34": round(dist_ma34_d1, 2), "dist_ma89": round(dist_ma89_d1, 2)},
        "h4": {"bias": h4_bias, "structure": h4_structure, "notes": h4_notes,
               "above_ma34": h4_above_ma34, "above_ma89": h4_above_ma89,
               "crossed_ma34": h4_x_ma34_up, "slope_ma34": slope_ma34,
               "slope_ma89": slope_ma89, "slope_ma200": slope_ma200,
               "ma34": smart_round(row_h4["ma34"]),
               "ma89": smart_round(row_h4["ma89"]),
               "ma200": smart_round(row_h4["ma200"])},
        "h1": {"fib_zone": fib_zone, "fib_zone_price": fib_zone_price,
               "vol_ratio": round(vol_ratio, 2), "h1_bullish": h1_bullish,
               "breakout": h1_breakout},
        "fib_ret":    fib_ret,
        "fib_ext":    fib_ext,
        "swing_high": smart_round(sh),
        "swing_low":  smart_round(sl_),
        "candles":    candles,
        "timestamp":  datetime.now().isoformat(),
        "h1_status":        h1_status,
        "h1_status_note":   h1_status_note,
        "entry_checklist":  entry_checklist,
        "entry_verdict":    entry_verdict,
    }
    return sanitize(result)
