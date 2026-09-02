"""
phase6_long_momentum.py — Research LONG edge (đối trọng short edge đã validate).

KỶ LUẬT (như hồi tìm short edge):
  - Walk-forward 3 năm (7/2023→6/2026), cross-sectional 32 coin (không single-coin).
  - Threshold SỐ TRÒN, không grid-fit. Test vài giả thuyết pre-specified, không dò.
  - Report TRUNG THỰC kể cả FAIL.
  - Bar để coi là "edge thật": overall exp>0 VÀ ≥70% quý dương (short edge đạt 75%).

Giả thuyết LONG (ngược short mean-reversion — đây là trend/momentum):
  L1  Trend pullback : uptrend H1 (e34>e89>e200) + hồi chạm EMA34 + bounce.
  L2  Rel-strength   : coin outperform BTC 7d ≥ Xpp + uptrend + momentum (close>ema9).
  L3  Breakout       : phá đỉnh 5 ngày + uptrend.
  L4  RS + macro gate: L2 nhưng THÊM H4 uptrend (kiểm regime-gate có cứu như ARB không).
"""
import os, numpy as np, pandas as pd
from phase3_walkforward import load_long          # klines_long + add_indicators
from backtester import simulate_trade

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KL = os.path.join(DATA_DIR, "klines_long")
COOLDOWN = 24            # nến H1 giữa 2 lệnh/coin
SL_ATR, TP_ATR = 2.0, 3.0   # trend/momentum: để winner chạy (ngược short 3/2)

def universe():
    return sorted(set(f.split("_")[0] for f in os.listdir(KL) if f.endswith("_1h.parquet")))

def st(d):
    n = len(d)
    if n == 0: return None
    w = (d["pnl_r"] > 0).sum()
    return dict(n=n, wr=round(w/n*100,1), exp=round(d["pnl_r"].mean(),3), totalR=round(d["pnl_r"].sum(),1))

def quarterly(trades, label):
    if not trades:
        print(f"[{label:30s}] 0 lệnh"); return None
    df = pd.DataFrame(trades)
    df["q"] = pd.to_datetime(df["time"]).dt.to_period("Q").astype(str)
    qs = sorted(df["q"].unique())
    pos = sum(1 for q in qs if (st(df[df["q"]==q]) or {}).get("exp",0) > 0)
    o = st(df)
    ok = o["exp"] > 0 and pos/len(qs) >= 0.70
    print(f"[{label:30s}] n={o['n']:>4} WR={o['wr']:>5} exp={o['exp']:>+.3f} "
          f"totR={o['totalR']:>+7.1f} | quý+ {pos}/{len(qs)}={pos/len(qs)*100:>3.0f}% {'✅' if ok else '❌'}")
    return df

# BTC 7d return để tính relative strength (align theo timestamp)
_BTC = None
def btc_ret7():
    global _BTC
    if _BTC is None:
        b = load_long("BTCUSDT", "1h")
        _BTC = (b["close"].pct_change(168) * 100)
    return _BTC

# ── Signal generators (mỗi cái trả list trades {time,pnl_r}) ──────────────
def L1_pullback(df, **kw):
    trades=[]; last=-10**9
    for i in range(200, len(df)-1):
        if i-last < COOLDOWN: continue
        r=df.iloc[i]; p=df.iloc[i-1]
        if not (r.ema34 > r.ema89 > r.ema200): continue           # uptrend
        touched = p.low <= p.ema34 or r.low <= r.ema34            # hồi chạm EMA34
        bounce  = r.close > r.open and r.close > r.ema9           # bật lên
        if not (touched and bounce) or r.rsi > 70: continue
        close=float(r.close); atr=float(r.atr)
        sl=min(float(r.low),float(p.low))-atr*0.5; tp=close+atr*TP_ATR
        if sl>=close: continue
        res=simulate_trade(df,i+1,"LONG",sl,tp)
        if res: trades.append({"time":str(df.index[i]),"pnl_r":round(res[1],3)}); last=i
    return trades

def L2_relstrength(df, rel_min=5.0, **kw):
    br = btc_ret7().reindex(df.index, method="ffill")
    coin7 = df["close"].pct_change(168)*100
    trades=[]; last=-10**9
    for i in range(200, len(df)-1):
        if i-last < COOLDOWN: continue
        r=df.iloc[i]
        rel = coin7.iloc[i] - br.iloc[i]
        if pd.isna(rel) or rel < rel_min: continue                # mạnh hơn BTC ≥ rel_min pp
        if not (r.ema34 > r.ema89): continue                      # uptrend nhẹ
        if not (r.close > r.ema9): continue                       # momentum
        if r.rsi > 75: continue
        close=float(r.close); atr=float(r.atr)
        sl=close-atr*SL_ATR; tp=close+atr*TP_ATR
        res=simulate_trade(df,i+1,"LONG",sl,tp)
        if res: trades.append({"time":str(df.index[i]),"pnl_r":round(res[1],3)}); last=i
    return trades

def L3_breakout(df, **kw):
    hi5 = df["high"].rolling(120, min_periods=20).max().shift(1)  # đỉnh 5 ngày (120 H1)
    trades=[]; last=-10**9
    for i in range(200, len(df)-1):
        if i-last < COOLDOWN: continue
        r=df.iloc[i]
        if not (r.ema34 > r.ema89 > r.ema200): continue           # uptrend
        if pd.isna(hi5.iloc[i]) or r.close <= hi5.iloc[i]: continue  # phá đỉnh 5d
        if r.rsi > 78: continue
        close=float(r.close); atr=float(r.atr)
        sl=close-atr*SL_ATR; tp=close+atr*TP_ATR
        res=simulate_trade(df,i+1,"LONG",sl,tp)
        if res: trades.append({"time":str(df.index[i]),"pnl_r":round(res[1],3)}); last=i
    return trades

def L4_rs_macrogate(df, sym=None, rel_min=5.0, **kw):
    # L2 + THÊM H4 uptrend (regime-gate). Load H4 của coin, map lên H1.
    try:
        h4 = load_long(sym, "4h")
    except Exception:
        return []
    h4_up = (h4["ema34"] > h4["ema89"]).reindex(df.index, method="ffill")
    br = btc_ret7().reindex(df.index, method="ffill")
    coin7 = df["close"].pct_change(168)*100
    trades=[]; last=-10**9
    for i in range(200, len(df)-1):
        if i-last < COOLDOWN: continue
        if not bool(h4_up.iloc[i]): continue                      # macro gate H4
        r=df.iloc[i]; rel = coin7.iloc[i] - br.iloc[i]
        if pd.isna(rel) or rel < rel_min: continue
        if not (r.ema34 > r.ema89) or r.close <= r.ema9 or r.rsi > 75: continue
        close=float(r.close); atr=float(r.atr)
        sl=close-atr*SL_ATR; tp=close+atr*TP_ATR
        res=simulate_trade(df,i+1,"LONG",sl,tp)
        if res: trades.append({"time":str(df.index[i]),"pnl_r":round(res[1],3)}); last=i
    return trades

def L5_trend_widetp(df, tp_atr=6.0, **kw):
    # Trend pullback nhưng TP RỘNG (để lời chạy) — kiểm "cắt lời sớm?"
    trades=[]; last=-10**9
    for i in range(200, len(df)-1):
        if i-last < COOLDOWN: continue
        r=df.iloc[i]; p=df.iloc[i-1]
        if not (r.ema34 > r.ema89 > r.ema200): continue
        touched = p.low <= p.ema34 or r.low <= r.ema34
        bounce  = r.close > r.open and r.close > r.ema9
        if not (touched and bounce) or r.rsi > 70: continue
        close=float(r.close); atr=float(r.atr)
        sl=min(float(r.low),float(p.low))-atr*0.5; tp=close+atr*tp_atr
        if sl>=close: continue
        res=simulate_trade(df,i+1,"LONG",sl,tp,max_bars=240)   # cho 10 ngày để lời chạy
        if res: trades.append({"time":str(df.index[i]),"pnl_r":round(res[1],3)}); last=i
    return trades

def L6_btc_regime(df, tp_atr=6.0, **kw):
    # L5 + CHỈ long khi BTC cũng uptrend (macro risk-on)
    b = load_long("BTCUSDT","1h"); btc_up=(b["ema34"]>b["ema89"]).reindex(df.index,method="ffill")
    trades=[]; last=-10**9
    for i in range(200, len(df)-1):
        if i-last < COOLDOWN: continue
        if not bool(btc_up.iloc[i]): continue                  # BTC risk-on
        r=df.iloc[i]; p=df.iloc[i-1]
        if not (r.ema34 > r.ema89 > r.ema200): continue
        touched = p.low <= p.ema34 or r.low <= r.ema34
        bounce  = r.close > r.open and r.close > r.ema9
        if not (touched and bounce) or r.rsi > 70: continue
        close=float(r.close); atr=float(r.atr)
        sl=min(float(r.low),float(p.low))-atr*0.5; tp=close+atr*tp_atr
        if sl>=close: continue
        res=simulate_trade(df,i+1,"LONG",sl,tp,max_bars=240)
        if res: trades.append({"time":str(df.index[i]),"pnl_r":round(res[1],3)}); last=i
    return trades

def run(fn, label, **kw):
    allt=[]
    for sym in universe():
        try:
            df = load_long(sym, "1h")
            if df is None or len(df) < 400: continue
            allt += fn(df, sym=sym, **kw)
        except Exception as e:
            print(f"  [{sym} err] {e}")
    return quarterly(allt, label)

def main():
    print(f"LONG momentum research — {len(universe())} coin, 3 năm walk-forward")
    print(f"SL {SL_ATR}×ATR / TP {TP_ATR}×ATR, cooldown {COOLDOWN} nến\n" + "="*82)
    run(L1_pullback,  "L1 Trend pullback")
    run(L2_relstrength,"L2 Rel-strength ≥5pp", rel_min=5.0)
    run(L2_relstrength,"L2 Rel-strength ≥10pp", rel_min=10.0)
    run(L2_relstrength,"L2 Rel-strength ≥0pp", rel_min=0.0)
    run(L3_breakout,  "L3 Breakout 5d-high")
    run(L4_rs_macrogate,"L4 RS≥5pp + H4-gate", rel_min=5.0)
    run(L5_trend_widetp,"L5 Trend TP-rộng 6ATR")
    run(L6_btc_regime,  "L6 L5 + BTC risk-on")
    print("="*82)
    print("Bar 'edge thật': exp>0 + ≥70% quý dương (✅). Nếu toàn ❌ → không có long-edge robust.")

if __name__ == "__main__":
    main()
