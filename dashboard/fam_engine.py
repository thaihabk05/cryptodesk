"""dashboard/engine.py — FAM Signal Engine. Chỉ sửa file này khi thay đổi logic Dashboard."""
import math
from datetime import datetime, timezone, timedelta

_TZ_VN = timezone(timedelta(hours=7))

from core.binance import (fetch_klines, fetch_funding_rate,
                           fetch_oi_change, fetch_btc_context)
from core.indicators import (prepare, ma_slope, find_swing_points,
                              classify_structure, fib_retracement,
                              fib_extension, is_no_trade_zone, calc_atr_context,
                              weekly_macro_bias)
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
    df_w  = prepare(fetch_klines(symbol, "1w", 250, force_futures=ff))
    df_d1 = prepare(fetch_klines(symbol, "1d", 300, force_futures=ff))
    df_h4 = prepare(fetch_klines(symbol, "4h", 300, force_futures=ff))
    df_h1 = prepare(fetch_klines(symbol, "1h", 150, force_futures=ff))

    for df in [df_d1, df_h4, df_h1]:
        if len(df) < 10:
            raise ValueError(f"Không đủ data cho {symbol}")

    # ── TẦNG 0 — Weekly Macro Bias (FAM Trading method) ──
    w_bias = weekly_macro_bias(df_w) if len(df_w) >= 10 else {
        "trend": "NEUTRAL", "death_cross": False, "golden_cross": False,
        "near_death": False, "below_ma200": False, "notes": [], "score_adj": 0,
        "ma34_slope": "FLAT", "ma89_slope": "FLAT", "ma34": None, "ma89": None, "ma200": None,
    }

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

    ma34_h1  = float(df_h1["ma34"].iloc[-1])

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
        dir_vn = "LONG" if direction == "LONG" else "SHORT"

        # 1. Confidence
        if confidence == "HIGH":
            checks.append({"ok": True,  "text": "Confidence HIGH — tín hiệu đa khung đủ mạnh"})
        elif confidence == "MEDIUM":
            checks.append({"ok": False, "text": "Confidence MEDIUM — chờ thêm 1-2 nến H4 xác nhận rõ hướng"})
        else:
            checks.append({"ok": False, "text": "Confidence LOW — tín hiệu yếu, không vào lệnh"})

        # 2. H1 confirmation
        if h1_status == "CONFIRMED":
            checks.append({"ok": True,  "text": "H1 đang chạy đúng hướng " + dir_vn + " — momentum tốt"})
        elif h1_status == "PULLBACK":
            checks.append({"ok": None,  "text": "H1 đang pullback — chờ nến H1 tiếp theo đóng cửa theo hướng " + dir_vn})
        elif h1_status == "COUNTER":
            checks.append({"ok": False, "text": "H1 đang ngược chiều " + dir_vn + " — không vào, chờ H1 đổi hướng"})
        else:
            checks.append({"ok": None,  "text": "H1 chưa xác nhận — chờ nến H1 đóng cửa rõ hướng " + dir_vn})

        # 3. No-trade zone
        if not no_trade:
            checks.append({"ok": True,  "text": "Giá nằm ngoài vùng MA34/89 — tín hiệu rõ hướng"})
        else:
            checks.append({"ok": None,  "text": "Giá kẹt giữa MA34-MA89 — chờ giá thoát hẳn ra ngoài vùng này"})

        # 4. R:R
        if rr >= 2.0:
            checks.append({"ok": True,  "text": f"R:R 1:{rr} — rủi ro/lợi nhuận tốt (≥ 1:2)"})
        elif rr >= 1.5:
            checks.append({"ok": True,  "text": f"R:R 1:{rr} — chấp nhận được (≥ 1:1.5)"})
        elif rr >= 1.0:
            checks.append({"ok": None,  "text": f"R:R 1:{rr} — thấp, cân nhắc chờ giá về gần entry hơn"})
        else:
            checks.append({"ok": False, "text": f"R:R 1:{rr} < 1:1 — không vào, rủi ro cao hơn lợi nhuận"})

        # 5. Funding
        if funding is not None:
            if direction == "LONG":
                if funding < -0.01:
                    checks.append({"ok": True,  "text": f"Funding {funding:+.4f}% âm — thị trường nghiêng SHORT, tốt cho LONG entry"})
                elif funding > 0.05:
                    checks.append({"ok": False, "text": f"Funding {funding:+.4f}% quá cao — Long overcrowded, chờ funding về dưới 0.03%"})
                else:
                    checks.append({"ok": None,  "text": f"Funding {funding:+.4f}% trung tính — không ảnh hưởng đáng kể, có thể vào"})
            else:  # SHORT
                if funding > 0.01:
                    checks.append({"ok": True,  "text": f"Funding {funding:+.4f}% dương — bạn được nhận phí khi giữ SHORT"})
                elif funding < -0.05:
                    checks.append({"ok": False, "text": f"Funding {funding:+.4f}% âm sâu — Short overcrowded, chờ funding về trên -0.03%"})
                else:
                    checks.append({"ok": None,  "text": f"Funding {funding:+.4f}% trung tính — không ảnh hưởng đáng kể, có thể vào"})

        # 6. BTC context
        sentiment = btc_ctx.get("sentiment", "NEUTRAL")
        btc_chg   = btc_ctx.get("chg_24h", 0) or 0
        if sentiment == "RISK_ON" and direction == "LONG":
            checks.append({"ok": True,  "text": f"BTC đang BULL D1+H4 — thị trường thuận, LONG altcoin có lợi"})
        elif sentiment in ("RISK_OFF", "DUMP") and direction == "SHORT":
            checks.append({"ok": True,  "text": f"BTC đang giảm ({btc_chg:+.1f}% 24h) — SHORT altcoin theo xu hướng thị trường"})
        elif sentiment in ("RISK_OFF", "DUMP") and direction == "LONG":
            checks.append({"ok": False, "text": f"BTC đang BEAR/DUMP ({btc_chg:+.1f}% 24h) — không LONG altcoin khi BTC giảm mạnh"})
        elif sentiment == "RISK_ON" and direction == "SHORT":
            checks.append({"ok": None,  "text": f"BTC đang BULL — SHORT ngược chiều thị trường, cần tín hiệu mã rất rõ"})
        else:
            checks.append({"ok": None,  "text": f"BTC sideways ({btc_chg:+.1f}% 24h) — không hỗ trợ cũng không cản, xét tín hiệu mã riêng"})

        # 7. OI
        if oi_change is not None:
            if direction == "LONG":
                if oi_change > 5:
                    checks.append({"ok": True,  "text": f"OI tăng +{oi_change}% — tiền đang đổ vào thị trường, hỗ trợ LONG"})
                elif oi_change < -5:
                    checks.append({"ok": False, "text": f"OI giảm {oi_change}% — vị thế đang đóng, chờ OI ổn định hoặc tăng lại"})
                else:
                    checks.append({"ok": None,  "text": f"OI {oi_change:+.1f}% — chưa có dòng tiền rõ, theo dõi thêm"})
            else:  # SHORT
                if oi_change < -5:
                    checks.append({"ok": True,  "text": f"OI giảm {oi_change}% — Long đang đóng vị thế, hỗ trợ SHORT"})
                elif oi_change > 5:
                    checks.append({"ok": False, "text": f"OI tăng +{oi_change}% — Long đang vào mạnh, rủi ro SHORT bị squeeze, chờ OI chững lại"})
                else:
                    checks.append({"ok": None,  "text": f"OI {oi_change:+.1f}% — chưa có tín hiệu dòng tiền rõ, theo dõi thêm"})

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
            confidence = "HIGH" if score >= 5 else "MEDIUM" if score >= 3 else "LOW"

    # ── Weekly macro bias adjustments ──
    weekly_warnings = []
    weekly_score_adj = w_bias["score_adj"]
    for wnote in w_bias["notes"]:
        weekly_warnings.append(wnote)

    # Weekly death cross + LONG → cảnh báo mạnh (nhưng KHÔNG block hoàn toàn)
    # FAM: death cross Weekly = tín hiệu tử thần, nhưng vẫn có thể có nhịp hồi ngắn hạn
    if direction == "LONG" and (w_bias["death_cross"] or w_bias.get("near_death")):
        if confidence == "HIGH":
            confidence = "MEDIUM"
        weekly_warnings.append("⚠️ WEEKLY downtrend — LONG chỉ là hồi ngắn hạn, giảm confidence")

    # Weekly BEAR + dưới MA200 + LONG → giảm thêm
    if direction == "LONG" and w_bias["trend"] == "BEAR" and w_bias["below_ma200"]:
        if confidence == "HIGH":
            confidence = "MEDIUM"
        elif confidence == "MEDIUM" and score < 4:
            confidence = "LOW"

    # Weekly BULL + LONG → bonus
    if direction == "LONG" and w_bias["trend"] == "BULL":
        weekly_score_adj += 1

    # Weekly BEAR + SHORT → thuận xu hướng, bonus
    if direction == "SHORT" and w_bias["trend"] == "BEAR":
        weekly_score_adj += 1
        weekly_warnings.append("✅ WEEKLY BEAR — SHORT thuận xu hướng macro")

    # ── ATR & Funding adjustments ──
    atr_warnings = []
    if atr_ctx["atr_state"] == "COMPRESS":
        atr_warnings.append(f"⚠️ {atr_ctx['atr_note']}")
    elif atr_ctx["atr_state"] == "EXPAND":
        atr_warnings.append(f"⚠️ {atr_ctx['atr_note']}")
    atr_score_adj = atr_ctx["score_adj"]

    funding_warnings, funding_score_adj = _interpret_funding(funding, oi_change, direction)
    all_warnings = weekly_warnings + warnings + atr_warnings + funding_warnings

    # BTC context warning
    if direction == "LONG" and btc_ctx["sentiment"] in ("RISK_OFF", "DUMP"):
        all_warnings.insert(0, f"⚠️ Market: {btc_ctx['note']}")
    elif direction == "SHORT" and btc_ctx["sentiment"] in ("RISK_ON", "PUMP"):
        all_warnings.insert(0, f"⚠️ Market: {btc_ctx['note']}")


    # ── PATCH F: Abnormal Candle Spike Filter ──
    # Check cả nến cuối VÀ nến trước — spike có thể ở nến trước, nến sau chưa confirm
    _spike_atr_avg = float(df_h1["atr"].iloc[-20:].mean()) if len(df_h1) >= 20 else atr_h1
    _spike_threshold = _spike_atr_avg * 2.0
    _spike_triggered = False
    _spike_body = 0.0
    _spike_which = ""
    for _si, _slabel in [(-1, "hiện tại"), (-2, "trước")]:
        _sr = df_h1.iloc[_si]
        _sb = abs(float(_sr["close"]) - float(_sr["open"]))
        if _sb > _spike_threshold:
            _spike_triggered = True
            _spike_body = _sb
            _spike_which = _slabel
            break
    if _spike_triggered and direction in ("LONG", "SHORT"):
        direction  = "WAIT"
        confidence = "LOW"
        all_warnings.insert(0, f"🚫 SPIKE FILTER — Nến H1 {_spike_which} body {_spike_body:.5f} > 2x ATR ({_spike_threshold:.5f}) — pump/dump đột ngột, chờ confirmation")

    # ── PATCH A: BTC Hard Block ──
    # Không ra signal ngược chiều BTC macro — đây là nguyên nhân chính loss
    btc_sent = btc_ctx.get("sentiment", "NEUTRAL")
    btc_d1   = btc_ctx.get("d1_trend", "")
    if direction == "LONG" and btc_sent in ("RISK_OFF", "DUMP") and btc_d1 == "BEAR":
        direction  = "WAIT"
        confidence = "LOW"
        all_warnings.insert(0, f"🚫 BLOCK LONG — BTC D1 BEAR + {btc_sent}: {btc_ctx.get('note','')}")
    elif direction == "SHORT" and btc_sent in ("RISK_ON",) and btc_d1 == "BULL":
        direction  = "WAIT"
        confidence = "LOW"
        all_warnings.insert(0, f"🚫 BLOCK SHORT — BTC D1 BULL + {btc_sent}: {btc_ctx.get('note','')}")

    # ── PATCH E: OI Hard Block ──
    # OI giảm mạnh = vị thế đang đóng = không có buyer mới → block LONG
    # OI tăng mạnh khi SHORT = short squeeze risk → block SHORT
    if oi_change is not None and direction == "LONG" and oi_change < -3:
        direction  = "WAIT"
        confidence = "LOW"
        all_warnings.insert(0, f"🚫 BLOCK LONG — OI {oi_change:+.1f}%: vị thế đang đóng, không có buyer mới")
    elif oi_change is not None and direction == "SHORT" and oi_change > 3:
        direction  = "WAIT"
        confidence = "LOW"
        all_warnings.insert(0, f"🚫 BLOCK SHORT — OI {oi_change:+.1f}%: tiền đang vào LONG, rủi ro squeeze")

    # ── PATCH G: Price-OI Divergence Block ──
    # OI tăng nhưng giá đang giảm = tiền vào SHORT, không phải LONG → block LONG
    # OI giảm nhưng giá đang tăng = tiền rời khỏi SHORT → block SHORT  
    if oi_change is not None and direction == "LONG" and oi_change > 3:
        _price_chg_h1 = (float(df_h1["close"].iloc[-1]) - float(df_h1["close"].iloc[-4])) / float(df_h1["close"].iloc[-4]) * 100
        if _price_chg_h1 < -1.0:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0, f"🚫 OI DIVERGENCE — OI +{oi_change:.1f}% nhưng giá giảm {_price_chg_h1:.1f}% (4h) — tiền đang vào SHORT, không LONG")

    # ── PATCH H: EMA9 H1 Price Position ──
    # Chuyển từ hard block → soft warning + giảm confidence
    # Lý do: EMA9 quá nhạy, block quá nhiều signal hợp lệ trong trending market
    # FAM Trading: EMA9 chỉ là tham khảo, MA34 mới là chốt chặn chính
    if "ema9" in df_h1.columns:
        _ema9_h1 = float(df_h1["ema9"].iloc[-1])
        if direction == "LONG" and price < _ema9_h1 * 0.997:
            # Chỉ block nếu giá dưới EMA9 QUÁ XA (>0.3%) — tức momentum bearish rõ
            if confidence == "HIGH":
                confidence = "MEDIUM"
            all_warnings.append(f"⚠️ Giá dưới EMA9 H1 ({_ema9_h1:.5f}) — momentum ngắn hạn yếu, cân nhắc chờ pullback")
        elif direction == "SHORT" and price > _ema9_h1 * 1.003:
            if confidence == "HIGH":
                confidence = "MEDIUM"
            all_warnings.append(f"⚠️ Giá trên EMA9 H1 ({_ema9_h1:.5f}) — momentum ngắn hạn mạnh, cân nhắc chờ rejection")

    # ── PATCH I: Far From EMA34 H1 — soft warning + hard block khi quá xa ──
    # 5–7%: cảnh báo, hạ confidence
    # > 7%: HARD BLOCK — giá đã pump/dump quá xa, entry là chasing top/bottom
    # Lý do: case OPENUSDT 28/04/2026 — entry 0.281 khi EMA34 H1 ~0.255 (xa ~10%)
    # → 2 trade LONG liên tiếp đều SL ngay đỉnh pump parabolic
    if direction in ("LONG", "SHORT") and ma34_h1 > 0:
        _dist_ema34_h1 = (price - ma34_h1) / ma34_h1 * 100
        if direction == "LONG" and _dist_ema34_h1 > 7:
            direction  = "WAIT"
            confidence = "LOW"
            _pullback_target = round(ma34_h1 * 1.005, 6)
            all_warnings.insert(0,
                f"🚫 BLOCK LONG — Giá cách EMA34 H1 +{round(_dist_ema34_h1,1)}% (>7%) "
                f"— chasing top, chờ pullback về ~{_pullback_target}"
            )
        elif direction == "SHORT" and _dist_ema34_h1 < -7:
            direction  = "WAIT"
            confidence = "LOW"
            _pullback_target = round(ma34_h1 * 0.995, 6)
            all_warnings.insert(0,
                f"🚫 BLOCK SHORT — Giá cách EMA34 H1 {round(_dist_ema34_h1,1)}% (<-7%) "
                f"— chasing bottom, chờ rebound về ~{_pullback_target}"
            )
        elif direction == "LONG" and _dist_ema34_h1 > 5:
            if confidence == "HIGH":
                confidence = "MEDIUM"
            _pullback_target = round(ma34_h1 * 1.005, 6)
            all_warnings.append(
                f"⚠️ GIÁ CÁCH EMA34 H1 +{round(_dist_ema34_h1,1)}% — entry không tối ưu, "
                f"chờ pullback về ~{_pullback_target} (đã hạ confidence)"
            )
        elif direction == "SHORT" and _dist_ema34_h1 < -5:
            if confidence == "HIGH":
                confidence = "MEDIUM"
            _pullback_target = round(ma34_h1 * 0.995, 6)
            all_warnings.append(
                f"⚠️ GIÁ CÁCH EMA34 H1 {round(_dist_ema34_h1,1)}% — entry không tối ưu, "
                f"chờ rebound về ~{_pullback_target} (đã hạ confidence)"
            )


    # ── PATCH J: Pump Exhaustion — 7-day price change ──
    # Nếu giá đã tăng > 50% trong 7 ngày = pump exhaustion, rủi ro dump cao
    # Nếu giá đã giảm > 40% trong 7 ngày = capitulation zone, SHORT cẩn thận
    if len(df_d1) >= 8:
        _price_7d_ago = float(df_d1["close"].iloc[-8])
        _chg_7d = (price - _price_7d_ago) / _price_7d_ago * 100 if _price_7d_ago > 0 else 0
        if direction == "LONG" and _chg_7d > 50:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0,
                f"🚫 PUMP EXHAUSTION — Giá tăng +{round(_chg_7d,1)}% trong 7 ngày "
                f"— rủi ro reversal/dump cao, không LONG đuổi giá"
            )
        elif direction == "SHORT" and _chg_7d < -40:
            all_warnings.append(
                f"⚠️ GIẢM MẠNH 7D ({round(_chg_7d,1)}%) — có thể oversold, "
                f"cẩn thận SHORT vùng capitulation"
            )

    # ── PATCH K: Short-term Pump/Dump Filter — 4h price change ──
    # Patch J chỉ check 7d, mù với pump parabolic trong vài giờ.
    # Case OPENUSDT 28/04/2026: pump 0.25→0.281 (+12%) trong vài giờ → engine vẫn LONG HIGH.
    # Block LONG nếu giá tăng >10% trong 4h gần nhất (4 nến H1).
    # Block SHORT nếu giá giảm >10% trong 4h gần nhất.
    if len(df_h1) >= 5 and direction in ("LONG", "SHORT"):
        _price_4h_ago = float(df_h1["close"].iloc[-5])
        if _price_4h_ago > 0:
            _chg_4h = (price - _price_4h_ago) / _price_4h_ago * 100
            if direction == "LONG" and _chg_4h > 10:
                direction  = "WAIT"
                confidence = "LOW"
                all_warnings.insert(0,
                    f"🚫 SHORT-TERM PUMP — Giá tăng +{round(_chg_4h,1)}% trong 4h "
                    f"— parabolic blow-off, không LONG đuổi đỉnh"
                )
            elif direction == "SHORT" and _chg_4h < -10:
                direction  = "WAIT"
                confidence = "LOW"
                all_warnings.insert(0,
                    f"🚫 SHORT-TERM DUMP — Giá giảm {round(_chg_4h,1)}% trong 4h "
                    f"— capitulation, không SHORT đuổi đáy"
                )

    # ── PATCH L: RSI Veto trên H1 ──
    # Veto cứng: bất kể score cao đến đâu, RSI extreme là cấm entry cùng chiều.
    # RSI H1 > 75 → cấm LONG (overbought, momentum sắp reversal)
    # RSI H1 < 25 → cấm SHORT (oversold)
    if "rsi" in df_h1.columns and direction in ("LONG", "SHORT"):
        _rsi_h1 = float(df_h1["rsi"].iloc[-1])
        if direction == "LONG" and _rsi_h1 > 75:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0,
                f"🚫 RSI VETO — RSI H1 {_rsi_h1:.1f} > 75 (overbought) "
                f"— cấm LONG dù score cao, chờ RSI reset về <65"
            )
        elif direction == "SHORT" and _rsi_h1 < 25:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0,
                f"🚫 RSI VETO — RSI H1 {_rsi_h1:.1f} < 25 (oversold) "
                f"— cấm SHORT dù score cao, chờ RSI reset về >35"
            )

    # ── PATCH M: ATR Expansion Guard ──
    # ATR H1 đang nở mạnh + giá extended = pump/dump parabolic đang diễn ra.
    # ATR hiện tại > 2x ATR trung bình 20 nến H1 + giá xa EMA34 H1 > 4%
    # → block entry cùng chiều với move.
    if len(df_h1) >= 21 and direction in ("LONG", "SHORT") and ma34_h1 > 0:
        _atr_avg_h1 = float(df_h1["atr"].iloc[-21:-1].mean())
        _atr_now_h1 = float(df_h1["atr"].iloc[-1])
        _dist_ma34_pct = abs((price - ma34_h1) / ma34_h1 * 100)
        if _atr_avg_h1 > 0 and _atr_now_h1 > _atr_avg_h1 * 2 and _dist_ma34_pct > 4:
            _atr_ratio = _atr_now_h1 / _atr_avg_h1
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0,
                f"🚫 ATR EXPANSION — ATR H1 {_atr_ratio:.1f}x baseline + "
                f"giá cách EMA34 H1 {_dist_ma34_pct:.1f}% — biến động cực mạnh, chờ ATR ổn định"
            )

    # ── PATCH N: 24h Extreme Proximity Guard ──
    # Chasing đỉnh/đáy 24h là setup R:R xấu — giá thường bounce/reject ở extreme.
    # LONG: giá cách 24h HIGH < 2% → block (chasing top)
    # SHORT: giá cách 24h LOW < 2% → block (chasing bottom)
    # Case HUMAUSDT 29/04/2026: SHORT entry 0.0209 sát đáy 24h 0.020718 (~0.4%)
    # → cấu trúc đúng nhưng timing sai, dễ bounce trước khi reach TP1.
    if len(df_h1) >= 24 and direction in ("LONG", "SHORT"):
        _high_24h = float(df_h1["high"].iloc[-24:].max())
        _low_24h  = float(df_h1["low"].iloc[-24:].min())
        if direction == "LONG" and _high_24h > 0:
            _dist_high_pct = (_high_24h - price) / price * 100
            if _dist_high_pct < 2:
                direction  = "WAIT"
                confidence = "LOW"
                all_warnings.insert(0,
                    f"🚫 CHASING TOP — Giá {price:.6f} sát đỉnh 24h {_high_24h:.6f} "
                    f"({_dist_high_pct:.1f}% cách) — chờ pullback rồi vào lại"
                )
        elif direction == "SHORT" and _low_24h > 0:
            _dist_low_pct = (price - _low_24h) / price * 100
            if _dist_low_pct < 2:
                direction  = "WAIT"
                confidence = "LOW"
                all_warnings.insert(0,
                    f"🚫 CHASING BOTTOM — Giá {price:.6f} sát đáy 24h {_low_24h:.6f} "
                    f"({_dist_low_pct:.1f}% cách) — chờ bounce rồi short lại tốt hơn"
                )

    # ── PATCH O: TP1 Cushion vs EMA200 H4 ──
    # EMA200 H4 là support/resistance động cực mạnh. Nếu entry quá gần EMA200
    # thì TP1 không đủ chỗ thở — giá thường bounce/reject ở EMA200 trước khi
    # reach TP1, và setup R:R thực tế tệ hơn engine tính.
    # SHORT: nếu entry cách EMA200 H4 (phía dưới) < 8% → giảm confidence
    #        nếu < 4% → block (TP1 gần như sát EMA200 = vô nghĩa)
    # LONG (mirror): nếu entry cách EMA200 H4 (phía trên) < 8% → giảm
    #               nếu < 4% → block
    try:
        _ma200_h4 = float(row_h4["ma200"])
    except Exception:
        _ma200_h4 = 0
    if _ma200_h4 > 0 and direction in ("LONG", "SHORT"):
        if direction == "SHORT" and price > _ma200_h4:
            _cushion_pct = (price - _ma200_h4) / price * 100
            if _cushion_pct < 4:
                direction  = "WAIT"
                confidence = "LOW"
                all_warnings.insert(0,
                    f"🚫 SHORT QUÁ GẦN EMA200 H4 — entry cách EMA200 H4 ({_ma200_h4:.6f}) "
                    f"chỉ {_cushion_pct:.1f}% — TP1 không đủ chỗ thở, EMA200 sẽ đỡ giá"
                )
            elif _cushion_pct < 8:
                if confidence == "HIGH":
                    confidence = "MEDIUM"
                all_warnings.append(
                    f"⚠️ SHORT cách EMA200 H4 {_cushion_pct:.1f}% — gần support động, "
                    f"cẩn thận bounce. Đã hạ confidence."
                )
        elif direction == "LONG" and price < _ma200_h4:
            _cushion_pct = (_ma200_h4 - price) / price * 100
            if _cushion_pct < 4:
                direction  = "WAIT"
                confidence = "LOW"
                all_warnings.insert(0,
                    f"🚫 LONG QUÁ GẦN EMA200 H4 — entry cách EMA200 H4 ({_ma200_h4:.6f}) "
                    f"chỉ {_cushion_pct:.1f}% — TP1 không đủ chỗ thở, EMA200 sẽ đè giá"
                )
            elif _cushion_pct < 8:
                if confidence == "HIGH":
                    confidence = "MEDIUM"
                all_warnings.append(
                    f"⚠️ LONG cách EMA200 H4 {_cushion_pct:.1f}% — gần resistance động, "
                    f"cẩn thận rejection. Đã hạ confidence."
                )


    total_adj = funding_score_adj + atr_score_adj + weekly_score_adj
    if total_adj <= -2 and confidence != "LOW":
        confidence = "LOW"
        all_warnings.append("⚠️ Confidence hạ xuống LOW do funding/volatility bất lợi")
    elif total_adj == -1 and confidence == "HIGH":
        confidence = "MEDIUM"

    # ────────────────────────────────────────
    # SL / TP  (v3 — swing recent + Fib ext + entry optimal)
    # ────────────────────────────────────────
    recent_h1_high = float(df_h1["high"].iloc[-20:].max())
    recent_h1_low  = float(df_h1["low"].iloc[-20:].min())

    ma34_h4  = float(row_h4["ma34"])
    ma89_h4  = float(row_h4["ma89"])
    ma200_h4 = float(row_h4["ma200"])

    # Swing H4 — chỉ lấy 30 nến gần đây (120h = 5 ngày) để tránh swing xa vô nghĩa
    df_h4_recent = df_h4.iloc[-30:]
    highs_h4_recent, lows_h4_recent = find_swing_points(df_h4_recent, lookback=3)
    swing_highs_recent = sorted([v for _, v in highs_h4_recent], reverse=True)
    swing_lows_recent  = sorted([v for _, v in lows_h4_recent])

    # H4 swing high/low cho Fib extension (60 nến ~ 10 ngày)
    recent_h4_high = float(df_h4["high"].iloc[-60:].max())
    recent_h4_low  = float(df_h4["low"].iloc[-60:].min())

    # Fib H4 retracement — vùng hỗ trợ/kháng cự lý tưởng để chờ entry
    fib_h4_ret = fib_retracement(recent_h4_high, recent_h4_low)

    def _pick_tp1_long(entry, swing_highs, ma34, ma89, ma200, atr):
        """
        TP1 = kháng cự gần nhất phía trên, theo thứ tự ưu tiên:
        1. Swing high H4 gần đây (30 nến) — level trader thực đang dùng
        2. MA34 H4 nếu nằm trên entry (MA là kháng cự động)
        3. MA89, MA200 H4
        4. ATR×3 fallback
        Yêu cầu: cách entry ít nhất 2% VÀ không xa quá 15%
        """
        min_dist = entry * 1.02   # ≥ 2% để R:R có ý nghĩa
        max_dist = entry * 1.15   # ≤ 15% để thực tế

        # 1. Swing high H4 gần nhất (trong vùng hợp lý)
        candidates = [h for h in swing_highs if min_dist < h < max_dist]
        if candidates:
            return smart_round(min(candidates))

        # 2-4. MA theo thứ tự gần → xa
        for ma in [ma34, ma89, ma200]:
            if min_dist < ma < max_dist:
                return smart_round(ma)

        # 5. Fallback ATR — đảm bảo ≥ 2% dù không có level nào
        return smart_round(max(entry * 1.02, entry + atr * 3))

    def _pick_tp1_short(entry, swing_lows, ma34, ma89, ma200, atr):
        """TP1 SHORT = hỗ trợ gần nhất phía dưới"""
        max_dist = entry * 0.98
        min_dist = entry * 0.85

        candidates = [l for l in swing_lows if min_dist < l < max_dist]
        if candidates:
            return smart_round(max(candidates))

        for ma in [ma34, ma89, ma200]:
            if min_dist < ma < max_dist:
                return smart_round(ma)

        return smart_round(min(entry * 0.98, entry - atr * 3))

    def _pick_tp2_long(entry, tp1, fib_ext_data, atr):
        """TP2 = Fib Extension 1.272 hoặc 1.618 của sóng H4"""
        fib_127 = fib_ext_data.get("1.272", 0)
        fib_162 = fib_ext_data.get("1.618", 0)
        # Hợp lý: nằm trên TP1 và không quá 40% từ entry
        if fib_127 > tp1 * 1.005 and fib_127 < entry * 1.40:
            return smart_round(fib_127)
        if fib_162 > tp1 * 1.005 and fib_162 < entry * 1.50:
            return smart_round(fib_162)
        # Fallback: TP2 = TP1 + 1× khoảng TP1–entry
        return smart_round(tp1 + (tp1 - entry))

    def _pick_tp2_short(entry, tp1, fib_ext_data, atr):
        fib_127 = fib_ext_data.get("1.272", 0)
        fib_162 = fib_ext_data.get("1.618", 0)
        if 0 < fib_127 < tp1 * 0.995 and fib_127 > entry * 0.60:
            return smart_round(fib_127)
        if 0 < fib_162 < tp1 * 0.995 and fib_162 > entry * 0.50:
            return smart_round(fib_162)
        return smart_round(tp1 - (entry - tp1))

    def _calc_entry_optimal_long(price, fib_h4, ma34_h1, ma34_h4, atr):
        """
        Entry tốt hơn cho LONG = điểm pullback về vùng hỗ trợ:
        Ưu tiên: Fib 0.5 H4 → Fib 0.618 H4 → MA34 H1 → MA34 H4
        Chỉ đề xuất nếu thấp hơn giá hiện tại ít nhất 0.5%
        """
        candidates = []
        for key in ["0.500", "0.618", "0.382"]:
            v = fib_h4.get(key, 0)
            if 0 < v < price * 0.995:
                candidates.append((abs(v - price), key, v))
        # MA34 H1 — support động gần nhất
        if ma34_h1 < price * 0.995:
            candidates.append((abs(ma34_h1 - price), "MA34 H1", ma34_h1))
        # MA34 H4
        if ma34_h4 < price * 0.995:
            candidates.append((abs(ma34_h4 - price), "MA34 H4", ma34_h4))

        if not candidates:
            return None, None
        # Lấy level gần nhất với giá hiện tại
        candidates.sort(key=lambda x: x[0])
        _, label, val = candidates[0]
        return smart_round(val), label

    def _calc_entry_optimal_short(price, fib_h4, ma34_h1, ma34_h4, atr):
        """Entry tốt hơn cho SHORT = điểm bounce lên để sell"""
        candidates = []
        for key in ["0.500", "0.618", "0.382"]:
            v = fib_h4.get(key, 0)
            if v > price * 1.005:
                candidates.append((abs(v - price), key, v))
        if ma34_h1 > price * 1.005:
            candidates.append((abs(ma34_h1 - price), "MA34 H1", ma34_h1))
        if ma34_h4 > price * 1.005:
            candidates.append((abs(ma34_h4 - price), "MA34 H4", ma34_h4))

        if not candidates:
            return None, None
        candidates.sort(key=lambda x: x[0])
        _, label, val = candidates[0]
        return smart_round(val), label

    # Fib extension H4 (dùng price vì entry chưa được gán)
    fib_ext_h4_long  = fib_extension(recent_h4_low,  recent_h4_high, price)
    fib_ext_h4_short = fib_extension(recent_h4_high, recent_h4_low,  price)

    if direction == "LONG" or (direction == "WAIT" and h4_bias == "LONG"):
        entry     = price
        sl_struct = recent_h1_low - atr_h1 * 0.5
        sl_price  = smart_round(min(entry * 0.98, max(sl_struct, entry * 0.94)))

        tp1 = _pick_tp1_long(entry, swing_highs_recent, ma34_h4, ma89_h4, ma200_h4, atr_h1)
        tp2 = _pick_tp2_long(entry, tp1, fib_ext_h4_long, atr_h1)

        entry_opt, entry_opt_label = _calc_entry_optimal_long(
            price, fib_h4_ret, ma34_h1, ma34_h4, atr_h1)

    elif direction == "SHORT" or (direction == "WAIT" and h4_bias == "SHORT"):
        entry     = price
        sl_struct = recent_h1_high + atr_h1 * 0.5
        sl_price  = smart_round(max(entry * 1.02, min(sl_struct, entry * 1.06)))

        tp1 = _pick_tp1_short(entry, swing_lows_recent, ma34_h4, ma89_h4, ma200_h4, atr_h1)
        tp2 = _pick_tp2_short(entry, tp1, fib_ext_h4_short, atr_h1)

        entry_opt, entry_opt_label = _calc_entry_optimal_short(
            price, fib_h4_ret, ma34_h1, ma34_h4, atr_h1)

    else:
        entry = sl_price = tp1 = tp2 = price
        entry_opt, entry_opt_label = None, None

    # Đảm bảo TP2 không nằm sai phía
    if direction == "LONG" and tp2 <= tp1:
        tp2 = smart_round(tp1 + (tp1 - entry))
    if direction == "SHORT" and tp2 >= tp1:
        tp2 = smart_round(tp1 - (entry - tp1))

    sl_pct  = round(abs(entry - sl_price) / entry * 100, 2) if entry != sl_price else 0
    tp1_pct = round(abs(tp1 - entry) / entry * 100, 2)      if entry != tp1 else 0
    rr      = round(tp1_pct / sl_pct, 2)                    if sl_pct > 0 else 0

    # R:R với entry optimal (nếu có) — SL tính lại từ entry_opt
    entry_opt_rr = None
    if entry_opt and direction == "LONG" and entry_opt < entry:
        opt_sl_pct  = abs(entry_opt - sl_price) / entry_opt * 100
        opt_tp1_pct = abs(tp1 - entry_opt) / entry_opt * 100
        entry_opt_rr = round(opt_tp1_pct / opt_sl_pct, 2) if opt_sl_pct > 0 else None
    elif entry_opt and direction == "SHORT" and entry_opt > entry:
        opt_sl_pct  = abs(entry_opt - sl_price) / entry_opt * 100
        opt_tp1_pct = abs(tp1 - entry_opt) / entry_opt * 100
        entry_opt_rr = round(opt_tp1_pct / opt_sl_pct, 2) if opt_sl_pct > 0 else None

    # Update checklist với rr thực
    entry_checklist, entry_verdict = build_entry_checklist(
        direction, h1_status, rr, funding, oi_change, btc_ctx, no_trade, confidence
    )

    if direction in ("LONG", "SHORT") and rr < 1.5:
        all_warnings.append(f"❌ R:R {rr} < 1.5 — signal yếu, chờ điểm entry tốt hơn")
        direction      = "WAIT"
        confidence     = "LOW"
        entry_verdict  = "NO"   # Override: R:R quá thấp → không vào

    # Override verdict theo confidence
    if confidence == "LOW":
        if entry_verdict == "GO": entry_verdict = "WAIT"
    elif confidence == "MEDIUM":
        if entry_verdict == "GO": entry_verdict = "WAIT"  # MEDIUM tối đa WAIT

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
        "weekly": {
            "trend":       w_bias["trend"],
            "death_cross": w_bias["death_cross"],
            "near_death":  w_bias.get("near_death", False),
            "golden_cross": w_bias["golden_cross"],
            "below_ma200": w_bias["below_ma200"],
            "ma34_slope":  w_bias["ma34_slope"],
            "ma89_slope":  w_bias["ma89_slope"],
            "ma34":        w_bias["ma34"],
            "ma89":        w_bias["ma89"],
            "ma200":       w_bias["ma200"],
            "notes":       w_bias["notes"],
        },
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
        "timestamp":  datetime.now(_TZ_VN).isoformat(),
        "h1_status":        h1_status,
        "h1_status_note":   h1_status_note,
        "entry_checklist":  entry_checklist,
        "entry_verdict":    entry_verdict,
        "entry_now":        smart_round(entry),
        "entry_opt":        entry_opt,
        "entry_opt_label":  entry_opt_label,
        "entry_opt_rr":     entry_opt_rr,
    }
    return sanitize(result)
