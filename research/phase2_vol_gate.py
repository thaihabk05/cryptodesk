"""
phase2_vol_gate.py — Test BTC volatility gate cho H2 mean-reversion.

Phát hiện: corr(exp, BTC daily-vol) = -0.56. Mean reversion cần mean ổn định.
Gate: chỉ trade khi BTC realized-vol (point-in-time) DƯỚI ngưỡng.

Test: ngưỡng vol khác nhau + walk-forward 3 đoạn phải CÙNG dương.
"""
import os
import numpy as np
import pandas as pd

from backtester import load_coin, run_strategy
from phase2_refine import _stat, walk_forward

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ── Precompute BTC realized vol (point-in-time, no lookahead) ─────────────
_BTC_DVOL = None
def _btc_dvol_series():
    global _BTC_DVOL
    if _BTC_DVOL is not None:
        return _BTC_DVOL
    df = load_coin("BTCUSDT", "1h")
    # Realized vol: std của H1 returns trong 24h gần nhất, annualize-free (%/h scale)
    ret = df["close"].pct_change()
    # daily-equivalent vol: std(H1 ret) trong 168h (7 ngày) × sqrt(24) → %/ngày
    dvol = ret.rolling(168, min_periods=48).std() * np.sqrt(24) * 100
    _BTC_DVOL = dvol
    return dvol

def btc_dvol_asof(ts):
    s = _btc_dvol_series()
    sub = s[s.index <= ts]
    if sub.empty or pd.isna(sub.iloc[-1]):
        return None
    return float(sub.iloc[-1])


def make_h2_gated(dist_thresh, rsi_thresh, sl_atr, tp_mode, vol_max):
    """H2 + BTC vol gate. Chỉ trade khi btc_dvol < vol_max."""
    def strat(ctx):
        if ctx["tier"] != "midcap":
            return None
        if ctx["regime"] != "RANGING":
            return None
        row = ctx["row"]
        if row["ema50"] <= row["ema200"]:
            return None
        # BTC vol gate (point-in-time)
        ts = ctx["df"].index[ctx["i"]]
        dvol = btc_dvol_asof(ts)
        if dvol is None or dvol >= vol_max:
            return None
        if row["rsi"] < rsi_thresh and row["dist_vwap_atr"] <= dist_thresh:
            entry = float(row["close"])
            sl = entry - row["atr"] * sl_atr
            if tp_mode == "vwap_atr":
                tp = float(row["vwap24"]) + row["atr"] * 0.5
            else:
                tp = float(row["vwap24"])
            if tp > entry:
                return {"direction": "LONG", "sl": sl, "tp": tp, "tag": f"H2g{vol_max}"}
        return None
    return strat


def main():
    # Đầu tiên xem phân bố BTC dvol để chọn ngưỡng hợp lý
    s = _btc_dvol_series().dropna()
    print(f"BTC dvol distribution: min={s.min():.2f} p25={s.quantile(.25):.2f} "
          f"median={s.median():.2f} p75={s.quantile(.75):.2f} max={s.max():.2f}")

    print("\n" + "="*78)
    print(f"{'GATE (vol_max)':16s} {'N':>4s} {'WR':>5s} {'totR':>7s} {'exp':>7s} {'WF 3 đoạn':>24s} {'ROBUST':>7s}")
    print("="*78)

    best = None
    for vol_max in [99, 2.5, 2.2, 2.0, 1.8, 1.6]:
        strat = make_h2_gated(-2.5, 28, 1.5, "vwap_atr", vol_max)
        trades = run_strategy(strat, f"gate{vol_max}")
        if trades.empty:
            print(f"vol<{vol_max:<11} 0 trades"); continue
        ov = _stat(trades)
        wf = walk_forward(trades, 3)
        wfstr = str([round(e,3) for e in wf["exps"]]) if wf else "n/a"
        robust = "✅" if (wf and wf["all_positive"]) else "❌"
        label = "no-gate" if vol_max == 99 else f"vol<{vol_max}"
        print(f"{label:16s} {ov['n']:>4d} {ov['wr']:>5.1f} {ov['totalR']:>7.2f} "
              f"{ov['exp']:>7.3f} {wfstr:>24s} {robust:>5s}")
        if wf and wf["all_positive"] and (best is None or wf["min_exp"] > best[1]):
            best = (label, wf["min_exp"], trades, vol_max)

    if best:
        best[2].to_parquet(os.path.join(DATA_DIR, "trades_gated_robust.parquet"))
        print(f"\n✅ ROBUST: {best[0]} — min_exp={best[1]:.3f}. Saved trades_gated_robust.parquet")
        # Per tier/regime đã fix midcap+ranging. In thêm per-coin top
        t = best[2]
        print(f"\nTop coin (gate {best[0]}):")
        for sym, g in sorted(t.groupby("symbol"),
                             key=lambda x: -x[1]['pnl_r'].sum())[:10]:
            s = _stat(g)
            if s and s['n'] >= 3:
                print(f"  {sym:12s}: {s}")
    else:
        print("\n⚠️ Vẫn chưa robust. Cần biến gate khác hoặc nhiều data hơn.")


if __name__ == "__main__":
    main()
