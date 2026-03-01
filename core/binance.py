"""binance.py — Tất cả Binance API calls, dùng chung cho Dashboard & Scanner"""
import requests
import pandas as pd

FUTURES_BASE = "https://fapi.binance.com"

# Luôn dùng Futures API — tránh 451 geo-block của Spot API
# Dashboard chỉ trade futures nên data futures là chính xác hơn

def fetch_klines(symbol: str, interval: str, limit: int = 300,
                 force_futures: bool = False) -> pd.DataFrame:
    import time as _t
    url = FUTURES_BASE + "/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    for attempt in range(3):
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 429:
            wait = 2 ** attempt  # 1s, 2s, 4s
            _t.sleep(wait)
            continue
        r.raise_for_status()
        break
    else:
        r.raise_for_status()

    df = pd.DataFrame(r.json(), columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qv","trades","tbb","tbq","ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df.set_index("open_time", inplace=True)
    return df[["open","high","low","close","volume"]]


def fetch_all_futures_tickers(min_volume_usd: float = 10_000_000) -> list:
    """Lấy toàn bộ USDT perpetual futures có volume > threshold, đã lọc coin rác."""
    import re

    # ── Blacklist patterns ──────────────────────────────────────────────
    # Leverage tokens: BTCUP, ETHDOWN, BNBBULL, BTC2L, ETH3S...
    LEVERAGE_PAT = re.compile(r'(UP|DOWN|BULL|BEAR|[2-9]L|[2-9]S|HEDGE|HALF)USDT$')
    # Stablecoins & wrapped USD
    STABLE_PAT   = re.compile(r'^(USDC|BUSD|TUSD|FDUSD|USDP|DAI|FRAX|LUSD|SUSD|USDD|USTC|GUSD)')
    # Blacklist cứng
    BLACKLIST = {"LUNA2USDT", "LUNCUSDT", "LUNAUSDT", "USDTUSDT", "BCCUSDT"}
    # Giá tối thiểu — coin dưới $0.000001 thường là dead meme
    MIN_PRICE = 0.000001
    # ───────────────────────────────────────────────────────────────────

    r = requests.get(FUTURES_BASE + "/fapi/v1/ticker/24hr", timeout=15)
    r.raise_for_status()
    out = []
    for t in r.json():
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):        continue
        if sym in BLACKLIST:               continue
        if LEVERAGE_PAT.search(sym):       continue
        if STABLE_PAT.match(sym):          continue

        vol   = float(t.get("quoteVolume", 0))
        if vol < min_volume_usd:           continue

        price = float(t.get("lastPrice", 0))
        if price < MIN_PRICE:              continue

        out.append({
            "symbol":           sym,
            "volume_24h":       vol,
            "price_change_pct": float(t.get("priceChangePercent", 0)),
            "last_price":       price,
            "is_futures":       True,
        })
    return sorted(out, key=lambda x: x["volume_24h"], reverse=True)


def fetch_funding_rate(symbol: str):
    try:
        r = requests.get(FUTURES_BASE + "/fapi/v1/premiumIndex",
                         params={"symbol": symbol}, timeout=5)
        if r.status_code != 200: return None
        d = r.json()
        if isinstance(d, list): return None
        return float(d.get("lastFundingRate", 0)) * 100
    except: return None


def fetch_oi_change(symbol: str, period: str = "1h", limit: int = 25):
    try:
        r = requests.get(FUTURES_BASE + "/futures/data/openInterestHist",
                         params={"symbol": symbol, "period": period, "limit": limit}, timeout=5)
        if r.status_code != 200: return None
        data = r.json()
        if not data or isinstance(data, dict) or len(data) < 2: return None
        ois = [float(d["sumOpenInterest"]) for d in data]
        return round((ois[-1] - ois[0]) / ois[0] * 100, 2) if ois[0] else None
    except: return None


def fetch_btc_context() -> dict:
    """BTC market sentiment — dùng để warn khi LONG altcoin lúc BTC bear."""
    try:
        df_d1 = fetch_klines("BTCUSDT", "1d", 50)
        df_h4 = fetch_klines("BTCUSDT", "4h", 100)
        df_h1 = fetch_klines("BTCUSDT", "1h", 50)
        for df in [df_d1, df_h4, df_h1]:
            for p in [34, 89]:
                df[f"ma{p}"] = df["close"].rolling(p, min_periods=max(1, p//2)).mean()

        price = float(df_h1["close"].iloc[-1])
        r_d1  = df_d1.iloc[-1]
        r_h4  = df_h4.iloc[-1]

        def trend(p, row):
            if p > row["ma34"] and p > row["ma89"]: return "BULL"
            if p < row["ma34"] and p < row["ma89"]: return "BEAR"
            return "NEUTRAL"

        btc_d1 = trend(price, r_d1)
        btc_h4 = trend(price, r_h4)

        chg_24h = round((price - float(df_h1["close"].iloc[-24])) / float(df_h1["close"].iloc[-24]) * 100, 2) \
                  if len(df_h1) >= 24 else 0

        if btc_d1 == "BULL" and btc_h4 == "BULL":
            sentiment, note = "RISK_ON", "BTC trend BULL D1+H4 — thị trường thuận cho LONG"
        elif btc_d1 == "BEAR" and btc_h4 == "BEAR":
            sentiment, note = "RISK_OFF", "BTC trend BEAR D1+H4 — thận trọng với LONG altcoin"
        elif btc_h4 == "BEAR" and chg_24h < -3:
            sentiment, note = "DUMP", f"BTC dump {chg_24h}% / 24h — tránh LONG altcoin"
        elif btc_h4 == "BULL" and chg_24h > 3:
            sentiment, note = "PUMP", f"BTC pump {chg_24h}% / 24h — LONG altcoin có lợi"
        else:
            sentiment, note = "NEUTRAL", "BTC sideways — xét tín hiệu từng mã riêng"

        return {"price": round(price, 2), "chg_24h": chg_24h,
                "d1_trend": btc_d1, "h4_trend": btc_h4,
                "sentiment": sentiment, "note": note}
    except Exception as e:
        return {"price": None, "chg_24h": None, "d1_trend": "N/A", "h4_trend": "N/A",
                "sentiment": "UNKNOWN", "note": str(e)}
