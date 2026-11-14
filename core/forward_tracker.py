"""
forward_tracker.py — Tự giám sát edge LIVE (out-of-sample) vs backtest, cảnh báo edge-decay.

Hai SỔ, mỗi sổ có baseline riêng:
  A   Rule A funding-short — paper engine tự mở/đóng lệnh (paper_trades_v1.json)
  BC  Rule B/C — alert ARB gửi Telegram, được GHI LẠI + tự chấm SL/TP (arb_alerts_v1.json)

Vì sao cần: backtest đẹp KHÔNG bảo đảm edge sống khi chạy thật. Phase9 (11/2026) cho thấy
Rule B/C 4 tháng gần nhất chỉ còn +0.044R (CI95 −0.20…+0.29, n=80) so với +0.153R trước đó
→ chưa kết luận được còn hay mất. Chỉ có đo live mới trả lời.

Thống kê (bảo thủ cho edge mỏng): expectancy + SE + CI95, binomial WR 1 phía, rolling-20.
Chỉ phán khi lệch baseline > 1 SE hoặc cận-trên 95% < 0 — KHÔNG phán theo dấu của exp.
"""
import os, json, math, shutil
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TZ = timezone(timedelta(hours=7))
MIN_N = 20
ROLL_N = 20

BOOKS = {
    "A": {
        "file": "paper_trades_v1.json",
        "title": "Rule A — funding-short (paper tự mở lệnh)",
        "wr": 0.62, "exp": 0.051,
        "label": "backtest 3y @0.03%: WR 62% · exp +0.051R",
        "empty": "Rule A chỉ bắn khi funding ≥0.03% (long đòn bẩy đông). Funding đang ~0 → "
                 "không có setup, không phải lỗi. Xem dải 'Đang canh' để biết còn cách bao xa.",
    },
    "BC": {
        "file": "arb_alerts_v1.json",
        "title": "Rule B/C — alert ARB (Telegram)",
        "wr": 0.51, "exp": 0.141,
        "label": "backtest 3.3y: WR 51% · exp +0.141R (4 tháng gần nhất chỉ +0.044R)",
        "empty": "Alert B/C bắt đầu được ghi & tự chấm SL/TP từ bản này. Alert cũ chỉ nằm trên Telegram.",
    },
}


def state_dir():
    """Railway Volume (/data) nếu có — bền qua deploy. Không thì ./data (MẤT khi redeploy)."""
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data"
    return os.path.join(ROOT, "data")


def is_persistent():
    return state_dir() == "/data"


def state_file(name):
    p = os.path.join(state_dir(), name)
    legacy = os.path.join(ROOT, "data", name)
    if p != legacy and not os.path.exists(p) and os.path.exists(legacy):
        try: shutil.copy(legacy, p)
        except Exception: pass
    return p


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _load(name):
    p = state_file(name)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {"open": [], "closed": []}


def _rs(closed):
    return [float(t["pnl_r"]) for t in closed if isinstance(t.get("pnl_r"), (int, float))]


def _stats_block(rs, base):
    n = len(rs)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for r in rs if r > 0)
    wr = wins / n
    exp = sum(rs) / n
    if n >= 2:
        sd = math.sqrt(sum((r - exp) ** 2 for r in rs) / (n - 1))
        se = sd / math.sqrt(n)
    else:
        sd = se = 0.0
    p0 = base["wr"]
    wr_z = (wr - p0) / math.sqrt(p0 * (1 - p0) / n)
    return {
        "n": n, "wins": wins, "wr": round(wr, 3), "exp": round(exp, 4),
        "totR": round(sum(rs), 2), "sd": round(sd, 3), "se": round(se, 4),
        "ci_lo": round(exp - 1.96 * se, 4), "ci_hi": round(exp + 1.96 * se, 4),
        "t_vs0": round(exp / se, 2) if se > 0 else 0.0,
        "t_vs_base": round((exp - base["exp"]) / se, 2) if se > 0 else 0.0,
        "wr_z": round(wr_z, 2), "wr_p_low": round(_ncdf(wr_z), 3),
    }


def _verdict(allb, rollb, base):
    """THU THẬP <MIN_N · CẢNH BÁO (CI_hi<0 hoặc WR thấp rõ p<0.01 + exp<0) · THEO DÕI (lệch >1 SE
    hoặc 20 lệnh gần chảy máu <-0.15R) · OK."""
    n = allb.get("n", 0)
    if n < MIN_N:
        return {"state": "THU THẬP", "emoji": "⏳",
                "msg": f"Mới {n}/{MIN_N} lệnh đóng — chưa đủ mẫu để kết luận. "
                       f"Edge mỏng (+{base['exp']:.3f}R) cần ~80-100 lệnh mới xác nhận chắc."}
    exp = allb["exp"]; ci_hi = allb["ci_hi"]
    wr_p_low = allb["wr_p_low"]; t_base = allb["t_vs_base"]
    roll_exp = rollb.get("exp") if rollb.get("n", 0) >= ROLL_N else None
    # Đỏ phải KHÓ bật: tracker bị xem lặp lại nhiều lần → ngưỡng 5% sẽ tích luỹ báo động giả
    # (mô phỏng edge khoẻ vẫn đỏ ~1/5 lần). Dùng p<0.01 cho nhánh WR; lệch nhẹ để 🟡 lo.
    if ci_hi < 0 or (wr_p_low < 0.01 and exp < 0):
        return {"state": "CẢNH BÁO", "emoji": "🔴",
                "msg": (f"Edge có dấu hiệu CHẾT: exp live {exp:+.3f}R, cận-trên 95% = {ci_hi:+.3f}R"
                        + (f"; WR {allb['wr']*100:.0f}% thấp hơn baseline {base['wr']*100:.0f}% đáng kể "
                           f"(p={wr_p_low:.3f})" if wr_p_low < 0.01 else "")
                        + ". → DỪNG tăng size, xem lại regime/giả định trước khi tiếp tục.")}
    if t_base < -1.0 or (roll_exp is not None and roll_exp < -0.15):
        bits = []
        if t_base < -1.0:
            bits.append(f"exp live {exp:+.3f}R thấp hơn baseline {abs(t_base):.1f} SE")
        if roll_exp is not None and roll_exp < -0.15:
            bits.append(f"20 lệnh gần nhất {roll_exp:+.3f}R (chảy máu)")
        return {"state": "THEO DÕI", "emoji": "🟡",
                "msg": "Lệch dưới backtest đáng chú ý nhưng CHƯA kết luận chết: "
                       + "; ".join(bits) + ". Giữ size nhỏ, theo dõi thêm ~15-20 lệnh."}
    return {"state": "OK", "emoji": "🟢",
            "msg": f"Edge sống đúng kỳ vọng: exp live {exp:+.3f}R (baseline +{base['exp']:.3f}R), "
                   f"WR {allb['wr']*100:.0f}%. Khác biệt vs backtest trong vùng nhiễu (≤1 SE)."}


def _book(key):
    base = BOOKS[key]
    p = _load(base["file"])
    closed = p.get("closed", []); open_pos = p.get("open", [])
    rs = _rs(closed)
    allb = _stats_block(rs, base)
    rollb = _stats_block(rs[-ROLL_N:], base) if rs else {"n": 0}
    # Nhóm: sổ A theo coin, sổ BC theo rule (B / C / B+C)
    gkey = "symbol" if key == "A" else "rule"
    groups = {}
    for t in closed:
        if isinstance(t.get("pnl_r"), (int, float)):
            groups.setdefault(str(t.get(gkey, "?")), []).append(float(t["pnl_r"]))
    per_group = []
    for g, lst in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        b = _stats_block(lst, base)
        per_group.append({"name": g.replace("USDT", ""), "n": b["n"], "wr": b["wr"],
                          "exp": b["exp"], "totR": b["totR"]})
    now = datetime.now(TZ); open_info = []
    for pos in open_pos:
        age_h = None
        try:
            age_h = round((now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 3600, 1)
        except Exception:
            pass
        open_info.append({"symbol": pos.get("symbol"), "rule": pos.get("rule"), "entry": pos.get("entry"),
                          "sl": pos.get("sl"), "tp": pos.get("tp"), "age_h": age_h})
    recent = [{"time": (t.get("entry_time") or "")[5:16].replace("T", " "),
               "name": t.get("rule") or (t.get("symbol") or "").replace("USDT", ""),
               "status": t.get("status"), "pnl_r": t.get("pnl_r")} for t in closed[-6:]][::-1]
    return {"key": key, "title": base["title"], "baseline": base["label"], "empty": base["empty"],
            "overall": allb, "rolling20": rollb, "verdict": _verdict(allb, rollb, base),
            "per_group": per_group, "open": open_info, "open_count": len(open_pos), "recent": recent}


def forward_report():
    return {"books": [_book("BC"), _book("A")], "min_n": MIN_N,
            "persistent": is_persistent(), "storage": state_dir()}


def _cli():
    r = forward_report()
    print("=" * 72)
    print(f"FORWARD-TRACKER · storage={r['storage']} · bền qua deploy={r['persistent']}")
    for bk in r["books"]:
        b = bk["overall"]; v = bk["verdict"]
        print("-" * 72); print(bk["title"], "·", bk["baseline"])
        if b["n"] == 0:
            print("  Chưa có lệnh đóng.", bk["empty"])
        else:
            print(f"  n={b['n']} WR={b['wr']*100:.1f}% exp={b['exp']:+.4f}R totR={b['totR']:+.1f} "
                  f"CI95=[{b['ci_lo']:+.3f},{b['ci_hi']:+.3f}] t_base={b['t_vs_base']}")
            for g in bk["per_group"]:
                print(f"    {g['name']:8s} n={g['n']:>3} WR={g['wr']*100:>5.1f}% exp={g['exp']:+.3f}R")
        print(f"  {v['emoji']} {v['state']}: {v['msg']}")
        if bk["open_count"]:
            print(f"  Đang mở: {bk['open_count']}")
    print("=" * 72)


if __name__ == "__main__":
    _cli()
