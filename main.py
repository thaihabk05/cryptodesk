"""main.py — Entry point. Chạy: python main.py"""
import json, os, threading, time
from datetime import datetime, timezone, timedelta
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
# ── Persistent storage ──────────────────────────────────────────
# Railway Volume: mount volume vào /data trong Railway dashboard
# Settings → Volumes → Add → Mount Path: /data
# Local fallback: ./data (tự động tạo nếu không có volume)
import os as _os
_VOLUME_PATH = Path("/data") if Path("/data").exists() and _os.access("/data", _os.W_OK) else Path("data")
DATA_DIR       = _VOLUME_PATH
CONFIG_FILE    = DATA_DIR / "config.json"
HISTORY_FILE   = DATA_DIR / "history.json"
POSITIONS_FILE = DATA_DIR / "positions.json"

# ── Algorithm Version — tăng mỗi khi thay đổi filter/threshold ──
ALGO_VERSION = "v2.6"   # v2.6: position monitor + Telegram bot commands (2026-04-27)
ALGO_DATE    = "2026-04-27"

DATA_DIR.mkdir(exist_ok=True)
_storage_type = "Railway Volume (/data)" if str(DATA_DIR) == "/data" else f"Local fallback ({DATA_DIR.resolve()})"
print(f"[STORAGE] Using: {_storage_type}")
print(f"[STORAGE] Files: config={CONFIG_FILE.exists()}, history={HISTORY_FILE.exists()}, positions={POSITIONS_FILE.exists()}")

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

def _clean_for_json(obj):
    """Recursively replace NaN/Infinity with None — json chuẩn không hỗ trợ."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_for_json(v) for v in obj]
    return obj

def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []

def save_history(h):
    cleaned = _clean_for_json(h[-500:])  # giữ 500 records (tăng từ 200)
    HISTORY_FILE.write_text(json.dumps(cleaned, indent=2, allow_nan=False, default=str))

# ── Background auto-scanner (Dashboard) ───────
scan_results = {}
scan_lock    = threading.Lock()
scanner_running = False
scanner_status  = {"is_scanning": False, "last_scan": None,
                   "next_scan": None, "scan_count": 0}

_loss_cooldown_cache = {}  # (sym, dir) -> (checked_at_ts, loss_count)

def _count_recent_losses(symbol: str, direction: str, hours: int = 12) -> int:
    """
    Đếm số LOSS gần đây cho (symbol, direction) bằng cách:
      - Lọc history items cùng (symbol, direction) trong `hours` qua
      - Nếu có ≥2 candidates, fetch M15 klines 1 lần và check SL hit
    Cache 5 phút trong process để giảm API call.
    """
    if not symbol or direction not in ("LONG", "SHORT"):
        return 0
    import time as _t
    now = _t.time()
    key = (symbol, direction)
    cached = _loss_cooldown_cache.get(key)
    if cached and (now - cached[0]) < 300:
        return cached[1]

    cutoff = now - hours * 3600
    try:
        history = load_history()
    except Exception:
        history = []

    recent = []
    for h in history[-300:]:
        if h.get("symbol") != symbol or h.get("direction") != direction:
            continue
        try:
            ts = datetime.fromisoformat(h.get("time", "")).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        recent.append((ts, h))

    if len(recent) < 2:
        _loss_cooldown_cache[key] = (now, 0)
        return 0

    try:
        from core.binance import fetch_klines
        oldest_ts = min(ts for ts, _ in recent)
        hours_back = max(1.0, (now - oldest_ts) / 3600 + 0.5)
        candles_needed = min(int(hours_back * 4) + 5, 200)
        df = fetch_klines(symbol, "15m", candles_needed, force_futures=True)
        if df is None or len(df) == 0:
            _loss_cooldown_cache[key] = (now, 0)
            return 0
        df = df.copy()
        df["ts"] = df.index.astype("int64") // 10**9
    except Exception:
        _loss_cooldown_cache[key] = (now, 0)
        return 0

    loss_count = 0
    for sig_ts, h in recent:
        try:
            sl = float(h.get("sl") or 0)
        except (TypeError, ValueError):
            continue
        if sl <= 0:
            continue
        df_after = df[df["ts"] >= sig_ts - 60]
        if df_after.empty:
            continue
        try:
            if direction == "LONG":
                if float(df_after["low"].min()) <= sl:
                    loss_count += 1
            else:
                if float(df_after["high"].max()) >= sl:
                    loss_count += 1
        except Exception:
            continue

    _loss_cooldown_cache[key] = (now, loss_count)
    return loss_count


def _should_block_signal(r: dict):
    """
    Lọc signal dựa trên backtest 168h (revise 2026-05-03 từ 500 signals):
    - LONG + oi_change > 10%  → FOMO chase (≥10% WR=22%, sumR=-12.5R)
                                 (cũ: >8% bị revise vì OI 8-10% có WR=89%, sumR=+18R)
    - LONG + funding < -0.01% → vẫn giữ (WR=26%, sumR=-6.9R trên 57 signals — confirm)
    - cooldown 12h: (symbol, direction) đã LOSS ≥2 lần → block
    - rr cap đã bỏ: LONG RR≥3 thực tế WR=42% sumR=+57R (data 7 ngày ngược dự đoán cũ)

    Trả về (block: bool, reason: str|None).
    """
    direction = r.get("direction", "")

    sym = r.get("symbol", "")
    if sym and direction in ("LONG", "SHORT"):
        recent_losses = _count_recent_losses(sym, direction, hours=12)
        if recent_losses >= 2:
            return True, f"{sym} {direction} đã LOSS {recent_losses}x trong 12h (cooldown)"

    if direction != "LONG":
        return False, None

    mk = r.get("market") or {}
    oi_change = mk.get("oi_change", r.get("oi_change"))
    funding   = mk.get("funding",   r.get("funding"))

    if oi_change is not None:
        try:
            oi_v = float(oi_change)
            if oi_v > 10:
                return True, f"LONG oi_change {oi_v:+.1f}% > 10% (FOMO, backtest WR=22%)"
        except (TypeError, ValueError):
            pass

    if funding is not None:
        try:
            f_v = float(funding)
            if f_v < -0.01:
                return True, f"LONG funding {f_v:+.3f}% < -0.01% (heavy-short bias, WR=26%)"
        except (TypeError, ValueError):
            pass

    return False, None


def _is_duplicate_signal(result: dict, history: list, window_hours: int = 2) -> bool:
    """
    Kiểm tra signal có phải duplicate không.
    Ba tầng dedup:
    1. REVERSAL hard cooldown 60 phút: REVERSAL setup cần thời gian nến confirm,
       fire dồn dập là dấu hiệu engine đọc nhầm trend (case ARBUSDT 29/04/2026)
    2. Hard cooldown 30 phút: cùng symbol + direction → dup BẤT KỂ entry price
       (chống case parabolic pump: 2 entry cách nhau vài phút, giá khác >1% nhưng cùng đỉnh)
    3. Soft dedup window_hours: cùng symbol + direction + entry ±1% → dup
    """
    now    = time.time()
    cutoff = now - window_hours * 3600
    hard_cutoff    = now - 30 * 60   # 30 phút (default)
    rev_cutoff     = now - 60 * 60   # 60 phút (REVERSAL)
    sym    = result.get("symbol", "")
    dirr   = result.get("direction", "")
    strat  = (result.get("strategy") or result.get("algo") or "").upper()
    is_rev = strat == "REVERSAL"
    entry  = float(result.get("entry", 0) or 0)

    for h in history[-100:]:
        try:
            ts = datetime.fromisoformat(h.get("time", "")).timestamp()
            if ts < cutoff:
                continue
            if h.get("symbol") != sym or h.get("direction") != dirr:
                continue
            h_strat = (h.get("strategy") or h.get("algo") or "").upper()
            # Tầng 1: REVERSAL cooldown 60 phút — chỉ áp khi cả 2 đều REVERSAL
            if is_rev and h_strat == "REVERSAL" and ts >= rev_cutoff:
                return True
            # Tầng 2: hard cooldown 30 phút — bất kể entry, mọi strategy
            if ts >= hard_cutoff:
                return True
            # Tầng 3: soft dedup ±1% entry trong window_hours
            prev_entry = float(h.get("entry", 0) or 0)
            if prev_entry > 0 and abs(entry - prev_entry) / prev_entry <= 0.01:
                return True
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
    """Lưu signal vào history — dedup chặt theo symbol+direction+entry±1% trong 2 giờ.
    Bypass dedup cho watchlist/position-reversal save (cooldown đã handled ở caller).
    """
    history = load_history()
    source  = result.get("source", "market_scan")
    bypass  = source in ("watchlist_go", "watchlist_approaching", "position_reversal")
    if not bypass and _is_duplicate_signal(result, history, window_hours=2):
        print(f"[DEDUP] Skip {result.get('symbol')} {result.get('direction')} — duplicate trong 2h")
        return
    history.append({
        "time":            result.get("timestamp", _local_isoformat()),
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
        # ── Source tracking — phân biệt market_scan / watchlist / position_reversal
        "source":          source,
        "algo":            result.get("algo", ""),
        "alert_type":      result.get("alert_type", ""),
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

    # Lấy trend bias
    d1_b = str(result.get("d1_bias") or result.get("d1",{}).get("bias","?"))
    h4_b = str(result.get("h4_bias") or result.get("h4",{}).get("bias","?"))

    # Cảnh báo nếu trend ngược chiều signal
    trend_warn = ""
    if dirr == "LONG"  and any(x in d1_b+h4_b for x in ("DOWNTREND","BEARISH","SHORT","BEAR")):
        trend_warn = "!! CANH BAO: D1/H4 dang DOWN — Long counter-trend, rui ro cao"
    elif dirr == "SHORT" and any(x in d1_b+h4_b for x in ("UPTREND","BULLISH","LONG","BULL")):
        trend_warn = "!! CANH BAO: D1/H4 dang UP — Short counter-trend, rui ro cao"

    lines = [
        dir_emoji + " " + sym + " — " + dirr + " | HIGH",
        verdict_line,
    ]
    if trend_warn:
        lines.append(trend_warn)
    lines += [
        "--------------------",
        "Chien luoc: " + strat_label,
        "Price: " + str(result.get("price","")) + " | R:R 1:" + str(result.get("rr","")),
        "Entry: " + str(result.get("entry","")),
        "SL: " + str(result.get("sl","")) + " (-" + str(result.get("sl_pct","")) + "%)",
        "TP1: " + str(result.get("tp1","")) + " (+" + str(result.get("tp1_pct","")) + "%) | TP2: " + str(result.get("tp2","")),
        "--------------------",
        "D1: " + d1_b + " | H4: " + h4_b,
        "Funding: " + funding_str + " | OI: " + oi_str + " | ATR: " + atr_str,
        "--------------------",
        "Checklist:",
        check_lines.rstrip(),
    ]
    # Thêm warnings nếu có
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
    """Alert Telegram khi mã watchlist đang đúng điểm entry hoặc gần entry tốt."""
    token   = cfg.get("telegram_token", "")
    chat_id = cfg.get("telegram_chat", "")
    if not token or not chat_id:
        return

    direction  = result.get("direction", "WAIT")
    confidence = result.get("confidence", "LOW")
    rr         = result.get("rr", 0) or 0
    verdict    = result.get("entry_verdict", "WAIT")
    price      = float(result.get("price", 0) or 0)
    entry_opt  = result.get("entry_opt")  # entry tốt hơn nếu có

    if direction not in ("LONG", "SHORT"):
        return
    if confidence not in ("HIGH", "MEDIUM"):
        return
    if rr < 1.5:
        return

    # Filter backtest 72h (2026-04-30): chặn LONG-FOMO/funding âm + RR quá xa
    bt_block, bt_reason = _should_block_signal(result)
    if bt_block:
        print(f"[BT FILTER] {sym} {direction} watchlist alert blocked: {bt_reason}")
        return

    # ── Alert types ──
    # 1. GO: vào ngay
    # 2. APPROACHING: giá đang tiến gần entry tốt (< 0.5%) — chuẩn bị vào
    alert_type = None
    if verdict == "GO":
        alert_type = "GO"
    elif entry_opt and price > 0:
        try:
            entry_opt_val = float(entry_opt)
            dist_pct = abs(price - entry_opt_val) / price * 100
            if dist_pct < 0.5:  # giá cách entry_opt < 0.5%
                if direction == "LONG" and price > entry_opt_val:
                    alert_type = "APPROACHING"
                elif direction == "SHORT" and price < entry_opt_val:
                    alert_type = "APPROACHING"
        except (TypeError, ValueError):
            pass

    if not alert_type:
        return

    # Cooldown ngắn hơn cho fast loop — 15 phút (vs 1 tiếng cũ)
    import time
    cooldown_key = f"{sym}_{direction}_{algo_key}_{alert_type}"
    now = time.time()
    last_alert = _watchlist_alert_cooldown.get(cooldown_key, 0)
    if now - last_alert < 900:  # cooldown 15 phút
        return
    _watchlist_alert_cooldown[cooldown_key] = now

    # Save vào history với tag source — trước đây watchlist alert không lưu nên
    # backtest + dashboard history không phản ánh đầy đủ những signal engine đã ra
    # (case ARB 30/4-3/5: nhiều SHORT MEDIUM gửi Telegram nhưng history trống).
    try:
        _save_signal_to_history({
            **result,
            "source":     f"watchlist_{alert_type.lower()}",
            "algo":       algo_key,
            "alert_type": alert_type,
            "timestamp":  result.get("timestamp", _local_isoformat()),
        })
    except Exception as e:
        print(f"[WATCHLIST SAVE ERROR] {sym}: {e}")

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

    type_emoji = "✅" if alert_type == "GO" else "⏰"
    type_text  = "DA SAN SANG VAO LENH" if alert_type == "GO" else f"GIA SAP CHAM ENTRY {entry_opt}"

    lines = [
        f"📌 [WATCHLIST {alert_type}] {sym}",
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
        f"{type_emoji} {type_text}",
    ]

    send_telegram(token, chat_id, chr(10).join(lines))


_watchlist_alert_cooldown = {}

_funding_spike_last_run = {"ts": 0}

def _auto_add_funding_spike_watchlist(cfg, min_volume: float = 20_000_000,
                                       funding_threshold: float = 0.05,
                                       max_add: int = 5,
                                       run_interval_sec: int = 1800) -> list:
    """
    Tự động thêm vào watchlist các coin có funding spike — pattern thắng cao
    (case CRCL: funding +0.16% → +3.57R; setup `fund_pos_oi_up` WR=90% trên backtest).

    Logic:
      - Throttle ≥30 phút giữa 2 lần chạy
      - Lấy top-volume futures pairs (>= min_volume)
      - Lấy tất cả funding rates trong 1 API call
      - Lọc coin có |funding| > funding_threshold (mặc định 0.05%)
      - Sort theo |funding| giảm dần, lấy max_add coin chưa có trong watchlist
      - Add vào cfg["symbols"] với algo RANGE_SCALP (vì M15 reversal bắt setup nhanh)
      - Save config

    Returns: list các symbol vừa add (rỗng nếu không có / chưa đến lúc chạy).
    """
    import time as _t
    now = _t.time()
    if now - _funding_spike_last_run["ts"] < run_interval_sec:
        return []
    _funding_spike_last_run["ts"] = now

    try:
        from core.binance import fetch_all_futures_tickers, fetch_all_funding_rates
        tickers  = fetch_all_futures_tickers(min_volume_usd=min_volume)
        fundings = fetch_all_funding_rates()
    except Exception as e:
        print(f"[FUNDING SPIKE] Fetch error: {e}")
        return []

    if not tickers or not fundings:
        return []

    existing = set(cfg.get("symbols", []))
    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if sym in existing:
            continue
        f = fundings.get(sym)
        if f is None:
            continue
        if abs(f) > funding_threshold:
            candidates.append((sym, f, t.get("volume_24h", 0)))

    if not candidates:
        return []

    # Ưu tiên |funding| lớn nhất
    candidates.sort(key=lambda x: -abs(x[1]))
    picked = candidates[:max_add]

    if "watchlist_algos" not in cfg:
        cfg["watchlist_algos"] = {}

    added = []
    for sym, f, vol in picked:
        cfg["symbols"].append(sym)
        cfg["watchlist_algos"][sym] = "RANGE_SCALP"
        added.append(sym)
        direction_hint = "SHORT" if f > 0 else "LONG"
        print(f"[FUNDING SPIKE] +{sym} funding {f:+.4f}% vol {vol/1e6:.1f}M → watchlist (hint: {direction_hint})")

    if added:
        save_config(cfg)
    return added


def dashboard_scan_cycle(cfg):
    """Scan các symbol trong watchlist — dùng đúng algo đã gắn cho từng mã."""
    from dashboard.fam_engine       import fam_analyze
    from dashboard.swing_h1_engine  import swing_h1_analyze
    from dashboard.scalp_engine     import scalp_analyze
    from dashboard.range_engine     import range_analyze
    from dashboard.reversal_engine  import reversal_analyze

    algo_map = {
        "TREND":       get_analyze_fn(cfg),
        "RANGE_SCALP": range_analyze,
        "REVERSAL":    reversal_analyze,
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
    high_signals = []
    min_conf = cfg.get("alert_confidence", "HIGH")
    conf_ok  = {"HIGH"} if min_conf == "HIGH" else {"HIGH", "MEDIUM"}
    for r in results:
        if r.get("confidence") not in conf_ok: continue
        if r.get("direction") not in ("LONG","SHORT"): continue
        if r.get("rr", 0) < min_rr: continue
        # Chỉ gửi Telegram khi verdict GO — có thể vào lệnh ngay
        if r.get("entry_verdict") != "GO": continue

        # Block nếu D1 VÀ H4 đều ngược chiều signal
        dirr = r.get("direction","")
        d1_b = str(r.get("d1_bias") or r.get("d1",{}).get("bias","") or "")
        h4_b = str(r.get("h4_bias") or r.get("h4",{}).get("bias","") or "")
        d1_down = "DOWNTREND" in d1_b or d1_b in ("BEAR","SHORT","BEARISH")
        h4_down = "DOWNTREND" in h4_b or h4_b in ("BEAR","SHORT","BEARISH")
        d1_up   = "UPTREND"   in d1_b or d1_b in ("BULL","LONG","BULLISH")
        h4_up   = "UPTREND"   in h4_b or h4_b in ("BULL","LONG","BULLISH")

        if dirr == "LONG"  and d1_down and h4_down:
            print(f"[TELEGRAM BLOCK] {r.get('symbol')} LONG blocked: D1={d1_b} H4={h4_b}")
            continue
        if dirr == "SHORT" and d1_up and h4_up:
            print(f"[TELEGRAM BLOCK] {r.get('symbol')} SHORT blocked: D1={d1_b} H4={h4_b}")
            continue

        # Filter backtest 72h (2026-04-30): chặn LONG-FOMO/funding âm + RR quá xa
        bt_block, bt_reason = _should_block_signal(r)
        if bt_block:
            print(f"[BT FILTER] {r.get('symbol')} {dirr} blocked: {bt_reason}")
            continue

        high_signals.append(r)

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

# ── Position Monitor — alert reversal cho lệnh đang mở ──
_position_alert_cooldown = {}

def _check_position_reversal(pos: dict, cfg: dict):
    """Check 1 position đang mở: nếu có dấu hiệu reversal → Telegram alert."""
    from dashboard.reversal_engine import reversal_analyze

    sym = pos.get("symbol", "")
    pos_dir = pos.get("direction", "")
    if not sym or not pos_dir:
        return

    token   = cfg.get("telegram_token", "")
    chat_id = cfg.get("telegram_chat", "")
    if not token or not chat_id:
        return

    try:
        r = reversal_analyze(sym, {"force_futures": True})
    except Exception as e:
        print(f"[POS MONITOR] {sym} error: {e}")
        return

    rev_dir  = r.get("direction")
    rev_conf = r.get("confidence")
    rev_score = r.get("score", 0)

    # Trigger alert: reversal NGƯỢC chiều với position đang mở
    # LONG position + reversal SHORT detected = nên close
    # SHORT position + reversal LONG detected = nên close & flip
    flip_dir = "SHORT" if pos_dir == "LONG" else "LONG"
    if rev_dir != flip_dir:
        return
    if rev_conf not in ("HIGH", "MEDIUM"):
        return
    if rev_score < 3:
        return

    # Cooldown 30 phút theo position id
    import time as _t
    pid = pos.get("id", sym)
    cooldown_key = f"pos_{pid}_{rev_dir}"
    now = _t.time()
    last_alert = _position_alert_cooldown.get(cooldown_key, 0)
    if now - last_alert < 1800:
        return
    _position_alert_cooldown[cooldown_key] = now

    rd = r.get("reversal_data", {})
    price = r.get("price", "?")
    entry = pos.get("entry", "?")

    # PnL ước lượng
    pnl_pct = "?"
    try:
        e = float(entry); p = float(price)
        if pos_dir == "LONG":
            pnl_pct = f"{(p-e)/e*100:+.2f}%"
        else:
            pnl_pct = f"{(e-p)/e*100:+.2f}%"
    except: pass

    flip_emoji = "🔄"
    lines = [
        f"{flip_emoji} REVERSAL ALERT — {sym} {pos_dir}",
        f"Lenh dang mo: Entry {entry} | Hien tai {price} ({pnl_pct})",
        "--------------------",
        f"Reversal {rev_dir} signal: confidence {rev_conf}, score {rev_score}",
    ]
    conds = r.get("conditions", [])
    for c in conds[:5]:
        lines.append(f"  + {c}")

    if rd.get("rsi_h1"):
        lines.append(f"RSI H1: {rd['rsi_h1']} | RSI M15: {rd.get('rsi_m15', '?')}")
    t = rd.get("taker", {})
    if t:
        lines.append(f"Taker: {t.get('buy_ratio')}x ({t.get('trend')})")

    lines.append("--------------------")
    if pos_dir == "LONG":
        lines.append(f"⚠️ Can nhac CLOSE LONG (reversal SHORT detected)")
    else:
        lines.append(f"⚠️ Can nhac CLOSE SHORT + flip LONG")

    send_telegram(token, chat_id, chr(10).join(lines))
    print(f"[POS ALERT] {sym} {pos_dir} → reversal {rev_dir} alert sent")

    # Save vào history với tag source — track lại các reversal alert cho lệnh đang mở
    try:
        _save_signal_to_history({
            **r,
            "source":      "position_reversal",
            "algo":        "REVERSAL",
            "alert_type":  "POSITION_FLIP",
            "position_id": pos.get("id", sym),
            "position_entry": pos.get("entry"),
            "timestamp":   _local_isoformat(),
        })
    except Exception as e:
        print(f"[POS ALERT SAVE ERROR] {sym}: {e}")


def _check_position_sl_tp(pos: dict, cfg: dict) -> str:
    """Check nếu giá đã chạm SL hoặc TP → return 'SL' / 'TP' / None.
    Nếu chạm → gửi Telegram alert + return reason để xóa khỏi monitor.
    """
    sym = pos.get("symbol", "")
    pos_dir = pos.get("direction", "")
    entry = pos.get("entry")
    sl = pos.get("sl")
    tp = pos.get("tp")
    if not sym or not pos_dir or entry is None:
        return None
    if sl is None and tp is None:
        return None  # không có SL/TP để check

    try:
        from core.binance import fetch_klines
        # Lấy 5 nến M5 gần nhất để check high/low
        df = fetch_klines(sym, "5m", 5, force_futures=True)
        if df is None or len(df) == 0:
            return None
        recent_high = float(df["high"].max())
        recent_low  = float(df["low"].min())
        last_price  = float(df["close"].iloc[-1])
    except Exception as e:
        print(f"[POS SL/TP] {sym} fetch error: {e}")
        return None

    hit = None
    exit_price = None

    if pos_dir == "LONG":
        # SL hit: low chạm xuống SL
        if sl is not None and recent_low <= float(sl):
            hit = "SL"; exit_price = float(sl)
        # TP hit: high chạm lên TP
        elif tp is not None and recent_high >= float(tp):
            hit = "TP"; exit_price = float(tp)
    else:  # SHORT
        if sl is not None and recent_high >= float(sl):
            hit = "SL"; exit_price = float(sl)
        elif tp is not None and recent_low <= float(tp):
            hit = "TP"; exit_price = float(tp)

    if not hit:
        return None

    # Gửi Telegram alert
    token   = cfg.get("telegram_token", "")
    chat_id = cfg.get("telegram_chat", "")
    if token and chat_id:
        try:
            e = float(entry); ex = float(exit_price)
            if pos_dir == "LONG":
                pnl_pct = (ex - e) / e * 100
            else:
                pnl_pct = (e - ex) / e * 100
            emoji = "✅" if hit == "TP" else "❌"
            lines = [
                f"{emoji} POSITION CLOSED — {sym} {pos_dir}",
                f"Entry: {entry} → Exit: {exit_price} (chạm {hit})",
                f"PnL ước lượng: {pnl_pct:+.2f}%",
                "─────────────────",
                f"Đã tự xóa khỏi monitor (id: {pos.get('id')})",
            ]
            send_telegram(token, chat_id, chr(10).join(lines))
        except Exception as e:
            print(f"[POS SL/TP ALERT] {sym} error: {e}")

    print(f"[POS AUTO-CLOSE] {sym} {pos_dir} hit {hit} @ {exit_price}")
    return hit


def position_monitor_loop():
    """Monitor positions đang mở:
    1. Check nếu chạm SL/TP → tự xóa khỏi monitor + Telegram alert
    2. Check reversal signal → Telegram alert (vẫn giữ position)
    """
    global scanner_running
    while scanner_running:
        try:
            cfg = load_config()
            interval = int(cfg.get("position_monitor_interval_sec", 120))
            positions = load_positions()
            # Chỉ monitor positions tạo trong 7 ngày gần nhất
            from datetime import datetime as _dt, timedelta as _td
            cutoff = (_dt.now() - _td(days=7)).isoformat()
            active = [p for p in positions if p.get("saved_at", "") >= cutoff]

            # Bước 1: Check SL/TP — tự xóa nếu chạm
            ids_to_remove = []
            for pos in active[:10]:
                try:
                    hit = _check_position_sl_tp(pos, cfg)
                    if hit:
                        ids_to_remove.append(pos.get("id"))
                except Exception as e:
                    print(f"[POS SL/TP CHECK] {pos.get('symbol')} error: {e}")

            if ids_to_remove:
                positions = load_positions()  # reload để tránh race
                positions = [p for p in positions if p.get("id") not in ids_to_remove]
                save_positions(positions)
                print(f"[POS AUTO-CLOSE] Removed {len(ids_to_remove)} positions")

            # Bước 2: Check reversal signal cho positions còn lại
            active = [p for p in active if p.get("id") not in ids_to_remove]
            for pos in active[:10]:
                try:
                    _check_position_reversal(pos, cfg)
                except Exception as e:
                    print(f"[POS LOOP] {pos.get('symbol')} error: {e}")

            elapsed = 0
            while elapsed < interval and scanner_running:
                time.sleep(5); elapsed += 5
        except Exception as e:
            print(f"[POS MONITOR LOOP] {e} — retry sau 60s")
            time.sleep(60)


def watchlist_fast_loop():
    """Loop riêng cho watchlist — quét nhanh hơn (1-2 phút) để alert tức thì."""
    global scanner_running
    while scanner_running:
        try:
            cfg = load_config()
            wl_interval = int(cfg.get("watchlist_interval_sec", 90))  # mặc định 90s

            try:
                dashboard_scan_cycle(cfg)
            except Exception as e:
                print(f"[WATCHLIST FAST LOOP ERROR] {e}")

            elapsed = 0
            while elapsed < wl_interval and scanner_running:
                time.sleep(5); elapsed += 5
        except Exception as e:
            print(f"[WATCHLIST LOOP ERROR] {e} — retry sau 30s")
            time.sleep(30)


def dashboard_scanner_loop():
    """Market-wide scan — chậm hơn (15+ phút) vì quét toàn thị trường."""
    global scanner_running, scanner_status
    while scanner_running:
        try:
            cfg = load_config()
            interval_sec = cfg.get("interval_minutes", 30) * 60
            scan_start_ts = time.time()

            scanner_status["is_scanning"] = True
            scanner_status["scan_start"]  = _local_isoformat()

            # Auto-add coin có funding spike vào watchlist (throttle 30 phút bên trong)
            if cfg.get("auto_funding_watchlist", True):
                try:
                    added = _auto_add_funding_spike_watchlist(cfg)
                    if added:
                        cfg = load_config()  # reload sau khi save
                except Exception as e:
                    print(f"[FUNDING SPIKE ERROR] {e}")

            # Scan toàn thị trường futures — alert + lưu history cho HIGH
            try:
                market_scan_cycle(cfg)
            except Exception as e:
                print(f"[MARKET SCAN ERROR] {e}")

            scan_duration = round(time.time() - scan_start_ts)
            scanner_status["is_scanning"]  = False
            scanner_status["scan_count"]  += 1
            scanner_status["last_scan"]    = _local_isoformat()
            scanner_status["scan_duration"] = scan_duration
            scanner_status["next_scan"]     = datetime.fromtimestamp(
                time.time() + interval_sec).isoformat()

            elapsed = 0
            while elapsed < interval_sec and scanner_running:
                time.sleep(5); elapsed += 5
        except Exception as e:
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
    """Phân tích 1 coin. Optional ?algo=SWING_H1|REVERSAL|SCALP|SWING_H4|RANGE_SCALP"""
    cfg = load_config()
    algo_override = request.args.get("algo", "").upper()
    try:
        if algo_override:
            from dashboard.fam_engine       import fam_analyze
            from dashboard.swing_h1_engine  import swing_h1_analyze
            from dashboard.scalp_engine     import scalp_analyze
            from dashboard.range_engine     import range_analyze
            from dashboard.reversal_engine  import reversal_analyze
            engine_map = {
                "SWING_H4":    fam_analyze,
                "SWING_H1":    swing_h1_analyze,
                "SCALP":       scalp_analyze,
                "RANGE_SCALP": range_analyze,
                "REVERSAL":    reversal_analyze,
                "TREND":       get_analyze_fn(cfg),
            }
            engine = engine_map.get(algo_override, get_analyze_fn(cfg))
            result = engine(symbol, {**cfg, "force_futures": True})
            result["algo"] = algo_override
            return jsonify(result)
        return jsonify(get_analyze_fn(cfg)(symbol, cfg))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/results")
def get_results():
    with scan_lock: return jsonify(list(scan_results.values()))

@app.route("/api/history")
def get_history():
    from datetime import datetime as _dt
    import math
    history = load_history()
    # Optional filter theo algo_version
    ver_filter = request.args.get("algo_version")
    if ver_filter:
        history = [h for h in history if h.get("algo_version") == ver_filter]
    # Sort mới nhất lên đầu theo timestamp
    # Dùng string sort để tránh lỗi naive vs aware datetime
    def _ts(h):
        raw = h.get("time", "")
        if not raw: return ""
        # Normalize: bỏ timezone suffix để sort string thuần
        return raw[:19]  # "2026-03-07T15:20:37"
    history.sort(key=_ts, reverse=True)
    # Sanitize: replace NaN/Infinity with None (json không hỗ trợ)
    def _sanitize(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj
    return jsonify(_sanitize(history))

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
    history_before = len(load_history())
    _save_signal_to_history(sig)
    history_after = len(load_history())
    if history_after > history_before:
        return jsonify({"ok": True, "total": history_after})
    else:
        return jsonify({"ok": False, "error": "duplicate"})

@app.route("/api/history/import", methods=["POST"])
def history_import():
    """Import nhiều signals vào history (merge trực tiếp, không fetch thêm data)."""
    signals = (request.json or {}).get("signals", [])
    if not signals:
        return jsonify({"ok": False, "error": "Không có signals"})
    history = load_history()
    existing_keys = set()
    for h in history:
        key = f"{h.get('symbol','')}|{h.get('direction','')}|{h.get('time','')}"
        existing_keys.add(key)
    added = 0
    for sig in signals:
        if not sig.get("symbol"):
            continue
        key = f"{sig.get('symbol','')}|{sig.get('direction','')}|{sig.get('time','')}"
        if key not in existing_keys:
            history.append(sig)
            existing_keys.add(key)
            added += 1
    save_history(history)
    return jsonify({"ok": True, "added": added, "total": len(load_history())})

@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    save_history([])
    return jsonify({"ok": True})


# ── Backtest ──────────────────────────────────
def backtest_signal(signal: dict) -> dict:
    """
    Backtest signal: dùng M5 cho scalp, M15 cho swing H1, H1 cho swing H4.
    Market order → entry đã khớp ngay → check SL/TP liên tục trên nến nhỏ.
    """
    from core.binance import fetch_klines, fetch_volume_24h
    import pandas as pd

    symbol    = signal["symbol"]
    direction = signal["direction"]

    if direction == "WAIT":
        return {**signal, "bt_result": "SKIP", "bt_note": "Direction=WAIT — không có lệnh thực tế",
                "bt_candles": None, "bt_pnl_r": None, "bt_exit_price": None}
    entry     = float(signal["entry"])
    sl        = float(signal["sl"])
    tp1       = float(signal["tp1"])
    sig_time  = signal["time"]

    try:
        ts_parsed = pd.Timestamp(sig_time)
        if ts_parsed.tzinfo is None:
            ts_parsed = ts_parsed.tz_localize("Asia/Ho_Chi_Minh")
        sig_ts = ts_parsed.tz_convert("UTC").timestamp()
    except Exception:
        return {**signal, "bt_result": "ERROR", "bt_note": "Invalid timestamp",
                "bt_candles": None, "bt_pnl_r": None, "bt_exit_price": None}

    try:
        from datetime import timezone as _tz
        now_ts = datetime.now(_tz.utc).timestamp()
        hours_since = (now_ts - sig_ts) / 3600

        # ── Chọn timeframe backtest theo strategy ──
        # Tất cả strategy đều dùng M15 hoặc nhỏ hơn để wick detection chính xác.
        # H1 timeframe có thể MISS wick: nếu giá dip nhanh xuống SL trong vài phút
        # rồi bounce, H1 low ghi nhận được nhưng nếu data H1 chưa close (live candle)
        # thì có thể chưa cập nhật full wick → SL hit không được detect đúng.
        # Scalp: M5 / Swing H1: M15 / Swing H4: M15 (thay vì H1 cũ — chính xác 4x hơn)
        strategy = signal.get("strategy", "SWING_H4")
        if strategy == "SCALP":
            bt_interval, bt_label = "5m", "M5"
            minutes_per_candle = 5
        elif strategy == "SWING_H1":
            bt_interval, bt_label = "15m", "M15"
            minutes_per_candle = 15
        else:
            bt_interval, bt_label = "15m", "M15"
            minutes_per_candle = 15

        # Tính số nến cần fetch
        candles_needed = max(50, int(hours_since * 60 / minutes_per_candle) + 20)
        limit = min(candles_needed, 500)

        df = fetch_klines(symbol, bt_interval, limit, force_futures=True)
        df = df.copy()
        df["ts"] = df.index.astype("int64") // 10**9

        df_after = df[df["ts"] > sig_ts].reset_index(drop=True)

        sl_pct  = float(signal.get("sl_pct", 2))
        tp1_pct = float(signal.get("tp1_pct", 3))

        # ── Nếu không có nến nào sau signal → dùng nến cuối để check ──
        if len(df_after) == 0:
            last_price = float(df["close"].iloc[-1])
            if direction == "LONG":
                if last_price <= sl:
                    return {**signal, "bt_result": "LOSS",
                            "bt_note": f"Giá {round(last_price,6)} dưới SL {sl}",
                            "bt_candles": 0, "bt_pnl_r": -1.0,
                            "bt_exit_price": round(sl, 6)}
                if last_price >= tp1:
                    return {**signal, "bt_result": "WIN",
                            "bt_note": f"Giá {round(last_price,6)} trên TP1 {tp1}",
                            "bt_candles": 0,
                            "bt_pnl_r": round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0,
                            "bt_exit_price": round(tp1, 6)}
            else:
                if last_price >= sl:
                    return {**signal, "bt_result": "LOSS",
                            "bt_note": f"Giá {round(last_price,6)} trên SL {sl}",
                            "bt_candles": 0, "bt_pnl_r": -1.0,
                            "bt_exit_price": round(sl, 6)}
                if last_price <= tp1:
                    return {**signal, "bt_result": "WIN",
                            "bt_note": f"Giá {round(last_price,6)} dưới TP1 {tp1}",
                            "bt_candles": 0,
                            "bt_pnl_r": round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0,
                            "bt_exit_price": round(tp1, 6)}
            # Chưa chạm → OPEN
            unrealized = round((last_price - entry) / entry * 100, 2) if direction == "LONG" \
                         else round((entry - last_price) / entry * 100, 2)
            unrealized_r = round(unrealized / sl_pct, 2) if sl_pct > 0 else None
            return {**signal, "bt_result": "OPEN",
                    "bt_note": f"Giá hiện tại {round(last_price,6)} ({unrealized:+.2f}%) — dùng {bt_label}",
                    "bt_candles": 0, "bt_pnl_r": None,
                    "bt_unrealized_pct": unrealized, "bt_unrealized_r": unrealized_r,
                    "bt_exit_price": round(last_price, 6)}

        # ── Check SL/TP trên từng nến (market order = entry đã khớp ngay) ──
        sig_price      = float(signal.get("price", entry))
        is_limit_long  = direction == "LONG"  and entry < sig_price * 0.999
        is_limit_short = direction == "SHORT" and entry > sig_price * 1.001
        is_limit       = is_limit_long or is_limit_short

        entry_filled   = not is_limit  # market order → đã khớp ngay
        entry_fill_idx = None          # nến nào giá chạm entry

        # Track actual extremes after entry — diagnostic để user verify SL/TP detect
        actual_low_after  = float("inf")
        actual_high_after = float("-inf")

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

            # Track giá max/min sau khi đã khớp entry
            actual_low_after  = min(actual_low_after, low)
            actual_high_after = max(actual_high_after, high)

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
            time_est = round((i+1) * minutes_per_candle / 60, 1)
            return {**signal,
                    "bt_result":     result,
                    "bt_note":       f"Chạm {'TP1' if result=='WIN' else 'SL'} sau {i+1} nến {bt_label} (~{time_est}h){fill_note}",
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

        # ── Force-close timeout: nếu signal sống quá lâu mà chưa TP/SL → close at market ──
        # Backtest hiện có 53% signals OPEN sau 72h → expectancy không tin được.
        # Sau timeout, classify theo PnL hiện tại: >0R = WIN, ≤0R = LOSS.
        timeout_h_map = {
            "SCALP":       8,
            "RANGE_SCALP": 12,
            "SWING_H1":    24,
            "SWING_H4":    72,
        }
        timeout_h = timeout_h_map.get(strategy, 24)
        if hours_since >= timeout_h and unrealized_r is not None:
            forced_result = "WIN" if unrealized_r > 0 else "LOSS"
            return {**signal,
                    "bt_result":         forced_result,
                    "bt_note":           f"TIMEOUT {timeout_h}h ({bt_label}) — force close @ {round(last_price,6)} ({unrealized:+.2f}% / {unrealized_r:+.2f}R)",
                    "bt_candles":        len(df_after),
                    "bt_pnl_r":          unrealized_r,
                    "bt_exit_price":     round(last_price, 6),
                    "bt_exit_reason":    "TIMEOUT",
                    "bt_unrealized_pct": round(unrealized, 2),
                    "bt_unrealized_r":   unrealized_r}

        # Diagnostic: actual extremes after entry (giá đã đi tới đâu thực sự)
        diag_low  = round(actual_low_after, 6)  if actual_low_after  != float("inf")  else None
        diag_high = round(actual_high_after, 6) if actual_high_after != float("-inf") else None
        diag_note = ""
        if diag_low is not None and diag_high is not None:
            if direction == "LONG":
                # Đã đi gần tới SL chưa? Nếu low rất gần SL = sát kèo
                gap_to_sl = round((diag_low - sl) / sl * 100, 2)
                diag_note = f" | Low thực: {diag_low} (cách SL {sl}: {gap_to_sl:+.2f}%)"
            else:
                gap_to_sl = round((sl - diag_high) / sl * 100, 2)
                diag_note = f" | High thực: {diag_high} (cách SL {sl}: {gap_to_sl:+.2f}%)"

        return {**signal,
                "bt_result":          "OPEN",
                "bt_note":            f"Đã khớp, chưa chạm SL/TP — giá {round(last_price,6)} ({unrealized:+.2f}%) [{bt_label}]" + diag_note,
                "bt_candles":         len(df_after),
                "bt_pnl_r":           None,
                "bt_unrealized_pct":  round(unrealized, 2),
                "bt_unrealized_r":    unrealized_r,
                "bt_exit_price":      round(last_price, 6),
                "bt_actual_low":      diag_low,
                "bt_actual_high":     diag_high}

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
                if not symbol:
                    return jsonify({"error": "ATR mode cần nhập Symbol (ví dụ: ARBUSDT)"}), 400
                df_atr    = prepare(fetch_klines(symbol, "1h", 30, force_futures=True))
                atr_value = float(df_atr["atr"].iloc[-1]) if "atr" in df_atr.columns else None
                if not atr_value or atr_value <= 0:
                    return jsonify({"error": f"Không tính được ATR H1 cho {symbol}"}), 400
                # Sanity: ATR không được vượt quá 20% của entry
                if atr_value > entry * 0.20:
                    return jsonify({"error": f"ATR H1 ({_fmt(atr_value)}) quá lớn so với entry ({_fmt(entry)}). Kiểm tra lại Symbol"}), 400
                base_leg  = atr_value
                atr_pct   = round(atr_value / entry * 100, 2)
                base_note = f"ATR H1 {symbol} = ${_fmt(atr_value)} ({atr_pct}% từ entry)"
            except Exception as e:
                return jsonify({"error": f"Lỗi fetch ATR {symbol}: {str(e)[:80]}"}), 500

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

        # ── Phân tích coin realtime + gợi ý hành động cụ thể ──
        coin_price   = None
        coin_chg_1h  = None
        coin_ema_note= ""
        action_now   = None   # Gợi ý hành động ngay tại thời điểm phân tích

        if symbol:
            try:
                # Fetch 50 nến H1 + prepare để có RSI, ATR, vol_ratio cho reversal detection
                df_coin = prepare(fetch_klines(symbol, "1h", 50, force_futures=True))
                coin_price  = float(df_coin["close"].iloc[-1])
                coin_prev   = float(df_coin["close"].iloc[-2])
                coin_chg_1h = round((coin_price - coin_prev) / coin_prev * 100, 3)

                # EMA9 và EMA34 của coin
                coin_ema9  = float(df_coin["close"].rolling(min(9,  len(df_coin)), min_periods=1).mean().iloc[-1])
                coin_ema34 = float(df_coin["close"].rolling(min(34, len(df_coin)), min_periods=1).mean().iloc[-1])

                if is_long:
                    if coin_price < coin_ema9 and coin_chg_1h < -0.5:
                        coin_ema_note = f"{symbol} đang yếu ({coin_chg_1h:+.2f}% / 1h) — cẩn thận Long"
                    elif coin_price > coin_ema9 and coin_chg_1h > 0.3:
                        coin_ema_note = f"{symbol} đang mạnh ({coin_chg_1h:+.2f}% / 1h) — hỗ trợ Long"
                else:
                    if coin_price > coin_ema9 and coin_chg_1h > 0.5:
                        coin_ema_note = f"{symbol} đang hồi ({coin_chg_1h:+.2f}% / 1h) — cẩn thận Short"
                    elif coin_price < coin_ema9 and coin_chg_1h < -0.3:
                        coin_ema_note = f"{symbol} đang yếu ({coin_chg_1h:+.2f}% / 1h) — hỗ trợ Short"

                # ── Gợi ý hành động dựa trên khoảng cách đến TP ──
                if coin_price and tps:
                    # Tính khoảng cách từ giá hiện tại đến từng TP
                    tp_distances = []
                    for tp in tps:
                        tp_p = float(tp["price"]) if tp.get("price") else 0
                        if tp_p <= 0: continue
                        dist_pct = abs(coin_price - tp_p) / coin_price * 100
                        tp_distances.append((tp["label"], tp_p, dist_pct))

                    nearest_tp   = min(tp_distances, key=lambda x: x[2]) if tp_distances else None
                    tp0_dist     = tp_distances[0][2] if tp_distances else 999
                    tp1_dist     = tp_distances[1][2] if len(tp_distances) > 1 else 999

                    # Kiểm tra đã qua TP0 chưa (coin_price < TP0 với SHORT, > TP0 với LONG)
                    tp0_price   = float(tps[0]["price"]) if tps else 0
                    tp1_price   = float(tps[1]["price"]) if len(tps) > 1 else 0
                    tp2_price   = float(tps[2]["price"]) if len(tps) > 2 else 0

                    passed_tp0  = (is_long  and coin_price >= tp0_price) or                                   (not is_long and coin_price <= tp0_price)
                    passed_tp1  = (is_long  and coin_price >= tp1_price) or                                   (not is_long and coin_price <= tp1_price)
                    near_tp0    = tp0_dist < 0.3   # trong vòng 0.3% của TP0
                    near_tp1    = tp1_dist < 0.3

                    # Tổng hợp momentum: BTC + coin
                    btc_ok_for_dir = (is_long  and btc_sentiment in ("PUMP","RISK_ON")) or                                      (not is_long and btc_sentiment in ("DUMP","RISK_OFF"))
                    btc_bad_for_dir= (is_long  and btc_sentiment in ("DUMP","RISK_OFF")) or                                      (not is_long and btc_sentiment in ("PUMP","RISK_ON"))

                    coin_momentum_ok = (is_long and coin_chg_1h > 0) or                                        (not is_long and coin_chg_1h < 0)

                    # Build action_now
                    action_steps = []
                    urgency      = "normal"   # normal | urgent | wait

                    if passed_tp1:
                        action_steps.append({
                            "icon": "🎯",
                            "text": f"Đã qua TP1 ({_fmt(tp1_price)}) — chốt 70% nếu chưa",
                            "color": "success"
                        })
                        action_steps.append({
                            "icon": "🔒",
                            "text": f"Dời SL về TP0 ({_fmt(tp0_price)}) để bảo vệ lãi",
                            "color": "info"
                        })
                        if len(tps) > 2:
                            action_steps.append({
                                "icon": "⏳",
                                "text": f"Giữ 30% → TP2 ({_fmt(tp2_price)})" + (" — BTC hỗ trợ" if btc_ok_for_dir else ""),
                                "color": "info"
                            })
                        urgency = "urgent"

                    elif passed_tp0 or near_tp0:
                        action_steps.append({
                            "icon": "🎯",
                            "text": f"Giá đang {'qua' if passed_tp0 else 'chạm'} TP0 ({_fmt(tp0_price)}) — chốt 30% ngay",
                            "color": "success"
                        })
                        action_steps.append({
                            "icon": "🔒",
                            "text": "Dời SL về break-even (entry) cho phần còn lại",
                            "color": "info"
                        })
                        if btc_ok_for_dir and coin_momentum_ok:
                            action_steps.append({
                                "icon": "✅",
                                "text": f"Momentum thuận — giữ 70% → TP1 ({_fmt(tp1_price)})",
                                "color": "ok"
                            })
                        elif btc_bad_for_dir:
                            action_steps.append({
                                "icon": "⚠️",
                                "text": f"BTC ngược chiều — cân nhắc chốt thêm, chỉ giữ 30% → TP1",
                                "color": "warning"
                            })
                        else:
                            action_steps.append({
                                "icon": "⏳",
                                "text": f"Giữ 70% → TP1 ({_fmt(tp1_price)})",
                                "color": "info"
                            })
                        urgency = "urgent"

                    elif near_tp1:
                        action_steps.append({
                            "icon": "🔜",
                            "text": f"Gần TP1 ({_fmt(tp1_price)}, còn {tp1_dist:.2f}%) — sẵn sàng chốt 50%",
                            "color": "info"
                        })
                        urgency = "normal"

                    else:
                        # Chưa đến TP nào — check momentum
                        if btc_bad_for_dir and not coin_momentum_ok:
                            action_steps.append({
                                "icon": "⚠️",
                                "text": f"BTC và {symbol} đang ngược chiều lệnh — theo dõi kỹ",
                                "color": "warning"
                            })
                            action_steps.append({
                                "icon": "📌",
                                "text": f"Nếu giá quay về entry, cân nhắc cắt lỗ bảo vệ vốn",
                                "color": "warning"
                            })
                            urgency = "urgent"
                        elif btc_ok_for_dir and coin_momentum_ok:
                            action_steps.append({
                                "icon": "✅",
                                "text": f"Momentum thuận — giữ lệnh, TP0 tại {_fmt(tp0_price)} ({tp0_dist:.2f}% nữa)",
                                "color": "ok"
                            })
                            urgency = "normal"
                        else:
                            action_steps.append({
                                "icon": "⏳",
                                "text": f"Chờ lệnh chạy — TP0 còn {tp0_dist:.2f}% ({_fmt(tp0_price)})",
                                "color": "info"
                            })
                            urgency = "normal"

                    action_now = {
                        "coin_price":   _fmt(coin_price),
                        "coin_chg_1h":  coin_chg_1h,
                        "coin_ema_note":coin_ema_note,
                        "steps":        action_steps,
                        "urgency":      urgency,
                        "tp0_dist":     round(tp0_dist, 2),
                        "tp1_dist":     round(tp1_dist, 2) if len(tp_distances) > 1 else None,
                        "passed_tp0":   passed_tp0,
                        "passed_tp1":   passed_tp1,
                    }

            except Exception as _ce:
                coin_ema_note = f"Không fetch được {symbol}: {str(_ce)[:50]}"

        # ═══════════════════════════════════════════════════════════════
        # ENGINE OVERLAY — gọi fam_analyze để lấy view kỹ thuật của engine
        # cho symbol này, từ đó:
        #   1. engine_view: D1/H4 bias, SL/TP kỹ thuật (swing-based, không
        #      phải Fibo cứng từ %), warnings, direction engine khuyến
        #   2. entry_quality: entry user vs entry tối ưu engine — diagnose
        #      vào sai pha / vào sau pump / vào tốt
        #   3. smart_action: tổng hợp HOLD/CUT/PARTIAL/DCA dựa trên
        #      conflict direction + P&L hiện tại + khoảng cách tới
        #      SL/TP kỹ thuật của engine
        # ═══════════════════════════════════════════════════════════════
        engine_view    = None
        entry_quality  = None
        smart_action   = None

        if symbol and coin_price and coin_price > 0:
            try:
                from dashboard.fam_engine import fam_analyze
                eng_cfg = {"force_futures": True, "rr_ratio": 1.0}
                eng = fam_analyze(symbol, eng_cfg)

                eng_dir   = (eng.get("direction") or "WAIT").upper()
                eng_conf  = (eng.get("confidence") or "LOW").upper()
                eng_score = int(eng.get("score") or 0)
                eng_sl    = eng.get("sl")
                eng_tp1   = eng.get("tp1")
                eng_tp2   = eng.get("tp2")
                eng_entry_opt = eng.get("entry_optimal") or eng.get("entry")
                eng_warns = eng.get("warnings") or []
                eng_d1    = (eng.get("d1") or {}).get("bias", "")
                eng_h4    = (eng.get("h4") or {}).get("bias", "")
                eng_h1_status = eng.get("h1_status") or ""

                engine_view = {
                    "direction":     eng_dir,
                    "confidence":    eng_conf,
                    "score":         eng_score,
                    "d1_bias":       eng_d1,
                    "h4_bias":       eng_h4,
                    "h1_status":     eng_h1_status,
                    "sl":            _fmt(float(eng_sl)) if eng_sl else None,
                    "tp1":           _fmt(float(eng_tp1)) if eng_tp1 else None,
                    "tp2":           _fmt(float(eng_tp2)) if eng_tp2 else None,
                    "entry_optimal": _fmt(float(eng_entry_opt)) if eng_entry_opt else None,
                    "warnings":      [str(w) for w in eng_warns[:8]],
                }

                # ── Entry Quality ──
                pnl_pct_now = ((coin_price - entry) / entry * 100) if is_long                                 else ((entry - coin_price) / entry * 100)
                pnl_usd_now = round(pos_size * pnl_pct_now / 100, 2) if pos_size > 0 else 0

                aligned = (is_long and eng_dir == "LONG") or (not is_long and eng_dir == "SHORT")
                conflict = (is_long and eng_dir == "SHORT") or (not is_long and eng_dir == "LONG")

                # So entry user vs entry_optimal engine
                dist_from_opt_pct = None
                entry_verdict_text = ""
                if eng_entry_opt:
                    eo = float(eng_entry_opt)
                    if eo > 0:
                        if is_long:
                            # LONG tốt: entry <= entry_optimal (mua thấp hơn vùng tối ưu = tốt hơn)
                            dist_from_opt_pct = round((entry - eo) / eo * 100, 2)
                            if dist_from_opt_pct > 3:
                                entry_verdict_text = f"Vào cao hơn vùng tối ưu {dist_from_opt_pct}% — chasing top"
                            elif dist_from_opt_pct < -1:
                                entry_verdict_text = f"Vào thấp hơn vùng tối ưu {abs(dist_from_opt_pct)}% — entry đẹp"
                            else:
                                entry_verdict_text = f"Vào sát vùng tối ưu (chênh {dist_from_opt_pct:+.1f}%)"
                        else:
                            # SHORT tốt: entry >= entry_optimal
                            dist_from_opt_pct = round((entry - eo) / eo * 100, 2)
                            if dist_from_opt_pct < -3:
                                entry_verdict_text = f"Vào thấp hơn vùng tối ưu {abs(dist_from_opt_pct)}% — chasing bottom"
                            elif dist_from_opt_pct > 1:
                                entry_verdict_text = f"Vào cao hơn vùng tối ưu {dist_from_opt_pct}% — entry đẹp"
                            else:
                                entry_verdict_text = f"Vào sát vùng tối ưu (chênh {dist_from_opt_pct:+.1f}%)"

                entry_quality = {
                    "aligned":         aligned,
                    "conflict":        conflict,
                    "pnl_pct":         round(pnl_pct_now, 3),
                    "pnl_usd":         pnl_usd_now,
                    "dist_from_opt":   dist_from_opt_pct,
                    "verdict":         entry_verdict_text,
                    "engine_dir":      eng_dir,
                    "user_dir":        direction,
                }

                # ── Smart Action — synthesize ──
                action_label  = "HOLD"
                action_color  = "info"
                action_detail = ""
                action_steps_smart = []

                # Khoảng cách từ giá hiện tại tới SL/TP engine
                eng_sl_f  = float(eng_sl)  if eng_sl  else None
                eng_tp1_f = float(eng_tp1) if eng_tp1 else None
                eng_tp2_f = float(eng_tp2) if eng_tp2 else None

                # Đã chạm/qua TP1 của engine?
                hit_eng_tp1 = False
                if eng_tp1_f:
                    hit_eng_tp1 = (is_long and coin_price >= eng_tp1_f) or                                   (not is_long and coin_price <= eng_tp1_f)

                # Sắp chạm SL của engine?
                near_eng_sl = False
                broke_eng_sl = False
                if eng_sl_f:
                    if is_long:
                        broke_eng_sl = coin_price <= eng_sl_f
                        near_eng_sl  = (coin_price - eng_sl_f) / coin_price < 0.01 and coin_price > eng_sl_f
                    else:
                        broke_eng_sl = coin_price >= eng_sl_f
                        near_eng_sl  = (eng_sl_f - coin_price) / coin_price < 0.01 and coin_price < eng_sl_f

                # ═══════════════════════════════════════════════════════
                # REVERSAL SIGNALS — đo momentum exhaustion theo direction
                # 3 tín hiệu độc lập, count → quyết định mức độ cảnh báo
                # ═══════════════════════════════════════════════════════
                vol_exhaustion  = False
                rsi_divergence  = False
                oi_shift_against = False
                rev_detail      = []

                try:
                    # 1. Volume exhaustion: 2/3 nến gần nhất cùng chiều move với vol < 0.8x
                    if "vol_ratio" in df_coin.columns and len(df_coin) >= 3:
                        weak_count = 0
                        for i in range(-3, 0):
                            o = float(df_coin["open"].iloc[i])
                            c = float(df_coin["close"].iloc[i])
                            v = float(df_coin["vol_ratio"].iloc[i])
                            if is_long and c > o and v < 0.8:
                                weak_count += 1
                            elif (not is_long) and c < o and v < 0.8:
                                weak_count += 1
                        if weak_count >= 2:
                            vol_exhaustion = True
                            rev_detail.append(f"Vol exhaustion: {weak_count}/3 nến {'tăng' if is_long else 'giảm'} với vol < 0.8x")

                    # 2. RSI divergence H1 (5 nến gần vs 5 nến trước đó)
                    if "rsi" in df_coin.columns and len(df_coin) >= 10:
                        recent = df_coin.iloc[-5:]
                        prior  = df_coin.iloc[-10:-5]
                        if is_long:
                            # Bear divergence: price HH but RSI LH → cảnh báo LONG
                            p_hh = float(recent["high"].max()) > float(prior["high"].max())
                            r_lh = float(recent["rsi"].max()) < float(prior["rsi"].max())
                            if p_hh and r_lh:
                                rsi_divergence = True
                                rev_detail.append(f"RSI bear div: giá HH nhưng RSI {float(recent['rsi'].max()):.0f} < {float(prior['rsi'].max()):.0f}")
                        else:
                            # Bull divergence: price LL but RSI HL → cảnh báo SHORT
                            p_ll = float(recent["low"].min()) < float(prior["low"].min())
                            r_hl = float(recent["rsi"].min()) > float(prior["rsi"].min())
                            if p_ll and r_hl:
                                rsi_divergence = True
                                rev_detail.append(f"RSI bull div: giá LL nhưng RSI {float(recent['rsi'].min()):.0f} > {float(prior['rsi'].min()):.0f}")

                    # 3. OI shift against position
                    # SHORT: OI giảm > 1.5% → shorts đang đóng → bullish
                    # LONG: OI giảm > 1.5% → longs đang đóng → bearish
                    if oi_change is not None and oi_change < -1.5:
                        oi_shift_against = True
                        rev_detail.append(f"OI {oi_change:+.1f}% — vị thế cùng chiều đang đóng dần (unwinding)")
                except Exception as _re:
                    pass

                rev_count = sum([vol_exhaustion, rsi_divergence, oi_shift_against])

                # ═══════════════════════════════════════════════════════
                # SWING LEVELS — tìm swing high/low gần nhất từ H1
                # Dùng để: trail SL theo cấu trúc + suggest level chốt partial
                # ═══════════════════════════════════════════════════════
                swing_high_30 = swing_low_30 = None
                swing_high_10 = swing_low_10 = None
                trail_sl_suggest = None
                partial_level    = None

                try:
                    if len(df_coin) >= 30:
                        swing_high_30 = float(df_coin["high"].iloc[-30:].max())
                        swing_low_30  = float(df_coin["low"].iloc[-30:].min())
                    if len(df_coin) >= 10:
                        swing_high_10 = float(df_coin["high"].iloc[-10:].max())
                        swing_low_10  = float(df_coin["low"].iloc[-10:].min())

                    # Trail SL theo cấu trúc:
                    # SHORT: swing high gần nhất + buffer 0.3% (chỗ phá cấu trúc)
                    # LONG: swing low gần nhất - buffer 0.3%
                    if is_long and swing_low_10:
                        trail_sl_suggest = round(swing_low_10 * 0.997, 6)
                    elif (not is_long) and swing_high_10:
                        trail_sl_suggest = round(swing_high_10 * 1.003, 6)

                    # Partial level: swing low/high 30 nến (vùng support/resistance to)
                    # Chỉ suggest nếu giá còn cách >= 0.5% (chưa quá gần)
                    if is_long and swing_high_30 and swing_high_30 > coin_price * 1.005:
                        partial_level = round(swing_high_30 * 0.998, 6)
                    elif (not is_long) and swing_low_30 and swing_low_30 < coin_price * 0.995:
                        partial_level = round(swing_low_30 * 1.002, 6)
                except Exception:
                    pass

                # ═══════════════════════════════════════════════════════
                # SL ZONE DIAGNOSIS — SL của user đang ở đâu?
                # loss   = SL còn ở vùng lỗ (chưa BE)
                # be     = SL ở break-even (gần entry)
                # profit = SL đã lock profit (đã trail vào vùng lãi)
                # ═══════════════════════════════════════════════════════
                sl_zone = "loss"
                if sl_implied:
                    if is_long:
                        if sl_implied >= entry * 1.001:
                            sl_zone = "profit"
                        elif sl_implied >= entry * 0.999:
                            sl_zone = "be"
                    else:
                        if sl_implied <= entry * 0.999:
                            sl_zone = "profit"
                        elif sl_implied <= entry * 1.001:
                            sl_zone = "be"

                # ═══════════════════════════════════════════════════════
                # 3-TIER ACTION CLASSIFICATION với SL-aware mode
                # Priority order:
                #   1. Hard exits (conflict, broke SL eng, hit TP1 eng)
                #   2. SL ở profit zone → MONITOR (đã protected, ít can thiệp)
                #   3. SL ở loss/BE → dùng rev_count để quyết định CHỐT/CẢNH GIÁC/HOLD
                # ═══════════════════════════════════════════════════════

                # ── PRIORITY 1: Hard exits ──
                if conflict:
                    action_label  = "CẮT NGAY"
                    action_color  = "danger"
                    action_detail = f"Engine flip ngược chiều ({eng_dir} {eng_conf}) — không hợp lệ tiếp tục giữ"
                    action_steps_smart.append({
                        "icon": "🚫",
                        "text": f"Engine khuyến {eng_dir} ngược lại với lệnh {direction} của bạn",
                        "color": "warning",
                    })
                    if pnl_pct_now > 0:
                        action_steps_smart.append({
                            "icon": "💰",
                            "text": f"Đang lãi {pnl_pct_now:+.2f}% ({pnl_usd_now:+.2f} USDT) — chốt toàn bộ ngay",
                            "color": "success",
                        })
                    else:
                        action_steps_smart.append({
                            "icon": "✂️",
                            "text": f"Đang lỗ {pnl_pct_now:.2f}% ({pnl_usd_now:.2f} USDT) — cắt lỗ, không cố gồng",
                            "color": "warning",
                        })

                elif broke_eng_sl:
                    action_label  = "CẮT NGAY"
                    action_color  = "danger"
                    action_detail = f"Giá đã thủng SL kỹ thuật engine ({_fmt(eng_sl_f)}) — cấu trúc đã gãy"
                    action_steps_smart.append({
                        "icon": "🚨",
                        "text": f"Giá {_fmt(coin_price)} đã thủng SL kỹ thuật {_fmt(eng_sl_f)} — không còn lý do hold",
                        "color": "warning",
                    })

                elif hit_eng_tp1:
                    action_label  = "CHỐT 50–70%"
                    action_color  = "success"
                    action_detail = f"Đã chạm/qua TP1 kỹ thuật ({_fmt(eng_tp1_f)}) — chốt lợi nhuận một phần"
                    action_steps_smart.append({
                        "icon": "🎯",
                        "text": f"Chốt 50–70% tại {_fmt(coin_price)} (đã qua TP1 engine {_fmt(eng_tp1_f)})",
                        "color": "success",
                    })
                    action_steps_smart.append({
                        "icon": "🔒",
                        "text": "Dời SL về break-even (entry) cho phần còn lại",
                        "color": "info",
                    })
                    if eng_tp2_f:
                        action_steps_smart.append({
                            "icon": "⏳",
                            "text": f"Giữ 30% kéo về TP2 engine {_fmt(eng_tp2_f)}",
                            "color": "info",
                        })

                # ── PRIORITY 2: SL đã ở profit zone → MONITOR mode ──
                elif sl_zone == "profit":
                    if rev_count >= 2:
                        action_label  = "CẢNH GIÁC — sẵn sàng cắt tay"
                        action_color  = "warning"
                        action_detail = f"SL đã lock profit + có {rev_count}/3 reversal signal — chuẩn bị cắt thủ công nếu thêm signal"
                    else:
                        action_label  = "MONITOR (SL đã lock profit)"
                        action_color  = "success"
                        action_detail = f"SL đã ở vùng profit → downside = 0. {rev_count}/3 reversal signal — để market quyết định"
                    action_steps_smart.append({
                        "icon": "🔒",
                        "text": f"SL hiện tại {_fmt(sl_implied)} đã lock profit — không thể lỗ",
                        "color": "success",
                    })
                    if partial_level and rev_count >= 1:
                        action_steps_smart.append({
                            "icon": "⏰",
                            "text": f"Đặt limit chốt 30–50% ở swing level {_fmt(partial_level)} (vùng kháng cự/hỗ trợ to gần nhất)",
                            "color": "info",
                        })

                elif sl_zone == "be":
                    action_label  = "HOLD (SL ở BE)"
                    action_color  = "info"
                    action_detail = f"SL ở break-even — không thể lỗ. {rev_count}/3 reversal signal."
                    action_steps_smart.append({
                        "icon": "🛡️",
                        "text": f"SL ở BE {_fmt(sl_implied)} — đã free trade, để market quyết",
                        "color": "info",
                    })
                    if rev_count >= 2 and partial_level:
                        action_steps_smart.append({
                            "icon": "⚠️",
                            "text": f"{rev_count}/3 reversal signal — đặt limit chốt 30% ở {_fmt(partial_level)} chủ động",
                            "color": "warning",
                        })

                # ── PRIORITY 3: SL còn ở loss zone — dùng rev_count quyết định ──
                elif rev_count >= 2:
                    action_label  = "CHỐT 30–50% PARTIAL"
                    action_color  = "warning"
                    action_detail = f"{rev_count}/3 reversal signal + SL còn ở vùng lỗ → bảo vệ vốn ngay"
                    action_steps_smart.append({
                        "icon": "✂️",
                        "text": f"Chốt 30–50% tại {_fmt(coin_price)} để giảm risk khi reversal đang hình thành",
                        "color": "warning",
                    })
                    if trail_sl_suggest:
                        action_steps_smart.append({
                            "icon": "🛡️",
                            "text": f"Phần còn lại: dời SL về {_fmt(trail_sl_suggest)} (theo swing structure gần nhất)",
                            "color": "info",
                        })

                elif near_eng_sl:
                    action_label  = "THẬN TRỌNG"
                    action_color  = "warning"
                    action_detail = f"Giá sát SL kỹ thuật engine ({_fmt(eng_sl_f)}) — cân nhắc cắt sớm"
                    action_steps_smart.append({
                        "icon": "⚠️",
                        "text": f"Còn cách SL engine ~1% — nếu vol bán mạnh, cắt trước khi thủng",
                        "color": "warning",
                    })

                elif eng_dir == "WAIT":
                    if rev_count >= 1:
                        action_label  = "CẢNH GIÁC"
                        action_color  = "warning"
                        action_detail = f"Engine WAIT + {rev_count}/3 reversal signal — tighten SL về cấu trúc, đừng vội chốt"
                    else:
                        action_label  = "HOLD CẨN THẬN"
                        action_color  = "warning"
                        action_detail = f"Engine WAIT (confidence {eng_conf}) — cấu trúc yếu, không phải lúc DCA"
                    if pnl_pct_now < -2:
                        action_steps_smart.append({
                            "icon": "📉",
                            "text": f"Đang lỗ {pnl_pct_now:.2f}% — cắt nhẹ giảm risk",
                            "color": "warning",
                        })
                    elif trail_sl_suggest:
                        action_steps_smart.append({
                            "icon": "🛡️",
                            "text": f"Tighten SL về {_fmt(trail_sl_suggest)} (swing gần nhất) thay vì giữ SL gốc",
                            "color": "info",
                        })
                    else:
                        action_steps_smart.append({
                            "icon": "👀",
                            "text": "Engine chưa rõ — giữ SL gốc, không thêm vị thế",
                            "color": "info",
                        })

                else:
                    # Aligned + chưa đến TP/SL — phân loại theo rev_count
                    if rev_count >= 1:
                        action_label  = "CẢNH GIÁC"
                        action_color  = "warning"
                        action_detail = f"Engine xác nhận {eng_dir} nhưng có {rev_count}/3 reversal signal — tighten SL, chưa cần chốt"
                    else:
                        action_label  = "HOLD vững"
                        action_color  = "success" if eng_conf == "HIGH" else "info"
                        action_detail = f"Engine xác nhận {eng_dir} ({eng_conf}, score {eng_score}), 0/3 reversal signal — kế hoạch đang đúng"

                    if trail_sl_suggest:
                        action_steps_smart.append({
                            "icon": "🛡️",
                            "text": f"Trail SL về {_fmt(trail_sl_suggest)} (swing structure 10 nến gần nhất)",
                            "color": "info",
                        })

                    if eng_tp1_f:
                        dist_to_tp1 = abs(eng_tp1_f - coin_price) / coin_price * 100
                        action_steps_smart.append({
                            "icon": "🎯",
                            "text": f"TP1 engine: {_fmt(eng_tp1_f)} (còn {dist_to_tp1:.1f}%) — chốt 50% khi chạm",
                            "color": "info",
                        })

                    # Có cơ hội DCA chỉ khi 0 reversal signal + giá ở vùng entry tốt
                    if rev_count == 0 and eng_entry_opt and dist_from_opt_pct is not None:
                        can_dca = (is_long and dist_from_opt_pct < -2) or                                   ((not is_long) and dist_from_opt_pct > 2)
                        if can_dca:
                            action_steps_smart.append({
                                "icon": "💎",
                                "text": f"Giá ở vùng entry tốt (chênh {dist_from_opt_pct:+.1f}%) — có thể DCA nhỏ",
                                "color": "ok",
                            })

                # Append reversal signals breakdown như info (luôn hiển thị)
                if rev_count > 0 and rev_detail:
                    for rd in rev_detail:
                        action_steps_smart.append({
                            "icon": "🔍",
                            "text": rd,
                            "color": "warning" if rev_count >= 2 else "info",
                        })

                # Append warnings từ engine vào action steps nếu là blocker
                blocker_keywords = ("🚫", "BLOCK", "VETO", "EXHAUSTION", "PUMP", "DUMP")
                for w in eng_warns[:4]:
                    if any(k in str(w) for k in blocker_keywords):
                        action_steps_smart.append({
                            "icon": "📛",
                            "text": str(w),
                            "color": "warning",
                        })

                smart_action = {
                    "label":   action_label,
                    "color":   action_color,
                    "detail":  action_detail,
                    "steps":   action_steps_smart,
                    "pnl_pct": round(pnl_pct_now, 3),
                    "pnl_usd": pnl_usd_now,
                    # v2 fields — đo lường framework mới
                    "rev_count":         rev_count,
                    "rev_signals": {
                        "vol_exhaustion":   vol_exhaustion,
                        "rsi_divergence":   rsi_divergence,
                        "oi_shift_against": oi_shift_against,
                    },
                    "rev_detail":        rev_detail,
                    "sl_zone":           sl_zone,
                    "trail_sl_suggest":  _fmt(trail_sl_suggest) if trail_sl_suggest else None,
                    "partial_level":     _fmt(partial_level) if partial_level else None,
                    "swing_high_10":     _fmt(swing_high_10) if swing_high_10 else None,
                    "swing_low_10":      _fmt(swing_low_10) if swing_low_10 else None,
                    "swing_high_30":     _fmt(swing_high_30) if swing_high_30 else None,
                    "swing_low_30":      _fmt(swing_low_30) if swing_low_30 else None,
                }

            except Exception as _eng_e:
                engine_view = {"error": f"Không chạy được engine: {str(_eng_e)[:80]}"}

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
            "action_now":   action_now,
            "coin_price":   _fmt(coin_price) if coin_price else None,
            "coin_chg_1h":  coin_chg_1h,
            "engine_view":   engine_view,
            "entry_quality": entry_quality,
            "smart_action":  smart_action,
            "generated_at": _local_isoformat(),
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-600:]}), 500


@app.route("/api/entry-advice", methods=["POST"])
def entry_advice():
    """
    Gợi ý LONG/SHORT + điểm vào tối ưu cho 1 symbol.
    Tổng hợp: TREND engine + REVERSAL engine + reversal signals + swing levels +
    fade-bounce detection + structure analysis. Trả về 1 setup hoàn chỉnh
    (entry strategy LIMIT/MARKET, SL, TP1, TP2, R:R, invalidation, reasoning).

    Input: { symbol, risk_pct? (default 1.5) }
    """
    try:
        from core.binance import fetch_klines
        from core.indicators import prepare
        from dashboard.fam_engine      import fam_analyze
        from dashboard.reversal_engine import reversal_analyze

        def _fmt(v):
            if v is None: return None
            n = abs(float(v))
            d = 8 if n < 0.000001 else 6 if n < 0.0001 else 5 if n < 0.01 else 4 if n < 1 else 2
            return round(float(v), d)

        data     = request.json or {}
        symbol   = (data.get("symbol", "") or "").upper().strip()
        if not symbol:
            return jsonify({"error": "Thiếu symbol"}), 400
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        # ── Fetch data ──
        df_h4  = prepare(fetch_klines(symbol, "4h",  100, force_futures=True))
        df_h1  = prepare(fetch_klines(symbol, "1h",  150, force_futures=True))

        if len(df_h1) < 30 or len(df_h4) < 10:
            return jsonify({"error": f"Không đủ data cho {symbol}"}), 400

        price     = float(df_h1["close"].iloc[-1])
        atr_h1    = float(df_h1["atr"].iloc[-1])
        ema34_h1  = float(df_h1["ma34"].iloc[-1])
        ema89_h1  = float(df_h1["ma89"].iloc[-1])
        ema200_h1 = float(df_h1["ma200"].iloc[-1])
        ema34_h4  = float(df_h4["ma34"].iloc[-1])
        ema89_h4  = float(df_h4["ma89"].iloc[-1])

        # ── Run engines ──
        eng_cfg = {"force_futures": True, "rr_ratio": 1.0}
        try:
            trend = fam_analyze(symbol, eng_cfg)
        except Exception as _te:
            trend = {"direction": "WAIT", "confidence": "LOW", "score": 0, "warnings": [f"TREND error: {str(_te)[:80]}"]}
        try:
            rev = reversal_analyze(symbol, eng_cfg)
        except Exception:
            rev = {"direction": "WAIT", "confidence": "LOW", "score": 0}

        # ── Context: pump/dump pattern recent ──
        recent_high_10 = float(df_h1["high"].iloc[-10:].max())
        recent_low_10  = float(df_h1["low"].iloc[-10:].min())
        recent_drop_pct = (recent_high_10 - price) / recent_high_10 * 100  # >5 = vừa drop từ high
        recent_pump_pct = (price - recent_low_10) / recent_low_10 * 100    # >5 = vừa pump từ low

        # Bounce volume yếu (3 nến gần)
        bounce_vol_avg = float(df_h1["vol_ratio"].iloc[-3:].mean()) if "vol_ratio" in df_h1.columns else 1.0

        # Đỉnh đã hình thành chưa? (giá đã cách đỉnh > 2%)
        peak_made     = recent_drop_pct > 2
        bottom_made   = recent_pump_pct > 2

        # FADE BOUNCE patterns
        fade_short = (recent_drop_pct >= 4 and peak_made and bounce_vol_avg < 0.8)
        fade_long  = (recent_pump_pct >= 4 and bottom_made and bounce_vol_avg < 0.8 and price < ema34_h1)

        # ── Swing levels for entry/SL/TP ──
        swing_high_5  = float(df_h1["high"].iloc[-5:].max())
        swing_low_5   = float(df_h1["low"].iloc[-5:].min())
        swing_high_20 = float(df_h1["high"].iloc[-20:].max())
        swing_low_20  = float(df_h1["low"].iloc[-20:].min())

        # ── DECISION TREE ──
        decision      = None
        reasoning     = []

        trend_dir   = (trend.get("direction") or "WAIT").upper()
        trend_conf  = (trend.get("confidence") or "LOW").upper()
        trend_score = int(trend.get("score") or 0)
        rev_dir     = (rev.get("direction") or "WAIT").upper()
        rev_conf    = (rev.get("confidence") or "LOW").upper()
        rev_score   = int(rev.get("score") or 0)

        # Priority 1: TREND HIGH aligned
        if trend_conf == "HIGH" and trend_dir in ("LONG", "SHORT"):
            decision = {
                "direction":  trend_dir,
                "setup_type": "TREND_CONTINUATION",
                "confidence": "HIGH",
            }
            reasoning = [
                f"TREND engine HIGH (score {trend_score})",
                f"D1 {(trend.get('d1') or {}).get('bias','?')} / H4 {(trend.get('h4') or {}).get('bias','?')} — multi-TF aligned",
            ]
            for c in (trend.get("conditions") or [])[:3]:
                reasoning.append(c)

        # Priority 2: FADE BOUNCE (sau pump-and-dump)
        elif fade_short:
            decision = {
                "direction":  "SHORT",
                "setup_type": "FADE_BOUNCE",
                "confidence": "MEDIUM",
            }
            reasoning = [
                f"Vừa drop {recent_drop_pct:.1f}% từ đỉnh {_fmt(recent_high_10)} (10h gần)",
                f"Bounce volume yếu ({bounce_vol_avg:.2f}x baseline) — không có lực cầu thật",
                f"Cấu trúc bearish chưa break — fade ở vùng resistance {_fmt(swing_high_5)}",
            ]
            if trend_dir == "SHORT": reasoning.append(f"TREND engine cũng SHORT ({trend_conf}) — confluence")
        elif fade_long:
            decision = {
                "direction":  "LONG",
                "setup_type": "FADE_BOUNCE",
                "confidence": "MEDIUM",
            }
            reasoning = [
                f"Vừa pump {recent_pump_pct:.1f}% từ đáy {_fmt(recent_low_10)} (10h gần)",
                f"Pullback volume yếu ({bounce_vol_avg:.2f}x) — sellers exhausting",
                f"Long ở vùng support {_fmt(swing_low_5)}",
            ]

        # Priority 3: REVERSAL HIGH
        elif rev_conf == "HIGH" and rev_dir in ("LONG", "SHORT"):
            decision = {
                "direction":  rev_dir,
                "setup_type": "REVERSAL",
                "confidence": "HIGH",
            }
            reasoning = [f"REVERSAL engine HIGH (score {rev_score})"] + [str(c) for c in (rev.get("conditions") or [])[:3]]

        # Priority 4: TREND MEDIUM
        elif trend_conf == "MEDIUM" and trend_dir in ("LONG", "SHORT"):
            decision = {
                "direction":  trend_dir,
                "setup_type": "TREND_PULLBACK",
                "confidence": "MEDIUM",
            }
            reasoning = [
                f"TREND MEDIUM ({trend_score}/5)",
                f"Chờ pullback về vùng entry tốt — không market ngay",
            ]

        # No setup
        else:
            decision = {
                "direction":  "WAIT",
                "setup_type": "NO_SETUP",
                "confidence": "LOW",
            }
            reasoning = [
                "Không có setup rõ ràng tại thời điểm này",
                f"TREND: {trend_dir}/{trend_conf} (score {trend_score})",
                f"REVERSAL: {rev_dir}/{rev_conf} (score {rev_score})",
                f"Recent move: {recent_drop_pct:.1f}% drop / {recent_pump_pct:.1f}% pump",
            ]

        # ── Compute entry / SL / TP based on direction + setup_type ──
        result = {
            "symbol":        symbol,
            "current_price": _fmt(price),
            "ema34_h1":      _fmt(ema34_h1),
            "ema89_h1":      _fmt(ema89_h1),
            "ema200_h1":     _fmt(ema200_h1),
            "atr_h1":        _fmt(atr_h1),
            "swing_high_20": _fmt(swing_high_20),
            "swing_low_20":  _fmt(swing_low_20),
            "trend_engine": {
                "direction":  trend_dir,
                "confidence": trend_conf,
                "score":      trend_score,
                "warnings":   [str(w) for w in (trend.get("warnings") or [])[:5]],
            },
            "reversal_engine": {
                "direction":  rev_dir,
                "confidence": rev_conf,
                "score":      rev_score,
            },
            "context": {
                "recent_drop_pct":  round(recent_drop_pct, 2),
                "recent_pump_pct":  round(recent_pump_pct, 2),
                "bounce_vol_avg":   round(bounce_vol_avg, 2),
                "fade_short":       fade_short,
                "fade_long":        fade_long,
            },
            "reasoning":     reasoning,
            **decision,
            "entry_strategy": None,
            "entry_price":    None,
            "sl":             None,
            "tp1":            None,
            "tp2":            None,
            "rr":             None,
            "invalidation":   None,
        }

        if decision["direction"] in ("LONG", "SHORT"):
            is_long  = decision["direction"] == "LONG"
            stype    = decision["setup_type"]
            entry_price = price
            entry_strategy = "MARKET"
            sl = tp1 = tp2 = invalidation = None

            if stype == "FADE_BOUNCE":
                # SHORT: limit ở vùng resistance gần (swing_high_5 hoặc EMA34/89 H1)
                # LONG: limit ở vùng support gần
                if is_long:
                    candidates = [c for c in [ema34_h1, ema89_h1, swing_low_5 * 1.005] if c < price * 0.998]
                    entry_price = max(candidates) if candidates else price
                    sl  = min(swing_low_20 * 0.997, entry_price - atr_h1 * 1.5)
                    tp1 = swing_high_5 * 0.998 if swing_high_5 > entry_price * 1.01 else entry_price + atr_h1 * 3
                    tp2 = tp1 + (tp1 - entry_price) * 0.8
                    invalidation = swing_low_20 * 0.995
                else:
                    candidates = [c for c in [ema34_h1, ema89_h1, swing_high_5 * 0.995] if c > price * 1.002]
                    entry_price = min(candidates) if candidates else price
                    sl  = max(swing_high_20 * 1.003, entry_price + atr_h1 * 1.5)
                    tp1 = swing_low_5 * 1.002 if swing_low_5 < entry_price * 0.99 else entry_price - atr_h1 * 3
                    tp2 = tp1 - (entry_price - tp1) * 0.8
                    invalidation = swing_high_20 * 1.005
                entry_strategy = "LIMIT" if abs(entry_price - price) / price > 0.003 else "MARKET"

            elif stype in ("TREND_CONTINUATION", "TREND_PULLBACK"):
                if is_long:
                    if price > ema34_h1 * 1.012:
                        entry_price = ema34_h1 * 1.003
                        entry_strategy = "LIMIT"
                    sl  = min(ema89_h1 * 0.997, entry_price - atr_h1 * 1.5)
                    tp1 = swing_high_20 * 0.998
                    if tp1 < entry_price * 1.015: tp1 = entry_price + atr_h1 * 3
                    tp2 = entry_price + (entry_price - sl) * 2.5
                    invalidation = ema89_h1 * 0.995
                else:
                    if price < ema34_h1 * 0.988:
                        entry_price = ema34_h1 * 0.997
                        entry_strategy = "LIMIT"
                    sl  = max(ema89_h1 * 1.003, entry_price + atr_h1 * 1.5)
                    tp1 = swing_low_20 * 1.002
                    if tp1 > entry_price * 0.985: tp1 = entry_price - atr_h1 * 3
                    tp2 = entry_price - (sl - entry_price) * 2.5
                    invalidation = ema89_h1 * 1.005

            elif stype == "REVERSAL":
                # Sử dụng SL/TP từ reversal engine
                sl   = float(rev.get("sl")  or 0) or (price * (0.98 if is_long else 1.02))
                tp1  = float(rev.get("tp1") or 0) or (price * (1.02 if is_long else 0.98))
                tp2  = float(rev.get("tp2") or 0) or (price * (1.04 if is_long else 0.96))
                invalidation = sl

            # Compute R:R
            if entry_price and sl and tp1:
                if is_long:
                    risk   = entry_price - sl
                    reward = tp1 - entry_price
                else:
                    risk   = sl - entry_price
                    reward = entry_price - tp1
                rr = round(reward / risk, 2) if risk > 0 else 0
            else:
                rr = 0

            # Sanity: nếu R:R < 1.2 thì cấu hình entry/SL có vấn đề → flag
            quality = "OK"
            if rr < 1.2:
                quality = "POOR_RR"
                reasoning.append(f"⚠️ R:R thấp ({rr}) — setup này entry-SL chưa tối ưu, cân nhắc chờ giá về vùng tốt hơn")
            elif rr >= 2.5:
                quality = "EXCELLENT"

            result.update({
                "entry_strategy": entry_strategy,
                "entry_price":    _fmt(entry_price),
                "sl":             _fmt(sl),
                "tp1":            _fmt(tp1),
                "tp2":            _fmt(tp2),
                "rr":             rr,
                "invalidation":   _fmt(invalidation),
                "quality":        quality,
            })

        result["generated_at"] = _local_isoformat()
        return jsonify(result)

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-500:]}), 500


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
    max_signals_req = int(data.get("max_signals", 200))  # default 200 (vs 50 cũ)

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
        # Dùng UTC-aware comparison để tránh lệch timezone
        tz_vn = timezone(timedelta(hours=7))
        cutoff = datetime.now(tz_vn) - timedelta(hours=hours_ago)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")  # so sánh string
        def sig_after_cutoff(h):
            raw = h.get("time", "")
            if not raw: return False
            # So sánh string 19 ký tự đầu (bỏ timezone suffix)
            return raw[:19] >= cutoff_str
        signals = [h for h in signals if sig_after_cutoff(h)]

    if not signals:
        return jsonify({"results": [], "summary": {}, "error": "Không có signal phù hợp"})

    # Sort mới nhất trước → lấy 50 signal mới nhất (không phải cũ nhất)
    signals.sort(key=lambda h: h.get("time", "")[:19], reverse=True)

    # Backtest parallel — tránh timeout khi có nhiều signal
    # Cap dynamic theo request (default 200, max hard 500 để tránh OOM)
    results = []
    total_available = len(signals)
    max_signals = min(max(max_signals_req, 1), 500)
    signals_to_run = signals[:max_signals]
    truncated = total_available > max_signals
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(backtest_signal, sig): sig for sig in signals_to_run}
        for fut in concurrent.futures.as_completed(futures, timeout=180):
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

    # Warn nếu nhiều lệnh có bt_candles=0 (close check, không phải replay H1)
    stale_count = sum(1 for r in closed if r.get("bt_candles") == 0 and
                      "đã dưới SL" in (r.get("bt_note") or "") or
                      "đã trên SL" in (r.get("bt_note") or ""))

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
        "total":            len(signals_to_run),
        "total_available":  total_available,
        "truncated":        truncated,
        "max_signals_used": max_signals,
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
        "stale_signals":    stale_count,
        "stale_note":       f"{stale_count} lệnh close-check (giá đã qua SL/TP trước khi backtest chạy)" if stale_count > 0 else "",
        "truncated_note":   f"Có {total_available} signals trong khoảng thời gian này, chỉ chạy {max_signals} mới nhất. Tăng max_signals để chạy thêm." if truncated else "",
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

@app.route("/api/backtest/auto-analyze", methods=["POST"])
def auto_analyze_backtest():
    """Tự động phân tích backtest results → trả về insights + đề xuất cải thiện.
    Frontend có thể gọi định kỳ hoặc khi user bấm nút.
    """
    import concurrent.futures
    from collections import defaultdict

    data = request.json or {}
    hours_ago = int(data.get("hours_ago", 24))

    # Load & filter history
    history = load_history()
    if not history:
        return jsonify({"ok": False, "error": "Không có history"})

    tz_vn = timezone(timedelta(hours=7))
    cutoff_str = (datetime.now(tz_vn) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    signals = [h for h in history
               if h.get("direction") in ("LONG", "SHORT")
               and h.get("time", "")[:19] >= cutoff_str]

    if len(signals) < 5:
        return jsonify({"ok": False, "error": f"Chỉ có {len(signals)} signals trong {hours_ago}h — cần ít nhất 5"})

    # Backtest
    signals_to_bt = sorted(signals, key=lambda h: h.get("time", "")[:19], reverse=True)[:50]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(backtest_signal, sig): sig for sig in signals_to_bt}
        for fut in concurrent.futures.as_completed(futures, timeout=110):
            try:
                results.append(fut.result())
            except Exception:
                pass

    wins   = [r for r in results if r.get("bt_result") == "WIN"]
    losses = [r for r in results if r.get("bt_result") == "LOSS"]
    closed = wins + losses
    if not closed:
        return jsonify({"ok": False, "error": "Không có signal closed (WIN/LOSS)"})

    # ── Analysis ──
    insights = []
    recommendations = []

    # 1. Overall win rate
    wr = round(len(wins) / len(closed) * 100, 1)
    insights.append(f"Win rate: {wr}% ({len(wins)}W/{len(losses)}L)")

    # 2. LONG vs SHORT
    long_w = len([r for r in wins if r["direction"] == "LONG"])
    long_l = len([r for r in losses if r["direction"] == "LONG"])
    short_w = len([r for r in wins if r["direction"] == "SHORT"])
    short_l = len([r for r in losses if r["direction"] == "SHORT"])
    long_wr = round(long_w / (long_w + long_l) * 100) if (long_w + long_l) else 0
    short_wr = round(short_w / (short_w + short_l) * 100) if (short_w + short_l) else 0
    insights.append(f"LONG: {long_wr}% WR ({long_w}W/{long_l}L) | SHORT: {short_wr}% WR ({short_w}W/{short_l}L)")
    if long_wr < 35 and long_l >= 3:
        recommendations.append({"type": "direction", "severity": "HIGH",
                                 "msg": f"LONG win rate chỉ {long_wr}% — cân nhắc giảm LONG signals hoặc thắt filter"})

    # 3. SL analysis
    loss_sls = [r.get("sl_pct", 0) for r in losses]
    win_sls = [r.get("sl_pct", 0) for r in wins]
    avg_loss_sl = round(sum(loss_sls) / len(loss_sls), 2) if loss_sls else 0
    avg_win_sl = round(sum(win_sls) / len(win_sls), 2) if win_sls else 0
    tight_sl_losses = len([s for s in loss_sls if s <= 0.4])
    if tight_sl_losses >= 3:
        pct = round(tight_sl_losses / len(losses) * 100)
        recommendations.append({"type": "sl", "severity": "HIGH",
                                 "msg": f"SL ≤ 0.4%: {tight_sl_losses} LOSS ({pct}%) — SL quá chặt, nên nới lên 0.5%+"})
    insights.append(f"SL trung bình — LOSS: {avg_loss_sl}% | WIN: {avg_win_sl}%")

    # 4. OI correlation
    oi_loss = [r.get("oi_change", 0) or 0 for r in losses]
    oi_win = [r.get("oi_change", 0) or 0 for r in wins]
    avg_oi_loss = round(sum(oi_loss) / len(oi_loss), 2) if oi_loss else 0
    avg_oi_win = round(sum(oi_win) / len(oi_win), 2) if oi_win else 0
    insights.append(f"OI trung bình — LOSS: {avg_oi_loss:+.2f}% | WIN: {avg_oi_win:+.2f}%")

    # 5. Counter-trend
    counter_losses = len([r for r in losses if r["direction"] == "LONG"
                          and r.get("btc_sentiment") in ("RISK_OFF", "DUMP")])
    if counter_losses >= 3:
        pct = round(counter_losses / len(losses) * 100)
        recommendations.append({"type": "counter_trend", "severity": "HIGH",
                                 "msg": f"LONG counter-trend (BTC RISK_OFF): {counter_losses} LOSS ({pct}%) — block LONG altcoin khi BTC BEAR"})

    # 6. Score analysis
    score_perf = defaultdict(lambda: {"w": 0, "l": 0})
    for r in wins: score_perf[r.get("score", 0)]["w"] += 1
    for r in losses: score_perf[r.get("score", 0)]["l"] += 1
    score_insights = []
    for sc in sorted(score_perf.keys()):
        d = score_perf[sc]
        total = d["w"] + d["l"]
        swr = round(d["w"] / total * 100) if total else 0
        score_insights.append({"score": sc, "wins": d["w"], "losses": d["l"], "wr": swr})

    return jsonify({
        "ok": True,
        "period_hours": hours_ago,
        "total_signals": len(signals_to_bt),
        "closed": len(closed),
        "win_rate": wr,
        "insights": insights,
        "recommendations": recommendations,
        "direction": {"long_wr": long_wr, "short_wr": short_wr,
                      "long_w": long_w, "long_l": long_l,
                      "short_w": short_w, "short_l": short_l},
        "sl_analysis": {"avg_loss_sl": avg_loss_sl, "avg_win_sl": avg_win_sl,
                        "tight_sl_losses": tight_sl_losses},
        "oi_correlation": {"avg_loss": avg_oi_loss, "avg_win": avg_oi_win},
        "score_analysis": score_insights,
    })

@app.route("/api/scanner/start", methods=["POST"])
def start_dashboard_scanner():
    global scanner_running
    if not scanner_running:
        scanner_running = True
        threading.Thread(target=dashboard_scanner_loop, daemon=True).start()
        threading.Thread(target=watchlist_fast_loop, daemon=True).start()
        threading.Thread(target=position_monitor_loop, daemon=True).start()
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
    # Khi đang scan (results=[]), trả last_results để frontend vẫn show kết quả cũ
    results = scan_state["results"]
    if not results:
        results = scan_state.get("last_results", [])
    return jsonify(results)

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

def _parse_position_command(text: str) -> dict:
    """Parse các command position từ Telegram.
    Cú pháp:
      /pos add ARB short 0.1295 [sl 0.1310] [tp 0.1280]
      /pos add BTC long 75000 sl 74000 tp 76500
      /pos list
      /pos del <id>
      /pos help
    """
    parts = text.strip().split()
    if not parts or not parts[0].startswith('/pos'):
        return None

    if len(parts) == 1:
        return {"action": "help"}

    cmd = parts[1].lower()

    if cmd == "list":
        return {"action": "list"}
    if cmd == "help":
        return {"action": "help"}
    if cmd == "del" and len(parts) >= 3:
        try:
            return {"action": "del", "id": int(parts[2])}
        except ValueError:
            return {"action": "error", "msg": "ID phải là số"}

    if cmd == "add" and len(parts) >= 5:
        try:
            symbol = parts[2].upper()
            if not symbol.endswith("USDT"):
                symbol += "USDT"
            direction = parts[3].upper()
            if direction not in ("LONG", "SHORT"):
                return {"action": "error", "msg": f"Direction phải là LONG hoặc SHORT, không phải '{direction}'"}
            entry = float(parts[4])

            sl = None; tp = None
            i = 5
            while i < len(parts) - 1:
                key = parts[i].lower()
                try:
                    val = float(parts[i+1])
                    if key == "sl": sl = val
                    elif key == "tp": tp = val
                except ValueError:
                    pass
                i += 2

            return {
                "action": "add",
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tp": tp,
            }
        except (ValueError, IndexError) as e:
            return {"action": "error", "msg": f"Cú pháp sai: {e}"}

    return {"action": "error", "msg": "Cú pháp sai. Gõ /pos help để xem hướng dẫn"}


def _handle_telegram_command(text: str, chat_id: str, token: str) -> str:
    """Xử lý command từ Telegram, return reply message."""
    parsed = _parse_position_command(text)
    if not parsed:
        return None  # không phải /pos command

    action = parsed.get("action")

    if action == "help":
        return (
            "📋 POSITION COMMANDS\n"
            "─────────────────\n"
            "/pos add <coin> <long|short> <entry> [sl X] [tp Y]\n"
            "  vd: /pos add ARB short 0.1295 sl 0.131 tp 0.128\n"
            "  vd: /pos add BTC long 75000\n"
            "\n"
            "/pos list — xem positions đang theo dõi\n"
            "/pos del <id> — xóa 1 position\n"
            "/pos help — xem hướng dẫn\n"
            "\n"
            "Sau khi add, hệ thống sẽ alert khi có reversal signal!"
        )

    if action == "error":
        return f"❌ {parsed.get('msg', 'Lỗi')}"

    if action == "add":
        positions = load_positions()
        sym = parsed["symbol"]
        direction = parsed["direction"]
        entry = parsed["entry"]

        # Check duplicate
        dup = next((p for p in positions
                    if p.get("symbol") == sym
                    and p.get("direction") == direction
                    and abs(float(p.get("entry", 0) or 0) - entry) / entry < 0.005), None)
        if dup:
            return f"⚠️ Đã có position {sym} {direction} entry ~{entry} (id {dup.get('id')})"

        import time as _t
        new_pos = {
            "id":         int(_t.time() * 1000),
            "saved_at":   _local_isoformat(),
            "symbol":     sym,
            "direction":  direction,
            "entry":      entry,
            "sl":         parsed.get("sl"),
            "tp":         parsed.get("tp"),
            "base_mode":  "pct",
            "base_value": 2.0,
            "leverage":   10,
            "source":     "telegram",
        }
        positions.insert(0, new_pos)
        save_positions(positions)

        sl_str = f" SL={parsed['sl']}" if parsed.get("sl") else ""
        tp_str = f" TP={parsed['tp']}" if parsed.get("tp") else ""
        return (
            f"✅ Đã thêm position\n"
            f"{sym} {direction} entry={entry}{sl_str}{tp_str}\n"
            f"ID: {new_pos['id']}\n"
            f"\n"
            f"Hệ thống sẽ alert khi có reversal signal cho lệnh này."
        )

    if action == "list":
        positions = load_positions()
        if not positions:
            return "📭 Chưa có position nào."
        lines = [f"📋 POSITIONS ({len(positions)})"]
        lines.append("─────────────────")
        for p in positions[:10]:
            d_emoji = "🟢" if p.get("direction") == "LONG" else "🔴"
            lines.append(
                f"{d_emoji} {p.get('symbol')} {p.get('direction')} @ {p.get('entry')}"
                + (f" SL={p['sl']}" if p.get('sl') else "")
                + (f" TP={p['tp']}" if p.get('tp') else "")
                + f" [id:{p.get('id')}]"
            )
        return chr(10).join(lines)

    if action == "del":
        positions = load_positions()
        pid = parsed["id"]
        before = len(positions)
        positions = [p for p in positions if p.get("id") != pid]
        if len(positions) == before:
            return f"❌ Không tìm thấy position id={pid}"
        save_positions(positions)
        return f"✅ Đã xóa position id={pid}"

    return None


@app.route("/api/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Nhận update từ Telegram bot — xử lý commands."""
    update = request.json or {}
    msg = update.get("message", {})
    text = msg.get("text", "")
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))

    cfg = load_config()
    cfg_chat = str(cfg.get("telegram_chat", ""))
    token = cfg.get("telegram_token", "")

    # Chỉ accept message từ chat_id đã config (security)
    if chat_id != cfg_chat:
        return jsonify({"ok": True})  # silently ignore

    reply = _handle_telegram_command(text, chat_id, token)
    if reply:
        send_telegram(token, chat_id, reply)

    return jsonify({"ok": True})


@app.route("/api/telegram/setup-webhook", methods=["POST"])
def telegram_setup_webhook():
    """Setup webhook cho Telegram bot. Gọi 1 lần sau deploy."""
    cfg = load_config()
    token = cfg.get("telegram_token", "")
    if not token:
        return jsonify({"ok": False, "error": "Chưa có token"})

    base_url = request.json.get("base_url", "") if request.json else ""
    if not base_url:
        return jsonify({"ok": False, "error": "Thiếu base_url"})

    webhook_url = f"{base_url.rstrip('/')}/api/telegram/webhook"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message"]},
            timeout=10,
        )
        return jsonify({"ok": r.status_code == 200, "response": r.json(), "webhook": webhook_url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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
        threading.Thread(target=watchlist_fast_loop, daemon=True).start()
        threading.Thread(target=position_monitor_loop, daemon=True).start()
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


@app.route("/api/watchlist/funding-spike-scan", methods=["POST"])
def funding_spike_scan_now():
    """Manual trigger quét funding spike + auto-add vào watchlist.
    Body optional: {min_volume, funding_threshold, max_add}.
    Bỏ throttle để test ngay.
    """
    data = request.get_json() or {}
    cfg  = load_config()
    _funding_spike_last_run["ts"] = 0  # reset throttle
    added = _auto_add_funding_spike_watchlist(
        cfg,
        min_volume       = float(data.get("min_volume", 20_000_000)),
        funding_threshold= float(data.get("funding_threshold", 0.05)),
        max_add          = int(data.get("max_add", 5)),
        run_interval_sec = 0,
    )
    return jsonify({"ok": True, "added": added, "total_watchlist": len(load_config().get("symbols", []))})


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
