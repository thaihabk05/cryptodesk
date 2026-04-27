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
from datetime import datetime, timezone, timedelta

_TZ_VN = timezone(timedelta(hours=7))

from core.binance import (fetch_klines, fetch_funding_rate,
                           fetch_oi_change, fetch_btc_context,
                           fetch_taker_ratio, fetch_long_short_ratio,
                           fetch_order_book_imbalance)
from core.indicators import (prepare, ma_slope, find_swing_points,
                              classify_structure, fib_retracement,
                              fib_extension, calc_atr_context)
from core.utils import sanitize, smart_round


def scalp_analyze(symbol: str, cfg: dict) -> dict:
    """Phân tích theo strategy Scalp M15/H1."""
    ff = bool(cfg.get("force_futures", False))

    # Fetch: H1 (bias) + M15 (confirm) + M5 (entry)
    df_d1  = prepare(fetch_klines(symbol, "1d",   30, force_futures=ff))  # cho pump exhaustion check
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

    # ── Scalp-specific data (FAM Trading method) ──
    taker     = fetch_taker_ratio(symbol, period="5m", limit=6)
    ls_ratio  = fetch_long_short_ratio(symbol, period="5m", limit=6)
    ob_data   = fetch_order_book_imbalance(symbol, limit=50)

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

    # m5_status sẽ được gọi lại sau khi xác định direction cuối cùng

    # ────────────────────────────────────────
    # DIRECTION & SCORING
    # ────────────────────────────────────────
    warnings = []

    if h1_bias == "NEUTRAL":
        # Scalp: H1 NEUTRAL không tự động block nếu M15+M5 đồng thuận
        # FAM Trading scalp chủ yếu dựa vào M15, H1 chỉ là tham khảo
        m15_m5_agree_long  = m15_ema_bull and m5_ema_bull and m5_bullish
        m15_m5_agree_short = not m15_ema_bull and not m5_ema_bull and m5_bearish
        if m15_m5_agree_long:
            h1_bias = "LONG"  # override bằng M15+M5
            direction  = "LONG"
            confidence = "MEDIUM"  # giới hạn MEDIUM vì thiếu H1
            conditions = ["H1 NEUTRAL — M15+M5 đồng thuận LONG override"]
            score = 1
        elif m15_m5_agree_short:
            h1_bias = "SHORT"
            direction  = "SHORT"
            confidence = "MEDIUM"
            conditions = ["H1 NEUTRAL — M15+M5 đồng thuận SHORT override"]
            score = 1
        else:
            direction  = "WAIT"
            confidence = "LOW"
            conditions = ["H1 EMA9/21 chưa rõ hướng + M15/M5 không đồng thuận"]
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
            # ── Scalp data conditions (FAM Trading) ──
            if taker and taker["trend"] in ("BUY_STRONG", "BUY_MILD"):
                conditions.append(f"Lực mua mạnh (Taker {taker['buy_ratio']:.2f}x) — buyer aggressive")
            if taker and taker["trend"] == "SELL_STRONG":
                warnings.append(f"⚠️ Lực bán mạnh (Taker {taker['buy_ratio']:.2f}x) — ngược chiều LONG")
            if ob_data and ob_data["imbalance"] > 1.3:
                conditions.append(f"Sổ lệnh thiên mua ({ob_data['imbalance']:.1f}x) — hỗ trợ LONG")
            if ls_ratio and ls_ratio["extreme"] == "SHORT_CROWDED":
                conditions.append(f"Short crowded ({ls_ratio['short_pct']:.0f}%) — potential short squeeze")
            elif ls_ratio and ls_ratio["extreme"] == "LONG_CROWDED":
                warnings.append(f"⚠️ Long crowded ({ls_ratio['long_pct']:.0f}%) — rủi ro long squeeze khi LONG")
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
            # ── Scalp data conditions (FAM Trading) ──
            if taker and taker["trend"] in ("SELL_STRONG", "SELL_MILD"):
                conditions.append(f"Lực bán mạnh (Taker {taker['buy_ratio']:.2f}x) — seller aggressive")
            if taker and taker["trend"] == "BUY_STRONG":
                warnings.append(f"⚠️ Lực mua mạnh (Taker {taker['buy_ratio']:.2f}x) — ngược chiều SHORT")
            if ob_data and ob_data["imbalance"] < 0.77:
                conditions.append(f"Sổ lệnh thiên bán ({ob_data['imbalance']:.1f}x) — hỗ trợ SHORT")
            if ls_ratio and ls_ratio["extreme"] == "LONG_CROWDED":
                conditions.append(f"Long crowded ({ls_ratio['long_pct']:.0f}%) — potential long squeeze")
            elif ls_ratio and ls_ratio["extreme"] == "SHORT_CROWDED":
                warnings.append(f"⚠️ Short crowded ({ls_ratio['short_pct']:.0f}%) — rủi ro short squeeze khi SHORT")

        score = len(conditions)
        confidence = "HIGH" if score >= 5 else "MEDIUM" if score >= 3 else "LOW"

    # Xác định M5 status dựa trên direction cuối cùng
    m5_status, m5_note = get_m5_status(direction)

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


    # ── PATCH F: Abnormal Candle Spike Filter ──
    # Check cả nến cuối VÀ nến trước — spike có thể ở nến trước, nến sau chưa confirm
    _spike_atr_avg = float(df_m15["atr"].iloc[-20:].mean()) if len(df_m15) >= 20 else atr_m15
    _spike_threshold = _spike_atr_avg * 2.0
    _spike_triggered = False
    _spike_body = 0.0
    _spike_which = ""
    for _si, _slabel in [(-1, "hiện tại"), (-2, "trước")]:
        _sr = df_m15.iloc[_si]
        _sb = abs(float(_sr["close"]) - float(_sr["open"]))
        if _sb > _spike_threshold:
            _spike_triggered = True
            _spike_body = _sb
            _spike_which = _slabel
            break
    if _spike_triggered and direction in ("LONG", "SHORT"):
        direction  = "WAIT"
        confidence = "LOW"
        all_warnings.insert(0, f"🚫 SPIKE FILTER — Nến M15 {_spike_which} body {_spike_body:.5f} > 2x ATR ({_spike_threshold:.5f}) — pump/dump đột ngột, chờ confirmation")

    # ── PATCH A: BTC Context — Scalp v4 (symmetric block) ──
    # Backtest v2.0: 8/16 LOSS = LONG altcoin khi BTC RISK_OFF → fixed in v2.1
    # Backtest v2.1: 7/7 LOSS khi BTC RISK_ON → SHORT altcoin bị squeeze
    # Fix: block cả 2 chiều counter-trend cho altcoin
    # Cho phép: BTC/ETH scalp cả 2 chiều (FAM style) + taker override
    btc_sent = btc_ctx.get("sentiment", "NEUTRAL")
    btc_d1   = btc_ctx.get("d1_trend", "")
    _taker_buy  = taker and taker["trend"] in ("BUY_STRONG", "BUY_MILD")
    _taker_sell = taker and taker["trend"] in ("SELL_STRONG", "SELL_MILD")
    _is_major = symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT")

    # ── LONG counter-trend: BTC đang BEAR/DUMP ──
    if direction == "LONG" and btc_sent in ("RISK_OFF", "DUMP"):
        if _is_major and _taker_buy:
            all_warnings.append(f"⚠️ BTC {btc_sent} nhưng {symbol[:3]} + taker mua mạnh — scalp LONG OK, SL chặt")
        elif _is_major and btc_sent == "RISK_OFF":
            if confidence == "HIGH": confidence = "MEDIUM"
            all_warnings.append(f"⚠️ BTC RISK_OFF — {symbol[:3]} scalp LONG cẩn thận, giữ SL chặt")
        else:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0, f"🚫 BLOCK LONG altcoin — BTC {btc_sent}, không LONG ngược trend")

    # ── SHORT counter-trend: BTC đang BULL/PUMP ──
    # Backtest 7d: 0 SHORT signals trong khi nhiều altcoin downtrend rõ
    # Fix v5: cho phép SHORT khi altcoin có structure DOWNTREND rõ rệt
    # (giá dưới MA89 H1 + taker bán mạnh) — coin yếu hơn BTC nhiều
    elif direction == "SHORT" and btc_sent in ("RISK_ON", "PUMP"):
        # Check structure altcoin yếu rõ rệt
        _ma89_h1_val = float(df_h1["ma89"].iloc[-1]) if "ma89" in df_h1.columns else 0
        _ma34_h1_val = float(df_h1["ma34"].iloc[-1]) if "ma34" in df_h1.columns else 0
        _alt_weak = (_ma89_h1_val > 0 and price < _ma89_h1_val * 0.99
                     and _ma34_h1_val > 0 and price < _ma34_h1_val)

        if _is_major and _taker_sell:
            all_warnings.append(f"⚠️ BTC {btc_sent} nhưng {symbol[:3]} + taker bán mạnh — scalp SHORT OK, SL chặt")
        elif _is_major and btc_sent == "RISK_ON":
            if confidence == "HIGH": confidence = "MEDIUM"
            all_warnings.append(f"⚠️ BTC RISK_ON — {symbol[:3]} scalp SHORT cẩn thận, giữ SL chặt")
        elif _alt_weak and _taker_sell:
            # Altcoin yếu hơn BTC + taker bán mạnh → cho phép SHORT
            if confidence == "HIGH": confidence = "MEDIUM"
            all_warnings.append(f"⚠️ BTC RISK_ON nhưng altcoin yếu (giá < MA89 H1) + taker bán — SHORT OK")
        else:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0, f"🚫 BLOCK SHORT altcoin — BTC {btc_sent}, không SHORT ngược trend mạnh")

    # ── PATCH E: OI + Long/Short Ratio — Scalp version ──
    # Kết hợp OI với LS ratio: OI giảm + crowded = nguy hiểm, OI giảm nhẹ + taker mạnh = OK
    _ls_extreme = ls_ratio["extreme"] if ls_ratio else "BALANCED"
    if oi_change is not None and direction == "LONG" and oi_change < -5:
        if _ls_extreme == "LONG_CROWDED":
            # OI giảm + quá đông LONG = long đang tháo chạy → block
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0, f"🚫 OI giảm {oi_change:+.1f}% + Long crowded — long đang tháo chạy")
        elif _taker_override:
            # OI giảm nhưng taker BUY mạnh = chỉ cảnh báo
            all_warnings.append(f"⚠️ OI giảm {oi_change:+.1f}% nhưng lực mua taker vẫn mạnh — theo dõi")
        else:
            if confidence == "HIGH": confidence = "MEDIUM"
            all_warnings.append(f"⚠️ OI giảm {oi_change:+.1f}% — vị thế đang đóng, cân nhắc")
    elif oi_change is not None and direction == "SHORT" and oi_change > 5:
        if _ls_extreme == "SHORT_CROWDED":
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0, f"🚫 OI tăng {oi_change:+.1f}% + Short crowded — rủi ro short squeeze")
        else:
            all_warnings.append(f"⚠️ OI tăng {oi_change:+.1f}% — tiền đang vào, cân nhắc SHORT")


    total_adj = funding_adj + atr_adj
    if total_adj <= -2 and confidence != "LOW":
        confidence = "LOW"
        all_warnings.append("⚠️ Confidence hạ LOW do funding/volatility bất lợi")
    elif total_adj == -1 and confidence == "HIGH":
        confidence = "MEDIUM"


    # ── PATCH G: Price-OI Divergence — Scalp version ──
    # Nới ngưỡng cho scalp: OI > 5% (vs 3%) và giá giảm > 2% (vs 1%)
    if oi_change is not None and direction == "LONG" and oi_change > 5:
        _price_chg_m15 = (float(df_m15["close"].iloc[-1]) - float(df_m15["close"].iloc[-4])) / float(df_m15["close"].iloc[-4]) * 100
        if _price_chg_m15 < -2.0:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0, f"🚫 OI DIVERGENCE — OI +{oi_change:.1f}% nhưng giá giảm {_price_chg_m15:.1f}% — tiền vào SHORT")

    # ── PATCH H: EMA9 Price Position — Scalp version ──
    # Chuyển từ hard block → soft warning
    # Scalp entry thường ngay tại EMA, block EMA9 = block mọi entry
    if "ema9" in df_m15.columns:
        _ema9_m15 = float(df_m15["ema9"].iloc[-1])
        _price_m15 = float(df_m15["close"].iloc[-1])
        if direction == "LONG" and _price_m15 < _ema9_m15 * 0.995:
            if confidence == "HIGH": confidence = "MEDIUM"
            all_warnings.append(f"⚠️ Giá dưới EMA9 M15 — momentum ngắn hạn yếu")
        elif direction == "SHORT" and _price_m15 > _ema9_m15 * 1.005:
            if confidence == "HIGH": confidence = "MEDIUM"
            all_warnings.append(f"⚠️ Giá trên EMA9 M15 — momentum ngắn hạn mạnh")

    # ── PATCH I: Far From EMA21 M15 — gợi ý chờ pullback ──
    if direction in ("LONG", "SHORT") and ema21_m15 > 0:
        _dist_ema21_m15 = (price - ema21_m15) / ema21_m15 * 100
        if direction == "LONG" and _dist_ema21_m15 > 3:
            _pb = round(ema21_m15 * 1.003, 6)
            all_warnings.append(
                f"⚠️ GIÁ CÁCH EMA21 M15 +{round(_dist_ema21_m15,1)}% — entry ngay không tối ưu. "
                f"Chờ pullback về ~{_pb} (vùng EMA21 M15) để R:R tốt hơn"
            )
        elif direction == "SHORT" and _dist_ema21_m15 < -3:
            _pb = round(ema21_m15 * 0.997, 6)
            all_warnings.append(
                f"⚠️ GIÁ CÁCH EMA21 M15 -{round(abs(_dist_ema21_m15),1)}% — entry ngay không tối ưu. "
                f"Chờ rebound về ~{_pb} (vùng EMA21 M15) để R:R tốt hơn"
            )

    # ── PATCH J: Pump Exhaustion — 7-day price change ──
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

    # ── PATCH M: Trend Position Filter (backtest 7d v2.3 → 23% WR) ──
    # Pattern: LONG khi giá dưới MA89 H1 = bắt dao rơi trong downtrend
    # CRV/ARB cases: EMA9>EMA21 cross nhỏ trong downtrend → bị quét
    # Fix: LONG cần giá trên MA89 H1, SHORT cần giá dưới MA89 H1
    if "ma89" in df_h1.columns:
        _ma89_h1 = float(df_h1["ma89"].iloc[-1])
        if direction == "LONG" and price < _ma89_h1 * 0.998:
            # Cho phép nếu giá đang bounce mạnh từ MA200 (reversal setup)
            _ma200_h1 = float(df_h1["ma200"].iloc[-1]) if "ma200" in df_h1.columns else 0
            _bouncing_ma200 = _ma200_h1 > 0 and price > _ma200_h1 * 0.998 and price < _ma200_h1 * 1.02
            if not _bouncing_ma200:
                direction  = "WAIT"
                confidence = "LOW"
                all_warnings.insert(0,
                    f"🚫 LONG TRONG DOWNTREND — Giá {price:.5f} dưới MA89 H1 ({_ma89_h1:.5f}) "
                    f"— bắt dao rơi, chờ giá vượt MA89 hoặc bounce từ MA200")
        elif direction == "SHORT" and price > _ma89_h1 * 1.002:
            _ma200_h1 = float(df_h1["ma200"].iloc[-1]) if "ma200" in df_h1.columns else 0
            _rejecting_ma200 = _ma200_h1 > 0 and price < _ma200_h1 * 1.002 and price > _ma200_h1 * 0.98
            if not _rejecting_ma200:
                direction  = "WAIT"
                confidence = "LOW"
                all_warnings.insert(0,
                    f"🚫 SHORT TRONG UPTREND — Giá {price:.5f} trên MA89 H1 ({_ma89_h1:.5f}) "
                    f"— ngược trend mạnh, chờ rejection từ MA200")

    # ── PATCH K: Chasing Filter — strict cho BTC/ETH (ít volatile hơn) ──
    # Reject signal khi đang đuổi giá cuối sóng pump/dump
    # User real loss: BTC LONG 78,149 sau khi BTC pump → bị quét -1%
    if direction in ("LONG", "SHORT") and len(df_h1) >= 24:
        _h24_high = float(df_h1["high"].iloc[-24:].max())
        _h24_low  = float(df_h1["low"].iloc[-24:].min())
        _h24_move = round((_h24_high - _h24_low) / _h24_low * 100, 1) if _h24_low > 0 else 0
        _dist_high = round((_h24_high - price) / _h24_high * 100, 1) if _h24_high > 0 else 99
        _dist_low  = round((price - _h24_low) / _h24_low * 100, 1) if _h24_low > 0 else 99

        # Major coins: thresholds chặt hơn (BTC/ETH ít volatile, 5% move = nhiều)
        if _is_major:
            move_threshold = 5
            dist_threshold = 1.5
        else:
            move_threshold = 10
            dist_threshold = 3

        if direction == "LONG" and _h24_move > move_threshold and _dist_high < dist_threshold:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0,
                f"🚫 CHASING — Entry cách 24h High chỉ {_dist_high}% trong khi 24h move +{_h24_move}% "
                f"— đang đuổi giá cuối sóng pump, chờ pullback")
        elif direction == "SHORT" and _h24_move > move_threshold and _dist_low < dist_threshold:
            direction  = "WAIT"
            confidence = "LOW"
            all_warnings.insert(0,
                f"🚫 CHASING — Entry cách 24h Low chỉ {_dist_low}% trong khi 24h move -{_h24_move}% "
                f"— đang đuổi giá cuối sóng dump, chờ bounce")

    # ── PATCH L: RSI H1 overbought/oversold check ──
    # Hệ thống chỉ check RSI M15, bỏ qua RSI H1 → miss divergence
    _rsi_h1 = float(row_h1["rsi"])
    if direction == "LONG" and _rsi_h1 > 75:
        if confidence == "HIGH": confidence = "MEDIUM"
        all_warnings.append(f"⚠️ RSI H1 {_rsi_h1:.0f} overbought — rủi ro mean-reversion, cân nhắc chờ RSI hạ")
    elif direction == "SHORT" and _rsi_h1 < 25:
        if confidence == "HIGH": confidence = "MEDIUM"
        all_warnings.append(f"⚠️ RSI H1 {_rsi_h1:.0f} oversold — rủi ro bounce, cân nhắc chờ RSI tăng")

    # ────────────────────────────────────────
    # SL / TP — ATR M15, swing M15 gần nhất
    # ────────────────────────────────────────
    def _tp1_long(entry, swings, ema9, ema21, atr, ob_walls=None, sl_price_ref=None):
        mn, mx = entry * 1.005, entry * 1.03   # 0.5–3% (FAM scalp style)
        tp = None
        # 1. Order book resistance wall (target rõ nhất)
        if ob_walls:
            wall_cands = [w["price"] for w in ob_walls if mn < w["price"] < mx]
            if wall_cands:
                tp = smart_round(min(wall_cands) * 0.999)
        # 2. Swing high gần
        if tp is None:
            cands = [h for h in swings if mn < h < mx]
            if cands: tp = smart_round(min(cands))
        # 3. EMA resistance
        if tp is None:
            for ma in [ema9, ema21]:
                if mn < ma < mx: tp = smart_round(ma); break
        # 4. ATR fallback
        if tp is None:
            tp = smart_round(entry + atr * 1.5)
        # Đảm bảo TP >= SL distance (R:R >= 1.2)
        if sl_price_ref and sl_price_ref < entry:
            sl_dist = entry - sl_price_ref
            min_tp = entry + sl_dist * 1.2
            tp = smart_round(max(tp, min_tp))
        return tp

    def _tp1_short(entry, swings, ema9, ema21, atr, ob_walls=None, sl_price_ref=None):
        mx, mn = entry * 0.995, entry * 0.97   # 0.5–3%
        tp = None
        if ob_walls:
            wall_cands = [w["price"] for w in ob_walls if mn < w["price"] < mx]
            if wall_cands:
                tp = smart_round(max(wall_cands) * 1.001)
        if tp is None:
            cands = [l for l in swings if mn < l < mx]
            if cands: tp = smart_round(max(cands))
        if tp is None:
            for ma in [ema9, ema21]:
                if mn < ma < mx: tp = smart_round(ma); break
        if tp is None:
            tp = smart_round(entry - atr * 1.5)
        # Đảm bảo TP >= SL distance
        if sl_price_ref and sl_price_ref > entry:
            sl_dist = sl_price_ref - entry
            min_tp = entry - sl_dist * 1.2
            tp = smart_round(min(tp, min_tp))
        return tp

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

    # ── SL candidates theo FAM scalp style ──
    # FAM đặt SL ngay dưới/trên support/resistance gần nhất, SL chặt 0.5-1.2%
    # Ưu tiên: Order book wall > EMA21 M15 > Swing M15 gần > ATR fallback
    _ob_supports = [w["price"] for w in (ob_data or {}).get("support_walls", [])]
    _ob_resists  = [w["price"] for w in (ob_data or {}).get("resistance_walls", [])]

    # Swing M15 ngắn hơn — chỉ 10 nến gần nhất (2.5h) thay vì 30 nến
    _recent_10_low  = float(df_m15["low"].iloc[-10:].min())
    _recent_10_high = float(df_m15["high"].iloc[-10:].max())

    # ── SL minimum dynamic (ATR-aware) ──
    # Base: 0.5% altcoin, 0.4% major
    # Khi ATR cao (> 1.3x) → nới SL theo ATR, không dùng % cứng
    _is_major = symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT")
    _sl_min_pct = 0.004 if _is_major else 0.005

    # ATR scaling: khi volatility cao, SL cứng sẽ bị quét
    if atr_ratio > 1.3:
        _sl_min_pct = max(_sl_min_pct, 0.007)   # 0.7% khi ATR cao
    if atr_ratio > 1.8:
        _sl_min_pct = max(_sl_min_pct, 0.010)   # 1.0% khi ATR rất cao

    # OI cao → nới thêm (tiền đổ vào = volatility cao, SL sát dễ bị quét)
    if oi_change is not None and abs(oi_change) > 5:
        _sl_min_pct = max(_sl_min_pct, 0.006)
    if oi_change is not None and abs(oi_change) > 8:
        _sl_min_pct = max(_sl_min_pct, 0.008)
    if oi_change is not None and abs(oi_change) > 12:
        _sl_min_pct = max(_sl_min_pct, 0.012)  # OI cực cao (>12%) → SL 1.2%

    if direction == "LONG" or (direction == "WAIT" and h1_bias == "LONG"):
        entry = price

        # SL: tìm support gần nhất phía dưới
        sl_candidates = []
        # 1. Order book support wall (mạnh nhất)
        for sp in _ob_supports:
            if price * 0.995 > sp > price * 0.985:
                sl_candidates.append(("OB wall", sp - atr_m15 * 0.1))
        # 2. EMA21 M15 (support động)
        if ema21_m15 < price * 0.998 and ema21_m15 > price * 0.985:
            sl_candidates.append(("EMA21 M15", ema21_m15 - atr_m15 * 0.2))
        # 3. Swing low M15 gần (10 nến)
        if _recent_10_low < price * 0.998 and _recent_10_low > price * 0.985:
            sl_candidates.append(("Swing M15", _recent_10_low - atr_m15 * 0.1))

        if sl_candidates:
            # Lấy SL gần nhất (xa nhất về phía dưới nhưng trong range 0.5-1.5%)
            sl_candidates.sort(key=lambda x: x[1], reverse=True)  # gần nhất trước
            sl_price = smart_round(sl_candidates[0][1])
        else:
            # Fallback: ATR-based, cap 1.2%
            sl_price = smart_round(max(entry - atr_m15 * 1.2, entry * 0.988))

        # Đảm bảo SL trong range hợp lý cho scalp: _sl_min_pct - 1.5%
        sl_price = smart_round(max(sl_price, entry * 0.985))          # tối đa 1.5%
        sl_price = smart_round(min(sl_price, entry * (1 - _sl_min_pct)))  # tối thiểu dynamic

        tp1 = _tp1_long(entry, swing_highs_m15, ema9_m15, ema21_m15, atr_m15,
                        ob_data.get("resistance_walls") if ob_data else None,
                        sl_price_ref=sl_price)
        tp2 = _tp2(entry, tp1, fib_ext_long, "LONG")

    elif direction == "SHORT" or (direction == "WAIT" and h1_bias == "SHORT"):
        entry = price

        # SL: tìm resistance gần nhất phía trên
        sl_candidates = []
        for rp in _ob_resists:
            if price * 1.005 < rp < price * 1.015:
                sl_candidates.append(("OB wall", rp + atr_m15 * 0.1))
        if ema21_m15 > price * 1.002 and ema21_m15 < price * 1.015:
            sl_candidates.append(("EMA21 M15", ema21_m15 + atr_m15 * 0.2))
        if _recent_10_high > price * 1.002 and _recent_10_high < price * 1.015:
            sl_candidates.append(("Swing M15", _recent_10_high + atr_m15 * 0.1))

        if sl_candidates:
            sl_candidates.sort(key=lambda x: x[1])  # gần nhất trước
            sl_price = smart_round(sl_candidates[0][1])
        else:
            sl_price = smart_round(min(entry + atr_m15 * 1.2, entry * 1.012))

        sl_price = smart_round(min(sl_price, entry * 1.015))              # tối đa 1.5%
        sl_price = smart_round(max(sl_price, entry * (1 + _sl_min_pct)))  # tối thiểu dynamic

        tp1 = _tp1_short(entry, swing_lows_m15, ema9_m15, ema21_m15, atr_m15,
                         ob_data.get("support_walls") if ob_data else None,
                         sl_price_ref=sl_price)
        tp2 = _tp2(entry, tp1, fib_ext_short, "SHORT")

    else:
        entry = sl_price = tp1 = tp2 = price

    if direction == "LONG"  and tp2 <= tp1: tp2 = smart_round(tp1 + (tp1 - entry))
    if direction == "SHORT" and tp2 >= tp1: tp2 = smart_round(tp1 - (entry - tp1))

    sl_pct  = round(abs(entry - sl_price) / entry * 100, 2) if entry != sl_price else 0
    tp1_pct = round(abs(tp1 - entry) / entry * 100, 2)      if entry != tp1 else 0
    rr      = round(tp1_pct / sl_pct, 2)                    if sl_pct > 0 else 0

    # Scalp R:R minimum 1.2 (vs 1.5 cho swing)
    # FAM Trading scalp thường R:R ~1.3-1.5, block 1.5 = bỏ lỡ nhiều setup tốt
    if direction in ("LONG", "SHORT") and rr < 1.2:
        all_warnings.append(f"❌ R:R {rr} < 1.2 — signal yếu, chờ M15 pullback về EMA")
        direction  = "WAIT"
        confidence = "LOW"
    elif direction in ("LONG", "SHORT") and rr < 1.5:
        all_warnings.append(f"⚠️ R:R {rr} (1.2-1.5) — chấp nhận cho scalp, giữ SL chặt")

    # ────────────────────────────────────────
    # ENTRY CHECKLIST & VERDICT
    # ────────────────────────────────────────
    def build_checklist(direction, m5_status, rr, rsi_m15, funding, oi_change, btc_ctx, confidence,
                         taker_data, ls_data, ob_data_inner):
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

        # ── Taker Buy/Sell ──
        if taker_data:
            tr = taker_data["buy_ratio"]
            if direction == "LONG":
                if tr > 1.2:    checks.append({"ok": True,  "text": f"Lực mua mạnh (Taker {tr:.2f}x) — buyer đang aggressive"})
                elif tr < 0.8:  checks.append({"ok": False, "text": f"Lực bán mạnh (Taker {tr:.2f}x) — ngược chiều LONG"})
                else:           checks.append({"ok": None,  "text": f"Taker ratio {tr:.2f}x — cân bằng"})
            else:
                if tr < 0.8:    checks.append({"ok": True,  "text": f"Lực bán mạnh (Taker {tr:.2f}x) — seller đang aggressive"})
                elif tr > 1.2:  checks.append({"ok": False, "text": f"Lực mua mạnh (Taker {tr:.2f}x) — ngược chiều SHORT"})
                else:           checks.append({"ok": None,  "text": f"Taker ratio {tr:.2f}x — cân bằng"})

        # ── Long/Short Ratio ──
        if ls_data:
            if direction == "LONG" and ls_data["extreme"] == "LONG_CROWDED":
                checks.append({"ok": False, "text": f"Long crowded ({ls_data['long_pct']:.0f}%) — rủi ro squeeze"})
            elif direction == "SHORT" and ls_data["extreme"] == "SHORT_CROWDED":
                checks.append({"ok": False, "text": f"Short crowded ({ls_data['short_pct']:.0f}%) — rủi ro squeeze"})
            elif direction == "LONG" and ls_data["extreme"] == "SHORT_CROWDED":
                checks.append({"ok": True,  "text": f"Short crowded ({ls_data['short_pct']:.0f}%) — LONG có lợi thế"})
            elif direction == "SHORT" and ls_data["extreme"] == "LONG_CROWDED":
                checks.append({"ok": True,  "text": f"Long crowded ({ls_data['long_pct']:.0f}%) — SHORT có lợi thế"})
            else:
                checks.append({"ok": None,  "text": f"L/S ratio: {ls_data['long_pct']:.0f}%/{ls_data['short_pct']:.0f}% — cân bằng"})

        # ── Order Book ──
        if ob_data_inner:
            imb = ob_data_inner["imbalance"]
            if direction == "LONG" and imb > 1.3:
                checks.append({"ok": True,  "text": f"Sổ lệnh thiên mua ({imb:.1f}x) — hỗ trợ LONG"})
            elif direction == "SHORT" and imb < 0.77:
                checks.append({"ok": True,  "text": f"Sổ lệnh thiên bán ({imb:.1f}x) — hỗ trợ SHORT"})
            elif direction == "LONG" and imb < 0.77:
                checks.append({"ok": False, "text": f"Sổ lệnh thiên bán ({imb:.1f}x) — bất lợi cho LONG"})
            elif direction == "SHORT" and imb > 1.3:
                checks.append({"ok": False, "text": f"Sổ lệnh thiên mua ({imb:.1f}x) — bất lợi cho SHORT"})
            else:
                checks.append({"ok": None,  "text": f"Order book cân bằng ({imb:.1f}x)"})

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

        # M5 chưa confirm: HIGH → giữ GO (nhưng thêm note), MEDIUM → WAIT
        # Fix contradiction: HIGH confidence + WAIT verdict = mâu thuẫn
        if m5_status in ("FORMING", "OVERBOUGHT", "OVERSOLD") and verdict == "GO":
            if confidence == "HIGH" and ok_c >= 5 and fail_c == 0:
                # HIGH + nhiều OK + 0 fail → giữ GO, M5 chỉ là confirmation phụ
                checks.append({"ok": None, "text": "M5 chưa confirm nhưng signals đủ mạnh — GO với SL chặt"})
            else:
                verdict = "WAIT"

        # ── Taker ngược chiều mạnh → hạ verdict ──
        # Scalp: taker là chỉ số real-time quan trọng nhất
        # Nếu taker > 1.5x ngược chiều → không nên GO dù các điều kiện khác đủ
        if taker_data and verdict == "GO":
            tr = taker_data["buy_ratio"]
            if direction == "LONG" and tr < 0.67:
                verdict = "WAIT"
                checks.append({"ok": False, "text": f"⚠️ Taker {tr:.2f}x bán rất mạnh — chờ lực bán giảm"})
            elif direction == "SHORT" and tr > 1.5:
                verdict = "WAIT"
                checks.append({"ok": False, "text": f"⚠️ Taker {tr:.2f}x mua rất mạnh — chờ lực mua giảm"})

        return checks, verdict

    entry_checklist, entry_verdict = build_checklist(
        direction, m5_status, rr, rsi_m15, funding, oi_change, btc_ctx, confidence,
        taker, ls_ratio, ob_data
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
        "scalp_data": {
            "taker":    taker,
            "ls_ratio": ls_ratio,
            "ob": {
                "imbalance":        ob_data["imbalance"] if ob_data else None,
                "spread_pct":       ob_data["spread_pct"] if ob_data else None,
                "support_walls":    ob_data["support_walls"] if ob_data else [],
                "resistance_walls": ob_data["resistance_walls"] if ob_data else [],
            } if ob_data else None,
        },
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
        "timestamp":  datetime.now(_TZ_VN).isoformat(),
        "h1_status":       m5_status,
        "h1_status_note":  m5_note,
        "entry_checklist": entry_checklist,
        "entry_verdict":   entry_verdict,
        "d1_bias":  h1_bias,
        "h4_bias":  h1_bias,
    })
