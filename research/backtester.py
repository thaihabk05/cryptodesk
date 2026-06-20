"""
backtester.py — Replay engine cho rebuild. Test hypothesis với HÀNG NGHÌN lệnh.

Khác biệt cốt lõi vs backtester cũ:
- Replay từng nến H1 trên data thô (không backtest signal đã lưu).
- KHÔNG lookahead: tại nến t chỉ dùng data ≤ t. Entry ở open nến t+1.
- Funding point-in-time (as-of): chỉ lấy funding ≤ thời điểm nến.
- Walk-forward: tách in-sample / out-of-sample.
- Stats per tier + per regime.

Strategy = 1 hàm pluggable: (ctx) -> signal dict | None
  ctx có: row hiện tại, các indicator precomputed, funding as-of, regime, df slice.
  signal: {"direction": "LONG"|"SHORT", "sl": float, "tp": float, "tag": str}

Chạy: python3 research/backtester.py        (chạy demo H1/H2 trên universe)
"""
import os, json, glob
import numpy as np
import pandas as pd

DATA_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KLINES_DIR  = os.path.join(DATA_DIR, "klines")
FUNDING_DIR = os.path.join(DATA_DIR, "funding")

# ── Indicators (vectorized, no lookahead) ────────────────────────────────
def add_indicators(df):
    df = df.copy()
    for p in [9, 20, 34, 50, 89, 200]:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    # ATR
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"]  = tr.rolling(14, min_periods=1).mean()
    df["atr_pct"] = df["atr"] / df["close"] * 100
    # RSI
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss  = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
    # Rolling VWAP (anchored 24 nến H1) — proxy cho mean
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    df["vwap24"] = pv.rolling(24, min_periods=1).sum() / df["volume"].rolling(24, min_periods=1).sum()
    # Khoảng cách giá so VWAP theo ATR
    df["dist_vwap_atr"] = (df["close"] - df["vwap24"]) / df["atr"]
    # Prior day high/low (shift để no lookahead) — dùng D1 sẽ chính xác hơn, đây là proxy 24h
    df["hi_24"] = df["high"].rolling(24, min_periods=1).max().shift(1)
    df["lo_24"] = df["low"].rolling(24, min_periods=1).min().shift(1)
    return df


def load_coin(symbol, tf="1h"):
    path = os.path.join(KLINES_DIR, f"{symbol}_{tf}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    return add_indicators(df)


def load_funding(symbol):
    path = os.path.join(FUNDING_DIR, f"{symbol}.parquet")
    if not os.path.exists(path):
        return None
    return pd.read_parquet(path)


def funding_asof(fdf, ts):
    """Funding gần nhất ≤ ts (point-in-time, no lookahead)."""
    if fdf is None or fdf.empty:
        return None
    sub = fdf[fdf.index <= ts]
    if sub.empty:
        return None
    return float(sub["fundingRate"].iloc[-1])


# ── BTC regime (precompute 1 lần) ────────────────────────────────────────
def compute_btc_regime(tf="1h"):
    """Trả về Series regime theo timestamp: TRENDING_UP/DOWN, RANGING, CHOP."""
    df = load_coin("BTCUSDT", tf)
    if df is None:
        return None
    # ADX đơn giản hoá: dùng slope EMA50 + realized vol
    ema_slope = df["ema50"].pct_change(24) * 100      # % thay đổi EMA50 trong 24h
    rvol = df["close"].pct_change().rolling(24).std() * 100
    rvol_med = rvol.rolling(168, min_periods=24).median()

    regime = pd.Series("RANGING", index=df.index)
    regime[ema_slope > 1.5]  = "TRENDING_UP"
    regime[ema_slope < -1.5] = "TRENDING_DOWN"
    # High vol chop override
    regime[(rvol > rvol_med * 1.8) & (ema_slope.abs() <= 1.5)] = "CHOP"
    return regime


# ── Trade simulation (forward fill SL/TP, no lookahead) ──────────────────
def simulate_trade(df, entry_idx, direction, sl, tp, max_bars=72):
    """Entry ở open nến entry_idx. Quét forward tìm SL/TP chạm trước.
    Return (result, pnl_r, bars_held, exit_price)."""
    entry_price = float(df["open"].iloc[entry_idx])
    risk = abs(entry_price - sl)
    if risk <= 0:
        return None
    end = min(entry_idx + max_bars, len(df))
    for j in range(entry_idx, end):
        hi = float(df["high"].iloc[j]); lo = float(df["low"].iloc[j])
        if direction == "LONG":
            # Pessimistic: nếu cùng nến chạm cả SL+TP → tính SL trước
            if lo <= sl:
                return ("LOSS", -1.0, j - entry_idx, sl)
            if hi >= tp:
                return ("WIN", (tp - entry_price) / risk, j - entry_idx, tp)
        else:
            if hi >= sl:
                return ("LOSS", -1.0, j - entry_idx, sl)
            if lo <= tp:
                return ("WIN", (entry_price - tp) / risk, j - entry_idx, tp)
    # Timeout — close at last close
    last = float(df["close"].iloc[end - 1])
    pnl_r = ((last - entry_price) if direction == "LONG" else (entry_price - last)) / risk
    return ("TIMEOUT", pnl_r, end - 1 - entry_idx, last)


# ── Replay engine ────────────────────────────────────────────────────────
def replay(symbol, tier, strategy_fn, btc_regime, warmup=200, cooldown_bars=24):
    """Replay 1 coin qua strategy. Return list trades."""
    df = load_coin(symbol, "1h")
    if df is None or len(df) < warmup + 50:
        return []
    fdf = load_funding(symbol)
    trades = []
    last_entry_idx = -10**9

    for i in range(warmup, len(df) - 1):
        if i - last_entry_idx < cooldown_bars:
            continue
        ts = df.index[i]
        regime = btc_regime.get(ts, "RANGING") if btc_regime is not None else "RANGING"
        ctx = {
            "df": df, "i": i, "row": df.iloc[i],
            "funding": funding_asof(fdf, ts),
            "regime": regime, "tier": tier, "symbol": symbol,
        }
        sig = strategy_fn(ctx)
        if not sig:
            continue
        # Entry ở nến kế (open i+1) — realistic, no lookahead
        res = simulate_trade(df, i + 1, sig["direction"], sig["sl"], sig["tp"])
        if not res:
            continue
        result, pnl_r, bars, exit_px = res
        trades.append({
            "symbol": symbol, "tier": tier, "time": str(ts),
            "direction": sig["direction"], "tag": sig.get("tag", ""),
            "regime": regime, "funding": ctx["funding"],
            "result": result, "pnl_r": round(pnl_r, 3), "bars": bars,
            "entry": float(df["open"].iloc[i+1]), "sl": sig["sl"], "tp": sig["tp"],
            "dist_vwap_atr": round(float(df["dist_vwap_atr"].iloc[i]), 2),
            "rsi": round(float(df["rsi"].iloc[i]), 1),
            "atr_pct": round(float(df["atr_pct"].iloc[i]), 2),
        })
        last_entry_idx = i
    return trades


def run_strategy(strategy_fn, label="strategy"):
    """Chạy strategy trên toàn universe. Return DataFrame trades."""
    uni_path = os.path.join(DATA_DIR, "universe.json")
    if not os.path.exists(uni_path):
        raise FileNotFoundError("Chưa có universe.json — chạy data_pull.py trước")
    universe = json.load(open(uni_path))["coins"]
    btc_regime = compute_btc_regime("1h")

    all_trades = []
    for u in universe:
        all_trades.extend(replay(u["symbol"], u["tier"], strategy_fn, btc_regime))
    return pd.DataFrame(all_trades)


# ── Stats + walk-forward ─────────────────────────────────────────────────
def summarize(trades, label="strategy", split_date=None):
    if trades.empty:
        print(f"[{label}] 0 trades"); return
    def _stat(df):
        closed = df[df["result"].isin(["WIN", "LOSS", "TIMEOUT"])]
        n = len(closed)
        if n == 0: return None
        wins = (closed["pnl_r"] > 0).sum()
        wr = wins / n * 100
        totalR = closed["pnl_r"].sum()
        exp = closed["pnl_r"].mean()
        return dict(n=n, wr=round(wr,1), totalR=round(totalR,2), exp=round(exp,3))

    print(f"\n{'='*60}\n[{label}] Tổng: {_stat(trades)}")
    # Per tier
    print("Per tier:")
    for tier in ["major", "midcap"]:
        s = _stat(trades[trades["tier"] == tier])
        if s: print(f"  {tier:8s}: {s}")
    # Per regime
    print("Per regime:")
    for reg in trades["regime"].unique():
        s = _stat(trades[trades["regime"] == reg])
        if s: print(f"  {reg:14s}: {s}")
    # Walk-forward split
    if split_date:
        ins = trades[trades["time"] < split_date]
        oos = trades[trades["time"] >= split_date]
        print(f"Walk-forward (split {split_date}):")
        print(f"  IN-SAMPLE : {_stat(ins)}")
        print(f"  OUT-SAMPLE: {_stat(oos)}")


# ── Demo hypotheses ──────────────────────────────────────────────────────
def H1_funding_meanrev(ctx):
    """H1: funding cao + giá căng trên VWAP + tại 24h high → SHORT về VWAP."""
    row = ctx["row"]; f = ctx["funding"]
    if f is None: return None
    if f >= 0.08 and row["dist_vwap_atr"] >= 2.0 and row["close"] >= row["hi_24"]:
        entry = float(row["close"])
        sl = entry + row["atr"] * 1.5
        tp = float(row["vwap24"])
        if tp < entry:  # đảm bảo TP đúng phía
            return {"direction": "SHORT", "sl": sl, "tp": tp, "tag": "H1_fund_short"}
    return None


def H2_oversold_bounce(ctx):
    """H2: uptrend H4 + oversold + giá căng dưới VWAP → LONG bounce."""
    row = ctx["row"]; f = ctx["funding"]
    # Uptrend proxy: EMA50 > EMA200
    if row["ema50"] <= row["ema200"]: return None
    if row["rsi"] < 28 and row["dist_vwap_atr"] <= -2.0:
        entry = float(row["close"])
        sl = entry - row["atr"] * 1.5
        tp = float(row["vwap24"])
        if tp > entry:
            return {"direction": "LONG", "sl": sl, "tp": tp, "tag": "H2_oversold_long"}
    return None


if __name__ == "__main__":
    print("=== DEMO: chạy H1 + H2 trên universe ===")
    for fn, label in [(H1_funding_meanrev, "H1_funding_short"),
                      (H2_oversold_bounce, "H2_oversold_long")]:
        trades = run_strategy(fn, label)
        # Split ở 2/3 thời gian cho walk-forward
        if not trades.empty:
            times = sorted(trades["time"])
            split = times[int(len(times) * 0.66)]
            summarize(trades, label, split_date=split)
            trades.to_parquet(os.path.join(DATA_DIR, f"trades_{label}.parquet"))
