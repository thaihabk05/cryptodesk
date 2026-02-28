"""indicators.py — MA, ATR, Swing, Fibonacci — dùng chung"""
import numpy as np
import pandas as pd


def add_ma(df: pd.DataFrame) -> pd.DataFrame:
    for p in [34, 89, 200]:
        df[f"ma{p}"] = df["close"].rolling(p, min_periods=max(1, p // 2)).mean()
    return df

def add_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    df["vol_sma"]   = df["volume"].rolling(period, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_sma"].replace(0, np.nan)
    return df

def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    df["atr"] = df["high"].sub(df["low"]).rolling(period, min_periods=1).mean()
    return df

def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = add_ma(df)
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
