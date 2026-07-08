"""
paper_signal.py — Paper-trade engine EDGE V1 (funding-short). Deployable, chạy 24/7 trên Railway.

Edge (validated backtest 3 năm). Ngưỡng validate 0.03%: 1193 lệnh, WR 62%, exp +0.051R,
80% quý dương. Ngưỡng tiền-thật 0.05%: WR 67%, exp +0.13R, 75% quý.
  SHORT khi: funding ≥ FUNDING_MIN + giá rời 24h-high ≥1.5% + close < EMA9 H1.
  SL = 3×ATR(H1), TP = 2×ATR(H1).

KHÔNG đặt lệnh thật — chỉ log + Telegram [PAPER] để validate forward (out-of-sample).
Tách hoàn toàn khỏi engine cũ. State: data/paper_trades_v1.json (alert Telegram là record chính,
nên redeploy mất state json vẫn không sao).
"""
import os, json, time, threading
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
from core.binance import fetch_klines, fetch_all_funding_rates

ROOT       = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(ROOT, "data")
PAPER_FILE = os.path.join(DATA_DIR, "paper_trades_v1.json")
UNIVERSE   = os.path.join(DATA_DIR, "universe_v1.json")
CONFIG     = os.path.join(DATA_DIR, "config.json")
TZ = timezone(timedelta(hours=7))

# ── Edge v1 params ───────────────────────────────────────────────────────
# FUNDING_MIN 0.03 cho GIAI ĐOẠN VALIDATE (nhiều data nhanh hơn; backtest 3 năm
# ngưỡng 0.03: 1193 lệnh, WR 62%, exp +0.051R, 80% quý dương).
# Tiền thật sau này → về 0.05 (sắc nhất: WR 67%, exp +0.13R).
FUNDING_MIN  = 0.03
OFF_HIGH_MIN = 0.015
SL_ATR       = 3.0
TP_ATR       = 2.0
COOLDOWN_H   = 24
SCAN_INTERVAL_SEC = 3600   # mỗi giờ


def _now(): return datetime.now(TZ).isoformat()

def _load():
    if os.path.exists(PAPER_FILE):
        try: return json.load(open(PAPER_FILE))
        except: pass
    return {"open": [], "closed": []}

def _save(p):
    try: json.dump(p, open(PAPER_FILE, "w"), indent=2, default=str)
    except Exception as e: print(f"[paper save err] {e}")

def _tg(msg):
    try:
        cfg = json.load(open(CONFIG))
        token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat")
        if token and chat:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": msg}, timeout=5)
    except Exception as e: print(f"[paper tg err] {e}")

def _indicators(df):
    df = df.copy()
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    df["atr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14, min_periods=1).mean()
    return df


def _check_open(p):
    still = []
    for pos in p["open"]:
        try:
            df = fetch_klines(pos["symbol"], "1h", 50, force_futures=True)
            entry_dt = pd.to_datetime(pos["entry_time"]).tz_localize(None)
            after = df[df.index > entry_dt]
            hit = None
            for _, r in after.iterrows():
                if float(r["high"]) >= pos["sl"]:
                    hit = ("LOSS", -1.0, pos["sl"]); break
                if float(r["low"]) <= pos["tp"]:
                    rr = (pos["entry"] - pos["tp"]) / (pos["sl"] - pos["entry"])
                    hit = ("WIN", round(rr, 2), pos["tp"]); break
            if hit:
                pos.update({"status": hit[0], "pnl_r": hit[1], "exit": hit[2], "exit_time": _now()})
                p["closed"].append(pos)
                _tg(f"📕 [PAPER] ĐÓNG {pos['symbol']} SHORT → {hit[0]} {hit[1]:+}R "
                    f"(entry {pos['entry']:.6g} exit {hit[2]:.6g})")
            else:
                still.append(pos)
        except Exception as e:
            print(f"[paper check err] {pos['symbol']}: {e}")
            still.append(pos)
    p["open"] = still
    return p


def _recent(p, sym):
    cutoff = datetime.now(TZ) - timedelta(hours=COOLDOWN_H)
    for pos in p["open"] + p["closed"][-50:]:
        if pos["symbol"] == sym:
            try:
                if pd.to_datetime(pos["entry_time"]) > cutoff: return True
            except: pass
    return False


def _scan(p):
    try:
        universe = [u["symbol"] for u in json.load(open(UNIVERSE))["coins"]]
    except Exception as e:
        print(f"[paper universe err] {e}"); return p, 0
    fundings = fetch_all_funding_rates()
    new = 0
    for sym in universe:
        f = fundings.get(sym)
        if f is None or f < FUNDING_MIN or _recent(p, sym):
            continue
        try:
            df = _indicators(fetch_klines(sym, "1h", 60, force_futures=True))
            row = df.iloc[-1]
            close = float(row["close"]); atr = float(row["atr"]); ema9 = float(row["ema9"])
            hi24 = float(df["high"].iloc[-24:].max())
            if hi24 <= 0 or (hi24 - close) / hi24 < OFF_HIGH_MIN: continue
            if not (close < ema9): continue
            sl = round(close + atr*SL_ATR, 8); tp = round(close - atr*TP_ATR, 8)
            p["open"].append({"symbol": sym, "direction": "SHORT", "entry_time": _now(),
                              "entry": round(close,8), "sl": sl, "tp": tp,
                              "funding": round(f,4), "status": "OPEN"})
            new += 1
            _tg(f"📗 [PAPER] SHORT {sym}\nFunding {f:+.4f}% | rời đỉnh {(hi24-close)/hi24*100:.1f}%\n"
                f"Entry {close:.6g} | SL {sl:.6g} | TP {tp:.6g}\n(edge v1 — paper, không tiền thật)")
            print(f"[PAPER SIGNAL] {sym} funding {f:.4f}%")
        except Exception as e:
            print(f"[paper scan err] {sym}: {e}")
    return p, new


def _stats(p):
    c = p["closed"]
    if not c: return f"0 đóng / {len(p['open'])} mở"
    n=len(c); w=sum(1 for x in c if x.get("pnl_r",0)>0); tot=sum(x.get("pnl_r",0) for x in c)
    return f"{n} đóng | WR {w/n*100:.0f}% | totalR {tot:+.2f} | exp {tot/n:+.3f}R | {len(p['open'])} mở"


# ── ARB monitor — Rule B/C (chỉ CẢNH BÁO, KHÔNG auto paper-trade) ─────────
# Rule B/C single-coin dễ overfit → không tự đặt lệnh, chỉ báo để anh tự quyết.
_arb_monitor_cooldown = {"B": 0, "C": 0}

def _emas(df):
    for p in [9,34,89,200]: df[f"e{p}"]=df["close"].ewm(span=p,adjust=False).mean()
    d=df["close"].diff(); g=d.clip(lower=0).rolling(14,min_periods=1).mean()
    l=(-d.clip(upper=0)).rolling(14,min_periods=1).mean()
    df["rsi"]=(100-100/(1+g/l.replace(0,float('inf')))).fillna(50)
    return df

def arb_monitor():
    """Check Rule B (trend rejection) + C (rel-weak vs BTC) trên ARB. Alert nếu có setup."""
    import time as _t
    now = _t.time()
    try:
        arb = _emas(fetch_klines("ARBUSDT", "1h", 220, force_futures=True))
        row = arb.iloc[-1]; prev = arb.iloc[-2]
        close = float(row["close"]); atr_col = (arb["high"]-arb["low"]).rolling(14,min_periods=1).mean()
        atr = float(atr_col.iloc[-1])
        e9,e34,e89,e200 = [float(row[f"e{p}"]) for p in [9,34,89,200]]
        rsi = float(row["rsi"])
        downtrend = e34 < e89 < e200
        below9 = close < e9

        # Rule B — downtrend + hồi chạm EMA34 + rejection
        touched = float(prev["high"]) >= float(prev["e34"]) or float(row["high"]) >= e34
        rejected = below9 and close < float(row["open"])
        if downtrend and touched and rejected and rsi >= 45:
            if now - _arb_monitor_cooldown["B"] > 14400:  # cooldown 4h
                _arb_monitor_cooldown["B"] = now
                sl = max(float(row["high"]), float(prev["high"])) + atr*0.5
                _tg(f"🔍 [ARB MONITOR] Rule B — Trend rejection\n"
                    f"Downtrend + hồi chạm EMA34 rồi bị đẩy xuống\n"
                    f"Giá {close:.5g} | gợi ý SHORT: SL ~{sl:.5g} TP ~{close-atr*2:.5g}\n"
                    f"(discretionary — anh tự quyết, KHÔNG auto)")

        # Rule C — ARB underperform BTC ≥3pp/24h + downtrend
        btc = fetch_klines("BTCUSDT", "1h", 30, force_futures=True)
        arb24 = (close/float(arb["close"].iloc[-25])-1)*100
        btc24 = (float(btc["close"].iloc[-1])/float(btc["close"].iloc[-25])-1)*100
        if (arb24 - btc24) <= -3 and (e34 < e89) and below9:
            if now - _arb_monitor_cooldown["C"] > 14400:
                _arb_monitor_cooldown["C"] = now
                _tg(f"🔍 [ARB MONITOR] Rule C — Yếu hơn BTC\n"
                    f"ARB 24h {arb24:+.1f}% vs BTC {btc24:+.1f}% (thua {arb24-btc24:.1f}pp) + downtrend\n"
                    f"Giá {close:.5g} | gợi ý SHORT: SL ~{close+atr*3:.5g} TP ~{close-atr*2:.5g}\n"
                    f"(discretionary — anh tự quyết, KHÔNG auto)")
    except Exception as e:
        print(f"[arb monitor err] {e}")


def run_once():
    p = _load(); p = _check_open(p); p, new = _scan(p); _save(p)
    arb_monitor()   # Rule B/C — chỉ cảnh báo, không đụng paper state
    print(f"[PAPER {_now()}] signal mới: {new} | {_stats(p)}")
    return new


def paper_signal_loop():
    """Daemon thread — chạy mỗi giờ. Hook từ main.py."""
    print("[PAPER] Edge v1 paper-trade loop started")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[PAPER LOOP ERR] {e}")
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    run_once()
