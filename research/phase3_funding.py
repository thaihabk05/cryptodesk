"""
phase3_funding.py — Test FUNDING EXTREME mean reversion trên 3 năm.

Cơ chế (bất hiệu quả cấu trúc thật, không phải pattern ngẫu nhiên):
- Funding ≥ +X% → long trả phí nặng → bị ép cắt → giá revert xuống → SHORT
- Funding ≤ -Y% → short trả phí → squeeze → giá revert lên → LONG

Đây là edge document nhiều nhất crypto perp. Test nhiều ngưỡng + walk-forward quý.
"""
import os, json
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
from phase3_walkforward import load_long, load_funding_long, asof
from backtester import simulate_trade


def replay_funding(symbol, tier, fund_hi, fund_lo, sl_atr=2.0, tp_atr=2.0,
                   cooldown=24, warmup=200):
    """Funding extreme reversion. SL/TP theo ATR (không dùng VWAP vì có thể trending)."""
    df = load_long(symbol, "1h")
    if df is None or len(df) < warmup + 50:
        return []
    fdf = load_funding_long(symbol)
    if fdf is None or fdf.empty:
        return []
    trades, last = [], -10**9
    for i in range(warmup, len(df) - 1):
        if i - last < cooldown:
            continue
        ts = df.index[i]
        f = asof(fdf["fundingRate"], ts)
        if f is None:
            continue
        f = float(f)
        row = df.iloc[i]
        atr = row["atr"]
        direction = None
        if f >= fund_hi:
            direction = "SHORT"
        elif f <= fund_lo:
            direction = "LONG"
        if not direction:
            continue
        entry = float(row["close"])
        if direction == "SHORT":
            sl = entry + atr * sl_atr; tp = entry - atr * tp_atr
        else:
            sl = entry - atr * sl_atr; tp = entry + atr * tp_atr
        res = simulate_trade(df, i + 1, direction, sl, tp)
        if not res:
            continue
        r, pnl, bars, _ = res
        trades.append({"symbol": symbol, "tier": tier, "time": str(ts),
                       "direction": direction, "funding": round(f, 4),
                       "result": r, "pnl_r": round(pnl, 3)})
        last = i
    return trades


def st(d):
    n = len(d)
    if n == 0: return None
    w = (d["pnl_r"] > 0).sum()
    return dict(n=n, wr=round(w/n*100,1), totalR=round(d["pnl_r"].sum(),2),
               exp=round(d["pnl_r"].mean(),3))


def evaluate(trades, label):
    if trades.empty:
        print(f"[{label}] 0 trades"); return None
    trades["dt"] = pd.to_datetime(trades["time"])
    trades["q"] = trades["dt"].dt.to_period("Q").astype(str)
    overall = st(trades)
    qs = sorted(trades["q"].unique())
    pos = sum(1 for q in qs if (st(trades[trades["q"]==q]) or {}).get("exp",0) > 0)
    # Per direction
    sh = st(trades[trades["direction"]=="SHORT"])
    lo = st(trades[trades["direction"]=="LONG"])
    print(f"\n[{label}] TỔNG {overall} | Quý dương {pos}/{len(qs)} ({pos/len(qs)*100:.0f}%)")
    print(f"  SHORT: {sh}")
    print(f"  LONG : {lo}")
    return dict(overall=overall, q_pos_pct=pos/len(qs)*100, trades=trades)


def main():
    universe = json.load(open(os.path.join(DATA_DIR, "universe.json")))["coins"]
    configs = [
        ("fund ±0.10/-0.05", 0.10, -0.05),
        ("fund ±0.15/-0.10", 0.15, -0.10),
        ("fund ±0.05/-0.05", 0.05, -0.05),
        ("fund SHORT-only ≥0.10", 0.10, -99),   # chỉ short funding cao
        ("fund SHORT-only ≥0.15", 0.15, -99),
    ]
    best = None
    for label, hi, lo in configs:
        all_t = []
        for u in universe:
            all_t.extend(replay_funding(u["symbol"], u["tier"], hi, lo))
        res = evaluate(pd.DataFrame(all_t), label)
        if res and res["q_pos_pct"] >= 60 and (best is None or res["q_pos_pct"] > best[1]):
            best = (label, res["q_pos_pct"], res["trades"])
    if best:
        best[2].to_parquet(os.path.join(DATA_DIR, "trades_funding_robust.parquet"))
        print(f"\n✅ Robust nhất: {best[0]} ({best[1]:.0f}% quý dương)")
    else:
        print("\n⚠️ Funding extreme cũng chưa đạt ≥60% quý dương.")


if __name__ == "__main__":
    main()
