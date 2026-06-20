"""
signal_v1.py — Paper-trade engine cho EDGE V1 (funding-short).

Edge (validated 3 năm: WR 67%, exp +0.13R, 75% quý dương):
  SHORT khi: funding ≥ 0.05% + giá rời 24h-high ≥1.5% + close < EMA9 H1.
  SL = 3×ATR(H1), TP = 2×ATR(H1).

Chạy định kỳ (cron/loop mỗi giờ):
  1. check_open_paper()  — cập nhật/đóng paper position chạm SL/TP
  2. scan_signals()      — tìm setup mới → mở paper position + Telegram [PAPER]

KHÔNG đặt lệnh thật. Chỉ log để validate forward (out-of-sample thật sự).
So sánh forward vs backtest sau 2-4 tuần.

State: research/data/paper_trades.json
"""
import os, sys, json, time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import requests
import pandas as pd
from core.binance import fetch_klines, fetch_all_funding_rates

DATA_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PAPER_FILE = os.path.join(DATA_DIR, "paper_trades.json")
CONFIG     = os.path.join(ROOT, "data", "config.json")
TZ = timezone(timedelta(hours=7))

# ── Edge v1 params (KHÓA — không tune) ───────────────────────────────────
FUNDING_MIN   = 0.05    # %
OFF_HIGH_MIN  = 0.015   # 1.5% dưới 24h high
SL_ATR        = 3.0
TP_ATR        = 2.0
COOLDOWN_H    = 24      # không re-signal cùng coin trong 24h


def _now():
    return datetime.now(TZ).isoformat()


def _load_paper():
    if os.path.exists(PAPER_FILE):
        try: return json.load(open(PAPER_FILE))
        except: return {"open": [], "closed": []}
    return {"open": [], "closed": []}


def _save_paper(p):
    json.dump(p, open(PAPER_FILE, "w"), indent=2, default=str)


def _tg(msg):
    try:
        cfg = json.load(open(CONFIG))
        token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat")
        if not token or not chat: return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=5)
    except Exception as e:
        print(f"[TG err] {e}")


def _indicators(df):
    df = df.copy()
    df["ema9"]  = df["close"].ewm(span=9, adjust=False).mean()
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14, min_periods=1).mean()
    return df


def check_open_paper(p):
    """Quét open paper positions, đóng cái chạm SL/TP bằng giá H1 mới nhất."""
    still_open = []
    for pos in p["open"]:
        try:
            df = fetch_klines(pos["symbol"], "1h", 50, force_futures=True)
            # nến từ sau entry
            entry_dt = pd.to_datetime(pos["entry_time"])
            after = df[df.index > entry_dt.tz_localize(None)]
            hit = None
            for _, r in after.iterrows():
                hi, lo = float(r["high"]), float(r["low"])
                if hi >= pos["sl"]:          # SHORT → SL ở trên
                    hit = ("LOSS", -1.0, pos["sl"]); break
                if lo <= pos["tp"]:          # SHORT → TP ở dưới
                    rr = (pos["entry"] - pos["tp"]) / (pos["sl"] - pos["entry"])
                    hit = ("WIN", round(rr, 2), pos["tp"]); break
            if hit:
                pos.update({"status": hit[0], "pnl_r": hit[1], "exit": hit[2],
                            "exit_time": _now()})
                p["closed"].append(pos)
                _tg(f"📕 [PAPER] ĐÓNG {pos['symbol']} SHORT → {hit[0]} {hit[1]:+}R "
                    f"(entry {pos['entry']} exit {hit[2]})")
            else:
                still_open.append(pos)
        except Exception as e:
            print(f"[check err] {pos['symbol']}: {e}")
            still_open.append(pos)
    p["open"] = still_open
    return p


def _recent_signal(p, symbol):
    """True nếu coin đã có signal trong COOLDOWN_H giờ (open hoặc vừa closed)."""
    cutoff = datetime.now(TZ) - timedelta(hours=COOLDOWN_H)
    for pos in p["open"] + p["closed"][-50:]:
        if pos["symbol"] == symbol:
            try:
                if pd.to_datetime(pos["entry_time"]) > cutoff:
                    return True
            except: pass
    return False


def scan_signals(p):
    """Tìm setup funding-short mới trên universe."""
    universe = [u["symbol"] for u in
                json.load(open(os.path.join(DATA_DIR, "universe.json")))["coins"]]
    fundings = fetch_all_funding_rates()   # {sym: funding_pct}
    new_count = 0
    for sym in universe:
        f = fundings.get(sym)
        if f is None or f < FUNDING_MIN:
            continue
        if _recent_signal(p, sym):
            continue
        try:
            df = _indicators(fetch_klines(sym, "1h", 60, force_futures=True))
            row = df.iloc[-1]
            close = float(row["close"]); atr = float(row["atr"]); ema9 = float(row["ema9"])
            hi24 = float(df["high"].iloc[-24:].max())
            # Điều kiện edge v1
            if hi24 <= 0 or (hi24 - close) / hi24 < OFF_HIGH_MIN:
                continue
            if not (close < ema9):
                continue
            sl = round(close + atr * SL_ATR, 8)
            tp = round(close - atr * TP_ATR, 8)
            pos = {"symbol": sym, "direction": "SHORT", "entry_time": _now(),
                   "entry": round(close, 8), "sl": sl, "tp": tp,
                   "funding": round(f, 4), "atr_pct": round(atr/close*100, 2),
                   "status": "OPEN"}
            p["open"].append(pos)
            new_count += 1
            _tg(f"📗 [PAPER] SHORT {sym}\n"
                f"Funding {f:+.4f}% | giá rời đỉnh {(hi24-close)/hi24*100:.1f}%\n"
                f"Entry {close:.6g} | SL {sl:.6g} | TP {tp:.6g} | RR 1:{TP_ATR/SL_ATR:.2f}\n"
                f"(edge v1 — paper, không tiền thật)")
            print(f"[SIGNAL] {sym} SHORT funding {f:.4f}% entry {close}")
        except Exception as e:
            print(f"[scan err] {sym}: {e}")
    return p, new_count


def stats(p):
    closed = p["closed"]
    if not closed:
        return "Chưa có paper trade đóng."
    n = len(closed); wins = sum(1 for c in closed if c.get("pnl_r",0) > 0)
    totalR = sum(c.get("pnl_r",0) for c in closed)
    return (f"Paper forward: {n} đóng | WR {wins/n*100:.0f}% | "
            f"totalR {totalR:+.2f} | exp {totalR/n:+.3f}R "
            f"(backtest: WR 67%, exp +0.13R) | {len(p['open'])} đang mở")


def main():
    p = _load_paper()
    p = check_open_paper(p)
    p, new = scan_signals(p)
    _save_paper(p)
    summary = stats(p)
    print(f"\n{_now()}")
    print(f"Signal mới: {new} | {summary}")
    if new > 0:
        _tg(f"📊 [PAPER] {summary}")


if __name__ == "__main__":
    main()
