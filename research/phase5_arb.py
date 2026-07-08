"""
phase5_arb.py — Tìm quy tắc signal cho ARB riêng.

KỶ LUẬT: single-coin dễ overfit hơn cross-sectional. Nên:
1. Test edge ĐÃ VALIDATE (funding-short) trên ARB → có work không?
2. Dùng đặc tính cấu trúc ARB (beta cao, downtrend, downside asymmetry) → short-bias.
3. Report n + quarterly thật; cảnh báo nếu sample nhỏ.

Test:
  A. Funding-short (edge chung) trên ARB
  B. Trend-short: ARB dưới EMA200 H4 + bật lên EMA34/EMA9 rồi rejection → short hồi
  C. Relative weakness: ARB yếu hơn BTC (underperform) trong downtrend → short
"""
import os, numpy as np, pandas as pd
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
from phase3_walkforward import load_long, load_funding_long, asof
from backtester import simulate_trade

def st(d):
    n=len(d)
    if n==0: return None
    w=(d["pnl_r"]>0).sum()
    return dict(n=n, wr=round(w/n*100,1), totalR=round(d["pnl_r"].sum(),2), exp=round(d["pnl_r"].mean(),3))

def quarterly(trades, label):
    if not trades: print(f"[{label}] 0 lệnh"); return
    df=pd.DataFrame(trades); df["q"]=pd.to_datetime(df["time"]).dt.to_period("Q").astype(str)
    qs=sorted(df["q"].unique()); pos=sum(1 for q in qs if (st(df[df["q"]==q]) or {}).get("exp",0)>0)
    o=st(df)
    print(f"[{label:26s}] n={o['n']:>4d} WR={o['wr']:>4} exp={o['exp']:>+.3f} totR={o['totalR']:>+6.1f} | quý+ {pos}/{len(qs)}={pos/len(qs)*100:.0f}%")
    return df

def ind(df):
    df=df.copy()
    for p in [9,34,89,200]: df[f"e{p}"]=df["close"].ewm(span=p,adjust=False).mean()
    hl=df["high"]-df["low"]; hc=(df["high"]-df["close"].shift()).abs(); lc=(df["low"]-df["close"].shift()).abs()
    df["atr"]=pd.concat([hl,hc,lc],axis=1).max(axis=1).rolling(14,min_periods=1).mean()
    d=df["close"].diff(); g=d.clip(lower=0).rolling(14,min_periods=1).mean(); l=(-d.clip(upper=0)).rolling(14,min_periods=1).mean()
    df["rsi"]=(100-100/(1+g/l.replace(0,np.nan))).fillna(50)
    return df

def load_arb():
    return ind(load_long("ARBUSDT","1h"))

# ── A. Funding-short trên ARB ────────────────────────────────────────────
def test_funding(df, fund_hi=0.03):
    fdf=load_funding_long("ARBUSDT"); trades=[]; last=-10**9
    for i in range(200,len(df)-1):
        if i-last<24: continue
        ts=df.index[i]; f=asof(fdf["fundingRate"],ts)
        if f is None or float(f)<fund_hi: continue
        row=df.iloc[i]; close=float(row["close"]); atr=float(row["atr"])
        hi24=float(df["high"].iloc[i-24:i].max())
        if hi24<=0 or (hi24-close)/hi24<0.015: continue
        if not (close<float(row["e9"])): continue
        sl=close+atr*3; tp=close-atr*2
        r=simulate_trade(df,i+1,"SHORT",sl,tp)
        if not r: continue
        trades.append({"time":str(ts),"pnl_r":round(r[1],3)}); last=i
    return trades

# ── B. Trend-short: downtrend + rejection từ EMA ─────────────────────────
def test_trendshort(df):
    trades=[]; last=-10**9
    for i in range(200,len(df)-1):
        if i-last<24: continue
        row=df.iloc[i]
        # Downtrend H1: EMA34<EMA89<EMA200
        if not (float(row["e34"])<float(row["e89"])<float(row["e200"])): continue
        # Giá hồi lên chạm/vượt EMA34 rồi đóng dưới lại = rejection
        prev=df.iloc[i-1]; close=float(row["close"]); atr=float(row["atr"])
        touched = float(prev["high"])>=float(prev["e34"]) or float(row["high"])>=float(row["e34"])
        rejected = close<float(row["e9"]) and close<float(row["open"])
        if not (touched and rejected): continue
        if float(row["rsi"])<45: continue  # đừng short khi đã quá bán
        sl=max(float(row["high"]),float(prev["high"]))+atr*0.5; tp=close-atr*2
        if sl<=close: continue
        r=simulate_trade(df,i+1,"SHORT",sl,tp)
        if not r: continue
        trades.append({"time":str(df.index[i]),"pnl_r":round(r[1],3)}); last=i
    return trades

# ── C. Relative weakness vs BTC ──────────────────────────────────────────
def test_relweak(df):
    btc=ind(load_long("BTCUSDT","1h"))
    # align
    arb_ret24=df["close"].pct_change(24)*100
    btc_ret24=btc["close"].pct_change(24).reindex(df.index)*100
    trades=[]; last=-10**9
    for i in range(200,len(df)-1):
        if i-last<24: continue
        row=df.iloc[i]
        # ARB underperform BTC ≥3pp trong 24h + đang downtrend H1
        a=arb_ret24.iloc[i]; b=btc_ret24.iloc[i]
        if pd.isna(a) or pd.isna(b): continue
        if a - b > -3: continue                       # cần ARB yếu hơn BTC ≥3pp
        if float(row["e34"])>=float(row["e89"]): continue  # cần downtrend
        close=float(row["close"]); atr=float(row["atr"])
        if not (close<float(row["e9"])): continue
        sl=close+atr*3; tp=close-atr*2
        r=simulate_trade(df,i+1,"SHORT",sl,tp)
        if not r: continue
        trades.append({"time":str(df.index[i]),"pnl_r":round(r[1],3)}); last=i
    return trades


def main():
    df=load_arb()
    print(f"ARB {df.index[0].date()} → {df.index[-1].date()}, {len(df)} nến H1\n")
    print("="*74)
    quarterly(test_funding(df,0.03), "A. Funding-short 0.03%")
    quarterly(test_funding(df,0.02), "A. Funding-short 0.02%")
    quarterly(test_trendshort(df),   "B. Trend-short (rejection EMA)")
    quarterly(test_relweak(df),      "C. Rel-weak vs BTC (short)")
    print("="*74)


if __name__ == "__main__":
    main()
