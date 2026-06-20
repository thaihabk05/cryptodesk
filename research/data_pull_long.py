"""
data_pull_long.py — Kéo FULL lịch sử (tối đa 3 năm) cho universe đã chọn.

Khác data_pull.py: KHÔNG rebuild universe (đã chọn trên thanh khoản hiện tại).
Chỉ kéo dài lịch sử để walk-forward đa chu kỳ. Coin nào có bao nhiêu lấy bấy nhiêu
(BTC/ETH ~3 năm, midcap mới hơn ít hơn) — backtester xử lý series dài ngắn khác nhau.

Lưu vào klines_long/ + funding_long/ để không đè data 6 tháng.
"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_pull import fetch_klines_paged, fetch_funding_history, DATA_DIR, TARGET_TF

KLINES_LONG  = os.path.join(DATA_DIR, "klines_long")
FUNDING_LONG = os.path.join(DATA_DIR, "funding_long")
MONTHS_BACK  = 36


def main():
    os.makedirs(KLINES_LONG, exist_ok=True)
    os.makedirs(FUNDING_LONG, exist_ok=True)
    universe = json.load(open(os.path.join(DATA_DIR, "universe.json")))["coins"]

    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - MONTHS_BACK * 30 * 24 * 3600 * 1000
    print(f"Kéo {MONTHS_BACK} tháng cho {len(universe)} coin...")

    for i, u in enumerate(universe):
        sym = u["symbol"]
        try:
            n_bars = {}
            for tf in TARGET_TF:
                df = fetch_klines_paged(sym, tf, start_ms, now_ms)
                if not df.empty:
                    df.to_parquet(os.path.join(KLINES_LONG, f"{sym}_{tf}.parquet"))
                    n_bars[tf] = len(df)
                time.sleep(0.15)
            fdf = fetch_funding_history(sym, start_ms, now_ms)
            if not fdf.empty:
                fdf.to_parquet(os.path.join(FUNDING_LONG, f"{sym}.parquet"))
            h1 = n_bars.get("1h", 0)
            print(f"  [{i+1}/{len(universe)}] {sym}: H1={h1} nến (~{h1/24/30:.1f} tháng)")
        except Exception as e:
            print(f"  [{i+1}/{len(universe)}] {sym} ERROR: {str(e)[:60]}")
    print("\n✅ Long pull xong → klines_long/ + funding_long/")


if __name__ == "__main__":
    main()
