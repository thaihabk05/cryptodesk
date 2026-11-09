"""
risk.py — Module quản trị lệnh (position sizing + trade management). CHỈ TÍNH TOÁN & HIỂN THỊ.
KHÔNG đặt lệnh, KHÔNG đụng API key, KHÔNG phải lời khuyên vào lệnh — là máy tính rủi ro.

Vì sao cần: edge dù dương vẫn cháy tài khoản nếu size sai / không có kế hoạch thoát.
Module chuẩn hoá: rủi ro mỗi lệnh = R cố định, chốt lời từng phần, dời SL về hoà,
trailing theo ATR, và TRẦN rủi ro tổng danh mục (chống tất-tay nhiều lệnh cùng lúc).

Quy ước R: R = khoảng cách giá Entry→SL. Mọi mục tiêu đo bằng bội số R.
"""
import math


def trade_plan(direction, entry, stop, atr=None, equity=None, risk_pct=1.0,
               fee_pct=0.05, tp_rr=(1.0, 2.0), close_fracs=(0.5, 0.25),
               max_lev=10, liq_safety=2.0, trail_atr=1.5):
    """
    direction : 'LONG' | 'SHORT'
    entry,stop: giá vào & dừng lỗ (stop ĐÚNG phía: LONG stop<entry, SHORT stop>entry)
    atr       : ATR tuyệt đối (để gợi ý trailing); optional
    equity    : vốn (USDT) để tính size tuyệt đối; optional (thiếu → chỉ ra %/đòn bẩy)
    risk_pct  : % vốn chịu rủi ro mỗi lệnh (mặc định 1%)
    fee_pct   : phí 1 chiều % (taker ~0.05); breakeven tính 2 chiều
    liq_safety: thanh lý phải cách SL ≥ liq_safety lần khoảng SL (đệm cho wick vượt SL)
    """
    d = (direction or "").upper()
    if d not in ("LONG", "SHORT"):
        return {"error": "direction phải LONG/SHORT"}
    if entry <= 0 or stop <= 0:
        return {"error": "entry/stop không hợp lệ"}
    # Kiểm tra stop đúng phía
    if d == "LONG" and stop >= entry:
        return {"error": "LONG: stop phải DƯỚI entry"}
    if d == "SHORT" and stop <= entry:
        return {"error": "SHORT: stop phải TRÊN entry"}

    risk_price = abs(entry - stop)                 # 1R theo giá
    stop_dist_pct = risk_price / entry * 100        # khoảng SL %
    sign = 1 if d == "LONG" else -1                 # hướng có lợi

    # ── Đòn bẩy an toàn: thanh lý xa hơn SL liq_safety lần ──
    # Xấp xỉ: với lev L, thanh lý cách entry ~ (100/L)% (bỏ qua phí/maintenance).
    # Cần (100/L) ≥ liq_safety * stop_dist_pct → L ≤ 100/(liq_safety*stop_dist_pct).
    lev_cap = 100.0 / (liq_safety * stop_dist_pct) if stop_dist_pct > 0 else 1
    lev = max(1, min(max_lev, int(lev_cap)))

    # ── Breakeven có phí (giá phải đi bao nhiêu để hoà sau phí 2 chiều) ──
    be_move_pct = 2 * fee_pct                        # xấp xỉ: phí vào+ra trên notional
    breakeven = entry * (1 + sign * be_move_pct / 100)

    # ── Position sizing theo risk-budget ──
    pos = None
    if equity and equity > 0:
        risk_budget = equity * risk_pct / 100.0      # USDT chịu mất nếu dính SL
        qty = risk_budget / risk_price               # số coin
        notional = qty * entry
        margin = notional / lev
        pos = {
            "risk_budget_usdt": round(risk_budget, 2),
            "qty": round(qty, 6),
            "notional_usdt": round(notional, 2),
            "margin_usdt": round(margin, 2),
            "lev_used": lev,
        }

    # ── Thang chốt lời từng phần ──
    ladder = []
    remaining = 1.0
    for i, (rr, frac) in enumerate(zip(tp_rr, close_fracs)):
        price = entry + sign * rr * risk_price
        remaining -= frac
        action = f"Chốt {int(frac*100)}%"
        if i == 0:
            action += " · dời SL về hoà (breakeven)"
        ladder.append({
            "tp": f"TP{i+1}", "rr": rr, "price": round(price, 8),
            "pct_move": round(sign * (price/entry - 1) * 100, 2),
            "close_frac": frac, "action": action,
        })
    runner = round(max(0.0, remaining), 2)             # phần để chạy theo trailing

    # Kỳ vọng R nếu kịch bản khớp (phần runner giả định đạt TP cuối)
    plan_R = sum(rr * frac for rr, frac in zip(tp_rr, close_fracs))
    if runner > 0:
        plan_R += tp_rr[-1] * runner

    # ── Trailing stop gợi ý (sau khi qua TP2) ──
    trail = None
    if atr and atr > 0:
        trail = {
            "atr": round(atr, 8),
            "distance": round(trail_atr * atr, 8),
            "note": f"Sau TP2: dời SL bám theo giá, cách {trail_atr}×ATR "
                    f"(~{round(trail_atr*atr/entry*100,2)}%). Khoá lời phần runner.",
        }

    return {
        "direction": d, "entry": entry, "stop": stop,
        "R_price": round(risk_price, 8),
        "stop_dist_pct": round(stop_dist_pct, 2),
        "lev_suggested": lev, "lev_cap_raw": round(lev_cap, 1),
        "breakeven": round(breakeven, 8), "be_move_pct": round(be_move_pct, 3),
        "position": pos,
        "ladder": ladder, "runner_frac": runner, "plan_R": round(plan_R, 2),
        "trail": trail,
        "disclaimer": "Máy tính rủi ro — KHÔNG phải lời khuyên vào lệnh. Tự quyết & tự chịu.",
    }


def portfolio_exposure(positions, equity, max_risk_pct=3.0):
    """
    Tổng rủi ro đang mở vs TRẦN cho phép. Chống mở quá nhiều lệnh tương quan.
    positions: list dict {symbol, direction, entry, stop, qty?} — qty optional.
               Nếu có qty → risk = |entry-stop|*qty (USDT). Nếu không → bỏ qua tiền,
               chỉ đếm số lệnh.
    """
    total_risk = 0.0
    rows = []
    for p in positions:
        e = p.get("entry"); s = p.get("stop"); q = p.get("qty")
        risk_usdt = None
        if e and s and q:
            risk_usdt = abs(e - s) * q
            total_risk += risk_usdt
        rows.append({"symbol": p.get("symbol"), "direction": p.get("direction"),
                     "risk_usdt": round(risk_usdt, 2) if risk_usdt is not None else None})
    cap_usdt = equity * max_risk_pct / 100.0 if equity else None
    over = (cap_usdt is not None and total_risk > cap_usdt)
    return {
        "n_positions": len(positions),
        "total_risk_usdt": round(total_risk, 2),
        "cap_usdt": round(cap_usdt, 2) if cap_usdt is not None else None,
        "max_risk_pct": max_risk_pct,
        "over_cap": over,
        "rows": rows,
        "msg": (f"⚠️ VƯỢT trần: rủi ro mở {total_risk:.0f} > trần {cap_usdt:.0f} USDT "
                f"({max_risk_pct}% vốn). Giảm size hoặc đóng bớt." if over else
                (f"OK: rủi ro mở {total_risk:.0f}/{cap_usdt:.0f} USDT trong trần." if cap_usdt
                 else f"{len(positions)} lệnh mở (thiếu equity để tính % rủi ro).")),
    }


if __name__ == "__main__":
    import json
    print("── SHORT ARB demo (entry .161, SL .172, ATR .003, equity 1000, risk 1%) ──")
    p = trade_plan("SHORT", 0.161, 0.172, atr=0.003, equity=1000, risk_pct=1.0)
    print(json.dumps(p, ensure_ascii=False, indent=2))
    print("\n── Portfolio exposure ──")
    print(json.dumps(portfolio_exposure(
        [{"symbol": "ARBUSDT", "direction": "SHORT", "entry": 0.161, "stop": 0.172, "qty": 909},
         {"symbol": "BNBUSDT", "direction": "SHORT", "entry": 600, "stop": 630, "qty": 0.33}],
        equity=1000, max_risk_pct=3.0), ensure_ascii=False, indent=2))
