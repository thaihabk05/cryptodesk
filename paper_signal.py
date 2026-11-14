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
from core.forward_tracker import state_file
PAPER_FILE = state_file("paper_trades_v1.json")   # Railway Volume nếu có → bền qua deploy
ALERT_FILE = state_file("arb_alerts_v1.json")     # sổ alert Rule B/C (ghi + tự chấm SL/TP)
MAX_HOLD_H = 72                                    # timeout như backtest (simulate_trade max_bars=72)
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


def _entry_utc(pos):
    """entry_time lưu ISO +07:00; index nến Binance là UTC naive → phải ĐỔI múi giờ, không chỉ bỏ tz.
    (Bản cũ tz_localize(None) làm lệch 7h: bỏ sót 7 giờ đầu sau entry khi chấm SL/TP.)"""
    t = pd.to_datetime(pos["entry_time"])
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def _resolve(pos):
    """Chấm 1 lệnh SHORT đang mở bằng nến 15m. SL xét trước (bi quan). Trả (status, pnl_r, exit) hoặc None."""
    df = fetch_klines(pos["symbol"], "15m", 300, force_futures=True)
    et = _entry_utc(pos)
    after = df[df.index >= et]
    risk = pos["sl"] - pos["entry"]
    if risk <= 0:
        return ("INVALID", 0.0, pos["entry"])
    for _, r in after.iterrows():
        if float(r["high"]) >= pos["sl"]:
            return ("LOSS", -1.0, pos["sl"])
        if float(r["low"]) <= pos["tp"]:
            return ("WIN", round((pos["entry"] - pos["tp"]) / risk, 2), pos["tp"])
    if len(after) and (after.index[-1] - et) >= pd.Timedelta(hours=MAX_HOLD_H):
        last = float(after["close"].iloc[-1])
        return ("TIMEOUT", round((pos["entry"] - last) / risk, 2), last)
    return None


def _check_open(p, label="PAPER"):
    still = []
    for pos in p["open"]:
        try:
            hit = _resolve(pos)
            if hit:
                pos.update({"status": hit[0], "pnl_r": hit[1], "exit": hit[2], "exit_time": _now()})
                p["closed"].append(pos)
                _name = f"Rule {pos['rule']}" if pos.get("rule") else "SHORT"
                _tg(f"📕 [{label}] ĐÓNG {pos['symbol']} {_name} → {hit[0]} {hit[1]:+}R "
                    f"(entry {pos['entry']:.6g} exit {hit[2]:.6g})")
            else:
                still.append(pos)
        except Exception as e:
            print(f"[{label} check err] {pos['symbol']}: {e}")
            still.append(pos)
    p["open"] = still
    return p


def _load_alerts():
    if os.path.exists(ALERT_FILE):
        try: return json.load(open(ALERT_FILE))
        except Exception: pass
    return {"open": [], "closed": []}

def _save_alerts(a):
    try: json.dump(a, open(ALERT_FILE, "w"), indent=2, default=str)
    except Exception as e: print(f"[alert save err] {e}")

def _alert_recent(a, hours=4):
    """Cooldown gộp 4h giữa các alert B/C (như backtest) — đọc từ sổ nên bền qua restart."""
    cutoff = datetime.now(TZ) - timedelta(hours=hours)
    for x in a["open"] + a["closed"][-20:]:
        try:
            if pd.to_datetime(x["entry_time"]) > cutoff: return True
        except Exception: pass
    return False

def _check_alerts():
    a = _load_alerts()
    if a["open"]:
        a = _check_open(a, label="ARB MONITOR")
        _save_alerts(a)


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
_arb_monitor_cooldown = {"B": 0, "C": 0, "BC": 0}

def _emas(df):
    for p in [9,34,89,200]: df[f"e{p}"]=df["close"].ewm(span=p,adjust=False).mean()
    d=df["close"].diff(); g=d.clip(lower=0).rolling(14,min_periods=1).mean()
    l=(-d.clip(upper=0)).rolling(14,min_periods=1).mean()
    df["rsi"]=(100-100/(1+g/l.replace(0,float('inf')))).fillna(50)
    return df

def arb_monitor():
    """Check Rule B/C trên ARB — CHỈ khi ARB thật sự downtrend.

    REGIME-GATE (thêm 9/2026): Rule B/C validate trên 3 năm ARB DOWNTREND (-74%).
    Khi ARB flip UPTREND (Robinhood Chain catalyst) → short bắn sai liên tục
    (giá 0.085 short, chạy lên 0.106). Gate tắt HẲN short-alert khi ARB:
      (1) trên EMA200 H4 = uptrend macro, HOẶC
      (2) outperform BTC 7 ngày = đang MẠNH (không phải "yếu" như Rule C giả định).
    BREAKDOWN OVERRIDE (9/2026): gate cũ quá thận trọng — chặn cả short 1 cú giảm
    hợp lệ khi ARB vẫn trên EMA200 H4 (vd -28% vừa qua). Nếu st_break (thủng EMA200
    H1 + dưới EMA34 H1 + giảm) → MỞ gate, Rule B/C bắn được trong markdown ngắn hạn.
    """
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
        arb24 = (close/float(arb["close"].iloc[-25]) - 1) * 100

        # ── REGIME GATE ────────────────────────────────────────────────
        h4 = fetch_klines("ARBUSDT", "4h", 250, force_futures=True)
        e200_h4 = float(h4["close"].ewm(span=200, adjust=False).mean().iloc[-1])
        above_macro = close > e200_h4
        btc = fetch_klines("BTCUSDT", "1h", 200, force_futures=True)
        arb7 = (close / float(arb["close"].iloc[-168]) - 1) * 100     # 168 nến H1 = 7 ngày
        btc7 = (float(btc["close"].iloc[-1]) / float(btc["close"].iloc[-168]) - 1) * 100
        rel7 = arb7 - btc7
        # BREAKDOWN NGẮN HẠN xác nhận: thủng EMA200 H1 + dưới EMA34 H1 + đang giảm.
        # Mở gate kể cả khi cấu trúc chậm (EMA200 H4/rel7) chưa lật → bắt cú phân phối/
        # markdown mà gate cũ bỏ lỡ (vd ARB -28% vừa qua vẫn trên EMA200 H4).
        st_break = (close < e200) and (close < e34) and (arb24 < -2)
        # Mở gate khi: downtrend H1 xác nhận (e34<e89<e200) HOẶC st_break — EMA200 H4
        # quá chậm, không được phép veto khi cấu trúc nhanh đã lật bearish.
        if (above_macro or rel7 > 0) and not downtrend and not st_break:
            # ARB mạnh + CHƯA gãy ngắn hạn → KHÔNG short. Cảnh báo strength bất thường.
            if rel7 >= 10 and now - _arb_monitor_cooldown.get("S", 0) > 86400:  # 1 lần/ngày
                _arb_monitor_cooldown["S"] = now
                hi7 = float(arb["high"].iloc[-168:].max())
                off7 = (hi7 - close) / hi7 * 100
                if off7 >= 6 and close < e34:
                    # Đã rời đỉnh sâu + mất EMA34: KHÔNG được gọi là "mạnh" nữa (10/11: báo MẠNH ở
                    # 0.171 khi đã -9% từ đỉnh, sau đó rơi tiếp -19%). Chỉ đổi CÂU CHỮ, gate giữ nguyên
                    # (phase9: mở gate sớm hơn chỉ thêm 11 lệnh/3 năm, exp âm → không đổi luật).
                    _tg(f"⚠️ [ARB MONITOR] ARB ĐANG ĐIỀU CHỈNH sau pump\n"
                        f"Giá {close:.5g} = {-off7:.1f}% từ đỉnh 7d {hi7:.5g}, đã mất EMA34 H1 | "
                        f"7d vẫn hơn BTC {rel7:+.1f}pp\n"
                        f"→ KHÔNG phải tín hiệu mua. Short-monitor vẫn TẮT tới khi gãy cấu trúc "
                        f"(đóng dưới EMA200 H1 ~{e200:.5g}) — lịch sử: short sớm hơn mốc này không có lợi.")
                else:
                    _tg(f"⚠️ [ARB MONITOR] ARB đang MẠNH bất thường\n"
                        f"ARB 7d {arb7:+.1f}% vs BTC {btc7:+.1f}% (hơn {rel7:+.1f}pp) | giá {close:.5g}\n"
                        f"→ ARB uptrend/outperform BTC. KHÔNG short. Có thể có tin/catalyst — check news.\n"
                        f"(short-monitor tạm TẮT tới khi ARB yếu lại)")
            print(f"[arb monitor] SKIP short — uptrend/outperform "
                  f"(aboveEMA200H4={above_macro} rel7d={rel7:+.1f}pp st_break={st_break})")
            return

        # bearish = downtrend H1 HOẶC breakdown ngắn hạn (st_break).
        # LƯU Ý: từng thử thêm guard e34_falling (10/11) để chặn 1 tín hiệu sai 01:30,
        # NHƯNG backtest 3 năm (phase7) cho thấy nó HẠI: lệnh 645→274, exp +0.153→+0.128,
        # quý+ 85%→62% → ĐÃ REVERT. Bài học: đừng tune theo 1 ví dụ đơn lẻ, phải backtest.
        bearish = downtrend or st_break
        _tag = "Breakdown ngắn hạn (thủng EMA200 H1)" if (st_break and not downtrend) else "Downtrend"

        # Rule B — bearish + hồi chạm EMA34 + rejection (RSI≥45 tránh short đáy quá bán)
        touched = float(prev["high"]) >= float(prev["e34"]) or float(row["high"]) >= e34
        rejected = below9 and close < float(row["open"])
        b_fire = bearish and touched and rejected and rsi >= 45
        # Rule C — thua BTC + short khi HỒI lên chạm EMA34 bị đẩy xuống (KHÔNG short
        # lúc đang rơi/vào đáy — đó là chỗ dễ bounce ngược, entry tệ).
        btc24 = (float(btc["close"].iloc[-1])/float(btc["close"].iloc[-25])-1)*100
        c_reject = touched and close < float(row["open"])
        c_fire = (arb24 - btc24) <= -3 and bearish and c_reject and rsi >= 42

        # ĐIỀU KIỆN B/C GIỮ NGUYÊN (phase9 11/2026: không biến thể nào tốt hơn ở cả data cũ lẫn mới).
        # Đổi: (1) B và C cùng bắn → 1 tin duy nhất; (2) GHI vào sổ để forward-tracker tự chấm SL/TP;
        # (3) cooldown gộp 4h đọc từ sổ (bền qua restart, khớp backtest).
        if b_fire or c_fire:
            al = _load_alerts()
            if not _alert_recent(al, 4) and now - _arb_monitor_cooldown.get("BC", 0) > 14400:
                _arb_monitor_cooldown["BC"] = now
                rule = "B+C" if (b_fire and c_fire) else ("B" if b_fire else "C")
                sl = max(float(row["high"]), float(prev["high"])) + atr*0.5
                tp = close - atr*2
                rr = (close - tp) / (sl - close) if sl > close else 0
                al["open"].append({"symbol": "ARBUSDT", "direction": "SHORT", "rule": rule, "tag": _tag,
                                   "entry_time": _now(), "entry": round(close, 8),
                                   "sl": round(sl, 8), "tp": round(tp, 8),
                                   "btc24": round(btc24, 2), "arb24": round(arb24, 2),
                                   "rsi": round(rsi, 1), "status": "OPEN"})
                _save_alerts(al)
                why = {"B": "Hồi chạm EMA34 rồi bị đẩy xuống",
                       "C": f"ARB 24h {arb24:+.1f}% vs BTC {btc24:+.1f}% (thua {arb24-btc24:.1f}pp) · hồi chạm EMA34 bị đẩy xuống",
                       "B+C": f"Hồi chạm EMA34 bị đẩy xuống · ARB 24h {arb24:+.1f}% vs BTC {btc24:+.1f}% (thua {arb24-btc24:.1f}pp)"}[rule]
                hist = "Lịch sử 3 năm: thắng ~51%, +0.14R/lệnh (4 tháng gần: +0.04R) → thua 1 trong 2 lệnh là BÌNH THƯỜNG."
                if rule == "B+C":
                    hist += "\nLưu ý: B+C trùng nhau KHÔNG mạnh hơn B đơn lẻ (lịch sử n=42, ~−0.1R) — đừng tăng size."
                _tg(f"🔍 [ARB MONITOR] Rule {rule} — Rejection ({_tag})\n{why}\n"
                    f"Giá {close:.5g} | gợi ý SHORT: SL ~{sl:.5g} TP ~{tp:.5g} (RR {rr:.1f})\n"
                    f"{hist}\n"
                    f"(discretionary — anh tự quyết, KHÔNG auto · kết quả sẽ tự chấm trên /paper)")
    except Exception as e:
        print(f"[arb monitor err] {e}")


def run_once():
    p = _load(); p = _check_open(p); p, new = _scan(p); _save(p)
    try: _check_alerts()   # chấm SL/TP các alert B/C đang mở
    except Exception as e: print(f"[alert check err] {e}")
    arb_monitor()   # Rule B/C — cảnh báo + ghi sổ alert (không đụng paper state Rule A)
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
