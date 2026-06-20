"""
phase4_strengthen.py — Thêm factor CƠ CHẾ vào funding-short để chống bull-squeeze.

Base: SHORT khi funding≥0.05, sl3/tp2 (WR 65%, +54R, 62% quý).
Failure mode: bull squeeze 2025Q3-2026Q1 (funding cao nhưng giá vẫn lên).

Factor thêm (mỗi cái chống squeeze, backtest được 3 năm):
- price_weak: close < EMA9 (giá đã quay đầu, không bắt dao bay)
- off_high:   giá ≥1.5% dưới 24h high (không short mù tại đỉnh)
- not_para:   |giá - EMA50|/ATR < 6 (không short vào melt-up parabol)

KỶ LUẬT: factor tốt = giữ quý tốt + giảm lỗ quý xấu. Factor fit = xóa lệnh bừa.
In riêng good-period (2023Q4-2024Q4) vs drawdown (2025Q3-2026Q1) để kiểm.
"""
import os, json
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
from phase3_walkforward import load_long, load_funding_long, asof
from backtester import simulate_trade

DRAWDOWN_Q = {"2025Q3", "2025Q4", "2026Q1"}
GOOD_Q     = {"2023Q4", "2024Q1", "2024Q2", "2024Q4"}


def replay(symbol, fund_hi=0.05, sl_atr=3.0, tp_atr=2.0, cooldown=24, warmup=200,
           price_weak=False, off_high=False, not_para=False):
    df = load_long(symbol, "1h")
    if df is None or len(df) < warmup + 50: return []
    fdf = load_funding_long(symbol)
    if fdf is None or fdf.empty: return []
    trades, last = [], -10**9
    for i in range(warmup, len(df)-1):
        if i - last < cooldown: continue
        ts = df.index[i]
        f = asof(fdf["fundingRate"], ts)
        if f is None or float(f) < fund_hi: continue
        row = df.iloc[i]
        close = float(row["close"]); atr = row["atr"]
        # ── Factors (mechanistic anti-squeeze) ──
        if price_weak and not (close < float(row["ema9"])):
            continue
        if off_high:
            hi24 = float(row["hi_24"])
            if hi24 <= 0 or (hi24 - close) / hi24 < 0.015:
                continue
        if not_para:
            if atr > 0 and (close - float(row["ema50"])) / atr > 6:
                continue
        sl = close + atr*sl_atr; tp = close - atr*tp_atr
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
    if trades.empty: print(f"[{label:24s}] 0 trades"); return None
    trades["q"] = pd.to_datetime(trades["time"]).dt.to_period("Q").astype(str)
    qs = sorted(trades["q"].unique())
    pos = sum(1 for q in qs if (st(trades[trades["q"]==q]) or {}).get("exp",0)>0)
    ov = st(trades)
    good = st(trades[trades["q"].isin(GOOD_Q)])
    dd   = st(trades[trades["q"].isin(DRAWDOWN_Q)])
    print(f"[{label:24s}] n={ov['n']:>3d} WR={ov['wr']:>4} exp={ov['exp']:>+.3f} totR={ov['totalR']:>+6.1f} "
          f"| quý+ {pos}/{len(qs)}={pos/len(qs)*100:>3.0f}% | GOOD exp={good['exp'] if good else 'na':>} | DD exp={dd['exp'] if dd else 'na'}")
    return dict(ov=ov, qpos=pos/len(qs)*100, good=good, dd=dd, trades=trades, qs=qs)


def main():
    syms = [u["symbol"] for u in json.load(open(os.path.join(DATA_DIR,"universe.json")))["coins"]]
    configs = [
        ("base",                  dict()),
        ("+price_weak",           dict(price_weak=True)),
        ("+off_high",             dict(off_high=True)),
        ("+not_para",             dict(not_para=True)),
        ("+price_weak+off_high",  dict(price_weak=True, off_high=True)),
        ("+all3",                 dict(price_weak=True, off_high=True, not_para=True)),
    ]
    results = []
    for label, kw in configs:
        all_t = []
        for s in syms:
            all_t.extend(replay(s, **kw))
        r = evaluate(pd.DataFrame(all_t), label)
        if r: results.append((label, r))

    # Best: ưu tiên qpos, rồi giảm DD loss mà giữ good
    print("\n--- Đánh giá kỷ luật (factor tốt = DD đỡ hơn + GOOD còn) ---")
    base = next(r for l,r in results if l=="base")
    for label, r in results:
        if label=="base": continue
        dd_improve = (r["dd"]["exp"] if r["dd"] else -99) - (base["dd"]["exp"] if base["dd"] else -99)
        good_keep  = (r["good"]["exp"] if r["good"] else 0)
        verdict = "✅ tốt" if (dd_improve>0.05 and good_keep>0) else "⚠️"
        print(f"  {label:24s}: DD Δexp={dd_improve:+.3f}, GOOD exp={good_keep:+.3f} {verdict}")

    best = max(results, key=lambda x: (x[1]["qpos"], x[1]["ov"]["exp"]))
    print(f"\n=== BEST qpos: {best[0]} — {best[1]['qpos']:.0f}% quý+ ===")
    for q in best[1]["qs"]:
        e = (st(best[1]["trades"][best[1]["trades"]["q"]==q]) or {}).get("exp",0)
        print(f"  {q}: {e:+.3f} {'✅' if e>0 else '🔴'}")
    best[1]["trades"].to_parquet(os.path.join(DATA_DIR, "trades_funding_strong.parquet"))


if __name__ == "__main__":
    main()
