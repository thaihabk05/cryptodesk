"""
liquidations.py — Thanh lý gần đây (OKX public, KHÔNG cần key) → hỗ trợ đặt SL.
Vì sao: ARB hay wick quét SL rồi quay đầu. Biết CỤM thanh lý (price magnet) + bên nào
đang bị siết giúp đặt SL NGOÀI vùng quét, và đọc được áp lực squeeze tức thời.

Nguồn: OKX /api/v5/public/liquidation-orders (bkPx = giá phá sản, sz = số hợp đồng).
Notional USD = sz × ctVal × giá. Là proxy cross-sàn cho vùng thanh lý (giá là toàn cục).
posSide 'long' bị thanh lý = long bị siết (giá ĐẠP xuống); 'short' = short bị siết (giá ĐẨY lên).
"""
import time
import requests

_H = {"User-Agent": "cryptodesk"}
_T = 8
# ctVal (số coin / 1 hợp đồng) OKX SWAP — hardcode cho universe, fallback fetch cho coin khác.
_CTVAL = {"ARB": 10.0, "BNB": 0.01, "BTC": 0.01, "ETH": 0.1}
_ctval_cache = dict(_CTVAL)


def _ctval(base):
    if base in _ctval_cache:
        return _ctval_cache[base]
    try:
        r = requests.get("https://www.okx.com/api/v5/public/instruments",
                         params={"instType": "SWAP", "instId": f"{base}-USDT-SWAP"},
                         headers=_H, timeout=_T).json()
        v = float(r["data"][0]["ctVal"])
        _ctval_cache[base] = v
        return v
    except Exception:
        return 1.0


def recent_liquidations(base, limit=100, cluster_bps=30):
    """
    base: 'ARB'/'BNB'/'BTC'. cluster_bps: độ rộng bin gom cụm (30 = 0.30%).
    Trả: tổng long/short USD, cụm giá lớn nhất, bias squeeze, khoảng thời gian.
    """
    try:
        r = requests.get("https://www.okx.com/api/v5/public/liquidation-orders",
                         params={"instType": "SWAP", "instFamily": f"{base}-USDT",
                                 "state": "filled", "limit": limit},
                         headers=_H, timeout=_T).json()
        if r.get("code") != "0" or not r.get("data"):
            return {"available": False, "reason": "OKX không trả data"}
    except Exception as e:
        return {"available": False, "reason": str(e)}

    ctv = _ctval(base)
    ev = []
    for block in r["data"]:
        for d in block.get("details", []):
            try:
                px = float(d["bkPx"]); sz = float(d["sz"]); ts = int(d["ts"])
                side = d.get("posSide", "")
                ev.append({"px": px, "usd": sz * ctv * px, "side": side, "ts": ts})
            except Exception:
                continue
    if not ev:
        return {"available": True, "base": base, "n": 0,
                "note": "Không có thanh lý gần đây (thị trường calm)."}

    long_usd = sum(e["usd"] for e in ev if e["side"] == "long")
    short_usd = sum(e["usd"] for e in ev if e["side"] == "short")
    tot = long_usd + short_usd
    tmin = min(e["ts"] for e in ev); tmax = max(e["ts"] for e in ev)
    span_min = round((tmax - tmin) / 60000.0, 1)

    # ── Gom cụm theo giá (bin độ rộng cố định = cluster_bps của giá tham chiếu) ──
    ref = sum(e["px"] for e in ev) / len(ev)
    bin_w = ref * cluster_bps / 10000.0 or 1e-9
    buckets = {}
    for e in ev:
        key = int(e["px"] / bin_w)
        b = buckets.setdefault(key, {"usd": 0.0, "pxs": []})
        b["usd"] += e["usd"]; b["pxs"].append(e["px"])
    clusters = []
    for b in buckets.values():
        clusters.append({"px_lo": round(min(b["pxs"]), 8), "px_hi": round(max(b["pxs"]), 8),
                         "px": round(sum(b["pxs"]) / len(b["pxs"]), 8),
                         "usd": round(b["usd"]), "n": len(b["pxs"])})
    clusters.sort(key=lambda c: -c["usd"])
    top = clusters[:3]

    # ── Bias squeeze ──
    if tot <= 0:
        bias = "none"; blabel = "—"; bnote = ""
    elif short_usd >= long_usd * 1.6:
        bias = "short_squeeze"; blabel = "SHORT đang bị siết"
        bnote = "Thanh lý chủ yếu bên SHORT → giá bị ĐẨY LÊN. Cẩn trọng short đu đỉnh; SL short nên trên cụm."
    elif long_usd >= short_usd * 1.6:
        bias = "long_squeeze"; blabel = "LONG đang bị siết"
        bnote = "Thanh lý chủ yếu bên LONG → giá bị ĐẠP XUỐNG. Cẩn trọng long bắt dao; SL long nên dưới cụm."
    else:
        bias = "mixed"; blabel = "Hai chiều cân bằng"
        bnote = "Thanh lý hai bên cân bằng — chưa có áp lực squeeze rõ."

    return {
        "available": True, "base": base, "n": len(ev),
        "long_usd": round(long_usd), "short_usd": round(short_usd),
        "span_min": span_min, "clusters": top,
        "bias": bias, "bias_label": blabel, "bias_note": bnote,
        "src": "OKX swap",
    }


if __name__ == "__main__":
    import json
    for b in ("ARB", "BNB", "BTC"):
        print(f"── {b} ──")
        print(json.dumps(recent_liquidations(b), ensure_ascii=False, indent=2))
