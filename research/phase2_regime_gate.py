"""
phase2_regime_gate.py — Tìm biến số GATE phân biệt đoạn thua vs đoạn thắng.

Phát hiện: mọi variant H2 đều lỗ đoạn 1 (2 tháng đầu), lãi đoạn 2-3.
→ Edge phụ thuộc market regime. Tìm regime đó để gate.

Phân tích:
1. Monthly P&L breakdown → xác định chính xác tháng nào thua
2. Correlate với BTC macro: BTC trend, BTC realized vol mỗi tháng
3. Test gate: chỉ trade khi BTC ở trạng thái X
"""
import os
import numpy as np
import pandas as pd

from backtester import load_coin, run_strategy
from phase2_refine import make_h2, _stat

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def btc_monthly_context():
    """BTC trend + vol mỗi tháng để correlate với P&L."""
    df = load_coin("BTCUSDT", "1d")
    df = df.copy()
    df["month"] = df.index.to_period("M").astype(str)
    df["ret"] = df["close"].pct_change()
    out = {}
    for m, g in df.groupby("month"):
        chg = (g["close"].iloc[-1] - g["close"].iloc[0]) / g["close"].iloc[0] * 100
        vol = g["ret"].std() * 100
        out[m] = {"btc_chg_pct": round(float(chg), 1), "btc_dvol": round(float(vol), 2)}
    return out


def main():
    # Dùng variant tốt nhất từ phase2: deep_tp_atr
    strat = make_h2(-2.5, 28, 1.5, "vwap_atr", require_funding_neg=False)
    trades = run_strategy(strat, "deep_tp_atr")
    trades["dt"] = pd.to_datetime(trades["time"])
    trades["month"] = trades["dt"].dt.to_period("M").astype(str)

    btc_ctx = btc_monthly_context()

    print("="*72)
    print(f"{'Month':9s} {'N':>4s} {'WR':>5s} {'exp':>7s} {'totR':>7s} | {'BTC chg':>8s} {'BTC dvol':>9s}")
    print("="*72)
    monthly = []
    for m, g in trades.groupby("month"):
        s = _stat(g)
        if not s: continue
        bc = btc_ctx.get(m, {})
        monthly.append((m, s, bc))
        print(f"{m:9s} {s['n']:>4d} {s['wr']:>5.1f} {s['exp']:>7.3f} {s['totalR']:>7.2f} | "
              f"{bc.get('btc_chg_pct','?'):>8} {bc.get('btc_dvol','?'):>9}")

    # Correlation: BTC monthly chg vs strategy exp
    print("\n--- Tương quan BTC trend ↔ strategy edge ---")
    rows = [(s['exp'], bc.get('btc_chg_pct'), bc.get('btc_dvol'))
            for _, s, bc in monthly if bc.get('btc_chg_pct') is not None]
    if len(rows) >= 3:
        exps = np.array([r[0] for r in rows])
        chgs = np.array([r[1] for r in rows])
        dvols = np.array([r[2] for r in rows])
        print(f"corr(exp, BTC_chg):  {np.corrcoef(exps, chgs)[0,1]:+.2f}")
        print(f"corr(exp, BTC_dvol): {np.corrcoef(exps, dvols)[0,1]:+.2f}")

    # Test gate hypotheses: split theo BTC monthly chg
    print("\n--- Gate test: BTC tháng giảm vs tăng ---")
    bull_months = [m for m, _, bc in monthly if bc.get('btc_chg_pct', 0) > 0]
    bear_months = [m for m, _, bc in monthly if bc.get('btc_chg_pct', 0) <= 0]
    bull = trades[trades["month"].isin(bull_months)]
    bear = trades[trades["month"].isin(bear_months)]
    print(f"  BTC tháng TĂNG ({bull_months}): {_stat(bull)}")
    print(f"  BTC tháng GIẢM ({bear_months}): {_stat(bear)}")


if __name__ == "__main__":
    main()
