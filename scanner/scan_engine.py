"""scanner/scan_engine.py — Full Market Scanner.
Dùng chung fam_analyze() với Dashboard để đảm bảo kết quả nhất quán.
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core.binance import fetch_all_futures_tickers
from core.utils import sanitize
from dashboard.fam_engine import fam_analyze

scan_state = {
    "running": False, "progress": 0, "total": 0, "results": [],
    "started_at": None, "finished_at": None, "error": None,
}
_state_lock = threading.Lock()

# Config mặc định cho scanner — không cần telegram/interval
SCAN_CFG = {
    "symbols": [], "interval_minutes": 30,
    "telegram_token": "", "telegram_chat": "",
    "alert_confidence": "HIGH", "alert_rr": 1.5, "rr_ratio": 1.0,
}


def analyze_symbol(sym_info: dict):
    symbol = sym_info["symbol"]
    try:
        # Symbol từ fetch_all_futures_tickers luôn là futures — ghi vào config
        cfg = {**SCAN_CFG, "force_futures": True}
        result = fam_analyze(symbol, cfg)

        # Bỏ qua WAIT
        if result.get("direction") not in ("LONG", "SHORT"):
            return None

        # Filter R:R tối thiểu
        rr = result.get("rr", 0)
        conf = result.get("confidence", "LOW")
        if conf in ("HIGH", "MEDIUM") and rr < 1.0: return None
        if conf == "LOW" and rr < 1.0: return None

        # Thêm volume từ ticker
        result["volume_24h"] = sym_info["volume_24h"]

        # fam_analyze lưu market data trong nested "market" dict — flatten ra root cho UI
        mk = result.get("market", {})
        result["funding"]     = mk.get("funding")
        result["funding_str"] = mk.get("funding_pct") or "N/A"
        result["oi_change"]   = mk.get("oi_change")
        result["oi_str"]      = mk.get("oi_str") or "N/A"
        result["atr_ratio"]   = mk.get("atr_ratio")

        return sanitize(result)
    except Exception as e:
        import traceback
        print(f"[SCAN ERROR] {symbol}: {e}")
        return None


def run_full_scan(min_vol: float = 10_000_000, max_workers: int = 10):
    global scan_state
    with _state_lock:
        scan_state.update({"running": True, "progress": 0, "results": [],
                           "error": None, "started_at": datetime.now().isoformat(),
                           "finished_at": None})
    try:
        symbols = fetch_all_futures_tickers(min_vol)
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
                except: pass

        # Sort: HIGH → MEDIUM → LOW, trong mỗi tier sort score rồi R:R
        conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        results.sort(key=lambda x: (
            conf_order.get(x.get("confidence", "LOW"), 3),
            -x.get("score", 0),
            -x.get("rr", 0)
        ))
        scan_state["results"]     = results
        scan_state["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        import traceback
        scan_state["error"] = str(e)
        print(f"[SCAN FATAL] {e}")
        traceback.print_exc()
    finally:
        scan_state["running"] = False
