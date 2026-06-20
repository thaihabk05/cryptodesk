"""
data_pull.py — Phase 1 của rebuild: kéo historical data cho backtester replay.

Triết lý (vs hệ thống cũ):
- KHÔNG backtest signal đã lưu (sample nhỏ, overfit). Thay vào: kéo data thô,
  replay từng nến → hàng nghìn lệnh → thống kê thật + walk-forward.
- Universe CHẤT LƯỢNG, chống survivorship: coin phải có volume nhất quán SUỐT
  window (lọc từ chính kline data, không dùng rank hôm nay).

Output: research/data/
  - universe.json          danh sách coin cuối + tier + lý do
  - klines/{sym}_{tf}.parquet   OHLCV H1/H4/D1
  - funding/{sym}.parquet       funding rate history (8h)

Chạy: python3 research/data_pull.py
"""
import sys, os, json, time
import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FUTURES_BASE = "https://fapi.binance.com"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KLINES_DIR = os.path.join(DATA_DIR, "klines")
FUNDING_DIR = os.path.join(DATA_DIR, "funding")

# ── Config ──────────────────────────────────────────────────────────────
MONTHS_BACK       = 6
MIN_VOL_USD       = 30_000_000     # volume nhất quán tối thiểu / ngày
MIN_LISTING_DAYS  = 90             # loại coin mới list (casino)
MIN_VOL_COVERAGE  = 0.80           # ≥80% số ngày trong window phải đạt min vol
TARGET_TF         = ["1h", "4h", "1d"]

# Majors — mean reversion sạch, vol thấp. Phần còn lại = midcap.
MAJORS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "LTCUSDT",
    "BCHUSDT", "MATICUSDT", "ATOMUSDT",
}

import re
LEVERAGE_PAT = re.compile(r'(UP|DOWN|BULL|BEAR|[2-9]L|[2-9]S|HEDGE|HALF)USDT$')
STABLE_PAT   = re.compile(r'^(USDC|BUSD|TUSD|FDUSD|USDP|DAI|FRAX|LUSD|SUSD|USDD|USTC|GUSD)')
HARD_BL      = {"LUNA2USDT", "LUNCUSDT", "LUNAUSDT", "USDTUSDT", "BCCUSDT"}


def _get(url, params, retries=4):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
    return None


def fetch_klines_paged(symbol, interval, start_ms, end_ms):
    """Kéo klines có phân trang (Binance limit 1500/req)."""
    url = FUTURES_BASE + "/fapi/v1/klines"
    rows = []
    cur = start_ms
    while cur < end_ms:
        data = _get(url, {"symbol": symbol, "interval": interval,
                          "startTime": cur, "endTime": end_ms, "limit": 1500})
        if not data:
            break
        rows.extend(data)
        last_open = data[-1][0]
        if len(data) < 1500:
            break
        cur = last_open + 1
        time.sleep(0.25)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qv","trades","tbb","tbq","ignore"])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for c in ["open","high","low","close","volume","qv"]:
        df[c] = df[c].astype(float)
    df = df.drop_duplicates("open_time").set_index("open_time")
    return df[["open","high","low","close","volume","qv"]]


def fetch_funding_history(symbol, start_ms, end_ms):
    """Funding rate history (8h/lần). Limit 1000/req → 6 tháng = ~540 entry, 1 req đủ."""
    url = FUTURES_BASE + "/fapi/v1/fundingRate"
    rows = []
    cur = start_ms
    while cur < end_ms:
        data = _get(url, {"symbol": symbol, "startTime": cur,
                          "endTime": end_ms, "limit": 1000})
        if not data:
            break
        rows.extend(data)
        if len(data) < 1000:
            break
        cur = data[-1]["fundingTime"] + 1
        time.sleep(0.25)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms")
    df["fundingRate"] = df["fundingRate"].astype(float) * 100  # → pct
    df = df.drop_duplicates("fundingTime").set_index("fundingTime")
    return df[["fundingRate"]]


def build_universe():
    """Lọc universe chất lượng, chống survivorship qua historical volume."""
    print("[1/3] Lấy danh sách futures tickers...")
    tickers = _get(FUTURES_BASE + "/fapi/v1/ticker/24hr", {})
    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):     continue
        if sym in HARD_BL:               continue
        if LEVERAGE_PAT.search(sym):     continue
        if STABLE_PAT.match(sym):        continue
        vol = float(t.get("quoteVolume", 0))
        if vol < MIN_VOL_USD:            continue   # sơ lọc theo vol hôm nay
        candidates.append(sym)
    print(f"  → {len(candidates)} coin qua sơ lọc volume hôm nay")

    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - MONTHS_BACK * 30 * 24 * 3600 * 1000

    universe = []
    print(f"[2/3] Kiểm tra listing age + volume coverage ({MONTHS_BACK} tháng)...")
    for i, sym in enumerate(candidates):
        try:
            df_d1 = fetch_klines_paged(sym, "1d", start_ms, now_ms)
            time.sleep(0.2)
            if df_d1.empty:
                continue
            # Listing age: số ngày data thực có
            listing_days = len(df_d1)
            if listing_days < MIN_LISTING_DAYS:
                continue
            # Anti-survivorship: % số ngày đạt min vol (qv = quote volume USDT)
            days_ok = (df_d1["qv"] >= MIN_VOL_USD).sum()
            coverage = days_ok / len(df_d1)
            if coverage < MIN_VOL_COVERAGE:
                continue
            tier = "major" if sym in MAJORS else "midcap"
            universe.append({
                "symbol": sym, "tier": tier,
                "listing_days": int(listing_days),
                "vol_coverage": round(float(coverage), 3),
                "avg_daily_vol_usd": round(float(df_d1["qv"].mean()), 0),
            })
            if (i + 1) % 20 == 0:
                print(f"  ...{i+1}/{len(candidates)} checked, {len(universe)} pass")
        except Exception as e:
            print(f"  [skip] {sym}: {str(e)[:60]}")
            continue

    universe.sort(key=lambda x: (x["tier"] != "major", -x["avg_daily_vol_usd"]))
    return universe, start_ms, now_ms


def main():
    os.makedirs(KLINES_DIR, exist_ok=True)
    os.makedirs(FUNDING_DIR, exist_ok=True)

    universe, start_ms, now_ms = build_universe()
    majors  = [u for u in universe if u["tier"] == "major"]
    midcaps = [u for u in universe if u["tier"] == "midcap"]
    print(f"\n[Universe] {len(universe)} coin: {len(majors)} majors + {len(midcaps)} midcap")

    with open(os.path.join(DATA_DIR, "universe.json"), "w") as f:
        json.dump({"generated_ms": now_ms, "start_ms": start_ms,
                   "config": {"months": MONTHS_BACK, "min_vol_usd": MIN_VOL_USD,
                              "min_listing_days": MIN_LISTING_DAYS,
                              "min_vol_coverage": MIN_VOL_COVERAGE},
                   "coins": universe}, f, indent=2)
    print(f"  → universe.json saved")

    print(f"\n[3/3] Kéo klines + funding cho {len(universe)} coin...")
    for i, u in enumerate(universe):
        sym = u["symbol"]
        try:
            for tf in TARGET_TF:
                df = fetch_klines_paged(sym, tf, start_ms, now_ms)
                if not df.empty:
                    df.to_parquet(os.path.join(KLINES_DIR, f"{sym}_{tf}.parquet"))
                time.sleep(0.2)
            fdf = fetch_funding_history(sym, start_ms, now_ms)
            if not fdf.empty:
                fdf.to_parquet(os.path.join(FUNDING_DIR, f"{sym}.parquet"))
            print(f"  [{i+1}/{len(universe)}] {sym} ({u['tier']}) done")
        except Exception as e:
            print(f"  [{i+1}/{len(universe)}] {sym} ERROR: {str(e)[:60]}")
            continue

    print("\n✅ Phase 1 data pull xong. Tiếp theo: build backtester.")


if __name__ == "__main__":
    main()
