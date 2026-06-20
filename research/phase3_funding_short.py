"""
phase3_funding_short.py — Isolate edge: SHORT khi funding dương.

Phát hiện phase3: funding≥0.05 SHORT n=619 WR55% exp+0.11 (+68R), robust qua
nhiều ngưỡng. LONG funding-âm luôn lỗ → bỏ. Giờ tinh chỉnh ngưỡng + exit +
check quarterly walk-forward + context filter (regime/vol có cải thiện?).
"""
import os, json
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
from phase3_walkforward import load_long, load_funding_long, asof, btc_series
from backtester import simulate_trade


def replay(symbol, fund_hi, sl_atr, tp_atr, cooldown=24, warmup=200,
           vol_max=None, skip_uptrend=False):
    df = load_long(symbol, "1h")
    if df is None or len(df) < warmup + 50: return []
    fdf = load_funding_long(symbol)
    if fdf is None or fdf.empty: return []
    regime_s, dvol_s = btc_series() if (vol_max or skip_uptrend) else (None, None)
    trades, last = [], -10**9
    for i in range(warmup, len(df)-1):
        if i - last < cooldown: continue
        ts = df.index[i]
        f = asof(fdf["fundingRate"], ts)
        if f is None or float(f) < fund_hi: continue
        row = df.iloc[i]
        if vol_max is not None:
            dv = asof(dvol_s, ts)
            if dv is None or float(dv) >= vol_max: continue
        if skip_uptrend and asof(regime_s, ts) == "TRENDING_UP":
            continue  # không short khi BTC trending up mạnh
        entry = float(row["close"]); atr = row["atr"]
        sl = entry + atr*sl_atr; tp = entry - atr*tp_atr
        res = simulate_trade(df, i+1, "SHORT", sl, tp)
        if not res: continue
        r, pnl, bars, _ = res
        trades.append({"symbol": symbol, "time": str(ts), "funding": round(float(f),4),
                       "result": r, "pnl_r": round(pnl,3)})
        last = i
    return trades


def st(d):
    n=len(d)
    if n==0: return None
    w=(d["pnl_r"]>0).sum()
    return dict(n=n, wr=round(w/n*100,1), totalR=round(d["pnl_r"].sum(),2), exp=round(d["pnl_r"].mean(),3))


def evaluate(trades, label):
    if trades.empty: print(f"[{label}] 0 trades"); return None
    trades["q"] = pd.to_datetime(trades["time"]).dt.to_period("Q").astype(str)
    qs = sorted(trades["q"].unique())
    qexps = [(q, (st(trades[trades["q"]==q]) or {}).get("exp",0)) for q in qs]
    pos = sum(1 for _,e in qexps if e>0)
    ov = st(trades)
    print(f"[{label:30s}] {ov} | quý+ {pos}/{len(qs)} ({pos/len(qs)*100:.0f}%)")
    return dict(ov=ov, qpos=pos/len(qs)*100, trades=trades, qexps=qexps)


def main():
    universe = json.load(open(os.path.join(DATA_DIR, "universe.json")))["coins"]
    syms = [u["symbol"] for u in universe]

    configs = [
        # (label, fund_hi, sl_atr, tp_atr, vol_max, skip_uptrend)
        ("base ≥0.05 sl2 tp2",       0.05, 2.0, 2.0, None, False),
        ("≥0.05 sl2 tp1.5",          0.05, 2.0, 1.5, None, False),
        ("≥0.05 sl1.5 tp1.5",        0.05, 1.5, 1.5, None, False),
        ("≥0.05 sl3 tp2",            0.05, 3.0, 2.0, None, False),
        ("≥0.05 +skip_uptrend",      0.05, 2.0, 2.0, None, True),
        ("≥0.05 +volgate2.5",        0.05, 2.0, 2.0, 2.5,  False),
        ("≥0.07 sl2 tp2",            0.07, 2.0, 2.0, None, False),
        ("≥0.05 skipUp+vol2.5",      0.05, 2.0, 2.0, 2.5,  True),
    ]
    results = []
    for label, hi, sl, tp, vmax, skip in configs:
        all_t = []
        for s in syms:
            all_t.extend(replay(s, hi, sl, tp, vol_max=vmax, skip_uptrend=skip))
        r = evaluate(pd.DataFrame(all_t), label)
        if r: results.append((label, r))

    # Best theo qpos rồi exp
    results.sort(key=lambda x: (x[1]["qpos"], x[1]["ov"]["exp"]), reverse=True)
    best_label, best = results[0]
    print(f"\n=== BEST: {best_label} — quý+ {best['qpos']:.0f}% ===")
    print("Quarterly:")
    for q, e in best["qexps"]:
        print(f"  {q}: {e:+.3f} {'✅' if e>0 else '🔴'}")
    best["trades"].to_parquet(os.path.join(DATA_DIR, "trades_funding_short.parquet"))
    print(f"\n{'✅ ROBUST (≥70% quý+)' if best['qpos']>=70 else '⚠️ chưa đạt 70% — nhưng xem mức độ'}")


if __name__ == "__main__":
    main()
