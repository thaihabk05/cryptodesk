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

    # ── Filter 0: Coin blacklist — backtest 7 ngày data, các coin 0% WR ──
    sym = result.get("symbol", "")
    blacklist = SCAN_CFG.get("coin_blacklist") or []
    if sym in blacklist:
        return None

    # ── Filter 1: Confidence — bỏ LOW + MEDIUM ──
    # Backtest 7 ngày data: MEDIUM confidence WR chỉ 12% (n=8, -5R) → cấm.
    # Riêng REVERSAL chỉ chấp nhận HIGH (score ≥ 5) — score 3-4 fire quá tight SL → toàn LOSS.
    conf = result.get("confidence", "LOW")
    if conf in ("LOW", "MEDIUM"):
        return None

    # ── Filter 2: R:R tối thiểu — backtest cho thấy RR 1.5-2 break-even, nâng lên 1.8 ──
    rr = result.get("rr", 0) or 0
    min_rr = float(SCAN_CFG.get("scan_min_rr", 1.8))
    if rr < min_rr:
        return None

    # ── Filter 3: Volume 24h tối thiểu (lấy từ SCAN_CFG) ──
    _vol_usdt = float(sym_info.get("volume_24h", 0))
    _min_vol  = float(SCAN_CFG.get("min_vol", 5_000_000))
    if _vol_usdt < _min_vol:
        return None

    # ── Filter 4: Coin đang pump/dump quá mạnh 24h → rủi ro cao ──
    # Trend mode: không vào coin đang pump > 25% hoặc dump > -20% trong 24h
    # Range mode: được phép (coin đang dao động trong range)
    chg_24h = float(sym_info.get("price_change_pct", 0) or 0)
    if mode_tag != "RANGE_SCALP":
        if chg_24h > 25:
            return None   # đang pump mạnh — PATCH J trong engine đã block nhưng double-check
        if chg_24h < -20:
            return None   # đang dump mạnh — rủi ro tiếp tục rơi

    # ── Filter 4b: Alt-vs-BTC relative strength (Fix 1 — 12/5/2026) ──
    # Backtest 5/11-5/12 (45.9h, 162 closed): WR sụp về 19.8%, -55R. Phân tích:
    # - RISK_ON × LONG: 119 lệnh, WR 15%, -55R 🔴
    # - NEUTRAL × LONG: 32 lệnh, WR 34%, +1.5R ✅
    # Root cause: BTC pump mạnh (>2%), alt KHÔNG follow theo → distribution phase.
    # Capital flow vào BTC, alt sell pressure → LONG alt rất rủi ro.
    # Logic: BTC chg > +2% nhưng alt < BTC × 0.3 (hoặc alt âm) → block LONG.
    direction = result.get("direction", "")
    btc_chg_24h = float(SCAN_CFG.get("btc_24h_chg") or 0)
    if direction == "LONG" and btc_chg_24h > 2.0:
        # Alt yếu hơn nhiều BTC HOẶC âm khi BTC dương → distribution
        if chg_24h < 0 or chg_24h < btc_chg_24h * 0.3:
            print(f"[FIX1 BLOCK] {sym} LONG: BTC +{btc_chg_24h:.2f}% / alt {chg_24h:+.2f}% — distribution detect")
            return None
    # Ngược lại: BTC dump > -2% nhưng alt còn dương → over-extended, dễ catch-up dump
    if direction == "LONG" and btc_chg_24h < -2.0 and chg_24h > 0:
        print(f"[FIX1 BLOCK] {sym} LONG: BTC {btc_chg_24h:.2f}% nhưng alt {chg_24h:+.2f}% — catch-up dump risk")
        return None

    # ── Filter 4c (Fix 11 + Fix 12 — 26/5, expand 29/5): Anti-bounce-after-dump cho SHORT ──
    # Backtest 23/5 + 29/5 disaster: SHORT signals LOSS toàn bộ sau khi BTC dump → bounce.
    # Fix 11 (24h check) bị miss case 29/5: BTC 24h chỉ +0.23% (recovery) nhưng 48h -2.83%.
    # Fix 12: thêm check 48h và prev_24h để catch "bottom bounce phase".
    btc_48h_chg = float(SCAN_CFG.get("btc_48h_chg") or 0)
    btc_prev_24h_chg = float(SCAN_CFG.get("btc_prev_24h_chg") or 0)
    if direction == "SHORT":
        # Case 1: BTC đang dump (Fix 11 cũ)
        if btc_chg_24h < -2.0:
            print(f"[FIX11 BLOCK] {sym} SHORT: BTC dump 24h {btc_chg_24h:.2f}% — risk bounce")
            return None
        # Case 2: BTC vừa dump trong 48h trước (Fix 12 mới)
        if btc_48h_chg < -3.0:
            print(f"[FIX12 BLOCK] {sym} SHORT: BTC 48h {btc_48h_chg:.2f}% — đang recovery, risk squeeze")
            return None
        # Case 3: Recovery phase (Fix 12 mới) — BTC dump h48→h24, giờ đang phục hồi
        if btc_prev_24h_chg < -2.5 and btc_chg_24h > -1.0:
            print(f"[FIX12 BLOCK] {sym} SHORT: BTC prev_24h {btc_prev_24h_chg:.2f}% + recovery — bounce phase")
            return None
        # Case 4: alt đã dump quá nhiều → SHORT entry tệ (gần đáy)
        if chg_24h < -5.0:
            print(f"[FIX11 BLOCK] {sym} SHORT: alt đã dump {chg_24h:+.2f}% — short ở vùng đáy")
            return None

    # ── Filter 4d (Fix 13 — 03/6): Block coin high volatility ──
    # Backtest 2 LOSS 5/31-6/1: GUA SHORT @ 0.8604 hit SL nhưng giá thực sự GIẢM -5.94% sau đó.
    # Root cause: GUA ATR% H1 = 3.61% → SL 1R (~2%) bị quét bởi noise bình thường.
    # Coin biến động > 2.5%/giờ không phù hợp swing trade với SL fixed.
    atr_ratio_h1 = result.get("atr_ratio") or (result.get("market") or {}).get("atr_ratio") or 0
    try:
        atr_pct_h1 = float(atr_ratio_h1) * 100 if atr_ratio_h1 < 1 else float(atr_ratio_h1)
    except (ValueError, TypeError):
        atr_pct_h1 = 0
    if atr_pct_h1 > 2.5:
        print(f"[FIX13 BLOCK] {sym} {direction}: ATR% H1 {atr_pct_h1:.2f}% > 2.5% — too volatile for fixed SL")
        return None

    # ── Filter 5 (Fix 8 — 22/5): RANGE_SCALP score ≥ 7 paradox ──
    # Backtest 22/5: 28/30 score=7 là RANGE_SCALP, WR 3% (1W/29L), -25.94R.
    # Engine RANGE_SCALP fire "max confidence" (score 7) thường là trong range break out
    # → fake breakout → catch-top/bottom → fail.
    # Hard ban: RANGE_SCALP chỉ accept score 5-6.
    score = int(result.get("score", 0) or 0)
    if mode_tag == "RANGE_SCALP" and score >= 7:
        print(f"[FIX8 BLOCK] {sym} {direction} RANGE_SCALP score={score} — paradox 3% WR")
        return None

    # ── Filter 6 (Fix 7 — 22/5): RANGE_SCALP block khi coin tự trending ──
    # Backtest 22/5: RANGE_SCALP LONG 0% WR (0/18!), SHORT 13% WR. Lý do:
    # Fix 2 chỉ check BTC trending, nhưng BTC NEUTRAL 99% time → fix không trigger.
    # Cần check COIN-level: nếu coin H1 stack BEARISH → block LONG (catch-falling-knife).
    # Nếu coin H1 stack BULLISH → block SHORT (catch-top).
    if mode_tag == "RANGE_SCALP":
        h1_info = result.get("h1") or {}
        # H1 EMA stack từ engine output. Fallback: dùng raw price vs MA34/MA89
        ema34_h1 = h1_info.get("ma34") or 0
        ema89_h1 = h1_info.get("ma89") or 0
        price_now = result.get("price", 0) or 0
        if ema34_h1 and ema89_h1 and price_now:
            # H1 bearish: giá < MA34 < MA89
            h1_bearish = price_now < ema34_h1 < ema89_h1
            h1_bullish = price_now > ema34_h1 > ema89_h1
            if direction == "LONG" and h1_bearish:
                print(f"[FIX7 BLOCK] {sym} RANGE_SCALP LONG: H1 coin BEARISH stack (price<MA34<MA89) — catch-falling-knife")
                return None
            if direction == "SHORT" and h1_bullish:
                print(f"[FIX7 BLOCK] {sym} RANGE_SCALP SHORT: H1 coin BULLISH stack — catch-top")
                return None

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

    # ══════════════════════════════════════════
    # TIER_RATING — compute tier per signal (22/5/2026)
    # Mục tiêu: thay vì user phải đắn đo "có vào hay không", system tự tag.
    # Tier 1 = full size, Tier 2 = half size, Tier 3 = small / skip
    # ══════════════════════════════════════════
    result["tier"], result["tier_reasons"] = _compute_tier(result, sym_info)

    return sanitize(result)


def _compute_tier(result: dict, sym_info: dict) -> tuple:
    """Tính tier rating cho signal dựa trên 6 criteria từ backtest analysis.
    Returns: (tier_str, list_of_reasons)

    Criteria (backtest 22/5 — 14 ngày dữ liệu):
    1. Strategy = SWING_H1 (RANGE_SCALP WR 6%, REVERSAL n nhỏ)
    2. Score = 5 (sweet spot 34% WR), 6 = ok, 7 = paradox
    3. Direction cùng chiều H4 bias
    4. RR ≥ 2.0
    5. Coin có 7d momentum đúng chiều (LONG: 7d > -5%, SHORT: 7d < +5%)
    6. Funding KHÔNG ở extreme (< 0.05% absolute)
    """
    pts = 0
    reasons_pos = []
    reasons_neg = []

    strategy = result.get("strategy", "") or result.get("algo", "")
    score    = int(result.get("score", 0) or 0)
    rr       = float(result.get("rr", 0) or 0)
    direction = result.get("direction", "")
    h4_bias  = result.get("h4_bias", "") or (result.get("h4") or {}).get("bias", "")
    funding  = result.get("funding")

    # Criterion 1: Strategy
    if strategy == "SWING_H1":
        pts += 1; reasons_pos.append("SWING_H1 (best WR)")
    elif strategy == "REVERSAL":
        pts += 0.5; reasons_pos.append("REVERSAL (acceptable)")
    elif strategy == "RANGE_SCALP":
        pts -= 0.5; reasons_neg.append("RANGE_SCALP (WR 6%)")

    # Criterion 2: Score
    if score == 5:
        pts += 1; reasons_pos.append("Score 5 sweet-spot")
    elif score == 6:
        pts += 0.5; reasons_pos.append("Score 6 ok")
    elif score >= 7:
        pts -= 1; reasons_neg.append(f"Score {score} paradox (WR 3%)")

    # Criterion 3: Direction matches H4 bias
    if h4_bias in ("LONG", "SHORT") and h4_bias == direction:
        pts += 1; reasons_pos.append(f"Cùng chiều H4 {h4_bias}")
    elif h4_bias in ("LONG", "SHORT") and h4_bias != direction:
        pts -= 0.5; reasons_neg.append(f"Counter-trend H4 {h4_bias}")

    # Criterion 4: RR ≥ 2.0
    if rr >= 2.5:
        pts += 1; reasons_pos.append(f"RR {rr:.1f} excellent")
    elif rr >= 2.0:
        pts += 0.5; reasons_pos.append(f"RR {rr:.1f} good")
    # rr < 2.0 không cộng/trừ — đã pass min filter

    # Criterion 5: 7d momentum (cần fetch — đơn giản dùng price_change_pct 24h proxy)
    chg_24h = float(sym_info.get("price_change_pct", 0) or 0)
    if direction == "LONG":
        if chg_24h > -3:
            pts += 0.5; reasons_pos.append("Momentum không yếu")
        else:
            pts -= 0.5; reasons_neg.append(f"24h dump {chg_24h:.1f}%")
    else:  # SHORT
        if chg_24h < 3:
            pts += 0.5; reasons_pos.append("Momentum không strong")
        else:
            pts -= 0.5; reasons_neg.append(f"24h pump {chg_24h:.1f}%")

    # Criterion 6: Funding không extreme
    if funding is not None:
        try:
            f_abs = abs(float(funding))
            if f_abs < 0.03:
                pts += 0.5; reasons_pos.append(f"Funding neutral {funding:.4f}%")
            elif f_abs > 0.05:
                pts -= 0.5; reasons_neg.append(f"Funding extreme {funding:.4f}%")
        except (ValueError, TypeError):
            pass

    # ── Compute tier ──
    if pts >= 3.5:
        tier = "TIER_1"
    elif pts >= 2.0:
        tier = "TIER_2"
    elif pts >= 0.5:
        tier = "TIER_3"
    else:
        tier = "SKIP"

    reasons = {"pts": round(pts, 1), "positives": reasons_pos, "negatives": reasons_neg}
    return tier, reasons


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
        # Lấy blacklist + scan_min_rr từ data/config.json (load_config đã merge sẵn)
        try:
            from main import load_config
            _cfg = load_config()
            SCAN_CFG["coin_blacklist"] = _cfg.get("coin_blacklist") or []
            SCAN_CFG["scan_min_rr"]    = float(_cfg.get("scan_min_rr", 1.8))
        except Exception as e:
            print(f"[SCAN] load_config warning: {e}")
        # Fix 1 (12/5): cache BTC 24h change để filter alt-vs-BTC relative strength.
        # Fix 12 (29/5): thêm 48h chg để catch "bottom bounce" phase sau dump.
        # Backtest 5/29 disaster: 6/6 SHORT LOSS vì BTC vừa dump -3% rồi recovery →
        # Fix 11 (24h check) không trigger vì 24h chg = +0.23%, nhưng 48h chg = -2.83%.
        try:
            from core.binance import fetch_klines
            df_btc = fetch_klines("BTCUSDT", "1h", 50, force_futures=False)
            btc_now   = float(df_btc["close"].iloc[-1])
            btc_24ha  = float(df_btc["close"].iloc[-25])
            btc_48ha  = float(df_btc["close"].iloc[0])
            SCAN_CFG["btc_24h_chg"] = round((btc_now  - btc_24ha) / btc_24ha * 100, 2)
            SCAN_CFG["btc_48h_chg"] = round((btc_now  - btc_48ha) / btc_48ha * 100, 2)
            # Đoạn dump trong 48h trước (từ 48h trước đến 24h trước) — detect "recovery phase"
            SCAN_CFG["btc_prev_24h_chg"] = round((btc_24ha - btc_48ha) / btc_48ha * 100, 2)
            print(f"[SCAN] BTC chg: 24h={SCAN_CFG['btc_24h_chg']:+.2f}% | 48h={SCAN_CFG['btc_48h_chg']:+.2f}% | prev_24h={SCAN_CFG['btc_prev_24h_chg']:+.2f}%")
        except Exception as e:
            print(f"[SCAN] BTC fetch warning: {e}")
            SCAN_CFG["btc_24h_chg"] = 0
            SCAN_CFG["btc_48h_chg"] = 0
            SCAN_CFG["btc_prev_24h_chg"] = 0
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
