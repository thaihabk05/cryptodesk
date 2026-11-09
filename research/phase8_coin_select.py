"""
phase8_coin_select.py — Chọn 1 coin bổ sung (ngoài ARB/BTC) hợp nhất cho hệ thống.

Tiêu chí (data-driven):
  1. EDGE funding-short (edge đã validate của mình) per-coin: n, WR, exp, quý+
  2. THANH KHOẢN: 24h volume hiện tại (tradable, ít slippage)
  3. ĐA DẠNG HOÁ: tương quan return D1 với ARB (thấp = bổ sung tốt, bắn lúc khác)
Xếp hạng tổng hợp → đề xuất coin tốt nhất.
"""
import os, numpy as np, pandas as pd, requests
from phase3_walkforward import load_long, load_funding_long, asof
from backtester import simulate_trade

KL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "klines_long")
EXCLUDE = {"BTCUSDT", "ARBUSDT"}  # đã follow

def coins():
    return sorted(s for s in (f.split("_")[0] for f in os.listdir(KL) if f.endswith("_1h.parquet")))

def funding_short(sym, fmin=0.03):
    df = load_long(sym, "1h"); fdf = load_funding_long(sym)
    if df is None or fdf is None or len(df) < 400: return None
    trades = []; last = -10**9
    for i in range(200, len(df)-1):
        if i-last < 24: continue
        ts = df.index[i]; f = asof(fdf["fundingRate"], ts)
        if f is None or float(f) < fmin: continue
        row = df.iloc[i]; close = float(row["close"]); atr = float(row["atr"]); e9 = float(row["ema9"])
        hi24 = float(df["high"].iloc[i-24:i].max())
        if hi24 <= 0 or (hi24-close)/hi24 < 0.015: continue
        if not (close < e9): continue
        r = simulate_trade(df, i+1, "SHORT", close+atr*3, close-atr*2)
        if r: trades.append({"time": str(ts), "pnl_r": round(r[1], 3)}); last = i
    return trades

def stats(trades):
    if not trades: return None
    df = pd.DataFrame(trades); n = len(df); w = (df["pnl_r"]>0).sum()
    df["q"] = pd.to_datetime(df["time"]).dt.to_period("Q").astype(str)
    qs = df["q"].unique(); pos = sum(1 for q in qs if df[df["q"]==q]["pnl_r"].mean() > 0)
    return dict(n=n, wr=round(w/n*100,1), exp=round(df["pnl_r"].mean(),3),
                totR=round(df["pnl_r"].sum(),1), qpos=round(pos/len(qs)*100) if len(qs) else 0)

def vol24():
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15).json()
        return {d["symbol"]: float(d.get("quoteVolume",0)) for d in r if isinstance(d, dict)}
    except Exception: return {}

def main():
    arb = load_long("ARBUSDT","1h")["close"].pct_change().resample("1D").sum()
    vols = vol24()
    rows = []
    for c in coins():
        if c in EXCLUDE: continue
        s = stats(funding_short(c))
        if not s: continue
        # tương quan D1 với ARB
        try:
            cc = load_long(c,"1h")["close"].pct_change().resample("1D").sum()
            corr = round(arb.reindex(cc.index).corr(cc), 2)
        except Exception: corr = None
        v = vols.get(c, 0)/1e6
        s.update(symbol=c, corr=corr, vol_m=round(v))
        rows.append(s)
    # lọc thanh khoản tối thiểu $20M/24h, edge dương, đủ mẫu
    tradable = [r for r in rows if r["vol_m"] >= 20 and r["exp"] > 0 and r["n"] >= 30]
    # score: exp × căn(n) (độ tin) × (1 - |corr| ưu tiên đa dạng) ; chuẩn hoá đơn giản
    for r in tradable:
        sig = r["exp"] * (r["n"]**0.5)
        div = (1 - abs(r["corr"])) if r["corr"] is not None else 0.5
        r["score"] = round(sig * (0.6 + 0.4*div), 2)
    tradable.sort(key=lambda x: -x["score"])
    print(f"{'COIN':12s} {'n':>4} {'WR':>5} {'exp':>7} {'quý+':>5} {'vol$M':>6} {'corrARB':>7} {'score':>6}")
    print("-"*62)
    for r in tradable[:15]:
        print(f"{r['symbol']:12s} {r['n']:>4} {r['wr']:>5} {r['exp']:>+7.3f} {r['qpos']:>4}% {r['vol_m']:>6} {str(r['corr']):>7} {r['score']:>6}")
    print("\n>>> ĐỀ XUẤT:", tradable[0]['symbol'] if tradable else "(không coin nào đạt)")

if __name__ == "__main__":
    main()
