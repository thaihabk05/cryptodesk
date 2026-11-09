"""
forward_tracker.py — Tự giám sát edge LIVE (out-of-sample) vs backtest, cảnh báo edge-decay.

Vì sao cần: backtest đẹp KHÔNG bảo đảm edge sống khi chạy thật (overfit, regime đổi).
Module này đọc paper_trades_v1.json (các lệnh Rule A funding-short đã ĐÓNG — đây là
forward-test thật, out-of-sample), so với baseline backtest, và báo khi edge có dấu
hiệu CHẾT theo thống kê — KHÔNG phán theo 1-2 lệnh (đúng bài học tự-curve-fit).

Thống kê dùng (trung thực, bảo thủ):
  • Expectancy forward + sai số chuẩn (SE) → khoảng tin 95%.
  • Binomial 1 phía: WR live có thấp hơn baseline đủ để không-phải-nhiễu không.
  • Trạng thái: THU THẬP (n nhỏ) / OK / THEO DÕI / CẢNH BÁO edge-decay.
  • Rolling 20 lệnh gần nhất vs toàn bộ → bắt suy giảm GẦN ĐÂY.
KHÔNG tính PnL "ăn non"; chỉ dùng lệnh đã đóng (WIN/LOSS rõ ràng) để thống kê sạch.
"""
import os, json, math
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_FILE = os.path.join(ROOT, "data", "paper_trades_v1.json")
TZ = timezone(timedelta(hours=7))

# Baseline backtest 3 năm (Rule A funding-short, ngưỡng validate 0.03%).
# RR hệ thống = TP 2ATR / SL 3ATR ≈ 0.667R mỗi WIN, -1.0R mỗi LOSS.
BASELINE = {
    "wr": 0.62,          # backtest WR
    "exp": 0.051,        # backtest expectancy R/lệnh
    "rr_win": 0.667,     # R trung bình khi thắng
    "label": "Rule A funding-short · backtest 3y @0.03%: WR 62% · exp +0.051R · 80% quý+",
}
MIN_N = 20               # dưới ngưỡng này: chưa đủ để kết luận, chỉ THU THẬP
ROLL_N = 20              # cửa sổ rolling


def _ncdf(x):
    """CDF chuẩn tắc qua erf (khỏi cần scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _load():
    if os.path.exists(PAPER_FILE):
        try:
            return json.load(open(PAPER_FILE))
        except Exception:
            pass
    return {"open": [], "closed": []}


def _closed_rs(closed):
    """Lấy list pnl_r của các lệnh đã đóng, bỏ lệnh thiếu dữ liệu."""
    out = []
    for t in closed:
        r = t.get("pnl_r")
        if isinstance(r, (int, float)):
            out.append(float(r))
    return out


def _stats_block(rs):
    """n, WR, exp, SE, khoảng tin 95%, t-stat vs 0 và vs baseline, test WR."""
    n = len(rs)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for r in rs if r > 0)
    wr = wins / n
    exp = sum(rs) / n
    if n >= 2:
        mean = exp
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        sd = math.sqrt(var)
        se = sd / math.sqrt(n)
    else:
        sd = se = 0.0
    ci_lo = exp - 1.96 * se
    ci_hi = exp + 1.96 * se
    # t-stat expectancy vs 0 (edge có thật dương không)
    t0 = (exp / se) if se > 0 else 0.0
    # t-stat vs baseline (có thấp hơn backtest đáng kể không)
    tb = ((exp - BASELINE["exp"]) / se) if se > 0 else 0.0
    # Binomial 1 phía: P(WR quan sát ≤ | WR thật = baseline) — xấp xỉ chuẩn
    p0 = BASELINE["wr"]
    wr_z = ((wr - p0) / math.sqrt(p0 * (1 - p0) / n)) if n > 0 else 0.0
    wr_p_low = _ncdf(wr_z)  # p nhỏ ⇒ WR live thấp hơn baseline đáng kể
    return {
        "n": n, "wins": wins, "wr": round(wr, 3), "exp": round(exp, 4),
        "totR": round(sum(rs), 2), "sd": round(sd, 3), "se": round(se, 4),
        "ci_lo": round(ci_lo, 4), "ci_hi": round(ci_hi, 4),
        "t_vs0": round(t0, 2), "t_vs_base": round(tb, 2),
        "wr_z": round(wr_z, 2), "wr_p_low": round(wr_p_low, 3),
    }


def _verdict(allb, rollb):
    """
    Phân loại trạng thái edge từ thống kê — BẢO THỦ cho edge MỎNG (+0.05R).
    Với edge mỏng, exp<0 hay rolling<0 xảy ra ~1/3 thời gian DO NHIỄU kể cả khi
    edge khỏe → KHÔNG dùng dấu của exp làm cờ. Chỉ phán khi lệch baseline tính
    theo SE (sai số chuẩn) đủ lớn.
      THU THẬP < MIN_N.
      CẢNH BÁO  cận-trên 95% exp < 0 (đang THUA có ý nghĩa) HOẶC WR thấp hơn
                baseline rõ (p<0.05) kèm exp<0 → edge có dấu hiệu CHẾT.
      THEO DÕI  exp live thấp hơn baseline > 1 SE (t_vs_base < -1) HOẶC 20 lệnh
                gần nhất chảy máu rõ (roll exp < -0.15R) → lệch thật, cần chú ý.
      OK        lệch vs backtest nằm trong vùng nhiễu (≤ 1 SE).
    """
    n = allb.get("n", 0)
    if n < MIN_N:
        return {
            "state": "THU THẬP", "emoji": "⏳",
            "msg": f"Mới {n}/{MIN_N} lệnh đóng — chưa đủ mẫu để kết luận edge. "
                   f"Edge mỏng (+{BASELINE['exp']:.3f}R) cần ~80-100 lệnh mới xác nhận chắc.",
        }
    exp = allb["exp"]; ci_hi = allb["ci_hi"]
    wr_p_low = allb["wr_p_low"]; t_base = allb["t_vs_base"]
    roll_exp = rollb.get("exp") if rollb.get("n", 0) >= ROLL_N else None

    if ci_hi < 0 or (wr_p_low < 0.05 and exp < 0):
        return {
            "state": "CẢNH BÁO", "emoji": "🔴",
            "msg": (f"Edge có dấu hiệu CHẾT: exp live {exp:+.3f}R, cận-trên 95% = {ci_hi:+.3f}R"
                    + (f"; WR {allb['wr']*100:.0f}% thấp hơn baseline 62% đáng kể (p={wr_p_low:.2f})" if wr_p_low < 0.05 else "")
                    + ". → DỪNG tăng size, xem lại regime/giả định trước khi tiếp tục."),
        }
    if t_base < -1.0 or (roll_exp is not None and roll_exp < -0.15):
        bits = []
        if t_base < -1.0:
            bits.append(f"exp live {exp:+.3f}R thấp hơn baseline {abs(t_base):.1f} SE")
        if roll_exp is not None and roll_exp < -0.15:
            bits.append(f"20 lệnh gần nhất {roll_exp:+.3f}R (chảy máu)")
        return {
            "state": "THEO DÕI", "emoji": "🟡",
            "msg": ("Lệch dưới backtest đáng chú ý nhưng CHƯA kết luận chết: "
                    + "; ".join(bits) + ". Giữ size nhỏ, theo dõi thêm ~15-20 lệnh."),
        }
    return {
        "state": "OK", "emoji": "🟢",
        "msg": (f"Edge sống đúng kỳ vọng: exp live {exp:+.3f}R (baseline +{BASELINE['exp']:.3f}R), "
                f"WR {allb['wr']*100:.0f}%. Khác biệt vs backtest trong vùng nhiễu (≤1 SE)."),
    }


def forward_report():
    """Báo cáo đầy đủ cho endpoint/CLI."""
    p = _load()
    closed = p.get("closed", [])
    open_pos = p.get("open", [])
    rs = _closed_rs(closed)
    allb = _stats_block(rs)
    rollb = _stats_block(rs[-ROLL_N:]) if len(rs) >= 1 else {"n": 0}
    verdict = _verdict(allb, rollb)

    # Per-coin
    by_coin = {}
    for t in closed:
        r = t.get("pnl_r")
        if not isinstance(r, (int, float)):
            continue
        by_coin.setdefault(t.get("symbol", "?"), []).append(float(r))
    per_coin = []
    for sym, lst in sorted(by_coin.items(), key=lambda kv: -len(kv[1])):
        b = _stats_block(lst)
        per_coin.append({"symbol": sym, "n": b["n"], "wr": b["wr"],
                         "exp": b["exp"], "totR": b["totR"]})

    # Lệnh mở + tuổi
    open_info = []
    now = datetime.now(TZ)
    for pos in open_pos:
        age_h = None
        try:
            et = datetime.fromisoformat(pos["entry_time"])
            age_h = round((now - et).total_seconds() / 3600, 1)
        except Exception:
            pass
        open_info.append({"symbol": pos.get("symbol"), "entry": pos.get("entry"),
                          "sl": pos.get("sl"), "tp": pos.get("tp"),
                          "funding": pos.get("funding"), "age_h": age_h})

    return {
        "baseline": BASELINE,
        "overall": allb,
        "rolling20": rollb,
        "verdict": verdict,
        "per_coin": per_coin,
        "open": open_info,
        "open_count": len(open_pos),
        "min_n": MIN_N,
    }


def _cli():
    r = forward_report()
    b = r["overall"]; v = r["verdict"]
    print("=" * 70)
    print("FORWARD-TRACKER — edge LIVE vs backtest (out-of-sample)")
    print("=" * 70)
    print(r["baseline"]["label"])
    print("-" * 70)
    if b["n"] == 0:
        print("Chưa có lệnh đóng nào. Tracker sẽ bắt đầu thống kê khi paper engine đóng lệnh.")
    else:
        print(f"Tổng: n={b['n']}  WR={b['wr']*100:.1f}%  exp={b['exp']:+.4f}R  "
              f"totR={b['totR']:+.1f}  CI95=[{b['ci_lo']:+.3f},{b['ci_hi']:+.3f}]")
        print(f"      t(exp vs 0)={b['t_vs0']}  t(vs baseline)={b['t_vs_base']}  "
              f"WR_p_thấp={b['wr_p_low']}")
        rb = r["rolling20"]
        if rb.get("n", 0) >= 1:
            print(f"20 gần: n={rb['n']}  WR={rb['wr']*100:.1f}%  exp={rb['exp']:+.4f}R")
        print("Per-coin:")
        for c in r["per_coin"]:
            print(f"  {c['symbol']:12s} n={c['n']:>3}  WR={c['wr']*100:>5.1f}%  "
                  f"exp={c['exp']:+.4f}R  totR={c['totR']:+.1f}")
    print("-" * 70)
    print(f"{v['emoji']} {v['state']}: {v['msg']}")
    if r["open_count"]:
        print(f"\nĐang mở: {r['open_count']} lệnh")
        for o in r["open"]:
            print(f"  {o['symbol']} entry {o['entry']} SL {o['sl']} TP {o['tp']} "
                  f"({o['age_h']}h)")
    print("=" * 70)


if __name__ == "__main__":
    _cli()
