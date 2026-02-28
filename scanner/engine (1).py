"""scanner/engine.py — Full Market Scanner. Chỉ sửa file này khi thay đổi logic Scanner."""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core.binance import (fetch_klines, fetch_all_futures_tickers,
                           fetch_funding_rate, fetch_oi_change)
from core.indicators import (prepare, ma_slope, find_swing_points,
                              classify_structure, calc_atr_context)
from core.utils import sanitize, smart_round

# ── Scan state ──
scan_state = {
    "running":     False,
    "progress":    0,
    "total":       0,
    "results":     [],
    "started_at":  None,
    "finished_at": None,
    "error":       None,
}
_state_lock = threading.Lock()


def analyze_symbol(sym_info: dict):
    symbol = sym_info["symbol"]
    try:
        df_d1 = prepare(fetch_klines(symbol, "1d", 250))
        df_h4 = prepare(fetch_klines(symbol, "4h", 250))
        df_h1 = prepare(fetch_klines(symbol, "1h", 150))
        if len(df_d1) < 5 or len(df_h4) < 5 or len(df_h1) < 5: return None

        price   = float(df_h1["close"].iloc[-1])
        row_d1  = df_d1.iloc[-1]
        row_h4  = df_h4.iloc[-1]
        prev_h4 = df_h4.iloc[-2]
        row_h1  = df_h1.iloc[-1]
        atr_h1  = float(df_h1["atr"].iloc[-1])
        atr_h4  = float(df_h4["atr"].iloc[-1])

        # ── D1 bias ──
        if price > row_d1["ma34"] and price > row_d1["ma89"]:   d1_bias = "LONG"
        elif price < row_d1["ma34"] and price < row_d1["ma89"]: d1_bias = "SHORT"
        else:                                                     d1_bias = "NEUTRAL"

        # ── H4 bias ──
        h4_above34 = row_h4["close"] > row_h4["ma34"]
        h4_above89 = row_h4["close"] > row_h4["ma89"]
        h4_x34_up  = prev_h4["close"] <= prev_h4["ma34"] and row_h4["close"] > row_h4["ma34"]
        h4_x34_dn  = prev_h4["close"] >= prev_h4["ma34"] and row_h4["close"] < row_h4["ma34"]

        if h4_above34 and h4_above89:         h4_bias = "LONG"
        elif not h4_above34 and not h4_above89: h4_bias = "SHORT"
        else:                                  h4_bias = "NEUTRAL"

        # ── Filter cơ bản ──
        lo34_89 = min(float(row_h4["ma34"]), float(row_h4["ma89"]))
        hi34_89 = max(float(row_h4["ma34"]), float(row_h4["ma89"]))
        no_trade = lo34_89 * 0.998 <= price <= hi34_89 * 1.002

        if no_trade or d1_bias == "NEUTRAL" or h4_bias == "NEUTRAL": return None
        if d1_bias != h4_bias: return None

        direction = d1_bias

        # ── H1 pullback filter ──
        dist_ma34_h4 = (price - float(row_h4["ma34"])) / float(row_h4["ma34"]) * 100
        if direction == "LONG":
            if dist_ma34_h4 < -5.0 or dist_ma34_h4 > 8.0: return None
        else:
            if dist_ma34_h4 > 5.0 or dist_ma34_h4 < -8.0: return None

        # ── H1 momentum (không vào khi đang rơi/pump liên tục) ──
        h1_c = df_h1["close"].iloc[-5:].values
        h1_o = df_h1["open"].iloc[-5:].values
        bear5 = sum(1 for i in range(5) if h1_c[i] < h1_o[i])
        bull5 = sum(1 for i in range(5) if h1_c[i] > h1_o[i])
        if direction == "LONG"  and bear5 >= 4: return None
        if direction == "SHORT" and bull5 >= 4: return None

        # ── H1 MA34 slope ──
        h1_ma34_slope = ma_slope(df_h1["ma34"], n=3)
        if direction == "LONG"  and h1_ma34_slope == "DOWN": return None
        if direction == "SHORT" and h1_ma34_slope == "UP":   return None

        # ── D1 không quá xa MA ──
        dist_d1 = (price - float(row_d1["ma34"])) / float(row_d1["ma34"]) * 100
        if abs(dist_d1) > 10: return None

        # ── Scoring ──
        conditions = []
        if d1_bias == direction: conditions.append(f"D1 bias {direction}")
        if h4_bias == direction: conditions.append(f"H4 bias {direction}")

        sl_ma34 = ma_slope(df_h4["ma34"])
        sl_ma89 = ma_slope(df_h4["ma89"])
        slope_ok = (direction == "LONG"  and sl_ma34 == "UP"   and sl_ma89 == "UP") or \
                   (direction == "SHORT" and sl_ma34 == "DOWN" and sl_ma89 == "DOWN")
        if slope_ok: conditions.append(f"MA34/89 slope {direction}")

        highs, lows = find_swing_points(df_h4)
        structure   = classify_structure(highs, lows)
        if (direction == "LONG"  and structure == "UPTREND") or \
           (direction == "SHORT" and structure == "DOWNTREND"):
            conditions.append(f"H4 structure {structure}")

        vol_ratio = float(row_h1["vol_ratio"])
        if vol_ratio > 1.2: conditions.append(f"Volume {vol_ratio:.1f}x")

        recent_h = df_h1["high"].iloc[-60:].max()
        recent_l = df_h1["low"].iloc[-60:].min()
        f05  = recent_h - (recent_h - recent_l) * 0.5
        f618 = recent_h - (recent_h - recent_l) * 0.618
        if min(f618,f05)*0.998 <= price <= max(f618,f05)*1.002:
            conditions.append("Fib 0.5/0.618 zone")

        if h4_x34_up and direction == "LONG":  conditions.append("KEY: Vừa vượt MA34 H4")
        if h4_x34_dn and direction == "SHORT": conditions.append("KEY: Vừa break MA34 H4")

        if len(conditions) < 3: return None

        confidence = "HIGH" if len(conditions) >= 5 else "MEDIUM"

        # ── SL/TP ──
        rh1h = float(df_h1["high"].iloc[-20:].max())
        rh1l = float(df_h1["low"].iloc[-20:].min())

        if direction == "LONG":
            sl_price = smart_round(min(price * 0.99, max(rh1l - atr_h1 * 0.5, price * 0.96)))
            tp1 = next((smart_round(m) for m in [float(row_h4["ma34"]), float(row_h4["ma89"]), float(row_h4["ma200"])]
                        if m > price * 1.005), smart_round(price + atr_h1 * 3))
            tp2 = smart_round(price + atr_h1 * 5)
        else:
            sl_price = smart_round(max(price * 1.01, min(rh1h + atr_h1 * 0.5, price * 1.04)))
            tp1 = next((smart_round(m) for m in [float(row_h4["ma34"]), float(row_h4["ma89"]), float(row_h4["ma200"])]
                        if m < price * 0.995), smart_round(price - atr_h1 * 3))
            tp2 = smart_round(price - atr_h1 * 5)

        sl_pct  = round(abs(price - sl_price) / price * 100, 2)
        tp1_pct = round(abs(tp1 - price) / price * 100, 2)
        rr      = round(tp1_pct / sl_pct, 2) if sl_pct > 0 else 0
        if rr < 1.0: return None

        # ── Funding / OI / ATR ──
        funding  = fetch_funding_rate(symbol)
        oi_chg   = fetch_oi_change(symbol)
        atr_ctx  = calc_atr_context(df_h4, df_d1)

        if direction == "SHORT" and funding is not None and funding < -0.05: return None
        if direction == "LONG"  and funding is not None and funding >  0.05: return None
        if atr_ctx["atr_ratio"] < 0.5 or atr_ctx["atr_ratio"] > 2.0: return None

        return sanitize({
            "symbol":     symbol,
            "price":      smart_round(price),
            "direction":  direction,
            "confidence": confidence,
            "score":      len(conditions),
            "conditions": conditions,
            "sl":         sl_price, "tp1": tp1, "tp2": tp2,
            "sl_pct":     sl_pct,  "tp1_pct": tp1_pct, "rr": rr,
            "funding":    round(funding, 4) if funding is not None else None,
            "funding_str": f"{funding:+.4f}%" if funding is not None else "N/A",
            "oi_change":  oi_chg,
            "oi_str":     f"{oi_chg:+.2f}%" if oi_chg is not None else "N/A",
            "atr_ratio":  atr_ctx["atr_ratio"],
            "volume_24h": sym_info["volume_24h"],
            "structure":  structure,
            "d1_bias":    d1_bias,
            "h4_bias":    h4_bias,
            "timestamp":  datetime.now().isoformat(),
        })
    except Exception as e:
        return None


def run_full_scan(min_vol: float = 10_000_000, max_workers: int = 15):
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

        results.sort(key=lambda x: (0 if x["confidence"] == "HIGH" else 1, -x["rr"]))
        scan_state["results"]     = results
        scan_state["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        scan_state["error"] = str(e)
    finally:
        scan_state["running"] = False
