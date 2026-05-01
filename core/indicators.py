"""indicators.py — MA, ATR, Swing, Fibonacci — dùng chung"""
import numpy as np
import pandas as pd


def add_ma(df: pd.DataFrame) -> pd.DataFrame:
    for p in [34, 89, 200]:
        df[f"ma{p}"] = df["close"].rolling(p, min_periods=max(1, p // 2)).mean()
    # EMA nhanh cho scalp
    for p in [9, 21]:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df

def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(period, min_periods=1).mean()
    loss  = (-delta.clip(upper=0)).rolling(period, min_periods=1).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)
    return df

def add_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    df["vol_sma"]   = df["volume"].rolling(period, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_sma"].replace(0, np.nan)
    return df

def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    df["atr"] = df["high"].sub(df["low"]).rolling(period, min_periods=1).mean()
    return df


def detect_exhaustion_short(df_h1, atr_mult: float = 1.5,
                             vol_mult: float = 1.5, retrace_min: float = 0.5) -> tuple:
    """
    Phát hiện "Exhaustion candle" — nến H1 đỏ body lớn + volume cao + close gần đáy.
    Verify backtest 2026-04-30 với threshold (1.5, 1.5, 0.5):
      - SHORT-WIN hit: 5/25 (20%)  — CRCL +3.57R, OPG +2.16R, BASED +2.78R, ROBO +1.50R, MON +1.68R
      - SHORT-LOSS:    1/10 (10%)
      - LONG-LOSS:     1/30 (3%)  spurious
      - Precision:     71%

    Returns: (triggered: bool, note: str). Nến đánh giá là nến cuối df_h1 (đã close).
    """
    if df_h1 is None or len(df_h1) < 25:
        return False, ""
    last = df_h1.iloc[-1]
    prev = df_h1.iloc[-25:-1]

    last_open  = float(last["open"])
    last_close = float(last["close"])
    last_high  = float(last["high"])
    last_low   = float(last["low"])
    last_vol   = float(last.get("volume", 0))

    # 1) Nến đỏ
    if last_close >= last_open:
        return False, ""

    # 2) Body ≥ atr_mult × ATR(14)
    if "atr" in df_h1.columns:
        atr = float(df_h1["atr"].iloc[-15:-1].mean())
    else:
        prev_closes = prev["close"].tolist()
        trs = []
        for i in range(1, len(prev)):
            h = float(prev["high"].iloc[i]); l = float(prev["low"].iloc[i])
            pc = prev_closes[i-1]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs[-14:]) / max(1, min(14, len(trs)))
    if atr <= 0:
        return False, ""
    body = abs(last_close - last_open)
    if body < atr * atr_mult:
        return False, ""

    # 3) Volume ≥ vol_mult × avg 20 nến trước
    vol_avg = float(prev["volume"].tail(20).mean()) if "volume" in prev.columns else 0
    if vol_avg <= 0 or last_vol < vol_avg * vol_mult:
        return False, ""

    # 4) Close gần đáy: (high - close) / (high - low) ≥ retrace_min
    rng = last_high - last_low
    if rng <= 0:
        return False, ""
    retrace = (last_high - last_close) / rng
    if retrace < retrace_min:
        return False, ""

    note = (f"Exhaustion H1: body {body/atr:.1f}×ATR, vol {last_vol/vol_avg:.1f}×, "
            f"close {retrace*100:.0f}% từ đáy nến — buyer cạn lực")
    return True, note

def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = add_ma(df)
    df = add_rsi(df)
    df = add_volume_sma(df)
    df = add_atr(df)
    return df.dropna(subset=["ma34"])


def ma_slope(series: pd.Series, n: int = 5) -> str:
    if len(series) < n: return "FLAT"
    chg = (series.iloc[-1] - series.iloc[-n]) / series.iloc[-n] * 100
    if chg > 0.15:  return "UP"
    if chg < -0.15: return "DOWN"
    return "FLAT"


def find_swing_points(df: pd.DataFrame, lookback: int = 5):
    highs, lows = [], []
    for i in range(lookback, len(df) - lookback):
        window_h = df["high"].iloc[i - lookback: i + lookback + 1]
        window_l = df["low"].iloc[i - lookback: i + lookback + 1]
        if df["high"].iloc[i] == window_h.max():
            highs.append((df.index[i], float(df["high"].iloc[i])))
        if df["low"].iloc[i] == window_l.min():
            lows.append((df.index[i], float(df["low"].iloc[i])))
    return highs, lows


def classify_structure(highs: list, lows: list, n: int = 3) -> str:
    h_vals = [v for _, v in highs[-n:]]
    l_vals = [v for _, v in lows[-n:]]
    if len(h_vals) < 2 or len(l_vals) < 2: return "SIDEWAYS"
    hh = all(h_vals[i] > h_vals[i-1] for i in range(1, len(h_vals)))
    hl = all(l_vals[i] > l_vals[i-1] for i in range(1, len(l_vals)))
    lh = all(h_vals[i] < h_vals[i-1] for i in range(1, len(h_vals)))
    ll = all(l_vals[i] < l_vals[i-1] for i in range(1, len(l_vals)))
    if hh and hl: return "UPTREND"
    if lh and ll: return "DOWNTREND"
    return "SIDEWAYS"


def fib_retracement(swing_high: float, swing_low: float) -> dict:
    rng = swing_high - swing_low
    return {
        "0.236": round(swing_high - rng * 0.236, 6),
        "0.382": round(swing_high - rng * 0.382, 6),
        "0.500": round(swing_high - rng * 0.500, 6),
        "0.618": round(swing_high - rng * 0.618, 6),
        "0.786": round(swing_high - rng * 0.786, 6),
    }


def fib_extension(swing_low: float, swing_high: float, retracement_low: float) -> dict:
    rng = swing_high - swing_low
    base = retracement_low
    return {
        "1.272": round(base + rng * 1.272, 6),
        "1.618": round(base + rng * 1.618, 6),
        "2.000": round(base + rng * 2.000, 6),
    }


def is_no_trade_zone(price: float, row_h4) -> tuple:
    lo = min(float(row_h4["ma34"]), float(row_h4["ma89"]))
    hi = max(float(row_h4["ma34"]), float(row_h4["ma89"]))
    gap_pct = (hi - lo) / lo * 100 if lo else 0
    in_zone = lo * 0.998 <= price <= hi * 1.002
    if not in_zone: return False, ""
    if gap_pct < 0.5: return False, ""
    return True, f"Giá trong vùng MA34/89 (gap {gap_pct:.1f}%) — chờ thoát rõ hướng"


def weekly_macro_bias(df_w: pd.DataFrame) -> dict:
    """
    Phân tích Weekly macro bias — detect Death Cross MA34/MA89 và MA200 position.
    Nguồn: phương pháp FAM Trading.

    Returns dict:
      trend:       "BULL" | "BEAR" | "NEUTRAL"
      death_cross: True nếu MA34 vừa cắt xuống MA89 (hoặc sắp cắt)
      golden_cross: True nếu MA34 vừa cắt lên MA89
      below_ma200: True nếu giá dưới MA200 Weekly
      ma34_slope:  "UP" | "DOWN" | "FLAT"
      ma89_slope:  "UP" | "DOWN" | "FLAT"
      notes:       list cảnh báo
    """
    if len(df_w) < 10:
        return {"trend": "NEUTRAL", "death_cross": False, "golden_cross": False,
                "below_ma200": False, "ma34_slope": "FLAT", "ma89_slope": "FLAT",
                "notes": [], "score_adj": 0}

    row   = df_w.iloc[-1]
    prev  = df_w.iloc[-2]
    price = float(row["close"])

    ma34  = float(row["ma34"])
    ma89  = float(row["ma89"])
    ma200_val = float(row["ma200"]) if "ma200" in df_w.columns and not pd.isna(row["ma200"]) else None

    prev_ma34 = float(prev["ma34"])
    prev_ma89 = float(prev["ma89"])

    slope_34 = ma_slope(df_w["ma34"], n=3)
    slope_89 = ma_slope(df_w["ma89"], n=3)

    # Death cross: MA34 cắt xuống dưới MA89
    death_cross  = prev_ma34 >= prev_ma89 and ma34 < ma89
    golden_cross = prev_ma34 <= prev_ma89 and ma34 > ma89

    # Sắp death cross: MA34 đang trên MA89 nhưng gap < 1% và MA34 slope DOWN
    near_death_cross = (ma34 > ma89 and (ma34 - ma89) / ma89 * 100 < 1.0
                        and slope_34 == "DOWN")

    # Giá dưới MA200 Weekly
    below_ma200 = ma200_val is not None and price < ma200_val

    # Trend
    if price > ma34 and price > ma89:
        trend = "BULL"
    elif price < ma34 and price < ma89:
        trend = "BEAR"
    else:
        trend = "NEUTRAL"

    notes = []
    score_adj = 0

    if death_cross:
        notes.append("🚨 WEEKLY DEATH CROSS — MA34 cắt xuống MA89: xu hướng giảm dài hạn xác nhận")
        score_adj -= 2
    elif near_death_cross:
        notes.append("⚠️ WEEKLY CẢNH BÁO — MA34/MA89 sắp death cross (gap < 1%)")
        score_adj -= 1
    elif golden_cross:
        notes.append("✅ WEEKLY GOLDEN CROSS — MA34 cắt lên MA89: xu hướng tăng dài hạn")
        score_adj += 1

    if below_ma200:
        notes.append(f"⚠️ WEEKLY dưới MA200 ({ma200_val:.2f}) — macro bearish")
        score_adj -= 1

    if trend == "BEAR" and slope_34 == "DOWN" and slope_89 == "DOWN":
        notes.append("⚠️ WEEKLY cả MA34 và MA89 đang slope DOWN — áp lực bán mạnh")

    return {
        "trend":        trend,
        "death_cross":  death_cross,
        "golden_cross": golden_cross,
        "near_death":   near_death_cross,
        "below_ma200":  below_ma200,
        "ma34_slope":   slope_34,
        "ma89_slope":   slope_89,
        "ma34":         round(ma34, 6),
        "ma89":         round(ma89, 6),
        "ma200":        round(ma200_val, 6) if ma200_val else None,
        "notes":        notes,
        "score_adj":    score_adj,
    }


def calc_atr_context(df_h4: pd.DataFrame, df_d1: pd.DataFrame) -> dict:
    atr_h4    = float(df_h4["atr"].iloc[-1])
    atr_avg   = float(df_h4["atr"].iloc[-60:].mean()) if len(df_h4) >= 60 else atr_h4
    atr_ratio = round(atr_h4 / atr_avg, 2) if atr_avg else 1.0
    if atr_ratio < 0.6:
        state, note = "COMPRESS", "ATR thấp — thị trường đang nén, tín hiệu yếu"
        score_adj   = -1
    elif atr_ratio > 1.8:
        state, note = "EXPAND",   "ATR cao — volatility lớn, SL dễ bị quét"
        score_adj   = -1
    else:
        state, note = "NORMAL",   ""
        score_adj   = 0
    return {"atr_h4": round(atr_h4, 6), "atr_ratio": atr_ratio,
            "atr_state": state, "atr_note": note, "score_adj": score_adj}
