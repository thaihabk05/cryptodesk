"""scanner/scan_engine.py — Full Market Scanner.
Dùng chung engine với Dashboard — theo strategy được chọn (SWING_H4 hoặc SWING_H1).
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
_TZ_VN = timezone(timedelta(hours=7))
from pathlib import Path

from core.binance import fetch_all_futures_tickers
from core.utils import sanitize

# Dùng cùng volume path với main.py
import os as _os
_SCAN_DATA_DIR  = Path("/data") if Path("/data").exists() and _os.access("/data", _os.W_OK) else Path("data")
SCAN_CACHE_FILE = _SCAN_DATA_DIR / "last_scan.json"

def _clean_for_json(obj):
    """Replace NaN/Infinity → None, numpy types → native Python."""
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

def _persist_scan_state(state: dict):
    """Lưu kết quả scan vào file để survive restart."""
    try:
        SCAN_CACHE_FILE.parent.mkdir(exist_ok=True)
        data = _clean_for_json({
            "results":     state.get("results", []),
            "finished_at": state.get("finished_at"),
            "total":       state.get("total", 0),
            "strategy":    state.get("strategy", "SWING_H4"),
        })
        SCAN_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, default=str))
    except Exception as e:
        print(f"[SCAN PERSIST ERROR] {e}")

def _load_persisted_scan() -> dict:
    """Load kết quả scan từ file nếu có."""
    try:
        if SCAN_CACHE_FILE.exists():
            data = json.loads(SCAN_CACHE_FILE.read_text())
            return data
    except Exception:
        pass
    return {}
from dashboard.fam_engine       import fam_analyze
from dashboard.swing_h1_engine  import swing_h1_analyze
from dashboard.scalp_engine     import scalp_analyze
from dashboard.range_engine     import range_analyze
from dashboard.reversal_engine  import reversal_analyze

_persisted = _load_persisted_scan()
scan_state = {
    "running":     False,
    "progress":    0,
    "total":       _persisted.get("total", 0),
    "results":     _persisted.get("results", []),
    "last_results": _persisted.get("results", []),  # backup — luôn giữ kết quả scan cuối
    "started_at":  None,
    "finished_at": _persisted.get("finished_at"),
    "error":       None,
    "strategy":    _persisted.get("strategy", "SWING_H4"),
}
_state_lock = threading.Lock()

# Config mặc định cho scanner — không cần telegram/interval
SCAN_CFG = {
    "symbols": [], "interval_minutes": 30,
    "telegram_token": "", "telegram_chat": "",
    "alert_confidence": "HIGH", "alert_rr": 1.5, "rr_ratio": 1.0,
    "strategy": "SWING_H4",
}

def _get_engine(cfg):
    """Chọn engine phù hợp với strategy trong config."""
    s = cfg.get("strategy", "SWING_H4")
    if s == "SWING_H1":    return swing_h1_analyze
    if s == "SCALP":       return scalp_analyze
    if s == "RANGE_SCALP": return range_analyze
    return fam_analyze


def _get_engines_for_modes(cfg):
    """Trả về list các engine cần chạy dựa trên scan_modes."""
    modes   = cfg.get("scan_modes", ["TREND"])  # default chỉ TREND
    engines = []
    strategy = cfg.get("strategy", "SWING_H4")
    if "TREND" in modes:
        if strategy == "SWING_H1": engines.append(("TREND", swing_h1_analyze))
        elif strategy == "SCALP":  engines.append(("TREND", scalp_analyze))
        else:                      engines.append(("TREND", fam_analyze))
    if "RANGE_SCALP" in modes:
        engines.append(("RANGE_SCALP", range_analyze))
    if "REVERSAL" in modes:
        engines.append(("REVERSAL", reversal_analyze))
    return engines


def _process_result(result, sym_info, mode_tag):
    """Xử lý kết quả từ engine: filter, tag, flatten — giữ đầy đủ các filter coin rác."""
    if result.get("direction") not in ("LONG", "SHORT"):
        return None

    # ── Filter 1: Confidence — bỏ LOW ──
    conf = result.get("confidence", "LOW")
    if conf == "LOW":
        return None

    # ── Filter 2: R:R tối thiểu 1.5 ──
    rr = result.get("rr", 0) or 0
    if rr < 1.5:
        return None

    # ── Filter 3: Volume 24h tối thiểu (lấy từ SCAN_CFG) ──
    _vol_usdt = float(sym_info.get("volume_24h", 0))
    _min_vol  = float(SCAN_CFG.get("min_vol", 5_000_000))
    if _vol_usdt < _min_vol:
        return None

    # ── Filter 4: Coin đang pump/dump quá mạnh 24h → rủi ro cao ──
    # Trend mode: không vào coin đang pump > 25% hoặc dump > -20% trong 24h
    # Range mode: được phép (coin đang dao động trong range)
    if mode_tag != "RANGE_SCALP":
        chg_24h = float(sym_info.get("price_change_pct", 0) or 0)
        if chg_24h > 25:
            return None   # đang pump mạnh — PATCH J trong engine đã block nhưng double-check
        if chg_24h < -20:
            return None   # đang dump mạnh — rủi ro tiếp tục rơi

    # ── Tag algo source ──
    result["algo"] = mode_tag

    # ── Flatten market data ──
    result["volume_24h"]  = sym_info["volume_24h"]
    mk = result.get("market", {})
    result["funding"]     = mk.get("funding")
    result["funding_str"] = mk.get("funding_pct") or "N/A"
    result["oi_change"]   = mk.get("oi_change")
    result["oi_str"]      = mk.get("oi_str") or "N/A"
    result["atr_ratio"]   = mk.get("atr_ratio")

    # ── D1/H4 bias cho history ──
    result["d1_bias"] = (result.get("d1") or {}).get("bias", "")
    result["h4_bias"] = (result.get("h4") or {}).get("bias", "")

    return sanitize(result)


def analyze_symbol(sym_info: dict):
    import time as _t
    symbol = sym_info["symbol"]
    try:
        _t.sleep(0.3)
        cfg     = {**SCAN_CFG, "force_futures": True}
        engines = _get_engines_for_modes(cfg)
        results = []

        for mode_tag, engine_fn in engines:
            try:
                result = engine_fn(symbol, cfg)
                processed = _process_result(result, sym_info, mode_tag)
                if processed:
                    results.append(processed)
            except Exception as e:
                print(f"[SCAN {mode_tag}] {symbol}: {e}")

        # Trả về result tốt nhất (ưu tiên HIGH confidence, rồi RR cao)
        if not results:
            return None
        results.sort(key=lambda r: (
            0 if r.get("confidence") == "HIGH" else 1,
            -(r.get("rr") or 0)
        ))
        return results[0]

    except Exception as e:
        if "429" in str(e):
            import time as _t2; _t2.sleep(2)
        print(f"[SCAN ERROR] {symbol}: {e}")
        return None


def run_full_scan(min_vol: float = 10_000_000, max_workers: int = 3, strategy: str = "SWING_H4", scan_modes: list = None):
    global scan_state
    with _state_lock:
        if scan_state["running"]:
            print("[SCAN] Đang chạy, bỏ qua lần này")
            return
        SCAN_CFG["strategy"]   = strategy
        SCAN_CFG["min_vol"]    = min_vol
        SCAN_CFG["scan_modes"] = scan_modes or ["TREND"]
        scan_state.update({"running": True, "progress": 0, "results": [],
                           "error": None, "started_at": datetime.now(_TZ_VN).isoformat(),
                           "finished_at": None, "strategy": strategy})
        # Giữ last_results không reset — frontend show kết quả cũ trong khi scan mới
    try:
        print(f"[SCAN] Bắt đầu fetch tickers min_vol={min_vol:,.0f}...")
        symbols = fetch_all_futures_tickers(min_vol)
        print(f"[SCAN] Lấy được {len(symbols)} symbols")
        scan_state["total"] = len(symbols)
        results, done = [], 0

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(analyze_symbol, s): s for s in symbols}
            for fut in as_completed(futures):
                done += 1
                scan_state["progress"] = done
                try:
                    r = fut.result()
                    if r: results.append(r)
                except Exception as fe:
                    print(f"[SCAN SYM ERROR] {fe}")
                # Throttle nhẹ tránh 429
                if done % 10 == 0:
                    import time as _t; _t.sleep(0.5)

        # Sort: HIGH → MEDIUM → LOW, trong mỗi tier sort score rồi R:R
        conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        results.sort(key=lambda x: (
            conf_order.get(x.get("confidence", "LOW"), 3),
            -x.get("score", 0),
            -x.get("rr", 0)
        ))
        scan_state["results"]      = results
        scan_state["last_results"] = results  # backup cho lần restart tiếp
        scan_state["finished_at"]  = datetime.now(_TZ_VN).isoformat()
        _persist_scan_state(scan_state)
        print(f"[SCAN] Lưu {len(results)} kết quả vào {SCAN_CACHE_FILE}")
    except Exception as e:
        import traceback
        scan_state["error"] = str(e)
        print(f"[SCAN FATAL] {e}")
        traceback.print_exc()
    finally:
        scan_state["running"] = False
