"""
phase7_arb_monitor_eval.py — Backtest ĐÚNG logic ARB monitor Rule B/C HIỆN TẠI
(có gate regime + st_break + e34_falling guard) trên 3 năm ARB, walk-forward theo quý.

KỶ LUẬT: báo cả quý CŨ (out-of-sample, trước khi tune guard) lẫn quý gần (in-sample).
So 3 biến thể để biết guard thực sự giúp hay chỉ fit data gần:
  V0 = Rule B/C gốc (chỉ downtrend, không guard)
  V1 = + st_break (mở gate khi gãy ngắn hạn)
  V2 = V1 + e34_falling (chỉ short khi EMA34 thực sự xuống) = BẢN DEPLOY HIỆN TẠI
"""
import os, numpy as np, pandas as pd
from phase3_walkforward import load_long
from backtester import simulate_trade

def ema(s,p): return s.ewm(span=p,adjust=False).mean()
def rsi(s,n=14):
    d=s.diff();g=d.clip(lower=0).rolling(n,min_periods=1).mean();l=(-d.clip(upper=0)).rolling(n,min_periods=1).mean()
    return (100-100/(1+g/l.replace(0,np.nan))).fillna(50)

def st(df):
    n=len(df)
    if n==0: return None
    w=(df["pnl_r"]>0).sum()
    return dict(n=n, wr=round(w/n*100,1), exp=round(df["pnl_r"].mean(),3), totalR=round(df["pnl_r"].sum(),1))

def quarterly(trades, label):
    if not trades: print(f"[{label:30s}] 0 lệnh"); return None
    df=pd.DataFrame(trades); df["q"]=pd.to_datetime(df["time"]).dt.to_period("Q").astype(str)
    qs=sorted(df["q"].unique()); pos=sum(1 for q in qs if (st(df[df["q"]==q]) or {}).get("exp",0)>0)
    o=st(df)
    print(f"[{label:30s}] n={o['n']:>4} WR={o['wr']:>5} exp={o['exp']:>+.3f} totR={o['totalR']:>+6.1f} | quý+ {pos}/{len(qs)}={pos/len(qs)*100:.0f}%")
    return df

def backtest(variant):
    arb=load_long("ARBUSDT","1h"); btc=load_long("BTCUSDT","1h"); arb4=load_long("ARBUSDT","4h")
    e200h4=ema(arb4["close"],200).reindex(arb.index, method="ffill")
    h,l,pc=arb["high"],arb["low"],arb["close"].shift()
    atr=pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1).rolling(14,min_periods=1).mean()
    rsiH=rsi(arb["close"]); btc_c=btc["close"].reindex(arb.index,method="ffill")
    cl=arb["close"].values; op=arb["open"].values; hi=arb["high"].values
    e9=arb["ema9"].values; e34=arb["ema34"].values; e89=arb["ema89"].values; e200=arb["ema200"].values
    atrv=atr.values; e200h4v=e200h4.values; btcv=btc_c.values; rsiv_=rsiH.values
    trades=[]; last=-10**9; n=len(arb)
    for i in range(200,n-1):
        if i-last<4: continue
        close=cl[i]
        downtrend = e34[i]<e89[i]<e200[i]
        arb24 = (close/cl[i-24]-1)*100
        st_break = (close<e200[i]) and (close<e34[i]) and (arb24<-2)
        above_macro = close>e200h4v[i] if not np.isnan(e200h4v[i]) else False
        arb7=(close/cl[i-168]-1)*100; btc7=(btcv[i]/btcv[i-168]-1)*100; rel7=arb7-btc7
        # gate theo variant
        if variant=="V0":
            bearish_base = downtrend
            if above_macro or rel7>0: continue
        else:  # V1, V2
            if (above_macro or rel7>0) and not downtrend and not st_break: continue
            bearish_base = downtrend or st_break
        if variant=="V2":
            bearish = bearish_base and (e34[i] <= e34[i-6])
        else:
            bearish = bearish_base
        if not bearish: continue
        below9=close<e9[i]; rsval=rsiv_[i]
        touched=hi[i-1]>=e34[i-1] or hi[i]>=e34[i]; rejected=below9 and close<op[i]
        fired=None
        if touched and rejected and rsval>=45:
            fired=True
        else:
            btc24=(btcv[i]/btcv[i-24]-1)*100
            if (arb24-btc24)<=-3 and touched and close<op[i] and rsval>=42:
                fired=True
        if fired:
            sl=max(hi[i],hi[i-1])+atrv[i]*0.5; tp=close-atrv[i]*2
            if sl<=close: continue
            r=simulate_trade(arb,i+1,"SHORT",sl,tp)
            if r: trades.append({"time":str(arb.index[i]),"pnl_r":round(r[1],3)}); last=i
    return trades

def main():
    arb=load_long("ARBUSDT","1h")
    print(f"ARB {arb.index[0].date()} → {arb.index[-1].date()}, {len(arb)} nến H1")
    print("Rule B/C SHORT · SL=rejection-high+0.5ATR · TP=-2ATR · cooldown 4h\n"+"="*80)
    for v,lbl in [("V0","V0 gốc (downtrend only)"),("V1","V1 +st_break"),("V2","V2 +e34_falling (DEPLOY)")]:
        quarterly(backtest(v), lbl)
    print("="*80)
    print("Bar 'edge': exp>0 + ≥60% quý dương. Rule B/C là alert discretionary (không phải edge validate).")
    print("Quý gần = IN-SAMPLE (guard tune theo đó). Xem quý CŨ cho đánh giá out-of-sample.")

if __name__ == "__main__":
    main()
