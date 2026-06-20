"""
phase2_refine.py — Refine H2 mean-reversion với kỷ luật walk-forward.

Mục tiêu: tìm subset/variant của H2 có edge ROBUST (in-sample VÀ out-sample
CÙNG dương). Test nhiều variant cùng lúc, rank theo độ ổn định.

Luật cứng:
- Chỉ giữ variant nào IN-SAMPLE và OUT-SAMPLE cùng exp > 0.
- Ưu tiên ổn định (2 nửa gần nhau) hơn là exp cao nhưng lệch.

Chạy: python3 research/phase2_refine.py
"""
import os, json
import numpy as np
import pandas as pd

from backtester import (load_coin, load_funding, funding_asof,
                        compute_btc_regime, simulate_trade, run_strategy)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _stat(df):
    closed = df[df["result"].isin(["WIN", "LOSS", "TIMEOUT"])]
    n = len(closed)
    if n == 0:
        return None
    wins = (closed["pnl_r"] > 0).sum()
    return dict(n=n, wr=round(wins/n*100, 1),
                totalR=round(closed["pnl_r"].sum(), 2),
                exp=round(closed["pnl_r"].mean(), 3))


def walk_forward(df, n_splits=3):
    """Chia thời gian thành n_splits đoạn, đo exp mỗi đoạn → ổn định?"""
    closed = df[df["result"].isin(["WIN", "LOSS", "TIMEOUT"])].sort_values("time")
    if len(closed) < n_splits * 10:
        return None
    chunks = np.array_split(closed, n_splits)
    exps = [round(c["pnl_r"].mean(), 3) for c in chunks]
    ns   = [len(c) for c in chunks]
    all_pos = all(e > 0 for e in exps)
    return dict(exps=exps, ns=ns, all_positive=all_pos,
                min_exp=min(exps), worst=min(exps))


# ── H2 variants — mỗi cái 1 bộ tham số ───────────────────────────────────
def make_h2(dist_thresh, rsi_thresh, sl_atr, tp_mode, require_funding_neg=False,
            midcap_only=True, ranging_only=True):
    """Factory tạo strategy H2 với tham số khác nhau.
    tp_mode: 'vwap' | 'vwap_atr' (vwap + 0.5 atr) | 'half_dist' (về nửa đường)
    """
    def strat(ctx):
        row = ctx["row"]; f = ctx["funding"]
        if midcap_only and ctx["tier"] != "midcap":
            return None
        if ranging_only and ctx["regime"] != "RANGING":
            return None
        if row["ema50"] <= row["ema200"]:
            return None
        if require_funding_neg and (f is None or f >= 0):
            return None
        if row["rsi"] < rsi_thresh and row["dist_vwap_atr"] <= dist_thresh:
            entry = float(row["close"])
            sl = entry - row["atr"] * sl_atr
            if tp_mode == "vwap":
                tp = float(row["vwap24"])
            elif tp_mode == "vwap_atr":
                tp = float(row["vwap24"]) + row["atr"] * 0.5
            else:  # half_dist — về nửa đường tới vwap
                tp = entry + (float(row["vwap24"]) - entry) * 0.5
            if tp > entry:
                return {"direction": "LONG", "sl": sl, "tp": tp, "tag": "H2v"}
        return None
    return strat


def main():
    variants = [
        # (label, dist, rsi, sl_atr, tp_mode, fund_neg)
        ("base_d2.0_rsi28",      -2.0, 28, 1.5, "vwap",     False),
        ("deep_d2.5_rsi28",      -2.5, 28, 1.5, "vwap",     False),
        ("deep_d2.5_rsi25",      -2.5, 25, 1.5, "vwap",     False),
        ("deeper_d3.0_rsi30",    -3.0, 30, 1.5, "vwap",     False),
        ("deep_tp_atr",          -2.5, 28, 1.5, "vwap_atr", False),
        ("deep_tp_half",         -2.5, 28, 1.5, "half_dist",False),
        ("deep_sl2.0",           -2.5, 28, 2.0, "vwap",     False),
        ("deep_fundneg",         -2.5, 28, 1.5, "vwap",     True),
        ("deep_d3_fundneg",      -3.0, 30, 1.5, "vwap",     True),
    ]

    results = []
    for label, dist, rsi, sl_atr, tp_mode, fneg in variants:
        strat = make_h2(dist, rsi, sl_atr, tp_mode, require_funding_neg=fneg)
        trades = run_strategy(strat, label)
        if trades.empty:
            print(f"[{label}] 0 trades"); continue
        overall = _stat(trades)
        wf = walk_forward(trades, n_splits=3)
        results.append((label, overall, wf, trades))

    # Rank: ưu tiên all_positive walk-forward, rồi min_exp
    print("\n" + "="*80)
    print(f"{'VARIANT':22s} {'N':>4s} {'WR':>5s} {'totR':>7s} {'exp':>7s} {'WF exps (3 đoạn)':>26s} {'ROBUST':>7s}")
    print("="*80)
    def sortkey(r):
        wf = r[2]
        if wf is None: return (0, -999)
        return (1 if wf["all_positive"] else 0, wf["min_exp"])
    for label, ov, wf, _ in sorted(results, key=sortkey, reverse=True):
        wfstr = str(wf["exps"]) if wf else "n/a"
        robust = "✅" if (wf and wf["all_positive"]) else "❌"
        print(f"{label:22s} {ov['n']:>4d} {ov['wr']:>5.1f} {ov['totalR']:>7.2f} {ov['exp']:>7.3f} {wfstr:>26s} {robust:>5s}")

    # Save best robust variant trades
    robust_variants = [r for r in results if r[2] and r[2]["all_positive"]]
    if robust_variants:
        best = max(robust_variants, key=lambda r: r[2]["min_exp"])
        best[3].to_parquet(os.path.join(DATA_DIR, "trades_best_robust.parquet"))
        print(f"\n✅ Best robust: {best[0]} — saved trades_best_robust.parquet")
    else:
        print("\n⚠️ KHÔNG variant nào robust (cả 3 đoạn cùng dương). Edge chưa ổn định.")


if __name__ == "__main__":
    main()
