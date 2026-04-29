"""dashboard/reversal_engine.py — Mean Reversion / Reversal Scalp Engine.

Strategy: Reversal (counter-trend bounce tại support/resistance mạnh)
─────────────────────────────────────────────────────────────────────
Không dùng trend-following rules — logic hoàn toàn khác:
  - Giá chạm EMA200/MA89 + pin bar + RSI extreme + BTC cùng bounce
  - SL dưới đáy pin bar
  - TP lên EMA34 hoặc EMA21

Chỉ kích hoạt khi ĐỦ điều kiện đặc biệt (ít signal, chất lượng cao).
"""
import math
from datetime import datetime, timezone, timedelta

from core.binance import (fetch_klines, fetch_funding_rate,
                           fetch_oi_change, fetch_btc_context,
                           fetch_taker_ratio, fetch_long_short_ratio,
                           fetch_order_book_imbalance)
from core.indicators import (prepare, ma_slope, find_swing_points,
                              fib_retracement, fib_extension)
from core.utils import sanitize, smart_round

_TZ_VN = timezone(timedelta(hours=7))


def _is_pin_bar(row, direction, atr):
    """Detect pin bar (rejection candle).
    LONG pin bar: shadow dưới dài (> 2x body), close gần high
    SHORT pin bar: shadow trên dài (> 2x body), close gần low
    """
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body = abs(c - o)
    if body < atr * 0.05:  # doji — cũng tính
        body = max(body, atr * 0.05)

    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    total_range  = h - l

    if total_range < atr * 0.3:  # nến quá nhỏ
        return False, 0

    if direction == "LONG":
        # Pin bar bullish: shadow dưới dài, close gần high
        if lower_shadow > body * 2 and lower_shadow > total_range * 0.55:
            strength = round(lower_shadow / total_range, 2)
            return True, strength
    else:
        # Pin bar bearish: shadow trên dài, close gần low
        if upper_shadow > body * 2 and upper_shadow > total_range * 0.55:
            strength = round(upper_shadow / total_range, 2)
            return True, strength

    return False, 0


def _check_ma_bounce(price, df, ma_col, direction="LONG", tolerance_pct=0.5):
    """Check giá vừa chạm MA và bounce ĐÚNG hướng (trong 5 nến gần nhất).

    LONG bounce: giá từ TRÊN xuống chạm MA rồi BẬT LÊN
        - Nến trước close > MA (giá đang ở trên)
        - Nến hiện tại: low chạm MA, close > MA, nến xanh (close > open)

    SHORT rejection: giá từ DƯỚI lên chạm MA rồi RỚT XUỐNG
        - Nến trước close < MA (giá đang ở dưới)
        - Nến hiện tại: high chạm MA, close < MA, nến đỏ

    Return: (is_bounce, ma_value, touch_candle_idx)
    """
    if ma_col not in df.columns:
        return False, None, None

    ma_val = float(df[ma_col].iloc[-1])
    if ma_val <= 0:
        return False, None, None

    tol = ma_val * tolerance_pct / 100

    # Check 5 nến gần nhất
    for i in range(-5, 0):
        if abs(i) > len(df) - 1:
            continue
        low_i   = float(df["low"].iloc[i])
        high_i  = float(df["high"].iloc[i])
        close_i = float(df["close"].iloc[i])
        open_i  = float(df["open"].iloc[i])

        # Lấy nến TRƯỚC để biết hướng tiếp cận
        prev_close = float(df["close"].iloc[i-1])
        prev_low   = float(df["low"].iloc[i-1])
        prev_high  = float(df["high"].iloc[i-1])

        if direction == "LONG":
            # LONG bounce: nến trước phải ABOVE MA (giá đang ở trên)
            # → nến hiện tại dip down chạm MA rồi bật lên
            came_from_above = prev_close > ma_val and prev_low > ma_val * 0.998
            touched_ma = low_i <= ma_val + tol
            closed_above = close_i > ma_val
            bullish_candle = close_i > open_i

            if came_from_above and touched_ma and closed_above and bullish_candle:
                return True, round(ma_val, 6), i

        elif direction == "SHORT":
            # SHORT rejection: nến trước phải BELOW MA (giá đang ở dưới)
            # → nến hiện tại rally up chạm MA rồi rớt xuống
            came_from_below = prev_close < ma_val and prev_high < ma_val * 1.002
            touched_ma = high_i >= ma_val - tol
            closed_below = close_i < ma_val
            bearish_candle = close_i < open_i

            if came_from_below and touched_ma and closed_below and bearish_candle:
                return True, round(ma_val, 6), i

    return False, round(ma_val, 6), None


def reversal_analyze(symbol: str, cfg: dict) -> dict:
    """Phân tích Mean Reversion signal."""
    ff = bool(cfg.get("force_futures", False))

    # Fetch data: H4 cho trend filter, H1 chính, M15 cho entry
    df_h4  = prepare(fetch_klines(symbol, "4h",  100, force_futures=ff))
    df_h1  = prepare(fetch_klines(symbol, "1h",  150, force_futures=ff))
    df_m15 = prepare(fetch_klines(symbol, "15m", 100, force_futures=ff))

    for df in [df_h1, df_m15]:
        if len(df) < 20:
            raise ValueError(f"Không đủ data cho {symbol}")

    price    = float(df_m15["close"].iloc[-1])
    row_h1   = df_h1.iloc[-1]
    row_m15  = df_m15.iloc[-1]

    # Market data
    funding   = fetch_funding_rate(symbol)
    oi_change = fetch_oi_change(symbol)
    btc_ctx   = fetch_btc_context()
    taker     = fetch_taker_ratio(symbol, period="5m", limit=6)

    atr_h1  = float(df_h1["atr"].iloc[-1])
    atr_m15 = float(df_m15["atr"].iloc[-1])
    rsi_h1  = float(row_h1["rsi"])
    rsi_m15 = float(row_m15["rsi"])

    # ────────────────────────────────────────
    # DETECT REVERSAL CONDITIONS
    # ────────────────────────────────────────
    conditions = []
    warnings   = []
    score      = 0

    # === LONG REVERSAL (bounce from support) ===
    long_signals = 0
    long_conditions = []

    # 1. Giá chạm EMA200 H1 và bounce LONG (giá > MA sau bounce)
    bounce_200, ma200_val, _ = _check_ma_bounce(price, df_h1, "ma200", direction="LONG", tolerance_pct=0.3)
    if bounce_200:
        long_conditions.append(f"Bounce LONG từ EMA200 H1 ({smart_round(ma200_val)})")
        long_signals += 2

    # 2. Giá chạm MA89 H1 và bounce LONG
    bounce_89, ma89_val, _ = _check_ma_bounce(price, df_h1, "ma89", direction="LONG", tolerance_pct=0.4)
    if bounce_89:
        long_conditions.append(f"Bounce LONG từ MA89 H1 ({smart_round(ma89_val)})")
        long_signals += 1

    # 3. RSI oversold H1
    if rsi_h1 < 30:
        long_conditions.append(f"RSI H1 {rsi_h1:.0f} — oversold (< 30)")
        long_signals += 1
    elif rsi_h1 < 35:
        long_conditions.append(f"RSI H1 {rsi_h1:.0f} — gần oversold")
        long_signals += 0.5

    # 4. Pin bar bullish trên H1
    is_pin, pin_str = _is_pin_bar(row_h1, "LONG", atr_h1)
    if is_pin:
        long_conditions.append(f"Pin bar bullish H1 (strength {pin_str})")
        long_signals += 1

    is_pin_m15, pin_str_m15 = _is_pin_bar(row_m15, "LONG", atr_m15)
    if is_pin_m15:
        long_conditions.append(f"Pin bar bullish M15 (strength {pin_str_m15})")
        long_signals += 0.5

    # 5. Taker buy spike (lực mua đang vào)
    if taker and taker["buy_ratio"] > 1.3:
        long_conditions.append(f"Taker BUY mạnh ({taker['buy_ratio']:.2f}x)")
        long_signals += 1

    # 6. BTC context — BTC cũng đang bounce?
    btc_bounce = False
    if btc_ctx.get("sentiment") in ("NEUTRAL",) and btc_ctx.get("chg_24h", 0) and btc_ctx["chg_24h"] > -2:
        btc_bounce = True
    elif btc_ctx.get("sentiment") == "RISK_ON":
        btc_bounce = True
    if btc_ctx.get("sentiment") == "NEUTRAL" and btc_ctx.get("d1_trend") == "BEAR":
        btc_bounce = True

    if btc_bounce:
        long_conditions.append("BTC đang hồi/ổn định — hỗ trợ reversal LONG")
        long_signals += 0.5

    # === SHORT REVERSAL (rejection from resistance) ===
    short_signals = 0
    short_conditions = []

    # 1. Giá chạm MA200 H1 và rejection SHORT (giá < MA sau touch từ dưới lên)
    bounce_200_s, ma200_s, _ = _check_ma_bounce(price, df_h1, "ma200", direction="SHORT", tolerance_pct=0.3)
    if bounce_200_s:
        short_conditions.append(f"Rejection SHORT từ EMA200 H1 ({smart_round(ma200_s)})")
        short_signals += 2

    bounce_89_s, ma89_s, _ = _check_ma_bounce(price, df_h1, "ma89", direction="SHORT", tolerance_pct=0.4)
    if bounce_89_s:
        short_conditions.append(f"Rejection SHORT từ MA89 H1 ({smart_round(ma89_s)})")
        short_signals += 1

    # 2. RSI overbought H1
    if rsi_h1 > 70:
        short_conditions.append(f"RSI H1 {rsi_h1:.0f} — overbought (> 70)")
        short_signals += 1
    elif rsi_h1 > 65:
        short_conditions.append(f"RSI H1 {rsi_h1:.0f} — gần overbought")
        short_signals += 0.5

    # 3. Pin bar bearish
    is_pin_s, pin_str_s = _is_pin_bar(row_h1, "SHORT", atr_h1)
    if is_pin_s:
        short_conditions.append(f"Pin bar bearish H1 (strength {pin_str_s})")
        short_signals += 1

    # 4. Taker sell mạnh
    if taker and taker["buy_ratio"] < 0.77:
        short_conditions.append(f"Taker SELL mạnh ({taker['buy_ratio']:.2f}x)")
        short_signals += 1

    # ────────────────────────────────────────
    # DIRECTION & CONFIDENCE
    # ────────────────────────────────────────
    # Cần ít nhất 3 điểm để trigger reversal signal
    MIN_SCORE = 3

    if long_signals >= MIN_SCORE and long_signals > short_signals:
        direction = "LONG"
        score = int(long_signals)
        conditions = long_conditions
    elif short_signals >= MIN_SCORE and short_signals > long_signals:
        direction = "SHORT"
        score = int(short_signals)
        conditions = short_conditions
    else:
        direction = "WAIT"
        score = 0
        # Hiển thị side đang gần trigger nhất để user biết đang quan sát gì
        if long_signals >= short_signals:
            conditions = [f"Đang theo dõi LONG bounce (score {long_signals:.1f}/3.0)"] + long_conditions
        else:
            conditions = [f"Đang theo dõi SHORT rejection (score {short_signals:.1f}/3.0)"] + short_conditions

    confidence = "HIGH" if score >= 5 else "MEDIUM" if score >= 3 else "LOW"

    if direction == "WAIT":
        confidence = "LOW"

    # BTC DUMP mạnh → không reversal LONG
    if direction == "LONG" and btc_ctx.get("sentiment") == "DUMP":
        direction = "WAIT"
        confidence = "LOW"
        warnings.append("🚫 BTC DUMP — không reversal LONG khi BTC đang rơi mạnh")

    # ════════════════════════════════════════════════════════════════
    # REV-PATCHES — Smart filters cho REVERSAL
    # Case ARBUSDT 29/04/2026: SHORT signals fire liên tục lúc 13:17,
    # 13:37, ... khi giá đang BREAKOUT từ 0.126 → 0.132. Engine đọc
    # nhầm "giá chạm EMA89/200 từ dưới" thành rejection thay vì breakout.
    # ════════════════════════════════════════════════════════════════

    # ── REV-1' Smart: H4 Trend Conflict Block ──
    # Block reversal đi NGƯỢC H4 trend khi giá CHƯA extended.
    # Cho phép khi giá đã extended >5% từ MA34 H4 (vùng blow-off — fade tốt).
    if direction in ("LONG", "SHORT") and len(df_h4) >= 5:
        try:
            row_h4   = df_h4.iloc[-1]
            ma34_h4  = float(row_h4.get("ma34") or 0)
            ma89_h4  = float(row_h4.get("ma89") or 0)
            slope_h4 = ma_slope(df_h4["ma34"], n=3) if "ma34" in df_h4.columns else "FLAT"
            h4_close = float(row_h4["close"])

            h4_strong_bull = (ma34_h4 > 0 and ma89_h4 > 0 and
                              h4_close > ma34_h4 > ma89_h4 and slope_h4 == "UP")
            h4_strong_bear = (ma34_h4 > 0 and ma89_h4 > 0 and
                              h4_close < ma34_h4 < ma89_h4 and slope_h4 == "DOWN")

            # Đo khoảng cách giá hiện tại tới MA34 H4 (% extended)
            dist_pct = (price - ma34_h4) / ma34_h4 * 100 if ma34_h4 > 0 else 0

            if direction == "SHORT" and h4_strong_bull and dist_pct < 5:
                # Đang fade trend mạnh, chưa extended → block
                direction  = "WAIT"
                confidence = "LOW"
                warnings.append(
                    f"🚫 REV-1: H4 BULL mạnh + giá chưa extended ({dist_pct:+.1f}% vs MA34 H4) "
                    f"— không SHORT fade trend đang lên"
                )
            elif direction == "LONG" and h4_strong_bear and dist_pct > -5:
                direction  = "WAIT"
                confidence = "LOW"
                warnings.append(
                    f"🚫 REV-1: H4 BEAR mạnh + giá chưa extended ({dist_pct:+.1f}% vs MA34 H4) "
                    f"— không LONG fade trend đang xuống"
                )
        except Exception as _e1:
            pass

    # ── REV-2' Smart: Active Breakout Block ──
    # Block SHORT trong nến break high đầu tiên với volume cao.
    # Cho phép sau khi có ≥2 nến đóng dưới breakout level (failed breakout).
    if direction in ("LONG", "SHORT") and len(df_h1) >= 22:
        try:
            high20_prev = float(df_h1["high"].iloc[-22:-2].max())
            low20_prev  = float(df_h1["low"].iloc[-22:-2].min())
            cur_high    = float(df_h1["high"].iloc[-1])
            cur_low     = float(df_h1["low"].iloc[-1])
            cur_close   = float(df_h1["close"].iloc[-1])
            prev_close  = float(df_h1["close"].iloc[-2])
            cur_vol_r   = float(df_h1["vol_ratio"].iloc[-1]) if "vol_ratio" in df_h1.columns else 1.0

            # Breakout UP: nến hiện tại make new HH
            broke_up   = cur_high > high20_prev * 1.002 and cur_vol_r > 1.5
            # Failed up: 2 nến đóng dưới breakout level
            failed_up  = cur_close < high20_prev and prev_close < high20_prev

            broke_dn   = cur_low < low20_prev * 0.998 and cur_vol_r > 1.5
            failed_dn  = cur_close > low20_prev and prev_close > low20_prev

            if direction == "SHORT" and broke_up and not failed_up:
                direction  = "WAIT"
                confidence = "LOW"
                warnings.append(
                    f"🚫 REV-2: Active breakout UP (high {cur_high:.6f} > {high20_prev:.6f}, "
                    f"vol {cur_vol_r:.1f}x) — không SHORT trong nến break, chờ confirm fail"
                )
            elif direction == "LONG" and broke_dn and not failed_dn:
                direction  = "WAIT"
                confidence = "LOW"
                warnings.append(
                    f"🚫 REV-2: Active breakout DOWN (low {cur_low:.6f} < {low20_prev:.6f}, "
                    f"vol {cur_vol_r:.1f}x) — không LONG trong nến break, chờ confirm bounce"
                )
        except Exception as _e2:
            pass

    # ── REV-3: Volume Confirmation cho Pin Bar ──
    # Pin bar reversal chỉ đáng tin khi có volume confirm. Vol < 1.2x
    # baseline = pin bar có thể là noise, hạ confidence một bậc.
    if direction in ("LONG", "SHORT") and confidence in ("HIGH", "MEDIUM"):
        try:
            cur_vol_r = float(df_h1["vol_ratio"].iloc[-1]) if "vol_ratio" in df_h1.columns else 1.0
            had_pin   = (direction == "LONG" and is_pin) or (direction == "SHORT" and is_pin_s)
            if had_pin and cur_vol_r < 1.2:
                if confidence == "HIGH":
                    confidence = "MEDIUM"
                elif confidence == "MEDIUM":
                    confidence = "LOW"
                warnings.append(
                    f"⚠️ REV-3: Pin bar volume thấp ({cur_vol_r:.2f}x < 1.2x) — "
                    f"có thể là noise, hạ confidence"
                )
        except Exception as _e3:
            pass

    # ── REV-4' Smart: MEDIUM Gating với 2/3 Strong Conditions ──
    # MEDIUM REVERSAL (score 3-4) phải có ≥2 trong 3 điều kiện mạnh:
    #   1. RSI extreme (>75 SHORT / <25 LONG)
    #   2. Pin bar strength > 0.7
    #   3. Taker confirm mạnh (>1.4x cho LONG / <0.7x cho SHORT)
    # Nếu không đủ → hạ MEDIUM xuống LOW.
    if direction in ("LONG", "SHORT") and confidence == "MEDIUM":
        strong_count = 0
        try:
            if direction == "SHORT":
                if rsi_h1 > 75: strong_count += 1
                if is_pin_s and pin_str_s > 0.7: strong_count += 1
                if taker and taker.get("buy_ratio", 1.0) < 0.7: strong_count += 1
            else:
                if rsi_h1 < 25: strong_count += 1
                if is_pin and pin_str > 0.7: strong_count += 1
                if taker and taker.get("buy_ratio", 1.0) > 1.4: strong_count += 1
        except Exception:
            pass

        if strong_count < 2:
            confidence = "LOW"
            warnings.append(
                f"⚠️ REV-4: MEDIUM reversal nhưng chỉ có {strong_count}/3 điều kiện mạnh "
                f"— hạ xuống LOW (cần RSI extreme/pin strong/taker confirm)"
            )

    # ────────────────────────────────────────
    # SL / TP — Reversal style
    # ────────────────────────────────────────
    _is_major = symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT")

    if direction == "LONG" or (direction == "WAIT" and long_signals > short_signals):
        entry = price
        # SL: dưới low của pin bar hoặc dưới MA bounce point
        recent_low = float(df_h1["low"].iloc[-3:].min())
        sl_price = smart_round(recent_low - atr_h1 * 0.2)
        # Cap SL: 0.5-2% cho altcoin, 0.4-1.5% cho major
        max_sl = entry * (0.985 if _is_major else 0.98)
        min_sl = entry * (0.996 if _is_major else 0.995)
        sl_price = smart_round(max(sl_price, max_sl))
        sl_price = smart_round(min(sl_price, min_sl))

        # TP: lên EMA34 hoặc EMA21 H1
        ema34_h1 = float(df_h1["ma34"].iloc[-1])
        ema21_m15 = float(df_m15["ema21"].iloc[-1]) if "ema21" in df_m15.columns else ema34_h1

        tp1 = smart_round(max(ema21_m15, entry * 1.005))
        if ema34_h1 > entry * 1.005:
            tp1 = smart_round(ema34_h1 * 0.998)  # TP ngay trước EMA34

        # Ensure TP > SL distance
        sl_dist = entry - sl_price
        if tp1 - entry < sl_dist * 1.2:
            tp1 = smart_round(entry + sl_dist * 1.5)

        tp2 = smart_round(tp1 + (tp1 - entry))

    elif direction == "SHORT" or (direction == "WAIT" and short_signals > long_signals):
        entry = price
        recent_high = float(df_h1["high"].iloc[-3:].max())
        sl_price = smart_round(recent_high + atr_h1 * 0.2)
        max_sl = entry * (1.015 if _is_major else 1.02)
        min_sl = entry * (1.004 if _is_major else 1.005)
        sl_price = smart_round(min(sl_price, max_sl))
        sl_price = smart_round(max(sl_price, min_sl))

        ema34_h1 = float(df_h1["ma34"].iloc[-1])
        tp1 = smart_round(min(ema34_h1, entry * 0.995))
        if ema34_h1 < entry * 0.995:
            tp1 = smart_round(ema34_h1 * 1.002)

        sl_dist = sl_price - entry
        if entry - tp1 < sl_dist * 1.2:
            tp1 = smart_round(entry - sl_dist * 1.5)

        tp2 = smart_round(tp1 - (entry - tp1))
    else:
        entry = sl_price = tp1 = tp2 = price

    sl_pct  = round(abs(entry - sl_price) / entry * 100, 2) if entry != sl_price else 0
    tp1_pct = round(abs(tp1 - entry) / entry * 100, 2)      if entry != tp1 else 0
    rr      = round(tp1_pct / sl_pct, 2)                    if sl_pct > 0 else 0

    if direction in ("LONG", "SHORT") and rr < 1.2:
        warnings.append(f"❌ R:R {rr} < 1.2 — reversal signal yếu")
        direction  = "WAIT"
        confidence = "LOW"

    # Entry verdict
    if direction in ("LONG", "SHORT") and confidence in ("HIGH", "MEDIUM") and rr >= 1.5:
        entry_verdict = "GO"
    elif direction in ("LONG", "SHORT") and confidence == "MEDIUM":
        entry_verdict = "WAIT"
    else:
        entry_verdict = "NO" if direction == "WAIT" else "WAIT"

    # ── Chart candles — H1 ──
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
        "strategy":      "REVERSAL",
        "price":         smart_round(price),
        "direction":     direction,
        "confidence":    confidence,
        "score":         int(score),
        "conditions":    conditions,
        "warnings":      warnings,
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
            "atr_ratio":   round(atr_h1 / float(df_h1["atr"].iloc[-60:].mean()) if len(df_h1) >= 60 else 1, 2),
            "atr_state":   "NORMAL",
            "atr_note":    "",
        },
        "btc_context": btc_ctx,
        "reversal_data": {
            "type":          "MEAN_REVERSION",
            "long_score":    round(long_signals, 1),
            "short_score":   round(short_signals, 1),
            "rsi_h1":        round(rsi_h1, 1),
            "rsi_m15":       round(rsi_m15, 1),
            "ma200_bounce":  bounce_200,
            "ma89_bounce":   bounce_89,
            "pin_bar_h1":    is_pin,
            "taker":         taker,
        },
        "d1":  {"bias": direction, "structure": "", "notes": conditions[:2]},
        "h4":  {"bias": direction, "structure": "",
                "above_ma34": False, "above_ma89": False,
                "crossed_ma34": False, "slope_ma34": "", "slope_ma89": "", "slope_ma200": "",
                "ma34": smart_round(row_h1["ma34"]),
                "ma89": smart_round(row_h1["ma89"]),
                "ma200": smart_round(row_h1["ma200"]),
                "notes": []},
        "h1":  {"fib_zone": "", "fib_zone_price": "",
                "vol_ratio": round(float(row_h1["vol_ratio"]), 2),
                "h1_bullish": bool(row_h1["close"] > row_h1["open"]),
                "breakout": False},
        "fib_ret":   {},
        "fib_ext":   {},
        "swing_high": smart_round(float(df_h1["high"].iloc[-20:].max())),
        "swing_low":  smart_round(float(df_h1["low"].iloc[-20:].min())),
        "candles":    candles,
        "timestamp":  datetime.now(_TZ_VN).isoformat(),
        "h1_status":       "REVERSAL",
        "h1_status_note":  f"Mean reversion signal — score {score}",
        "entry_checklist":  [],
        "entry_verdict":   entry_verdict,
        "d1_bias":  direction,
        "h4_bias":  direction,
    })
