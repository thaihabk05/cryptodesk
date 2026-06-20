"""
phase2_combined_gate.py — Test gate kép có cơ sở CƠ CHẾ (không curve-fit):
  vol thấp (mean ổn định) VÀ BTC không downtrend (không bắt dao rơi).

Đây là test mechanistically-motivated cuối cùng trên 6 tháng. Dù kết quả thế nào,
bước tiếp theo là kéo 2-3 năm data để walk-forward đa chu kỳ.
"""
import os
import numpy as np
import pandas as pd

from backtester import load_coin, run_strategy
from phase2_refine import _stat, walk_forward
from phase2_vol_gate import btc_dvol_asof

# BTC 7d return as-of (point-in-time) — macro direction gate
_BTC_7D = None
def _btc_7d_series():
    global _BTC_7D
    if _BTC_7D is None:
        df = load_coin("BTCUSDT", "1h")
        _BTC_7D = df["close"].pct_change(168) * 100   # % thay đổi 7 ngày
    return _BTC_7D

def btc_7d_asof(ts):
    s = _btc_7d_series()
    sub = s[s.index <= ts]
    if sub.empty or pd.isna(sub.iloc[-1]):
        return None
    return float(sub.iloc[-1])


def make_h2_combined(vol_max, btc_7d_min):
    def strat(ctx):
        if ctx["tier"] != "midcap" or ctx["regime"] != "RANGING":
            return None
        row = ctx["row"]
        if row["ema50"] <= row["ema200"]:
            return None
        ts = ctx["df"].index[ctx["i"]]
        dvol = btc_dvol_asof(ts)
        if dvol is None or dvol >= vol_max:
            return None
        b7 = btc_7d_asof(ts)
        if b7 is None or b7 < btc_7d_min:   # BTC không đang crash
            return None
        if row["rsi"] < 28 and row["dist_vwap_atr"] <= -2.5:
            entry = float(row["close"])
            sl = entry - row["atr"] * 1.5
            tp = float(row["vwap24"]) + row["atr"] * 0.5
            if tp > entry:
                return {"direction": "LONG", "sl": sl, "tp": tp, "tag": "H2comb"}
        return None
    return strat


def main():
    print("="*82)
    print(f"{'GATE':28s} {'N':>4s} {'WR':>5s} {'totR':>7s} {'exp':>7s} {'WF 3 đoạn':>22s} {'ROB':>4s}")
    print("="*82)
    best = None
    for vol_max, b7min in [(2.0, -5), (2.0, -3), (2.0, 0), (1.8, -3), (2.2, -3)]:
        strat = make_h2_combined(vol_max, b7min)
        trades = run_strategy(strat, f"comb")
        if trades.empty:
            print(f"vol<{vol_max} & btc7d>{b7min}: 0 trades"); continue
        ov = _stat(trades)
        wf = walk_forward(trades, 3)
        wfstr = str([round(e,3) for e in wf["exps"]]) if wf else "n/a"
        rob = "✅" if (wf and wf["all_positive"]) else "❌"
        label = f"vol<{vol_max} & btc7d>{b7min}%"
        print(f"{label:28s} {ov['n']:>4d} {ov['wr']:>5.1f} {ov['totalR']:>7.2f} "
              f"{ov['exp']:>7.3f} {wfstr:>22s} {rob:>4s}")
        if wf and wf["all_positive"] and (best is None or wf["min_exp"] > best[1]):
            best = (label, wf["min_exp"], trades)
    if best:
        best[2].to_parquet(os.path.join(os.path.dirname(__file__), "data", "trades_combined_robust.parquet"))
        print(f"\n✅ ROBUST: {best[0]} min_exp={best[1]:.3f}")
    else:
        print("\n⚠️ Chưa robust trên 6 tháng → cần 2-3 năm data (mỗi regime nhiều mẫu).")


if __name__ == "__main__":
    main()
