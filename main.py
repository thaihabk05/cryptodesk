"""main.py — Entry point. Chạy: python main.py"""
import json, os, threading, time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory, make_response

from core.utils import NumpyJSONProvider
from dashboard.fam_engine import fam_analyze
from scanner.scan_engine import run_full_scan, scan_state

# ── App ───────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.json_provider_class = NumpyJSONProvider
app.json = NumpyJSONProvider(app)

@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r

# ── Config ────────────────────────────────────
DATA_DIR     = Path("data")
CONFIG_FILE  = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "symbols":          ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "interval_minutes": 30,
    "telegram_token":   os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat":    os.getenv("TELEGRAM_CHAT_ID", ""),
    "alert_confidence": "MEDIUM",   # MEDIUM | HIGH | ALL
    "alert_rr":         1.5,        # min R:R để gửi alert
    "rr_ratio":         1.5,        # min R:R để hiện signal trên Dashboard
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            text = CONFIG_FILE.read_text().strip()
            if text:
                cfg = json.loads(text)
                # Merge với DEFAULT để không thiếu key mới
                merged = DEFAULT_CONFIG.copy()
                merged.update(cfg)
                return merged
        except (json.JSONDecodeError, Exception):
            pass  # File corrupt → dùng default
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []

def save_history(h):
    HISTORY_FILE.write_text(json.dumps(h[-200:], indent=2))

# ── Background auto-scanner (Dashboard) ───────
scan_results = {}
scan_lock    = threading.Lock()
scanner_running = False
scanner_status  = {"is_scanning": False, "last_scan": None,
                   "next_scan": None, "scan_count": 0}

def _is_duplicate_signal(result: dict, history: list, window_hours: int = 2) -> bool:
    """
    Kiểm tra signal có phải duplicate không.
    Duplicate = cùng symbol + direction + entry price tương tự (±1%) trong window_hours.
    """
    cutoff = time.time() - window_hours * 3600
    sym    = result.get("symbol", "")
    dirr   = result.get("direction", "")
    entry  = float(result.get("entry", 0) or 0)

    for h in history[-100:]:
        try:
            ts = datetime.fromisoformat(h.get("time", "")).timestamp()
            if ts < cutoff:
                continue
            if h.get("symbol") != sym or h.get("direction") != dirr:
                continue
            # Entry price tương tự ±1%
            prev_entry = float(h.get("entry", 0) or 0)
            if prev_entry > 0 and abs(entry - prev_entry) / prev_entry <= 0.01:
                return True  # duplicate
        except:
            continue
    return False


def _save_signal_to_history(result: dict):
    """Lưu signal vào history — dedup chặt theo symbol+direction+entry±1% trong 2 giờ."""
    history = load_history()
    if _is_duplicate_signal(result, history, window_hours=2):
        print(f"[DEDUP] Skip {result.get('symbol')} {result.get('direction')} — duplicate trong 2h")
        return
    history.append({
        "time":       result.get("timestamp", datetime.now().isoformat()),
        "symbol":     result.get("symbol", ""),
        "direction":  result.get("direction", ""),
        "confidence": result.get("confidence", ""),
        "price":      result.get("price", 0),
        "entry":      result.get("entry", 0),
        "sl":         result.get("sl", 0),
        "sl_pct":     result.get("sl_pct", 0),
        "tp1":        result.get("tp1", 0),
        "tp1_pct":    result.get("tp1_pct", 0),
        "tp2":        result.get("tp2", 0),
        "rr":         result.get("rr", 0),
        "d1_bias":    result.get("d1", {}).get("bias", ""),
        "h4_bias":    result.get("h4", {}).get("bias", ""),
        "score":      result.get("score", 0),
        "verdict":    result.get("entry_verdict", "WAIT"),
    })
    save_history(history)


def _send_high_alert(result: dict, token: str, chat_id: str):
    """Gửi Telegram alert cho tín hiệu HIGH — plain text, không Markdown."""
    verdict   = result.get("entry_verdict", "WAIT")
    sym       = result.get("symbol", "?")
    dirr      = result.get("direction", "?")
    dir_emoji = "🟢" if dirr == "LONG" else "🔴"

    if verdict == "GO":
        verdict_line = "✅ DA SAN SANG VAO LENH"
    elif verdict == "NO":
        verdict_line = "🔴 CHUA NEN VAO — cho setup ro hon"
    else:
        verdict_line = "🟡 CHO THEM TIN HIEU"

    mk          = result.get("market", {})
    funding_str = str(mk.get("funding_pct", "N/A"))
    oi_str      = str(mk.get("oi_str", "N/A"))
    atr_val     = mk.get("atr_ratio", "?")
    atr_str     = str(atr_val) + "x"

    checklist   = result.get("entry_checklist", [])
    check_lines = ""
    for c in checklist[:6]:
        icon = "OK" if c.get("ok") is True else "XX" if c.get("ok") is False else "--"
        check_lines += "  " + icon + " " + str(c.get("text", "")) + chr(10)

    lines = [
        dir_emoji + " " + sym + " — " + dirr + " | HIGH",
        verdict_line,
        "--------------------",
        "Price: " + str(result.get("price","")) + " | R:R 1:" + str(result.get("rr","")),
        "Entry: " + str(result.get("entry","")),
        "SL: " + str(result.get("sl","")) + " (-" + str(result.get("sl_pct","")) + "%)",
        "TP1: " + str(result.get("tp1","")) + " (+" + str(result.get("tp1_pct","")) + "%) | TP2: " + str(result.get("tp2","")),
        "--------------------",
        "D1: " + str(result.get("d1",{}).get("bias","")) + " | H4: " + str(result.get("h4",{}).get("bias","")),
        "Funding: " + funding_str + " | OI: " + oi_str + " | ATR: " + atr_str,
        "--------------------",
        "Checklist:",
        check_lines.rstrip(),
    ]
    msg = chr(10).join(lines)
    send_telegram(token, chat_id, msg)


def dashboard_scan_cycle(cfg):
    """Scan các symbol trong watchlist Dashboard."""
    for sym in cfg["symbols"]:
        try:
            result = fam_analyze(sym, cfg)
            with scan_lock:
                scan_results[sym] = result
        except Exception as e:
            with scan_lock:
                scan_results[sym] = {"symbol": sym, "error": str(e)}


def market_scan_cycle(cfg):
    """Scan toàn bộ thị trường futures — chạy song song với dashboard scan.
    Chỉ alert và lưu history với tín hiệu HIGH.
    """
    from scanner.scan_engine import run_full_scan, scan_state as msc_state
    token   = cfg.get("telegram_token", "")
    chat_id = cfg.get("telegram_chat", "")
    min_rr  = float(cfg.get("rr_ratio", 1.0))

    print("[MARKET SCAN] Bắt đầu quét toàn thị trường futures...")
    run_full_scan(min_vol=5_000_000, max_workers=3)

    # Đợi scan xong
    import time as _time
    timeout = 300
    elapsed = 0
    while msc_state["running"] and elapsed < timeout:
        _time.sleep(2); elapsed += 2

    results = msc_state.get("results", [])
    high_signals = [r for r in results if r.get("confidence") == "HIGH"
                    and r.get("direction") in ("LONG","SHORT")
                    and r.get("rr", 0) >= min_rr]

    print(f"[MARKET SCAN] Xong — {len(results)} signals, {len(high_signals)} HIGH")

    for result in high_signals:
        # Lưu history
        _save_signal_to_history(result)
        # Gửi Telegram
        if token and chat_id:
            try:
                _send_high_alert(result, token, chat_id)
            except Exception as e:
                print(f"[TELEGRAM ERROR] {result.get('symbol')}: {e}")

def dashboard_scanner_loop():
    global scanner_running, scanner_status
    while scanner_running:
        cfg = load_config()
        interval_sec = cfg.get("interval_minutes", 30) * 60
        scanner_status["is_scanning"] = True
        scanner_status["last_scan"]   = datetime.now().isoformat()

        # 1. Scan watchlist Dashboard (symbols cụ thể)
        dashboard_scan_cycle(cfg)

        # 2. Scan toàn thị trường futures — alert + lưu history cho HIGH
        try:
            market_scan_cycle(cfg)
        except Exception as e:
            print(f"[MARKET SCAN ERROR] {e}")

        scanner_status["is_scanning"] = False
        scanner_status["scan_count"] += 1
        scanner_status["next_scan"]   = datetime.fromtimestamp(
            time.time() + interval_sec).isoformat()
        elapsed = 0
        while elapsed < interval_sec and scanner_running:
            time.sleep(5); elapsed += 5

# ── API — Frontend (static) ───────────────────
@app.route("/")
def index():
    resp = make_response(send_from_directory("static", "index.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ── API — Dashboard ───────────────────────────
@app.route("/api/config", methods=["GET", "POST"])
def config_api():
    if request.method == "POST":
        cfg = load_config(); cfg.update(request.json); save_config(cfg)
        return jsonify({"ok": True})
    return jsonify(load_config())

@app.route("/api/scan")
def scan_now():
    cfg = load_config(); out = []
    for sym in cfg["symbols"]:
        try:
            r = fam_analyze(sym, cfg)
            with scan_lock: scan_results[sym] = r
            out.append(r)
        except Exception as e:
            out.append({"symbol": sym, "error": str(e)})
    return jsonify(out)

@app.route("/api/symbol/<symbol>")
def symbol_detail(symbol):
    cfg = load_config()
    try:    return jsonify(fam_analyze(symbol, cfg))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/results")
def get_results():
    with scan_lock: return jsonify(list(scan_results.values()))

@app.route("/api/history")
def get_history():
    return jsonify(load_history())

@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    save_history([])
    return jsonify({"ok": True})


# ── Backtest ──────────────────────────────────
def backtest_signal(signal: dict) -> dict:
    """
    Fetch H1 candles sau thời điểm signal, kiểm tra SL hay TP1 chạm trước.
    Return dict với result: WIN / LOSS / OPEN, candles_to_result, pnl_r
    """
    from core.binance import fetch_klines
    import pandas as pd

    symbol    = signal["symbol"]
    direction = signal["direction"]
    entry     = float(signal["entry"])
    sl        = float(signal["sl"])
    tp1       = float(signal["tp1"])
    sig_time  = signal["time"]  # ISO string

    try:
        sig_ts = pd.Timestamp(sig_time).timestamp()
    except Exception:
        return {**signal, "bt_result": "ERROR", "bt_note": "Invalid timestamp",
                "bt_candles": None, "bt_pnl_r": None, "bt_exit_price": None}

    try:
        # Lấy 200 nến H1 gần nhất — đủ để cover hầu hết trades
        df = fetch_klines(symbol, "1h", 200)
        # Index là DatetimeIndex (open_time) — convert sang timestamp số
        df = df.copy()
        df["ts"] = df.index.astype("int64") // 10**9  # nanoseconds → seconds

        # Chỉ xét nến SAU thời điểm signal
        df_after = df[df["ts"] > sig_ts].reset_index(drop=True)

        if len(df_after) == 0:
            # Fallback: thử dùng nến mới nhất để tính unrealized
            last_price   = float(df["close"].iloc[-1])
            if direction == "LONG":
                unrealized = round((last_price - entry) / entry * 100, 2)
            else:
                unrealized = round((entry - last_price) / entry * 100, 2)
            sl_pct_val   = float(signal.get("sl_pct", 2))
            unrealized_r = round(unrealized / sl_pct_val, 2) if sl_pct_val > 0 else None
            return {**signal, "bt_result": "OPEN",
                    "bt_note": f"Dùng giá mới nhất {round(last_price,6)} ({unrealized:+.2f}%)",
                    "bt_candles": 0,
                    "bt_pnl_r": None,
                    "bt_unrealized_pct": unrealized,
                    "bt_unrealized_r":   unrealized_r,
                    "bt_exit_price": round(last_price, 6)}

        sl_pct  = float(signal.get("sl_pct", 2))
        tp1_pct = float(signal.get("tp1_pct", 3))

        for i, row in df_after.iterrows():
            high = float(row["high"])
            low  = float(row["low"])

            if direction == "LONG":
                hit_sl  = low  <= sl
                hit_tp1 = high >= tp1
            else:  # SHORT
                hit_sl  = high >= sl
                hit_tp1 = low  <= tp1

            if hit_tp1 and hit_sl:
                # Cùng nến — assume TP trước nếu giá đi đúng hướng
                result     = "WIN"
                exit_price = tp1
                pnl_r      = round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0
            elif hit_tp1:
                result     = "WIN"
                exit_price = tp1
                pnl_r      = round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0
            elif hit_sl:
                result     = "LOSS"
                exit_price = sl
                pnl_r      = -1.0
            else:
                continue

            return {**signal,
                    "bt_result":     result,
                    "bt_note":       f"Chạm {'TP1' if result=='WIN' else 'SL'} sau {i+1} nến H1",
                    "bt_candles":    i + 1,
                    "bt_pnl_r":      pnl_r,
                    "bt_exit_price": round(exit_price, 6)}

        # Chưa chạm SL/TP
        last_price = float(df_after["close"].iloc[-1])
        if direction == "LONG":
            unrealized = round((last_price - entry) / entry * 100, 2)
        else:
            unrealized = round((entry - last_price) / entry * 100, 2)

        sl_pct_val  = float(signal.get("sl_pct", 2))
        unrealized_r = round(unrealized / sl_pct_val, 2) if sl_pct_val > 0 else None
        return {**signal,
                "bt_result":       "OPEN",
                "bt_note":         f"Chưa chạm SL/TP — giá hiện tại {round(last_price,6)} ({unrealized:+.2f}%)",
                "bt_candles":      len(df_after),
                "bt_pnl_r":        None,
                "bt_unrealized_pct": round(unrealized, 2),
                "bt_unrealized_r":   unrealized_r,
                "bt_exit_price":   round(last_price, 6)}

    except Exception as e:
        return {**signal, "bt_result": "ERROR", "bt_note": str(e),
                "bt_candles": None, "bt_pnl_r": None, "bt_exit_price": None}


@app.route("/api/backtest", methods=["POST"])
def run_backtest():
    """Backtest tất cả HIGH signals trong history."""
    data       = request.json or {}
    conf_filter = data.get("confidence", "HIGH")  # HIGH | ALL
    dir_filter  = data.get("direction",  "ALL")   # LONG | SHORT | ALL

    history = load_history()
    if not history:
        return jsonify({"results": [], "summary": {}, "error": "Không có history"})

    # Filter
    signals = [h for h in history
               if (conf_filter == "ALL" or h.get("confidence") == conf_filter)
               and (dir_filter == "ALL" or h.get("direction") == dir_filter)]

    if not signals:
        return jsonify({"results": [], "summary": {}, "error": "Không có signal phù hợp"})

    # Backtest từng signal
    results = []
    for sig in signals:
        r = backtest_signal(sig)
        results.append(r)

    # Summary
    wins   = [r for r in results if r["bt_result"] == "WIN"]
    losses = [r for r in results if r["bt_result"] == "LOSS"]
    opens  = [r for r in results if r["bt_result"] == "OPEN"]
    errors = [r for r in results if r["bt_result"] == "ERROR"]

    closed = wins + losses
    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0

    pnl_rs    = [r["bt_pnl_r"] for r in closed if r["bt_pnl_r"] is not None]
    total_r   = round(sum(pnl_rs), 2)
    avg_r     = round(sum(pnl_rs) / len(pnl_rs), 2) if pnl_rs else 0
    avg_candles_win  = round(sum(r["bt_candles"] for r in wins  if r["bt_candles"]) / len(wins),  1) if wins  else None
    avg_candles_loss = round(sum(r["bt_candles"] for r in losses if r["bt_candles"]) / len(losses), 1) if losses else None

    # Tính expectancy = (winrate * avg_win_r) + (lossrate * (-1))
    avg_win_r  = round(sum(r["bt_pnl_r"] for r in wins   if r["bt_pnl_r"]) / len(wins),   2) if wins   else 0
    avg_loss_r = round(sum(r["bt_pnl_r"] for r in losses if r["bt_pnl_r"]) / len(losses), 2) if losses else 0
    expectancy = round((len(wins)/len(closed)) * avg_win_r + (len(losses)/len(closed)) * avg_loss_r, 2) if closed else 0

    summary = {
        "total":            len(signals),
        "closed":           len(closed),
        "wins":             len(wins),
        "losses":           len(losses),
        "opens":            len(opens),
        "errors":           len(errors),
        "win_rate":         win_rate,
        "total_r":          total_r,
        "avg_r":            avg_r,
        "avg_win_r":        avg_win_r,
        "avg_loss_r":       avg_loss_r,
        "expectancy":       expectancy,
        "avg_candles_win":  avg_candles_win,
        "avg_candles_loss": avg_candles_loss,
    }

    return jsonify({"results": results, "summary": summary})

@app.route("/api/scanner/start", methods=["POST"])
def start_dashboard_scanner():
    global scanner_running
    if not scanner_running:
        scanner_running = True
        threading.Thread(target=dashboard_scanner_loop, daemon=True).start()
    return jsonify({"running": True})

@app.route("/api/scanner/stop", methods=["POST"])
def stop_dashboard_scanner():
    global scanner_running
    scanner_running = False
    return jsonify({"running": False})

@app.route("/api/scanner/status")
def dashboard_scanner_status():
    return jsonify({"running": scanner_running, **scanner_status})

# ── API — Market Scanner ──────────────────────
@app.route("/api/market-scan/start", methods=["POST"])
def market_scan_start():
    # Luôn cho phép manual scan — cancel auto scan nếu đang chạy
    data    = request.json or {}
    min_vol = float(data.get("min_vol", 10_000_000))
    # Reset state nếu đang chạy từ auto cycle
    if scan_state["running"]:
        scan_state["running"] = False
        import time as _t; _t.sleep(0.5)
    threading.Thread(target=run_full_scan, args=(min_vol,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/market-scan/status")
def market_scan_status():
    return jsonify({
        "running":    scan_state["running"],
        "progress":   scan_state["progress"],
        "total":      scan_state["total"],
        "found":      len(scan_state["results"]),
        "started_at": scan_state["started_at"],
        "finished_at":scan_state["finished_at"],
        "error":      scan_state["error"],
    })

@app.route("/api/market-scan/results")
def market_scan_results():
    return jsonify(scan_state["results"])

# ── Telegram ─────────────────────────────────
def send_telegram(token, chat_id, msg):
    if not token or not chat_id: return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=5)
        return r.status_code == 200
    except: return False

@app.route("/api/telegram/test", methods=["POST"])
def telegram_test():
    data   = request.json or {}
    token  = data.get("token", "")
    chat_id= data.get("chat_id", "")
    msg    = "✅ *CryptoDesk* — Kết nối Telegram thành công!\nAlerts sẽ được gửi vào đây."
    ok = send_telegram(token, chat_id, msg)
    if ok: return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Gửi thất bại — kiểm tra Token và Chat ID"}), 400


# ── Auto-start scanner khi app khởi động ──────
def auto_start_scanner():
    """Tự động start scanner sau khi app ready — không cần user bấm tay."""
    global scanner_running
    cfg = load_config()
    # Chỉ auto-start nếu config có symbols hoặc đây là production (Railway)
    if not scanner_running:
        scanner_running = True
        threading.Thread(target=dashboard_scanner_loop, daemon=True).start()
        print("[AUTO-START] Dashboard scanner started automatically")

# Chạy auto-start trong thread riêng để không block gunicorn worker
threading.Thread(target=auto_start_scanner, daemon=True).start()


# ── Run ───────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 CryptoDesk running at http://127.0.0.1:{port}\n")
    print("   Tab 1: Dashboard — theo dõi mã cụ thể")
    print("   Tab 2: Market Scan — quét toàn thị trường\n")
    app.run(debug=False, host="0.0.0.0", port=port)
