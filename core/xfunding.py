"""
xfunding.py — Funding CROSS-SÀN (Binance + Bybit + OKX) + tín hiệu divergence.
API CÔNG KHAI, KHÔNG cần key. Phục vụ edge funding-short: biết crowding long nằm
ở đâu & mạnh cỡ nào so với toàn thị trường, không chỉ Binance.

Ý nghĩa (cho hệ short-funding):
  • Binance funding CAO hơn trung vị sàn khác → long đông RIÊNG Binance → áp lực
    thanh lý/short trên Binance mạnh hơn (ủng hộ edge, vì mình short trên Binance).
  • Tất cả sàn funding cao đồng loạt → crowding long toàn thị trường → setup short rõ.
  • Tất cả thấp/âm → KHÔNG có crowding long → KHÔNG có setup funding-short.
  • Binance THẤP hơn hẳn sàn khác → crowding ở nơi khác, cẩn trọng.
Lưu ý: chu kỳ funding mỗi sàn có thể khác (4h/8h); đây là so SNAPSHOT tức thời.
"""
import requests

_H = {"User-Agent": "cryptodesk"}
_T = 8


def _binance(base):
    r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                     params={"symbol": f"{base}USDT"}, headers=_H, timeout=_T).json()
    return float(r["lastFundingRate"]) * 100


def _bybit(base):
    r = requests.get("https://api.bybit.com/v5/market/tickers",
                     params={"category": "linear", "symbol": f"{base}USDT"},
                     headers=_H, timeout=_T).json()
    return float(r["result"]["list"][0]["fundingRate"]) * 100


def _okx(base):
    r = requests.get("https://www.okx.com/api/v5/public/funding-rate",
                     params={"instId": f"{base}-USDT-SWAP"}, headers=_H, timeout=_T).json()
    return float(r["data"][0]["fundingRate"]) * 100


def cross_funding(base, binance_funding_pct=None):
    """
    base: 'ARB' | 'BTC' | 'BNB' ... (không kèm USDT)
    binance_funding_pct: nếu đã biết funding Binance (%, đã ×100) thì truyền vào để
                         KHỎI gọi lại Binance (tránh thêm tải fapi / rủi ro 418).
    """
    rates = {}
    # Binance (dùng lại nếu có)
    if binance_funding_pct is not None:
        try:
            rates["Binance"] = round(float(binance_funding_pct), 4)
        except Exception:
            pass
    else:
        try:
            rates["Binance"] = round(_binance(base), 4)
        except Exception:
            pass
    for name, fn in (("Bybit", _bybit), ("OKX", _okx)):
        try:
            rates[name] = round(fn(base), 4)
        except Exception:
            pass

    if len(rates) < 2:
        return {"available": False, "reason": "thiếu nguồn (chỉ có %d sàn)" % len(rates),
                "rates": rates}

    vals = sorted(rates.values())
    m = len(vals)
    median = vals[m // 2] if m % 2 else (vals[m // 2 - 1] + vals[m // 2]) / 2
    mean = sum(vals) / m
    others = [v for k, v in rates.items() if k != "Binance"]
    bnc = rates.get("Binance")
    others_med = None
    dev = None
    if others:
        om = sorted(others)
        others_med = om[len(om) // 2] if len(om) % 2 else (om[len(om)//2-1] + om[len(om)//2]) / 2
        if bnc is not None:
            dev = round(bnc - others_med, 4)   # Binance lệch so trung vị sàn khác
    spread = round(vals[-1] - vals[0], 4)      # biên độ phân tán

    # ── Tín hiệu ──
    HI = 0.03      # ngưỡng funding "cao" (%/kỳ) ~ tương đương ngưỡng edge
    DEV = 0.01     # ngưỡng lệch đáng kể
    sig = "neutral"; label = "Phân tán nhẹ"; note = ""
    all_low = all(v < HI * 0.5 for v in vals)
    all_hi = all(v >= HI for v in vals)
    if all_hi:
        sig = "short_strong"; label = "Crowding long TOÀN thị trường"
        note = "Mọi sàn funding cao → long đông đồng loạt → setup short rõ nhất."
    elif dev is not None and dev >= DEV and bnc is not None and bnc >= HI:
        sig = "short_binance"; label = "Long đông RIÊNG Binance"
        note = f"Binance {bnc:+.3f}% cao hơn trung vị sàn khác {others_med:+.3f}% ({dev:+.3f}pp) → áp lực short trên Binance mạnh hơn."
    elif all_low:
        sig = "no_setup"; label = "Không có crowding long"
        note = "Funding thấp/âm mọi sàn → KHÔNG có nhiên liệu cho short funding."
    elif dev is not None and dev <= -DEV:
        sig = "elsewhere"; label = "Crowding ở sàn khác"
        note = f"Binance {bnc:+.3f}% THẤP hơn sàn khác {others_med:+.3f}% → long đông nơi khác, không phải Binance."
    else:
        note = f"Funding các sàn gần nhau (biên {spread:.3f}pp), chưa lệch rõ."

    return {
        "available": True, "base": base, "rates": rates,
        "mean": round(mean, 4), "median": round(median, 4),
        "others_median": others_med, "binance_dev": dev, "spread": spread,
        "signal": sig, "label": label, "note": note,
    }


if __name__ == "__main__":
    import json
    for b in ("ARB", "BNB", "BTC"):
        print(f"── {b} ──")
        print(json.dumps(cross_funding(b), ensure_ascii=False, indent=2))
