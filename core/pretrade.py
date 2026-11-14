"""
pretrade.py — "Kiểm tra lệnh định vào": đối chiếu (hướng, entry) của người dùng với CHÍNH các gợi ý
hệ thống đang hiển thị trên card ARB. KHÔNG sinh tín hiệu mới, KHÔNG có trọng số/điểm mới.

Nguồn lập luận (đều lấy từ /api/arb/status):
  • entry_plan (Gợi ý vùng vào lệnh: setup, vùng vào, SL, mục tiêu, điều kiện kích hoạt)
  • short_score / long_score (điểm setup) + short_monitor (cổng regime Rule B/C)
  • resistances / supports / dn_res / dn_tgt (mốc cấu trúc)
  • sl_zone (Vùng SL an toàn, ATR) + liq (cụm thanh lý) + momentum
Hàm thuần: check_entry(status_dict, direction, entry) → dict. Không gọi mạng → test được.

Là công cụ ĐỐI CHIẾU, không phải lời khuyên vào lệnh. Setup & điểm số là thước đo kỹ thuật
chưa backtest; thứ duy nhất đã đo là alert Rule B/C (thắng ~51%, +0.14R/lệnh trên 3 năm).
"""
from core.risk import trade_plan

MIN_SL_ATR = 1.5     # SL gần hơn mức này coi là "chặt" (card: ARB hay wick quét SL chặt)
MATCH_ATR = 0.5      # entry cách vùng gợi ý ≤ 0.5 ATR coi là khớp setup


def _g(x):
    return f"{x:.5g}"


def _levels(d, keys):
    out = []
    for k in keys:
        for lv in d.get(k) or []:
            if isinstance(lv, dict) and lv.get("level"):
                out.append((float(lv["level"]), lv.get("label") or ("kháng cự D1" if k == "resistances" else "hỗ trợ D1")))
    return out


def check_entry(d, direction, entry=None):
    dr = (direction or "").upper()
    if dr not in ("LONG", "SHORT"):
        return {"error": "Chọn LONG hoặc SHORT"}
    if not d or d.get("error") or not d.get("price"):
        return {"error": "Chưa có dữ liệu ARB (" + str((d or {}).get("error", "trống")) + ")"}
    price = float(d["price"])
    z = d.get("sl_zone") or {}
    atr = float(z.get("atr") or price * float(z.get("atr_pct") or 1.5) / 100)
    try:
        X = float(entry) if entry not in (None, "") else price
    except Exception:
        return {"error": "Entry không hợp lệ"}
    if X <= 0 or abs(X / price - 1) > 0.25:
        return {"error": f"Entry {_g(X)} cách giá hiện tại {_g(price)} quá 25% — kiểm tra lại số"}
    long_ = dr == "LONG"
    opp = "SHORT" if long_ else "LONG"
    items = []
    def add(status, title, text):
        items.append({"status": status, "title": title, "text": text})

    # ── 1) HƯỚNG so với gợi ý hệ thống ───────────────────────────────────
    ep = d.get("entry_plan") or {}
    plan = ep.get("direction") or "RANGE"
    sc = d.get("long_score" if long_ else "short_score") or {}
    score = sc.get("score"); lab = sc.get("label", "—")
    sc_txt = (f"Điểm setup {dr}: {score}/100 ({lab}) — {sc.get('note','')}."
              + (f" Yếu tố: {'; '.join(sc.get('factors') or [])}." if sc.get("factors") else ""))
    gate_off = (not long_) and d.get("short_monitor") == "OFF"
    if gate_off:
        add("bad", "Hướng", f"Short-monitor đang TẮT — {d.get('reason','ARB đang mạnh/uptrend')}. {sc_txt}")
    elif plan == dr:
        st = "ok" if (score or 0) >= 55 else "warn" if (score or 0) >= 30 else "bad"
        add(st, "Hướng", f"CÙNG hướng hệ thống đang gợi ý ({plan} · {d.get('headline','')}). {sc_txt}")
    elif plan == opp:
        st = "bad" if (score or 0) < 30 else "warn"
        add(st, "Hướng", f"NGƯỢC hướng hệ thống đang gợi ý ({plan} · {d.get('headline','')}). {sc_txt}")
    else:
        st = "bad" if (score or 0) < 30 else "warn"
        add(st, "Hướng", f"Hệ thống đang trung tính (RANGE · {d.get('headline','')}) — chưa nghiêng về {dr}. {sc_txt}")
    if long_:
        add("info", "Lưu ý LONG", "Hệ thống chưa có edge LONG nào qua kiểm định (8 biến thể × 3 năm đều fail "
            "out-of-sample). Phần dưới chỉ là đối chiếu kỹ thuật.")

    # ── 2) LOẠI LỆNH (entry so với giá hiện tại) ─────────────────────────
    diff = (X / price - 1) * 100
    if abs(diff) <= 0.3:
        add("info", "Loại lệnh", f"Vào ngay quanh giá hiện tại {_g(price)}. {ep.get('now_note','')}")
    elif (long_ and X < price) or (not long_ and X > price):
        add("info", "Loại lệnh", f"Lệnh chờ (limit): giá phải {'GIẢM' if long_ else 'HỒI LÊN'} "
            f"{abs(diff):.1f}% từ {_g(price)} mới khớp {_g(X)}.")
    else:
        add("info", "Loại lệnh", f"Lệnh chờ phá vỡ: giá phải {'TĂNG' if long_ else 'GIẢM'} "
            f"{abs(diff):.1f}% từ {_g(price)} mới khớp {_g(X)} — vào theo đà, cần xác nhận đóng nến.")

    # ── 3) ENTRY so với vùng gợi ý ───────────────────────────────────────
    same = [s for s in (ep.get("setups") or []) if s.get("dir") == dr and s.get("entry")]
    matched = None; near = None
    def _dist(s):
        lo = s.get("entry_lo", s["entry"]); hi = s.get("entry_hi", s["entry"])
        return 0.0 if lo <= X <= hi else min(abs(X - lo), abs(X - hi))
    if not same:
        add("bad" if plan == opp else "warn", "Vùng vào",
            f"Hệ thống hiện KHÔNG có setup {dr} nào trong 'Gợi ý vùng vào lệnh'"
            + (f" (đang gợi ý {plan})." if plan in ("LONG", "SHORT") else "."))
    else:
        def _desc(s_):
            return (f"'{s_['name']}' — vào {s_['zone']} · SL {_g(s_['stop'])} · mục tiêu {_g(s_['target'])}"
                    + (f" · RR {s_['rr']}" if s_.get("rr") else ""))
        near = min(same, key=_dist)
        if _dist(near) <= MATCH_ATR * atr:
            matched = near
            trig = near.get("trigger")
            now_at = abs(X / price - 1) * 100 <= 0.3
            add("ok", "Vùng vào", f"KHỚP setup {_desc(near)}."
                + (f" Điều kiện: {trig}." if trig else "")
                + (" Giá đang ở ngay vùng này — chỉ hợp lệ khi điều kiện trên đã xảy ra." if (trig and now_at) else ""))
        else:
            # "giá tốt hơn" = cao hơn với SHORT, thấp hơn với LONG
            sg = -1 if long_ else 1
            better = [s_ for s_ in same if sg * float(s_["entry"]) > sg * X]   # setup cho giá vào TỐT hơn X
            worse = [s_ for s_ in same if sg * float(s_["entry"]) < sg * X]    # X đã đi QUA các setup này
            if better:
                # X vào sớm hơn vùng hệ thống chờ → đuổi giá
                nb = min(better, key=lambda s_: abs(float(s_["entry"]) - X)); e = float(nb["entry"])
                txt = (f"Entry {_g(X)} {'cao' if long_ else 'thấp'} hơn vùng gợi ý {_g(e)} là {abs(X/e-1)*100:.1f}% "
                       f"({abs(X-e)/atr:.1f} ATR) → vào SỚM/đuổi giá so với setup {_desc(nb)}. "
                       f"SL phải đặt xa hơn nên RR kém hơn setup.")
                if worse:
                    nw = min(worse, key=lambda s_: abs(float(s_["entry"]) - X))
                    txt += (f" Entry đang nằm GIỮA hai vùng gợi ý; lựa chọn còn lại của hệ thống là "
                            f"'{nw['name']}' ({nw['zone']}).")
                add("warn", "Vùng vào", txt)
            else:
                # X tốt giá hơn MỌI setup → giá phải đi xuyên qua các mốc hệ thống chờ phản ứng
                top = max(worse, key=lambda s_: sg * float(s_["entry"])); e = float(top["entry"]); stop = float(top["stop"])
                if (not long_ and X >= stop) or (long_ and X <= stop):
                    add("bad", "Vùng vào", f"Entry {_g(X)} nằm NGOÀI mức vô hiệu ({_g(stop)}) của setup {_desc(top)}. "
                        f"Giá tới được đây nghĩa là mốc mà hệ thống chờ {dr} đã bị phá — kịch bản {dr} hiện tại không còn.")
                else:
                    add("warn", "Vùng vào", f"Entry {_g(X)} {'thấp' if long_ else 'cao'} hơn vùng gợi ý {_g(e)} "
                        f"là {abs(X/e-1)*100:.1f}%: giá vào tốt hơn nhưng đã đi QUA mốc mà setup chờ phản ứng, "
                        f"chỉ còn cách mức vô hiệu {_g(stop)} {abs(stop/X-1)*100:.1f}%. Setup: {_desc(top)}.")

    # ── 4) SL — từ mốc hệ thống đã đưa ───────────────────────────────────
    cands = []
    if matched:
        cands.append((float(matched["stop"]), f"SL của setup '{matched['name']}'"))
    if long_:
        for lv, lb in _levels(d, ("dn_tgt", "supports")):
            if lv < X: cands.append((lv - 0.5 * atr, f"dưới {lb} {_g(lv)} − 0.5 ATR"))
        if z.get("sl_long") and float(z["sl_long"]) < X:
            cands.append((float(z["sl_long"]), "Vùng SL an toàn (đáy 24h − 0.5 ATR)"))
    else:
        for lv, lb in _levels(d, ("dn_res", "resistances")):
            if lv > X: cands.append((lv + 0.5 * atr, f"trên {lb} {_g(lv)} + 0.5 ATR"))
        if z.get("sl_short") and float(z["sl_short"]) > X:
            cands.append((float(z["sl_short"]), "Vùng SL an toàn (đỉnh 24h + 0.5 ATR)"))
    cands = [(p, s) for p, s in cands if (p < X if long_ else p > X)]
    cands.sort(key=lambda c: abs(c[0] - X))
    ok_c = [c for c in cands if abs(c[0] - X) >= MIN_SL_ATR * atr]
    tight = [c for c in cands if abs(c[0] - X) < MIN_SL_ATR * atr]
    if ok_c:
        sl, src = ok_c[0]
    else:
        sl = X - 2 * atr if long_ else X + 2 * atr
        src = "không có mốc cấu trúc phù hợp phía sau entry → tạm lấy 2×ATR"
    # Né cụm thanh lý: SL nằm trong/sát cụm → đẩy ra ngoài cụm
    liq = d.get("liq") or {}
    for c in (liq.get("clusters") or []):
        lo, hi = float(c.get("px_lo", 0)), float(c.get("px_hi", 0))
        if not lo or not hi: continue
        if (not long_) and lo * 0.997 <= sl <= hi * 1.002 and hi > X:
            sl = hi + 0.25 * atr; src += f" · đẩy lên trên cụm thanh lý {_g(lo)}–{_g(hi)}"
        if long_ and lo * 0.998 <= sl <= hi * 1.003 and lo < X:
            sl = lo - 0.25 * atr; src += f" · đẩy xuống dưới cụm thanh lý {_g(lo)}–{_g(hi)}"
    def _slrow(p, s):
        return {"price": round(p, 6), "source": s, "dist_pct": round(abs(p / X - 1) * 100, 2),
                "atr_mult": round(abs(p - X) / atr, 1)}
    sl_main = _slrow(sl, src)
    sl_alts = []
    if matched and abs(float(matched["stop"]) - sl) > 0.1 * atr:
        r = _slrow(float(matched["stop"]), f"SL gốc của setup '{matched['name']}'"); r["kind"] = "theo setup"
        r["note"] = ("RR cao như setup ghi, nhưng gần hơn %s ATR — dễ bị wick quét" % MIN_SL_ATR
                     if r["atr_mult"] < MIN_SL_ATR else "mức vô hiệu hệ thống ghi cho setup này")
        sl_alts.append(r)
    elif tight and abs(tight[-1][0] - X) >= 1.0 * atr:
        r = _slrow(*tight[-1]); r["kind"] = "chặt"
        r["note"] = f"gần hơn {MIN_SL_ATR} ATR — RR cao hơn nhưng dễ bị wick quét"
        sl_alts.append(r)
    wide = [c for c in ok_c if "Vùng SL an toàn" in c[1] and abs(c[0] - sl) > 0.1 * atr]
    if wide:
        r = _slrow(*wide[0]); r["kind"] = "rộng"; r["note"] = "sống qua wick tốt hơn, đổi lại size phải nhỏ"
        sl_alts.append(r)

    # ── 5) DƯ ĐỊA & RR tới mục tiêu hệ thống ─────────────────────────────
    tgt = None; tgt_lb = None
    if matched:
        tgt = float(matched["target"]); tgt_lb = f"mục tiêu của setup '{matched['name']}'"
    else:
        if long_:
            ah = [(lv, lb) for lv, lb in _levels(d, ("dn_res", "resistances")) if lv > X + 0.5 * atr]
            if ah: tgt, tgt_lb = min(ah, key=lambda t: t[0])
        else:
            ah = [(lv, lb) for lv, lb in _levels(d, ("dn_tgt", "supports")) if lv < X - 0.5 * atr]
            if ah: tgt, tgt_lb = max(ah, key=lambda t: t[0])
        if tgt is not None: tgt_lb = f"mốc cản gần nhất phía trước ({tgt_lb})"
    rr = None
    if tgt is not None:
        rr = round(abs(X - tgt) / abs(X - sl), 2)
        st = "ok" if rr >= 1.5 else "warn" if rr >= 1.0 else "bad"
        add(st, "Dư địa / RR", f"Tới {tgt_lb} {_g(tgt)} là {abs(tgt/X-1)*100:.1f}% · với SL đề xuất "
            f"{_g(sl)} ({sl_main['dist_pct']}%) → RR {rr}"
            + ("." if rr >= 1.5 else " — lời tiềm năng không đủ bù rủi ro." if rr < 1 else " — hơi mỏng."))
    else:
        add("info", "Dư địa / RR", "Không có mốc cản nào của hệ thống phía trước entry để tính RR.")

    # ── 6) BỐI CẢNH (chỉ thông tin — KHÔNG tính vào kết luận; đà đã nằm trong điểm setup) ──
    ctx = []
    m = d.get("momentum") or {}
    if m.get("state"):
        against = (m["state"] == "MẠNH LÊN" and not long_) or (m["state"] == "YẾU ĐI" and long_)
        ctx.append(f"Đà 4h: {m['state']}" + (" (ngược lệnh)" if against else ""))
    if liq.get("bias") in ("long_squeeze", "short_squeeze"):
        fav = (liq["bias"] == "long_squeeze") != long_
        ctx.append(f"Thanh lý: {liq.get('bias_label')} ({'thuận' if fav else 'ngược'} lệnh)")
    xf = d.get("xfund") or {}
    if xf.get("available"): ctx.append(f"Funding cross-sàn: {xf.get('label')}")
    if d.get("air_pocket"): ctx.append(str(d["air_pocket"]).replace("⚠️ ", ""))
    if ctx: add("info", "Bối cảnh", " · ".join(ctx) + ".")

    # ── KẾT LUẬN ─────────────────────────────────────────────────────────
    bad = [i["title"] for i in items if i["status"] == "bad"]
    warn = [i["title"] for i in items if i["status"] == "warn"]
    if bad:
        verdict = {"state": "KHÔNG KHỚP", "emoji": "🔴", "key": "bad",
                   "msg": f"Lệnh {dr} @ {_g(X)} KHÔNG khớp gợi ý hệ thống — vướng: {', '.join(bad)}"
                          + (f"; điểm trừ thêm: {', '.join(warn)}" if warn else "") + "."}
    elif warn:
        verdict = {"state": "KHỚP MỘT PHẦN", "emoji": "🟡", "key": "warn",
                   "msg": f"Lệnh {dr} @ {_g(X)} khớp một phần — điểm trừ: {', '.join(warn)}."}
    else:
        verdict = {"state": "KHỚP", "emoji": "🟢", "key": "ok",
                   "msg": f"Lệnh {dr} @ {_g(X)} khớp gợi ý hệ thống (hướng, vùng vào, RR)."}

    plan_ = trade_plan(dr, X, sl, atr=atr)
    return {
        "direction": dr, "entry": round(X, 6), "price": price, "atr": round(atr, 6),
        "atr_pct": round(atr / price * 100, 2),
        "verdict": verdict, "items": items,
        "sl": sl_main, "sl_alts": sl_alts, "target": tgt, "rr": rr,
        "lev_max": plan_.get("lev_suggested"), "breakeven": plan_.get("breakeven"),
        "ladder": plan_.get("ladder"), "runner_frac": plan_.get("runner_frac"),
        "size_pct_1risk": round(100 / sl_main["dist_pct"]) if sl_main["dist_pct"] else None,
        "disclaimer": "Đối chiếu với gợi ý kỹ thuật của hệ thống — KHÔNG phải lời khuyên vào lệnh. Setup & điểm số "
                      "chưa được backtest; thứ đã đo là alert Rule B/C (thắng ~51%, +0.14R/lệnh).",
    }
