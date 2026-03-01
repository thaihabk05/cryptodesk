"""scanner/scan_engine.py — Full Market Scanner.
Dùng chung engine với Dashboard — theo strategy được chọn (SWING_H4 hoặc SWING_H1).
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.binance import fetch_all_futures_tickers
from core.utils import sanitize

SCAN_CACHE_FILE = Path("data/last_scan.json")

def _persist_scan_state(state: dict):
    """Lưu kết quả scan vào file để survive restart."""
    try:
        SCAN_CACHE_FILE.parent.mkdir(exist_ok=True)
        SCAN_CACHE_FILE.write_text(json.dumps({
            "results":     state.get("results", []),
            "finished_at": state.get("finished_at"),
            "total":       state.get("total", 0),
            "strategy":    state.get("strategy", "SWING_H4"),
        }, ensure_ascii=False))
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
from dashboard.fam_engine import fam_analyze
from dashboard.swing_h1_engine import swing_h1_analyze
from dashboard.scalp_engine import scalp_analyze

_persisted = _load_persisted_scan()
scan_state = {
    "running":     False,
    "progress":    0,
    "total":       _persisted.get("total", 0),
    "results":     _persisted.get("results", []),
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
    if s == "SWING_H1": return swing_h1_analyze
    if s == "SCALP":    return scalp_analyze
    return fam_analyze


def analyze_symbol(sym_info: dict):
    import time as _t
    symbol = sym_info["symbol"]
    try:
        # Delay nhỏ để tránh 429 rate limit — 3 workers × 0.3s = ~1 req/100ms/worker
        _t.sleep(0.3)
        cfg    = {**SCAN_CFG, "force_futures": True}
        engine = _get_engine(cfg)
        result = engine(symbol, cfg)

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
        err_str = str(e)
        if "429" in err_str:
            # Rate limited — đợi và retry 1 lần
            import time as _t2
            _t2.sleep(2)
            try:
                cfg    = {**SCAN_CFG, "force_futures": True}
                engine = _get_engine(cfg)
                result = engine(symbol, cfg)
                # (tiếp tục xử lý bình thường nếu retry OK)
            except:
                pass
        print(f"[SCAN ERROR] {symbol}: {e}")
        return None


def run_full_scan(min_vol: float = 10_000_000, max_workers: int = 3, strategy: str = "SWING_H4"):
    global scan_state
    with _state_lock:
        if scan_state["running"]:
            print("[SCAN] Đang chạy, bỏ qua lần này")
            return
        SCAN_CFG["strategy"] = strategy
        scan_state.update({"running": True, "progress": 0, "results": [],
                           "error": None, "started_at": datetime.now().isoformat(),
                           "finished_at": None, "strategy": strategy})
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
        scan_state["results"]     = results
        scan_state["finished_at"] = datetime.now().isoformat()
        _persist_scan_state(scan_state)
        print(f"[SCAN] Lưu {len(results)} kết quả vào {SCAN_CACHE_FILE}")
    except Exception as e:
        import traceback
        scan_state["error"] = str(e)
        print(f"[SCAN FATAL] {e}")
        traceback.print_exc()
    finally:
        scan_state["running"] = False
