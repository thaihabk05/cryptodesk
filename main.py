"""main.py — Entry point. Chạy: python main.py"""
import json, os, threading, time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory, make_response

from core.utils import NumpyJSONProvider
from dashboard.fam_engine import fam_analyze
from dashboard.swing_h1_engine import swing_h1_analyze
from dashboard.scalp_engine import scalp_analyze
from scanner.scan_engine import run_full_scan, scan_state

def _local_isoformat() -> str:
    """Trả về timestamp ISO có timezone +07:00 để frontend parse đúng."""
    from datetime import timezone, timedelta
    tz_vn = timezone(timedelta(hours=7))
    return datetime.now(tz_vn).isoformat()

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
DATA_DIR       = Path("data")
CONFIG_FILE    = DATA_DIR / "config.json"
HISTORY_FILE   = DATA_DIR / "history.json"
POSITIONS_FILE = DATA_DIR / "positions.json"

# ── Algorithm Version — tăng mỗi khi thay đổi filter/threshold ──
ALGO_VERSION = "v2.0"   # v2.0: PATCH A-I + RR≥1.5 + SL 2% (2026-03-05)
ALGO_DATE    = "2026-03-05"

DATA_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "symbols":          ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "watchlist_algos":  {},
    "scan_modes":       ["TREND"],
    "range_override":   {},
    "interval_minutes": 5,   # mặc định 5 phút — có thể đổi trong config
    "telegram_token":   os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat":    os.getenv("TELEGRAM_CHAT_ID", ""),
    "alert_confidence": "MEDIUM",   # MEDIUM | HIGH | ALL
    "alert_rr":         1.5,        # min R:R để gửi alert
    "rr_ratio":         1.5,        # min R:R để hiện signal trên Dashboard
    "strategy":         "SWING_H4",  # SWING_H4 | SWING_H1 | SCALP
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

def load_positions():
    """Load danh sách position đã phân tích từ file."""
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            return []
    return []

def save_positions(positions: list):
    """Lưu tối đa 50 positions gần nhất."""
    POSITIONS_FILE.write_text(json.dumps(positions[:50], indent=2))

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


def _fetch_vol_safe(symbol: str) -> float:
    """Fetch volume 24h không throw exception."""
    try:
        return fetch_volume_24h(symbol) if symbol else 0.0
    except Exception:
        return 0.0


def _save_signal_to_history(result: dict):
    """Lưu signal vào history — dedup chặt theo symbol+direction+entry±1% trong 2 giờ."""
    history = load_history()
    if _is_duplicate_signal(result, history, window_hours=2):
        print(f"[DEDUP] Skip {result.get('symbol')} {result.get('direction')} — duplicate trong 2h")
        return
    history.append({
        "time":            result.get("timestamp", datetime.now().isoformat()),
        "symbol":          result.get("symbol", ""),
        "direction":       result.get("direction", ""),
        "confidence":      result.get("confidence", ""),
        "price":           result.get("price", 0),
        "entry":           result.get("entry", 0),
        "sl":              result.get("sl", 0),
        "sl_pct":          result.get("sl_pct", 0),
        "tp1":             result.get("tp1", 0),
        "tp1_pct":         result.get("tp1_pct", 0),
        "tp2":             result.get("tp2", 0),
        "rr":              result.get("rr", 0),
        "d1_bias":         result.get("d1", {}).get("bias", "") or result.get("d1_bias", ""),
        "h4_bias":         result.get("h4", {}).get("bias", "") or result.get("h4_bias", ""),
        "score":           result.get("score", 0),
        "verdict":         result.get("entry_verdict", "WAIT"),
        "entry_verdict":   result.get("entry_verdict", "WAIT"),
        "volume_24h":      result.get("volume_24h") or
                           _fetch_vol_safe(result.get("symbol","")),
        "strategy":        result.get("strategy", "SWING_H4"),
        "conditions":      result.get("conditions", []),
        "warnings":        result.get("warnings", []),
        "entry_checklist": result.get("entry_checklist", []),
        "market":          result.get("market", {}),
        "btc_context":     result.get("btc_context", {}),
        "d1":              result.get("d1", {}),
        "h4":              result.get("h4", {}),
        "h1":              result.get("h1", {}),
        # ── Feature vector cho AI Analysis ──
        "oi_change":       result.get("market", {}).get("oi_change"),
        "funding":         result.get("market", {}).get("funding"),
        "atr_x":           result.get("market", {}).get("atr"),
        "btc_sentiment":   result.get("btc_context", {}).get("sentiment", ""),
        "btc_d1_trend":    result.get("btc_context", {}).get("d1_trend", ""),
        "num_conditions":  len(result.get("conditions", [])),
        "num_warnings":    len(result.get("warnings", [])),
        # ── Algorithm version tracking ──
        "algo_version":    ALGO_VERSION,
        "algo_date":       ALGO_DATE,
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

    # Strategy label
    strat_raw = result.get("strategy", "SWING_H4")
    strat_map = {
        "SWING_H4": "Swing H4/D1", "SWING_H1": "Swing H1",
        "SCALP": "Scalp M15", "RANGE_SCALP": "Range Scalp"
    }
    algo_tag    = result.get("algo", "")
    strat_label = strat_map.get(strat_raw, strat_raw)
    if algo_tag and algo_tag != strat_raw:
        strat_label = strat_map.get(algo_tag, algo_tag)

    lines = [
        dir_emoji + " " + sym + " — " + dirr + " | HIGH",
        verdict_line,
        "--------------------",
        "Chien luoc: " + strat_label,
        "Price: " + str(result.get("price","")) + " | R:R 1:" + str(result.get("rr","")),
        "Entry: " + str(result.get("entry","")),
        "SL: " + str(result.get("sl","")) + " (-" + str(result.get("sl_pct","")) + "%)",
        "TP1: " + str(result.get("tp1","")) + " (+" + str(result.get("tp1_pct","")) + "%) | TP2: " + str(result.get("tp2","")),
        "--------------------",
        "D1: " + str(result.get("d1_bias") or result.get("d1",{}).get("bias","?")) +
        " | H4: " + str(result.get("h4_bias") or result.get("h4",{}).get("bias","?")),
        "Funding: " + funding_str + " | OI: " + oi_str + " | ATR: " + atr_str,
        "--------------------",
        "Checklist:",
        check_lines.rstrip(),
    ]

    # Thêm warnings nếu có (quan trọng để anh biết tại sao)
    warns = result.get("warnings", [])
    if warns:
        lines.append("--------------------")
        lines.append("Canh bao:")
        for w in warns[:3]:
            lines.append("  " + str(w))
    msg = chr(10).join(lines)
    send_telegram(token, chat_id, msg)



def get_analyze_fn(cfg):
    """Trả về engine function phù hợp với strategy được chọn."""
    strategy = cfg.get("strategy", "SWING_H4")
    if strategy == "SWING_H1":  return swing_h1_analyze
    if strategy == "SCALP":     return scalp_analyze
    return fam_analyze  # mặc định SWING_H4


def _check_watchlist_alert(sym: str, result: dict, cfg: dict, algo_key: str):
    """Alert Telegram khi mã watchlist đang đúng điểm entry."""
    token   = cfg.get("telegram_token", "")
    chat_id = cfg.get("telegram_chat", "")
    if not token or not chat_id:
        return

    direction  = result.get("direction", "WAIT")
    confidence = result.get("confidence", "LOW")
    rr         = result.get("rr", 0) or 0
    verdict    = result.get("entry_verdict", "WAIT")

    # Chỉ alert khi: có direction + confidence HIGH/MEDIUM + RR đạt + verdict GO/WAIT
    if direction not in ("LONG", "SHORT"):
        return
    if confidence not in ("HIGH", "MEDIUM"):
        return
    if rr < 1.5:
        return

    # Tránh spam — dùng cooldown key
    import time
    cooldown_key = f"{sym}_{direction}_{algo_key}"
    now = time.time()
    last_alert = _watchlist_alert_cooldown.get(cooldown_key, 0)
    if now - last_alert < 3600:  # cooldown 1 tiếng
        return
    _watchlist_alert_cooldown[cooldown_key] = now

    strat_labels = {
        "TREND": "Trend " + cfg.get("strategy", "SWING_H4"),
        "RANGE_SCALP": "Range Scalp",
        "SWING_H4": "Swing H4/D1",
        "SWING_H1": "Swing H1",
        "SCALP": "Scalp M15",
    }
    algo_label = strat_labels.get(algo_key, algo_key)
    dir_emoji  = "🟢" if direction == "LONG" else "🔴"
    mk         = result.get("market", {})

    lines = [
        f"📌 [WATCHLIST] {sym}",
        f"{dir_emoji} {direction} | {confidence} | [{algo_label}]",
        "--------------------",
    ]

    # Range scalp — thêm info range
    if algo_key == "RANGE_SCALP":
        pos = result.get("position_in_range", "")
        rh  = result.get("range_high", "")
        rl  = result.get("range_low", "")
        lines.append(f"Gia cham {pos} | Range: {rl} - {rh}")

    lines += [
        f"Price: {result.get('price','')} | R:R 1:{rr}",
        f"Entry: {result.get('entry','')}",
        f"SL: {result.get('sl','')} (-{result.get('sl_pct','')}%)",
        f"TP1: {result.get('tp1','')} (+{result.get('tp1_pct','')}%)",
        "--------------------",
        f"Funding: {mk.get('funding_pct','N/A')} | OI: {mk.get('oi_str','N/A')}",
    ]
    if verdict == "GO":
        lines.append("✅ DA SAN SANG VAO LENH")
    else:
        lines.append("🟡 CHO THEM XAC NHAN")

    send_telegram(token, chat_id, chr(10).join(lines))


_watchlist_alert_cooldown = {}


def dashboard_scan_cycle(cfg):
    """Scan các symbol trong watchlist — dùng đúng algo đã gắn cho từng mã."""
    from dashboard.fam_engine      import fam_analyze
    from dashboard.swing_h1_engine import swing_h1_analyze
    from dashboard.scalp_engine    import scalp_analyze
    from dashboard.range_engine    import range_analyze

    algo_map = {
        "TREND":       get_analyze_fn(cfg),
        "RANGE_SCALP": range_analyze,
        "SWING_H4":    fam_analyze,
        "SWING_H1":    swing_h1_analyze,
        "SCALP":       scalp_analyze,
    }
    watchlist_algos = cfg.get("watchlist_algos", {})

    for sym in cfg["symbols"]:
        try:
            algo_key  = watchlist_algos.get(sym, "TREND")
            engine_fn = algo_map.get(algo_key, get_analyze_fn(cfg))
            result    = engine_fn(sym, {**cfg, "force_futures": True})
            result["algo"] = algo_key
            with scan_lock:
                scan_results[sym] = result

            # Watchlist alert: chỉ alert khi đúng điểm entry
            _check_watchlist_alert(sym, result, cfg, algo_key)

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
    run_full_scan(min_vol=cfg.get("min_vol_scan", 5_000_000), max_workers=3, strategy=cfg.get("strategy","SWING_H4"), scan_modes=cfg.get("scan_modes",["TREND"]))

    # Đợi scan xong
    import time as _time
    timeout = 300
    elapsed = 0
    while msc_state["running"] and elapsed < timeout:
        _time.sleep(2); elapsed += 2

    results = msc_state.get("results", [])
    high_signals = [r for r in results if r.get("confidence") == "HIGH"
                    and r.get("direction") in ("LONG","SHORT")
                    and r.get("rr", 0) >= min_rr
                    and r.get("entry_verdict") == "GO"]  # Chỉ gửi khi sẵn sàng vào lệnh

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
        try:
            cfg = load_config()
            interval_sec = cfg.get("interval_minutes", 30) * 60
            scan_start_ts = time.time()

            scanner_status["is_scanning"] = True
            scanner_status["scan_start"]  = _local_isoformat()  # khi BẮT ĐẦU

            # 1. Scan watchlist Dashboard (symbols cụ thể)
            try:
                dashboard_scan_cycle(cfg)
            except Exception as e:
                print(f"[DASHBOARD SCAN ERROR] {e}")

            # 2. Scan toàn thị trường futures — alert + lưu history cho HIGH
            try:
                market_scan_cycle(cfg)
            except Exception as e:
                print(f"[MARKET SCAN ERROR] {e}")

            scan_duration = round(time.time() - scan_start_ts)
            scanner_status["is_scanning"]  = False
            scanner_status["scan_count"]  += 1
            scanner_status["last_scan"]    = _local_isoformat()  # khi HOÀN THÀNH
            scanner_status["scan_duration"] = scan_duration      # thời gian scan thực tế (giây)
            scanner_status["next_scan"]     = datetime.fromtimestamp(
                time.time() + interval_sec).isoformat()

            elapsed = 0
            while elapsed < interval_sec and scanner_running:
                time.sleep(5); elapsed += 5
        except Exception as e:
            # Catch-all: loop không bao giờ chết dù có lỗi bất ngờ
            print(f"[SCANNER LOOP ERROR] {e} — tiếp tục sau 60s")
            scanner_status["is_scanning"] = False
            time.sleep(60)

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
            r = get_analyze_fn(cfg)(sym, cfg)
            with scan_lock: scan_results[sym] = r
            out.append(r)
        except Exception as e:
            out.append({"symbol": sym, "error": str(e)})
    return jsonify(out)

@app.route("/api/symbol/<symbol>")
def symbol_detail(symbol):
    cfg = load_config()
    try:    return jsonify(get_analyze_fn(cfg)(symbol, cfg))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/results")
def get_results():
    with scan_lock: return jsonify(list(scan_results.values()))

@app.route("/api/history")
def get_history():
    from datetime import datetime
    history = load_history()
    # Optional filter theo algo_version
    ver_filter = request.args.get("algo_version")
    if ver_filter:
        history = [h for h in history if h.get("algo_version") == ver_filter]
    # Sort mới nhất lên đầu theo timestamp
    def _ts(h):
        try:
            return datetime.fromisoformat(h.get("time","").replace("Z",""))
        except Exception:
            return datetime.min
    history.sort(key=_ts, reverse=True)
    return jsonify(history)

@app.route("/api/history/versions")
def get_history_versions():
    """Trả về danh sách các algo_version có trong history và số lượng signal."""
    history = load_history()
    versions = {}
    for h in history:
        v = h.get("algo_version", "legacy")
        d = h.get("algo_date", "unknown")
        key = f"{v} ({d})"
        versions[key] = versions.get(key, 0) + 1
    return jsonify({"versions": versions, "current": ALGO_VERSION})

@app.route("/api/history/add", methods=["POST"])
def history_add():
    """Thêm thủ công 1 signal vào history từ popup."""
    sig = request.json or {}
    if not sig.get("symbol"):
        return jsonify({"ok": False, "error": "Thiếu symbol"})
    _save_signal_to_history(sig)
    return jsonify({"ok": True})

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
    from core.binance import fetch_klines, fetch_volume_24h
    import pandas as pd

    symbol    = signal["symbol"]
    direction = signal["direction"]

    # Bug fix 1: Skip WAIT signal — không có entry thực tế
    if direction == "WAIT":
        return {**signal, "bt_result": "SKIP", "bt_note": "Direction=WAIT — không có lệnh thực tế",
                "bt_candles": None, "bt_pnl_r": None, "bt_exit_price": None}
    entry     = float(signal["entry"])
    sl        = float(signal["sl"])
    tp1       = float(signal["tp1"])
    sig_time  = signal["time"]  # ISO string

    try:
        # Parse timestamp — normalize về UTC để so sánh đúng với Binance klines
        ts_parsed = pd.Timestamp(sig_time)
        if ts_parsed.tzinfo is None:
            # Naive timestamp — giả sử là UTC+7 (VN local)
            ts_parsed = ts_parsed.tz_localize("Asia/Ho_Chi_Minh")
        sig_ts = ts_parsed.tz_convert("UTC").timestamp()
    except Exception:
        return {**signal, "bt_result": "ERROR", "bt_note": "Invalid timestamp",
                "bt_candles": None, "bt_pnl_r": None, "bt_exit_price": None}

    try:
        # Tính số nến cần fetch: từ lúc signal đến hiện tại (H1) + buffer 24h
        from datetime import timezone as _tz
        now_ts    = datetime.now(_tz.utc).timestamp()
        hours_ago = max(48, int((now_ts - sig_ts) / 3600) + 24)
        limit     = min(hours_ago, 500)  # tối đa 500 nến H1 (~20 ngày)

        df = fetch_klines(symbol, "1h", limit, force_futures=True)
        # Index là DatetimeIndex (open_time) — convert sang timestamp số
        df = df.copy()
        df["ts"] = df.index.astype("int64") // 10**9  # nanoseconds → seconds

        # Chỉ xét nến SAU thời điểm signal
        df_after = df[df["ts"] > sig_ts].reset_index(drop=True)

        # Khai báo sớm để dùng trong mọi nhánh (bao gồm fallback)
        sl_pct  = float(signal.get("sl_pct", 2))
        tp1_pct = float(signal.get("tp1_pct", 3))

        if len(df_after) == 0:
            # Fallback: thử dùng nến mới nhất để tính unrealized
            last_price   = float(df["close"].iloc[-1])

            # ── Kiểm tra SL/TP trước khi tính unrealized ──
            # Nếu giá đã vượt SL → trả về LOSS tại SL, không tính unrealized oan
            if direction == "LONG":
                if last_price <= sl:
                    return {**signal, "bt_result": "LOSS",
                            "bt_note": f"Giá {round(last_price,6)} đã dưới SL {sl} — cắt lỗ tại SL",
                            "bt_candles": 0, "bt_pnl_r": -1.0,
                            "bt_exit_price": round(sl, 6)}
                if last_price >= tp1:
                    return {**signal, "bt_result": "WIN",
                            "bt_note": f"Giá {round(last_price,6)} đã trên TP1 {tp1}",
                            "bt_candles": 0,
                            "bt_pnl_r": round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0,
                            "bt_exit_price": round(tp1, 6)}
            else:
                if last_price >= sl:
                    return {**signal, "bt_result": "LOSS",
                            "bt_note": f"Giá {round(last_price,6)} đã trên SL {sl} — cắt lỗ tại SL",
                            "bt_candles": 0, "bt_pnl_r": -1.0,
                            "bt_exit_price": round(sl, 6)}
                if last_price <= tp1:
                    return {**signal, "bt_result": "WIN",
                            "bt_note": f"Giá {round(last_price,6)} đã dưới TP1 {tp1}",
                            "bt_candles": 0,
                            "bt_pnl_r": round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0,
                            "bt_exit_price": round(tp1, 6)}

            # Chưa chạm SL/TP → tính unrealized thực tế (cap tại SL)
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

        # ── Bước 1: Kiểm tra lệnh có được khớp không ──
        # Signal entry thường là giá thị trường lúc phát, nhưng có thể là Limit
        # → phải xác nhận giá SAU signal có chạm entry hay không
        # Nếu entry == price (market order) → coi như đã khớp ngay
        sig_price     = float(signal.get("price", entry))
        is_limit_long  = direction == "LONG"  and entry < sig_price * 0.999
        is_limit_short = direction == "SHORT" and entry > sig_price * 1.001
        is_limit       = is_limit_long or is_limit_short

        entry_filled   = not is_limit  # market order → đã khớp ngay
        entry_fill_idx = None          # nến nào giá chạm entry

        for i, row in df_after.iterrows():
            high = float(row["high"])
            low  = float(row["low"])

            # Nếu là Limit, chờ giá chạm entry trước
            if not entry_filled:
                if direction == "LONG"  and low  <= entry:
                    entry_filled   = True
                    entry_fill_idx = i
                elif direction == "SHORT" and high >= entry:
                    entry_filled   = True
                    entry_fill_idx = i
                else:
                    continue  # chưa khớp → bỏ qua nến này

            # ── Bước 2: Đã khớp entry → check TP/SL ──
            if direction == "LONG":
                hit_sl  = low  <= sl
                hit_tp1 = high >= tp1
            else:
                hit_sl  = high >= sl
                hit_tp1 = low  <= tp1

            if hit_tp1 and hit_sl:
                # Cùng nến — giả định TP trước (conservative)
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

            fill_note = f" (khớp nến {entry_fill_idx+1})" if entry_fill_idx is not None else ""
            return {**signal,
                    "bt_result":     result,
                    "bt_note":       f"Chạm {'TP1' if result=='WIN' else 'SL'} sau {i+1} nến H1{fill_note}",
                    "bt_candles":    i + 1,
                    "bt_pnl_r":      pnl_r,
                    "bt_exit_price": round(exit_price, 6)}

        # ── Bước 3: Hết dữ liệu, chưa kết quả ──
        if not entry_filled:
            # Lệnh Limit chưa được khớp lần nào
            last_price = float(df_after["close"].iloc[-1])
            dist_pct   = round((last_price - entry) / entry * 100, 2) if direction == "LONG" \
                         else round((entry - last_price) / entry * 100, 2)
            return {**signal,
                    "bt_result":         "PENDING",
                    "bt_note":           f"Chưa khớp lệnh — giá hiện tại {round(last_price,6)}, entry {entry} chưa được chạm",
                    "bt_candles":        len(df_after),
                    "bt_pnl_r":          None,
                    "bt_unrealized_pct": dist_pct,
                    "bt_unrealized_r":   None,
                    "bt_exit_price":     round(last_price, 6)}

        # Đã khớp nhưng chưa chạm SL/TP — check lần cuối bằng close giá mới nhất
        last_price = float(df_after["close"].iloc[-1])
        last_high  = float(df_after["high"].iloc[-1])
        last_low   = float(df_after["low"].iloc[-1])

        # Safety check: nếu giá hiện tại đã vượt TP1 hoặc SL → force result
        if direction == "LONG":
            if last_high >= tp1:
                return {**signal, "bt_result": "WIN",
                        "bt_note": f"TP1 chạm (close check) — giá {round(last_price,6)} vượt TP1 {tp1}",
                        "bt_candles": len(df_after), "bt_pnl_r": round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0,
                        "bt_exit_price": round(tp1, 6)}
            if last_low <= sl:
                return {**signal, "bt_result": "LOSS",
                        "bt_note": f"SL chạm (close check) — giá {round(last_price,6)} xuống SL {sl}",
                        "bt_candles": len(df_after), "bt_pnl_r": -1.0,
                        "bt_exit_price": round(sl, 6)}
        else:
            if last_low <= tp1:
                return {**signal, "bt_result": "WIN",
                        "bt_note": f"TP1 chạm (close check) — giá {round(last_price,6)} xuống TP1 {tp1}",
                        "bt_candles": len(df_after), "bt_pnl_r": round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0,
                        "bt_exit_price": round(tp1, 6)}
            if last_high >= sl:
                return {**signal, "bt_result": "LOSS",
                        "bt_note": f"SL chạm (close check) — giá {round(last_price,6)} vượt SL {sl}",
                        "bt_candles": len(df_after), "bt_pnl_r": -1.0,
                        "bt_exit_price": round(sl, 6)}

        if direction == "LONG":
            unrealized = round((last_price - entry) / entry * 100, 2)
        else:
            unrealized = round((entry - last_price) / entry * 100, 2)

        sl_pct_val   = float(signal.get("sl_pct", 2))
        unrealized_r = round(unrealized / sl_pct_val, 2) if sl_pct_val > 0 else None
        return {**signal,
                "bt_result":         "OPEN",
                "bt_note":           f"Đã khớp, chưa chạm SL/TP — giá hiện tại {round(last_price,6)} ({unrealized:+.2f}%)",
                "bt_candles":        len(df_after),
                "bt_pnl_r":          None,
                "bt_unrealized_pct": round(unrealized, 2),
                "bt_unrealized_r":   unrealized_r,
                "bt_exit_price":     round(last_price, 6)}

    except Exception as e:
        return {**signal, "bt_result": "ERROR", "bt_note": str(e),
                "bt_candles": None, "bt_pnl_r": None, "bt_exit_price": None}





@app.route("/api/positions", methods=["GET"])
def get_positions():
    """Lấy danh sách positions đã lưu."""
    return jsonify(load_positions())

@app.route("/api/positions", methods=["POST"])
def save_position_entry():
    """Lưu 1 position vào DB sau khi phân tích."""
    data = request.json or {}
    if not data.get("entry"):
        return jsonify({"error": "Thiếu entry"}), 400

    positions = load_positions()

    # Tránh duplicate: cùng entry + direction + symbol
    dup = next((p for p in positions
                if p.get("entry") == data.get("entry")
                and p.get("direction") == data.get("direction")
                and p.get("symbol") == data.get("symbol")), None)
    if dup:
        # Update timestamp nếu đã tồn tại
        dup["saved_at"] = _local_isoformat()
        save_positions(positions)
        return jsonify({"status": "updated", "id": dup["id"]})

    # Thêm mới
    import time as _t
    new_pos = {
        "id":         int(_t.time() * 1000),
        "saved_at":   _local_isoformat(),
        "direction":  data.get("direction", "LONG"),
        "entry":      data.get("entry"),
        "base_mode":  data.get("base_mode", "pct"),
        "base_value": data.get("base_value", 2.0),
        "margin":     data.get("margin", 0),
        "leverage":   data.get("leverage", 1),
        "symbol":     data.get("symbol", ""),
    }
    positions.insert(0, new_pos)
    save_positions(positions)
    return jsonify({"status": "saved", "id": new_pos["id"]})

@app.route("/api/positions/<int:pos_id>", methods=["DELETE"])
def delete_position(pos_id):
    """Xóa 1 position khỏi DB."""
    positions = load_positions()
    positions = [p for p in positions if p.get("id") != pos_id]
    save_positions(positions)
    return jsonify({"status": "deleted"})

@app.route("/api/positions/clear", methods=["POST"])
def clear_positions():
    """Xóa toàn bộ positions."""
    save_positions([])
    return jsonify({"status": "cleared"})

@app.route("/api/position/analyze", methods=["POST"])
def position_analyze():
    """
    Real-time position monitor: Fibo TP + BTC context.
    Input: { direction, entry, margin, leverage, symbol, base_mode, base_value }
    base_mode: 'pct' (% tự nhập), 'atr' (ATR H1 tự động), 'sl' (nhập SL thủ công)
    """
    try:
        from core.binance import fetch_btc_context, fetch_klines, fetch_funding_rate
        from core.indicators import prepare

        def _fmt(v):
            if v is None: return None
            n = abs(v)
            d = 8 if n < 0.000001 else 6 if n < 0.0001 else 5 if n < 0.01 else 4 if n < 1 else 2
            return round(v, d)

        data       = request.json or {}
        direction  = data.get("direction", "LONG").upper()
        entry      = float(data.get("entry", 0))
        margin     = float(data.get("margin", 0))
        leverage   = float(data.get("leverage", 1))
        symbol     = data.get("symbol", "").upper().strip()
        base_mode  = data.get("base_mode", "pct")   # 'pct' | 'atr' | 'sl'
        base_value = data.get("base_value", 2.0)     # % hoặc SL price

        if not entry:
            return jsonify({"error": "Thiếu entry price"}), 400

        is_long  = direction == "LONG"
        pos_size = margin * leverage

        # ── Tính base leg cho Fibo ──
        base_leg  = 0
        base_note = ""
        atr_value = None

        if base_mode == "sl":
            sl_val = float(base_value)
            if sl_val <= 0:
                return jsonify({"error": "SL price không hợp lệ"}), 400
            if is_long and sl_val >= entry:
                return jsonify({"error": f"LONG: SL ({sl_val}) phải nhỏ hơn Entry ({entry})"}), 400
            if not is_long and sl_val <= entry:
                return jsonify({"error": f"SHORT: SL ({sl_val}) phải lớn hơn Entry ({entry})"}), 400
            base_leg  = abs(entry - sl_val)
            sl_pct_val = round(base_leg / entry * 100, 2)
            base_note = f"SL = ${_fmt(sl_val)} ({sl_pct_val}% từ entry)"

        elif base_mode == "atr":
            try:
                sym_for_atr = symbol if symbol else "BTCUSDT"
                df_atr = prepare(fetch_klines(sym_for_atr, "1h", 30, force_futures=True))
                atr_value = float(df_atr["atr"].iloc[-1]) if "atr" in df_atr.columns else None
                if atr_value and atr_value > 0:
                    base_leg  = atr_value
                    atr_pct   = round(atr_value / entry * 100, 2)
                    base_note = f"ATR H1 = ${_fmt(atr_value)} ({atr_pct}% từ entry)"
                else:
                    base_leg  = entry * 0.02
                    base_note = "ATR không lấy được, dùng 2% mặc định"
            except Exception as e:
                base_leg  = entry * 0.02
                base_note = f"ATR lỗi ({str(e)[:40]}), dùng 2%"

        else:  # pct
            pct = float(base_value)
            if pct <= 0 or pct > 20:
                return jsonify({"error": f"% base phải từ 0.1 đến 20 (nhận được: {pct}). Nhập đúng % — ví dụ: 2 cho 2%"}), 400
            base_leg  = entry * pct / 100
            base_note = f"Base {pct}% từ entry"

        # Sanity check: base_leg không được quá 50% của entry
        if base_leg <= 0:
            return jsonify({"error": "Base leg = 0, kiểm tra lại SL hoặc % nhập vào"}), 400
        if base_leg > entry * 0.5:
            return jsonify({"error": f"Base leg ({_fmt(base_leg)}) quá lớn so với entry ({_fmt(entry)}). Kiểm tra lại % hoặc SL"}), 400

        sl_implied = (entry - base_leg) if is_long else (entry + base_leg)
        sl_pct     = round(base_leg / entry * 100, 4)
        risk_usd   = round(pos_size * sl_pct / 100, 2)
        liq        = round(entry * (1 - 0.9/leverage), 6) if is_long else round(entry * (1 + 0.9/leverage), 6)

        # ── Fibo TP levels ──
        mults  = [0.618, 1.0, 1.618, 2.618, 4.236]
        labels = ["TP0 — Fibo 0.618", "TP1 — Fibo 1.0", "TP2 — Fibo 1.618", "TP3 — Fibo 2.618", "TP4 — Fibo 4.236"]
        hints  = ["scalp nhanh (30%)", "an toàn (40–60%)", "lý tưởng (30%)", "aggressive (10%)", "nếu breakout mạnh"]
        tps = []
        for idx, mult in enumerate(mults):
            if is_long:
                tp_price = entry + base_leg * mult
                pct_v    = round(base_leg * mult / entry * 100, 4)   # dương
            else:
                tp_price = entry - base_leg * mult
                pct_v    = -round(base_leg * mult / entry * 100, 4)  # âm (SHORT đi xuống)
            pnl = round(pos_size * abs(pct_v) / 100, 2) if margin > 0 else None  # P&L luôn dương (lãi)
            tps.append({
                "label":      labels[idx],
                "hint":       hints[idx],
                "price":      _fmt(tp_price),
                "pct":        pct_v,           # âm cho SHORT, dương cho LONG
                "pct_abs":    round(abs(pct_v), 4),
                "rr":         round(mult, 3),
                "pnl":        pnl,
                "fibo":       mult,
                "is_valid":   tp_price > 0,
            })

        # ── BTC context real-time ──
        btc           = fetch_btc_context()
        btc_sentiment = btc.get("sentiment", "UNKNOWN")
        btc_price     = btc.get("price")
        btc_chg_24h   = btc.get("chg_24h", 0) or 0
        btc_d1        = btc.get("d1_trend", "N/A")
        btc_h4        = btc.get("h4_trend", "N/A")

        # BTC H1 slope
        try:
            df_h1      = fetch_klines("BTCUSDT", "1h", 12)
            h1_now     = float(df_h1["close"].iloc[-1])
            h1_4h_ago  = float(df_h1["close"].iloc[-4])
            btc_h1_slope = round((h1_now - h1_4h_ago) / h1_4h_ago * 100, 3)
            btc_h1_candles = []
            for _, row in df_h1.tail(4).iterrows():
                o, cl = float(row["open"]), float(row["close"])
                btc_h1_candles.append({"dir": "green" if cl >= o else "red",
                                        "pct": round((cl - o) / o * 100, 3)})
        except Exception:
            btc_h1_slope   = 0
            btc_h1_candles = []

        # Funding
        funding = None
        if symbol:
            try:
                funding = fetch_funding_rate(symbol)
            except Exception:
                pass

        # ── Risk score & signals ──
        signals    = []
        risk_score = 0

        if is_long:
            if btc_sentiment in ("PUMP", "RISK_ON"):
                signals.append({"type":"ok",   "msg":f"BTC {btc_sentiment} — thuận LONG, có thể hold đến TP2"})
                risk_score -= 1
            elif btc_sentiment in ("DUMP", "RISK_OFF"):
                signals.append({"type":"warn", "msg":f"BTC {btc_sentiment} — nguy hiểm LONG, cân nhắc chốt sớm"})
                risk_score += 2
            else:
                signals.append({"type":"info", "msg":"BTC sideways — chốt tại TP1 là an toàn"})
            if btc_h1_slope < -0.5:
                signals.append({"type":"warn", "msg":f"BTC H1 giảm {btc_h1_slope}%/4h — áp lực bán"})
                risk_score += 1
            elif btc_h1_slope > 0.5:
                signals.append({"type":"ok",   "msg":f"BTC H1 tăng +{btc_h1_slope}%/4h — hỗ trợ LONG"})
                risk_score -= 1
        else:
            if btc_sentiment in ("DUMP", "RISK_OFF"):
                signals.append({"type":"ok",   "msg":f"BTC {btc_sentiment} — thuận SHORT, có thể hold đến TP2"})
                risk_score -= 1
            elif btc_sentiment in ("PUMP", "RISK_ON"):
                signals.append({"type":"warn", "msg":f"BTC {btc_sentiment} — nguy hiểm SHORT, cân nhắc chốt sớm"})
                risk_score += 2
            if btc_h1_slope > 0.5:
                signals.append({"type":"warn", "msg":f"BTC H1 tăng +{btc_h1_slope}%/4h — áp lực SHORT"})
                risk_score += 1
            elif btc_h1_slope < -0.5:
                signals.append({"type":"ok",   "msg":f"BTC H1 giảm {btc_h1_slope}%/4h — hỗ trợ SHORT"})
                risk_score -= 1

        if funding is not None:
            if is_long and funding > 0.05:
                signals.append({"type":"warn", "msg":f"Funding +{funding:.4f}% cao — Long đang trả phí"})
                risk_score += 1
            elif is_long and funding < -0.02:
                signals.append({"type":"ok",   "msg":f"Funding {funding:.4f}% âm — Long được nhận phí"})
                risk_score -= 1
            elif not is_long and funding < -0.05:
                signals.append({"type":"warn", "msg":f"Funding {funding:.4f}% âm — Short đang trả phí"})
                risk_score += 1

        if abs(btc_chg_24h) > 5:
            lbl = "tăng mạnh" if btc_chg_24h > 0 else "giảm mạnh"
            signals.append({"type":"info", "msg":f"BTC {lbl} {btc_chg_24h:+.2f}%/24h — volatility cao"})

        if risk_score >= 2:
            rec, rec_detail, rec_color = "CHỐT SỚM",       "Rủi ro cao — nên chốt tại TP1 hoặc thoát một phần ngay", "danger"
        elif risk_score == 1:
            rec, rec_detail, rec_color = "THẬN TRỌNG",     "Chốt 60–70% tại TP1, dời SL về break-even",              "warning"
        elif risk_score <= -1:
            rec, rec_detail, rec_color = "HOLD ĐƯỢC",      "Momentum thuận lợi — có thể hold đến TP2 (Fibo 1.618)",  "success"
        else:
            rec, rec_detail, rec_color = "THEO KẾ HOẠCH", "Chốt 60% TP1, dời SL về break-even, giữ 40% đến TP2",   "info"

        return jsonify({
            "direction": direction, "entry": entry,
            "sl_implied": _fmt(sl_implied), "sl_pct": sl_pct,
            "margin": margin, "leverage": leverage,
            "pos_size": round(pos_size, 2), "risk_usd": risk_usd,
            "liq_approx": _fmt(liq), "symbol": symbol,
            "base_mode": base_mode, "base_note": base_note,
            "atr_value": _fmt(atr_value),
            "tps": tps,
            "btc": {
                "price": btc_price, "chg_24h": btc_chg_24h,
                "sentiment": btc_sentiment, "note": btc.get("note",""),
                "d1_trend": btc_d1, "h4_trend": btc_h4,
                "h1_slope": btc_h1_slope, "h1_candles": btc_h1_candles,
            },
            "funding": funding,
            "signals": signals,
            "recommendation": rec, "rec_detail": rec_detail, "rec_color": rec_color,
            "generated_at": _local_isoformat(),
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500



@app.route("/api/btc/trend")
def btc_trend():
    """
    Phân tích trend BTC 3 khung: ngắn hạn (H1/H4), trung hạn (D1), dài hạn (W1).
    Dùng MA34/89/200 + slope để xác định bias Long/Short cho altcoin.
    """
    from core.binance import fetch_klines
    from core.indicators import prepare
    import numpy as np

    def _bias(df, label):
        if len(df) < 10:
            return {"bias": "NEUTRAL", "detail": "Không đủ data"}
        r    = df.iloc[-1]
        prev = df.iloc[-5]
        price  = float(r["close"])
        ma34   = float(r.get("ma34",  0) or 0)
        ma89   = float(r.get("ma89",  0) or 0)
        ma200  = float(r.get("ma200", 0) or 0)
        ma34_p = float(prev.get("ma34", ma34) or ma34)
        slope34 = (ma34 - ma34_p) / ma34_p * 100 if ma34_p > 0 else 0

        # ATR ratio
        atr_mean = float(df.iloc[-20:]["atr"].mean()) if "atr" in df.columns else 0
        atr_last = float(r.get("atr", 0) or 0)
        atr_r    = round(atr_last / atr_mean, 2) if atr_mean > 0 else 1

        score = 0
        signs = []
        if ma34 > 0 and price > ma34:   score += 1; signs.append("Giá > MA34")
        else:                             score -= 1; signs.append("Giá < MA34")
        if ma89 > 0 and ma34 > ma89:    score += 1; signs.append("MA34 > MA89")
        else:                             score -= 1; signs.append("MA34 < MA89")
        if ma200 > 0 and price > ma200:  score += 1; signs.append("Giá > MA200")
        else:                             score -= 1; signs.append("Giá < MA200")
        if slope34 > 0.2:               score += 1; signs.append(f"MA34 dốc lên +{slope34:.2f}%")
        elif slope34 < -0.2:             score -= 1; signs.append(f"MA34 dốc xuống {slope34:.2f}%")

        if score >= 3:    bias = "UPTREND"
        elif score >= 1:  bias = "BULLISH"
        elif score <= -3: bias = "DOWNTREND"
        elif score <= -1: bias = "BEARISH"
        else:             bias = "NEUTRAL"

        # Chốt lời gợi ý
        if bias in ("UPTREND","BULLISH"):
            alt_action = "LONG altcoin"
            emoji = "🟢"
        elif bias in ("DOWNTREND","BEARISH"):
            alt_action = "SHORT altcoin hoặc đứng ngoài"
            emoji = "🔴"
        else:
            alt_action = "Range scalp, tránh trend"
            emoji = "🟡"

        return {
            "label":      label,
            "bias":       bias,
            "emoji":      emoji,
            "score":      score,
            "price":      round(price, 2),
            "ma34":       round(ma34, 2) if ma34 else None,
            "ma89":       round(ma89, 2) if ma89 else None,
            "ma200":      round(ma200, 2) if ma200 else None,
            "slope34":    round(slope34, 3),
            "atr_ratio":  atr_r,
            "signs":      signs,
            "alt_action": alt_action,
        }

    try:
        ff   = False
        d_h1  = prepare(fetch_klines("BTCUSDT", "1h",  60, force_futures=ff))
        d_h4  = prepare(fetch_klines("BTCUSDT", "4h",  60, force_futures=ff))
        d_d1  = prepare(fetch_klines("BTCUSDT", "1d",  60, force_futures=ff))
        d_w1  = prepare(fetch_klines("BTCUSDT", "1w",  30, force_futures=ff))

        short   = _bias(d_h4,  "Ngắn hạn (H4)")
        medium  = _bias(d_d1,  "Trung hạn (D1)")
        longterm= _bias(d_w1,  "Dài hạn (W1)")

        # Tổng hợp: dùng để gợi ý bias altcoin
        scores = [short["score"], medium["score"], longterm["score"]]
        avg_score = sum(scores) / len(scores)
        if avg_score >= 2:    overall = "LONG altcoin mạnh"
        elif avg_score >= 0.5:overall = "Ưu tiên LONG, cẩn thận SHORT"
        elif avg_score <= -2: overall = "SHORT altcoin / đứng ngoài"
        elif avg_score <= -0.5:overall = "Cẩn thận LONG, có thể SHORT"
        else:                  overall = "Range scalp — tránh trend mạnh"

        # BTC price change
        btc_price = short["price"]
        chg_1d = round((float(d_d1.iloc[-1]["close"]) - float(d_d1.iloc[-2]["close"])) / float(d_d1.iloc[-2]["close"]) * 100, 2) if len(d_d1) >= 2 else 0
        chg_7d = round((float(d_d1.iloc[-1]["close"]) - float(d_d1.iloc[-8]["close"])) / float(d_d1.iloc[-8]["close"]) * 100, 2) if len(d_d1) >= 8 else 0

        return jsonify({
            "btc_price": btc_price,
            "chg_1d":    chg_1d,
            "chg_7d":    chg_7d,
            "short":     short,
            "medium":    medium,
            "long":      longterm,
            "overall":   overall,
            "generated_at": _local_isoformat(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/backtest", methods=["POST"])
def run_backtest():
    """Backtest signals trong history, hỗ trợ filter theo khoảng thời gian."""
    from datetime import datetime, timezone, timedelta
    import pandas as pd
    import concurrent.futures

    data        = request.json or {}
    conf_filter  = data.get("confidence",   "HIGH")   # HIGH | ALL
    dir_filter   = data.get("direction",    "ALL")    # LONG | SHORT | ALL
    hours_ago    = int(data.get("hours_ago", 8))       # mặc định 8 tiếng
    algo_version = data.get("algo_version", "")        # "" = tất cả versions

    history = load_history()
    if not history:
        return jsonify({"results": [], "summary": {}, "error": "Không có history"})

    # Filter theo confidence + direction
    signals = [h for h in history
               if (conf_filter == "ALL" or h.get("confidence") == conf_filter)
               and (dir_filter == "ALL" or h.get("direction") == dir_filter)
               and (not algo_version or h.get("algo_version", "legacy") == algo_version)]

    # Filter theo thời gian (hours_ago = 0 → không giới hạn)
    if hours_ago > 0:
        # History timestamps không có timezone (local server time)
        # Dùng datetime.now() naive để so sánh — tránh lệch timezone UTC vs local
        cutoff = datetime.now() - timedelta(hours=hours_ago)
        def sig_after_cutoff(h):
            try:
                raw = h.get("time", "")
                if not raw:
                    return False
                t = pd.Timestamp(raw).replace(tzinfo=None)
                return t >= cutoff
            except Exception:
                return False
        signals = [h for h in signals if sig_after_cutoff(h)]

    if not signals:
        return jsonify({"results": [], "summary": {}, "error": "Không có signal phù hợp"})

    # Backtest parallel — tránh timeout khi có nhiều signal
    results = []
    max_signals = 50  # giới hạn 50 signal để tránh timeout
    signals = signals[:max_signals]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(backtest_signal, sig): sig for sig in signals}
        for fut in concurrent.futures.as_completed(futures, timeout=110):
            try:
                results.append(fut.result())
            except Exception as e:
                sig = futures[fut]
                results.append({**sig, "bt_result": "ERROR", "bt_note": str(e),
                                 "bt_candles": None, "bt_pnl_r": None, "bt_exit_price": None})

    # Summary
    wins    = [r for r in results if r["bt_result"] == "WIN"]
    losses  = [r for r in results if r["bt_result"] == "LOSS"]
    opens   = [r for r in results if r["bt_result"] == "OPEN"]
    pendings= [r for r in results if r["bt_result"] == "PENDING"]
    errors  = [r for r in results if r["bt_result"] == "ERROR"]
    skips   = [r for r in results if r["bt_result"] == "SKIP"]

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
        "pending":          len(pendings),
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


@app.route("/api/backtest/signals", methods=["POST"])
def run_backtest_signals():
    """Backtest danh sách signals cụ thể truyền thẳng từ frontend (history selection)."""
    data    = request.json or {}
    signals = data.get("signals", [])
    if not signals:
        return jsonify({"results": [], "summary": {}, "error": "Không có signal"})

    results = []
    for sig in signals:
        r = backtest_signal(sig)
        results.append(r)

    wins     = [r for r in results if r["bt_result"] == "WIN"]
    losses   = [r for r in results if r["bt_result"] == "LOSS"]
    opens    = [r for r in results if r["bt_result"] == "OPEN"]
    pendings = [r for r in results if r["bt_result"] == "PENDING"]
    errors   = [r for r in results if r["bt_result"] == "ERROR"]
    closed   = wins + losses
    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0

    pnl_rs   = [r["bt_pnl_r"] for r in closed if r["bt_pnl_r"] is not None]
    total_r  = round(sum(pnl_rs), 2)
    avg_r    = round(sum(pnl_rs) / len(pnl_rs), 2) if pnl_rs else 0
    avg_win_r  = round(sum(r["bt_pnl_r"] for r in wins   if r["bt_pnl_r"]) / len(wins),   2) if wins   else 0
    avg_loss_r = round(sum(r["bt_pnl_r"] for r in losses if r["bt_pnl_r"]) / len(losses), 2) if losses else 0
    expectancy = round((len(wins)/len(closed)) * avg_win_r + (len(losses)/len(closed)) * avg_loss_r, 2) if closed else 0
    avg_candles_win  = round(sum(r["bt_candles"] for r in wins   if r["bt_candles"]) / len(wins),   1) if wins   else None
    avg_candles_loss = round(sum(r["bt_candles"] for r in losses if r["bt_candles"]) / len(losses), 1) if losses else None

    summary = {
        "total": len(signals), "closed": len(closed),
        "wins": len(wins), "losses": len(losses),
        "opens": len(opens), "pending": len(pendings), "errors": len(errors),
        "win_rate": win_rate, "total_r": total_r, "avg_r": avg_r,
        "avg_win_r": avg_win_r, "avg_loss_r": avg_loss_r,
        "expectancy": expectancy,
        "avg_candles_win": avg_candles_win, "avg_candles_loss": avg_candles_loss,
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
    data    = request.json or {}
    min_vol = float(data.get("min_vol", 10_000_000))
    cfg      = load_config()
    strategy = cfg.get("strategy", "SWING_H4")
    print(f"[MARKET SCAN START] min_vol={min_vol:,.0f} strategy={strategy} running={scan_state['running']}")
    if scan_state["running"]:
        return jsonify({"ok": False, "msg": "Đang scan, vui lòng đợi"})
    threading.Thread(target=run_full_scan, kwargs={"min_vol": min_vol, "strategy": strategy}, daemon=True).start()
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


@app.route("/api/config/scan-modes", methods=["POST"])
def set_scan_modes():
    """Cập nhật scan_modes: ['TREND'], ['RANGE_SCALP'], ['TREND','RANGE_SCALP']."""
    data = request.get_json() or {}
    cfg  = load_config()
    modes = data.get("modes", ["TREND"])
    cfg["scan_modes"] = [m for m in modes if m in ("TREND", "RANGE_SCALP")]
    save_config(cfg)
    return jsonify({"ok": True, "scan_modes": cfg["scan_modes"]})


@app.route("/api/config/watchlist-algo", methods=["POST"])
def set_watchlist_algo():
    """Gắn algo cho symbol trong watchlist: {symbol, algo}."""
    data = request.get_json() or {}
    sym  = data.get("symbol", "").upper()
    algo = data.get("algo", "TREND")
    if not sym:
        return jsonify({"ok": False, "error": "Missing symbol"}), 400
    cfg  = load_config()
    if "watchlist_algos" not in cfg:
        cfg["watchlist_algos"] = {}
    cfg["watchlist_algos"][sym] = algo
    save_config(cfg)
    return jsonify({"ok": True, "symbol": sym, "algo": algo})


@app.route("/api/config/range-override", methods=["POST"])
def set_range_override():
    """Set/clear range tay cho symbol: {symbol, range_high, range_low} hoặc {symbol, clear:true}."""
    data = request.get_json() or {}
    sym  = data.get("symbol", "").upper()
    if not sym:
        return jsonify({"ok": False, "error": "Missing symbol"}), 400
    cfg  = load_config()
    if "range_override" not in cfg:
        cfg["range_override"] = {}
    if data.get("clear"):
        cfg["range_override"].pop(sym, None)
    else:
        cfg["range_override"][sym] = {
            "range_high": float(data.get("range_high", 0)),
            "range_low":  float(data.get("range_low",  0)),
        }
    save_config(cfg)
    return jsonify({"ok": True, "symbol": sym, "range_override": cfg["range_override"].get(sym)})


@app.route("/api/range-check/<symbol>")
def range_check(symbol):
    """Chạy range_engine cho symbol, trả về kết quả đầy đủ."""
    from dashboard.range_engine import range_analyze
    cfg = load_config()
    try:
        result = range_analyze(symbol.upper(), {**cfg, "force_futures": True})
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/scan-modes", methods=["GET"])
def get_scan_modes():
    cfg = load_config()
    return jsonify({
        "scan_modes":      cfg.get("scan_modes", ["TREND"]),
        "watchlist_algos": cfg.get("watchlist_algos", {}),
        "range_override":  cfg.get("range_override", {}),
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 CryptoDesk running at http://127.0.0.1:{port}\n")
    print("   Tab 1: Dashboard — theo dõi mã cụ thể")
    print("   Tab 2: Market Scan — quét toàn thị trường\n")
    app.run(debug=False, host="0.0.0.0", port=port)
