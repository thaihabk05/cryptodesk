"""
phase9_review.py — RÀ SOÁT TỔNG THỂ ARB monitor Rule B/C (11/2026).

Mục tiêu: tìm nâng cấp CÓ BẰNG CHỨNG, không sửa theo 1 chart.
  • Nối data 3 năm (→07/2026) với data MỚI 07→11/2026 (phase7 chưa từng thấy = OOS thật).
  • Mọi biến thể khai báo TRƯỚC, báo đủ: toàn bộ / CŨ / MỚI / % quý dương.
  • Chấp nhận nâng cấp chỉ khi tốt hơn ở CẢ cũ lẫn mới (không chỉ full-sample).

Câu hỏi kiểm định:
  Q1  Thành phần nào mang edge: B riêng, C riêng, hay B+C trùng (confluence)?
  Q2  Gate mở SỚM hơn (early_break: dưới EMA34+EMA89, 24h<-4%, chưa cần thủng EMA200 H1)
      có bắt thêm lệnh tốt không? (giả thuyết từ 10/11: gate còn báo "MẠNH" khi đã -9% từ đỉnh)
  Q3  TP 3×ATR có hơn TP 2×ATR?
  Q4  LIVE đang chấm trên nến CHƯA ĐÓNG (chạy phút :18) — lệch backtest bao nhiêu?
      Mô phỏng bằng nến 15m: tín hiệu tính trên nến H1 dở tại phút :15.
"""
import os, time, sys
import numpy as np, pandas as pd, requests

HERE = os.path.dirname(os.path.abspath(__file__))
KL = os.path.join(HERE, "data", "klines_long")
RECENT = os.path.join(HERE, "data", "klines_recent")
os.makedirs(RECENT, exist_ok=True)
SPLIT = pd.Timestamp("2026-07-08")      # sau mốc này = data phase7 chưa thấy
H = {"User-Agent": "cryptodesk-research"}


def _fetch(sym, interval, start):
    """Kéo klines từ `start` tới giờ (bỏ nến đang hình thành). Cache theo ngày."""
    cache = os.path.join(RECENT, f"{sym}_{interval}_{pd.Timestamp.utcnow().strftime('%Y%m%d')}.parquet")
    if os.path.exists(cache):
        return pd.read_parquet(cache)
    rows = []; t = int(start.timestamp() * 1000)
    while True:
        r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": sym, "interval": interval, "startTime": t, "limit": 1500},
                         headers=H, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"binance {r.status_code}: {r.text[:120]}")
        k = r.json()
        if not k: break
        rows += k; t = int(k[-1][0]) + 1
        if len(k) < 1500: break
        time.sleep(0.4)
    df = pd.DataFrame(rows).iloc[:, :8]
    df.columns = ["open_time", "open", "high", "low", "close", "volume", "close_time", "qv"]
    df = df[df["close_time"].astype("int64") < int(time.time() * 1000)]      # bỏ nến dở
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms")
    df = df.set_index("open_time")[["open", "high", "low", "close", "volume", "qv"]].astype(float)
    df.to_parquet(cache)
    return df


def load_ext(sym):
    """3 năm + phần mới, raw OHLCV H1."""
    old = pd.read_parquet(os.path.join(KL, f"{sym}_1h.parquet"))[["open", "high", "low", "close", "volume"]]
    new = _fetch(sym, "1h", old.index[-1] - pd.Timedelta(days=3))
    old = old.iloc[:-1]            # nến cuối data cũ là nến DỞ lúc kéo → bỏ, lấy bản đã đóng từ data mới
    ov = old.index.intersection(new.index)
    if len(ov):
        d = float((old.loc[ov, "close"] / new.loc[ov, "close"] - 1).abs().max())
        assert d < 1e-6, f"{sym}: data cũ/mới lệch {d}"
    df = pd.concat([old, new[["open", "high", "low", "close", "volume"]]])
    return df[~df.index.duplicated(keep="last")].sort_index()


def prep():
    arb = load_ext("ARBUSDT"); btc = load_ext("BTCUSDT")
    for p in (9, 34, 89, 200):
        arb[f"e{p}"] = arb["close"].ewm(span=p, adjust=False).mean()
    d = arb["close"].diff()
    g = d.clip(lower=0).rolling(14, min_periods=1).mean()
    l = (-d.clip(upper=0)).rolling(14, min_periods=1).mean()
    arb["rsi"] = (100 - 100 / (1 + g / l.replace(0, np.nan))).fillna(50)
    arb["atr"] = (arb["high"] - arb["low"]).rolling(14, min_periods=1).mean()   # như LIVE (H-L)
    # EMA200 H4 từ nến H4 ĐÃ ĐÓNG (shift 1 → không lookahead)
    h4 = arb["close"].resample("4h").last().dropna()
    arb["e200h4"] = h4.ewm(span=200, adjust=False).mean().shift(1).reindex(arb.index, method="ffill")
    arb["btc"] = btc["close"].reindex(arb.index, method="ffill")
    return arb.dropna(subset=["btc"])


def sim(a, i, sl, tp, max_bars=72):
    """Vào ở open nến i. SL xét trước (bi quan). Trả pnl_r."""
    e = a["open"].values[i]; risk = sl - e
    if risk <= 0: return None
    hi = a["high"].values; lo = a["low"].values
    end = min(i + max_bars, len(a))
    for j in range(i, end):
        if hi[j] >= sl: return -1.0
        if lo[j] <= tp: return (e - tp) / risk
    return (e - a["close"].values[end - 1]) / risk


def signals(a, gate="cur"):
    """Trả DataFrame cờ theo từng nến (đã đóng): bearish, B, C."""
    c, o, h = a["close"], a["open"], a["high"]
    arb24 = (c / c.shift(24) - 1) * 100
    btc24 = (a["btc"] / a["btc"].shift(24) - 1) * 100
    rel7 = (c / c.shift(168) - 1) * 100 - (a["btc"] / a["btc"].shift(168) - 1) * 100
    down = (a["e34"] < a["e89"]) & (a["e89"] < a["e200"])
    stb = (c < a["e200"]) & (c < a["e34"]) & (arb24 < -2)
    early = (c < a["e34"]) & (c < a["e89"]) & (arb24 < -4)
    strong = (c > a["e200h4"]) | (rel7 > 0)
    opened = down | stb | (early if gate == "early" else False)
    bearish = opened                      # strong & ~opened → skip; ~strong & ~opened → không bearish
    touched = (h.shift(1) >= a["e34"].shift(1)) | (h >= a["e34"])
    red = c < o
    B = bearish & touched & red & (c < a["e9"]) & (a["rsi"] >= 45)
    C = bearish & touched & red & ((arb24 - btc24) <= -3) & (a["rsi"] >= 42)
    return pd.DataFrame({"B": B, "C": C, "early_only": early & ~(down | stb), "strong": strong})


def run(a, gate="cur", rules="any", tp_atr=2.0, cooldown=4):
    s = signals(a, gate)
    B = s["B"].values; C = s["C"].values; eo = s["early_only"].values
    cl = a["close"].values; hi = a["high"].values; atr = a["atr"].values
    out = []; last = -10**9
    for i in range(200, len(a) - 1):
        if i - last < cooldown: continue
        b, c_ = B[i], C[i]
        fire = {"any": b or c_, "B": b, "C": c_, "both": b and c_,
                "B_only": b and not c_, "C_only": c_ and not b}[rules]
        if not fire: continue
        sl = max(hi[i], hi[i - 1]) + atr[i] * 0.5; tp = cl[i] - atr[i] * tp_atr
        r = sim(a, i + 1, sl, tp)
        if r is None: continue
        out.append({"time": a.index[i], "r": r, "early_only": bool(eo[i])}); last = i
    return pd.DataFrame(out)


def line(label, t):
    if t is None or len(t) == 0:
        print(f"  {label:34s} 0 lệnh"); return
    def blk(x):
        if len(x) == 0: return "   —  "
        return f"n={len(x):>4} WR={(x['r']>0).mean()*100:>4.0f}% exp={x['r'].mean():>+.3f}"
    old = t[t["time"] < SPLIT]; new = t[t["time"] >= SPLIT]
    q = t.groupby(t["time"].dt.to_period("Q"))["r"].mean()
    print(f"  {label:34s} ALL {blk(t)} | CŨ {blk(old)} | MỚI {blk(new)} | quý+ {(q>0).sum()}/{len(q)}")


def q4_forming_bar(a):
    """Q4: live chấm trên nến H1 DỞ (phút ~:15). So với chấm trên nến đã đóng — CHỈ trên giai đoạn MỚI
    (cần nến 15m; kéo ~4 tháng). Tín hiệu dở = dùng OHLC của 15 phút đầu làm 'nến hiện tại'."""
    m15 = _fetch("ARBUSDT", "15m", SPLIT - pd.Timedelta(days=1))
    first = m15[m15.index.minute == 0]                    # nến 15m đầu mỗi giờ
    b = a.copy()
    idx = b.index.intersection(first.index)
    # Thay nến H1 tại idx bằng nến 15m đầu (giả lập trạng thái lúc :15), EMA/RSI tính lại xấp xỉ:
    # dùng EMA nến trước + cập nhật 1 bước với close dở.
    f = first.loc[idx]
    part = pd.DataFrame(index=idx)
    part["open"] = f["open"]; part["high"] = f["high"]; part["low"] = f["low"]; part["close"] = f["close"]
    prev = b.shift(1).loc[idx]
    for p in (9, 34, 89, 200):
        k = 2 / (p + 1); part[f"e{p}"] = prev[f"e{p}"] + k * (part["close"] - prev[f"e{p}"])
    part["rsi"] = prev["rsi"]                              # RSI gần như không đổi trong 15'
    part["atr"] = prev["atr"]; part["e200h4"] = b.loc[idx, "e200h4"]; part["btc"] = prev["btc"]
    # cờ tín hiệu trên nến dở: cần shift() tham chiếu nến ĐÃ ĐÓNG trước đó → ghép chuỗi
    c = part["close"]; c24 = b["close"].shift(24).loc[idx]; b24 = b["btc"].shift(25).loc[idx]
    arb24 = (c / c24 - 1) * 100; btc24 = (part["btc"] / b24 - 1) * 100
    rel7 = (c / b["close"].shift(168).loc[idx] - 1) * 100 - (part["btc"] / b["btc"].shift(169).loc[idx] - 1) * 100
    down = (part["e34"] < part["e89"]) & (part["e89"] < part["e200"])
    stb = (c < part["e200"]) & (c < part["e34"]) & (arb24 < -2)
    bearish = down | stb
    touched = (prev["high"] >= prev["e34"]) | (part["high"] >= part["e34"])
    red = c < part["open"]
    Bf = bearish & touched & red & (c < part["e9"]) & (part["rsi"] >= 45)
    Cf = bearish & touched & red & ((arb24 - btc24) <= -3) & (part["rsi"] >= 42)
    fire = (Bf | Cf)
    # mô phỏng: vào tại close nến 15m đầu, SL/TP như live, đi tiếp bằng nến 15m
    hi15 = m15["high"].values; lo15 = m15["low"].values; cl15 = m15["close"].values
    pos = {t: n for n, t in enumerate(m15.index)}
    out = []; last = pd.Timestamp("2000-01-01")
    for t in idx[fire.values]:
        if (t - last) < pd.Timedelta(hours=4): continue
        n = pos.get(t)
        if n is None: continue
        e = part.at[t, "close"]; at = part.at[t, "atr"]
        sl = max(part.at[t, "high"], prev.at[t, "high"]) + at * 0.5; tp = e - at * 2
        if sl <= e: continue
        r = None
        for j in range(n + 1, min(n + 1 + 72 * 4, len(m15))):
            if hi15[j] >= sl: r = -1.0; break
            if lo15[j] <= tp: r = (e - tp) / (sl - e); break
        if r is None: r = (e - cl15[min(n + 72 * 4, len(m15) - 1)]) / (sl - e)
        out.append({"time": t, "r": r}); last = t
    return pd.DataFrame(out)


def main():
    a = prep()
    print(f"ARB H1 {a.index[0].date()} → {a.index[-1]} · {len(a)} nến · mốc CŨ/MỚI = {SPLIT.date()}")
    print("Short · SL = đỉnh rejection + 0.5ATR · vào open nến kế · timeout 72h · SL xét trước\n")
    print("Q1 — thành phần nào mang edge")
    line("V1 hiện tại (B hoặc C)", run(a))
    line("  chỉ Rule B", run(a, rules="B"))
    line("  chỉ Rule C", run(a, rules="C"))
    line("  B+C trùng nhau (confluence)", run(a, rules="both"))
    line("  B không kèm C", run(a, rules="B_only"))
    line("  C không kèm B", run(a, rules="C_only"))
    print("\nQ2 — gate mở sớm (early_break)")
    e = run(a, gate="early")
    line("V1 + early_break", e)
    line("  riêng các lệnh early thêm vào", e[e["early_only"]] if len(e) else e)
    print("\nQ3 — TP")
    line("TP 2×ATR (hiện tại)", run(a, tp_atr=2.0))
    line("TP 3×ATR", run(a, tp_atr=3.0))
    line("TP 1.5×ATR", run(a, tp_atr=1.5))
    print("\nQ4 — nến dở vs nến đóng (CHỈ giai đoạn MỚI, mô phỏng 15m)")
    closed = run(a); closed = closed[closed["time"] >= SPLIT]
    line("chấm trên nến ĐÃ ĐÓNG", closed)
    try:
        line("chấm trên nến DỞ (phút :15) = live", q4_forming_bar(a))
    except Exception as ex:
        print("  Q4 lỗi:", ex)
    print("\nMỚI theo tháng (V1):")
    v = run(a); v = v[v["time"] >= SPLIT]
    for m, x in v.groupby(v["time"].dt.to_period("M")):
        print(f"  {m}  n={len(x):>3} WR={(x['r']>0).mean()*100:>4.0f}% exp={x['r'].mean():>+.3f} totR={x['r'].sum():>+5.1f}")


if __name__ == "__main__":
    main()
