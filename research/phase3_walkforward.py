"""
phase3_walkforward.py — Validate H2 vol-gated trên 3 năm, walk-forward theo CALENDAR.

Mục tiêu: edge phải sống sót qua NHIỀU chu kỳ (mỗi nửa năm = 1 period).
Đây mới là validate thật — khác 6 tháng (mỗi regime 1 lần).

Đọc từ klines_long/ + funding_long/.
"""
import os, json
import numpy as np
import pandas as pd

DATA_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KLINES_LONG  = os.path.join(DATA_DIR, "klines_long")
FUNDING_LONG = os.path.join(DATA_DIR, "funding_long")

from backtester import add_indicators, simulate_trade


def load_long(symbol, tf="1h"):
    p = os.path.join(KLINES_LONG, f"{symbol}_{tf}.parquet")
    if not os.path.exists(p): return None
    return add_indicators(pd.read_parquet(p))


def load_funding_long(symbol):
    p = os.path.join(FUNDING_LONG, f"{symbol}.parquet")
    if not os.path.exists(p): return None
    return pd.read_parquet(p)


# Precompute BTC regime + vol + 7d trên long data
_CACHE = {}
def btc_series():
    if "btc" in _CACHE: return _CACHE["btc"]
    df = load_long("BTCUSDT", "1h")
    ema_slope = df["ema50"].pct_change(24) * 100
    rvol = df["close"].pct_change().rolling(24).std() * 100
    rvol_med = rvol.rolling(168, min_periods=24).median()
    regime = pd.Series("RANGING", index=df.index)
    regime[ema_slope > 1.5] = "TRENDING_UP"
    regime[ema_slope < -1.5] = "TRENDING_DOWN"
    regime[(rvol > rvol_med*1.8) & (ema_slope.abs() <= 1.5)] = "CHOP"
    dvol = df["close"].pct_change().rolling(168, min_periods=48).std() * np.sqrt(24) * 100
    _CACHE["btc"] = (regime, dvol)
    return _CACHE["btc"]


def asof(series, ts):
    sub = series[series.index <= ts]
    if sub.empty: return None
    v = sub.iloc[-1]
    if pd.isna(v): return None
    return v   # giữ raw (string cho regime, float cho dvol)


def replay_long(symbol, tier, vol_max=2.0, cooldown=24, warmup=200):
    df = load_long(symbol, "1h")
    if df is None or len(df) < warmup + 50: return []
    fdf = load_funding_long(symbol)
    regime_s, dvol_s = btc_series()
    trades, last = [], -10**9
    for i in range(warmup, len(df)-1):
        if i - last < cooldown: continue
        ts = df.index[i]
        if tier != "midcap": continue
        if asof(regime_s, ts) != "RANGING": continue
        dv = asof(dvol_s, ts)
        if dv is None or float(dv) >= vol_max: continue
        row = df.iloc[i]
        if row["ema50"] <= row["ema200"]: continue
        if row["rsi"] < 28 and row["dist_vwap_atr"] <= -2.5:
            entry = float(row["close"]); sl = entry - row["atr"]*1.5
            tp = float(row["vwap24"]) + row["atr"]*0.5
            if tp <= entry: continue
            res = simulate_trade(df, i+1, "LONG", sl, tp)
            if not res: continue
            r, pnl, bars, _ = res
            trades.append({"symbol": symbol, "time": str(ts), "result": r,
                           "pnl_r": round(pnl,3), "half": str(ts)[:7]})
            last = i
    return trades


def main():
    universe = json.load(open(os.path.join(DATA_DIR, "universe.json")))["coins"]
    midcaps = [u for u in universe if u["tier"] == "midcap"]
    all_trades = []
    for u in midcaps:
        all_trades.extend(replay_long(u["symbol"], "midcap", vol_max=2.0))
    df = pd.DataFrame(all_trades)
    if df.empty:
        print("0 trades"); return
    df["dt"] = pd.to_datetime(df["time"])
    df["period"] = df["dt"].dt.to_period("Q").astype(str)   # theo quý

    def st(d):
        n=len(d);
        if n==0: return None
        w=(d["pnl_r"]>0).sum()
        return dict(n=n, wr=round(w/n*100,1), totalR=round(d["pnl_r"].sum(),2),
                    exp=round(d["pnl_r"].mean(),3))

    print(f"\n=== H2 vol-gated trên {df['dt'].min().date()} → {df['dt'].max().date()} ===")
    print(f"TỔNG: {st(df)}")
    print(f"\nPer quarter (walk-forward đa chu kỳ):")
    print(f"{'Quý':9s} {'N':>4s} {'WR':>5s} {'totR':>7s} {'exp':>7s}")
    periods = sorted(df["period"].unique())
    pos = 0
    for p in periods:
        s = st(df[df["period"]==p])
        if s:
            flag = "✅" if s["exp"]>0 else "🔴"
            if s["exp"]>0: pos += 1
            print(f"{p:9s} {s['n']:>4d} {s['wr']:>5.1f} {s['totalR']:>7.2f} {s['exp']:>7.3f} {flag}")
    print(f"\nQuý dương: {pos}/{len(periods)} ({pos/len(periods)*100:.0f}%)")
    print("→ Edge robust nếu ≥70% quý dương. Dưới đó = chưa đủ tin.")
    df.to_parquet(os.path.join(DATA_DIR, "trades_longwf.parquet"))


if __name__ == "__main__":
    main()
