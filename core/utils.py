"""utils.py — Shared utilities: JSON, sanitize, smart_round"""
import math
import numpy as np
import pandas as pd
from flask.json.provider import DefaultJSONProvider


class NumpyJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        return super().dumps(self._convert(obj), **kwargs)

    def _convert(self, obj):
        if isinstance(obj, dict):   return {k: self._convert(v) for k, v in obj.items()}
        if isinstance(obj, list):   return [self._convert(i) for i in obj]
        if isinstance(obj, (bool, np.bool_)): return bool(obj)
        if isinstance(obj, np.integer):       return int(obj)
        if isinstance(obj, np.floating):
            return 0.0 if (np.isnan(obj) or np.isinf(obj)) else float(obj)
        if isinstance(obj, float):
            return 0.0 if (obj != obj or obj in (float('inf'), float('-inf'))) else obj
        if isinstance(obj, np.ndarray): return [self._convert(x) for x in obj.tolist()]
        if isinstance(obj, pd.Timestamp): return obj.isoformat()
        return obj


def sanitize(obj):
    """Đệ quy convert numpy/pandas types → Python native trước jsonify."""
    if isinstance(obj, dict):   return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):   return [sanitize(i) for i in obj]
    if isinstance(obj, (bool, np.bool_)): return bool(obj)
    if isinstance(obj, np.integer):       return int(obj)
    if isinstance(obj, np.floating):
        return 0.0 if (np.isnan(obj) or np.isinf(obj)) else float(obj)
    if isinstance(obj, float):
        return 0.0 if (obj != obj or obj in (float('inf'), float('-inf'))) else obj
    if isinstance(obj, np.ndarray): return [sanitize(x) for x in obj.tolist()]
    if isinstance(obj, pd.Timestamp): return obj.isoformat()
    return obj


def smart_round(val):
    """Round thông minh theo magnitude — tránh 0.1679 bị round thành 0.17"""
    if not val: return 0
    try:
        mag = math.floor(math.log10(abs(val)))
        if mag >= 2:    return round(val, 2)
        if mag >= 0:    return round(val, 3)
        if mag >= -2:   return round(val, 5)
        if mag >= -4:   return round(val, 6)
        return round(val, 8)
    except Exception:
        return round(val, 6)


def recommended_size(confidence, rr, direction=None, funding=None, atr_state=None):
    """Đề xuất % vốn account cho 1 lệnh dựa trên confidence + RR + market context.

    Trả về dict:
      - size_pct: float — % vốn account (0 = skip)
      - tier: "AGGRESSIVE" | "STANDARD" | "CONSERVATIVE" | "SKIP"
      - reasons: list[str] — giải thích adjustments

    Base tiers:
      HIGH + RR>=2.5  → 3.0% (AGGRESSIVE)
      HIGH + RR>=1.5  → 2.0% (STANDARD)
      MEDIUM + RR>=2  → 1.5% (STANDARD)
      MEDIUM + RR>=1.5→ 1.0% (CONSERVATIVE)
      LOW hoặc RR<1.5 → 0%   (SKIP)

    Adjust:
      |funding| >= 0.05%  → -0.5% (crowded)
      atr_state == "HIGH" → -0.5% (volatile, slippage rủi ro)
    """
    if direction == "WAIT" or confidence == "LOW" or (rr is not None and rr < 1.5):
        return {"size_pct": 0, "tier": "SKIP",
                "reasons": ["Confidence/RR không đủ để vào lệnh"]}

    rr = rr or 0
    if confidence == "HIGH" and rr >= 2.5:
        size, tier = 3.0, "AGGRESSIVE"
    elif confidence == "HIGH" and rr >= 1.5:
        size, tier = 2.0, "STANDARD"
    elif confidence == "MEDIUM" and rr >= 2.0:
        size, tier = 1.5, "STANDARD"
    elif confidence == "MEDIUM" and rr >= 1.5:
        size, tier = 1.0, "CONSERVATIVE"
    else:
        return {"size_pct": 0, "tier": "SKIP",
                "reasons": ["Setup không đạt tier tối thiểu"]}

    reasons = [f"Base: {tier} ({confidence}, RR {rr})"]

    # Adjust funding crowded — funding stored as %-value directly (0.05 means 0.05%)
    if funding is not None:
        if abs(funding) >= 0.05:
            penalty = 0.5
            size = max(0.5, size - penalty)
            reasons.append(f"Funding {funding:+.4f}% extreme → -{penalty}% (crowded)")

    # Adjust ATR volatile
    if atr_state == "HIGH":
        penalty = 0.5
        size = max(0.5, size - penalty)
        reasons.append(f"ATR HIGH → -{penalty}% (volatile)")

    return {"size_pct": round(size, 1), "tier": tier, "reasons": reasons}


def short_context_check(direction, df_recent, funding=None, atr_value=None, lookback=5):
    """Đánh giá độ tin cậy của SHORT signal dựa context volume/funding/wick.

    KHÔNG thay đổi direction/confidence của engine — chỉ trả thêm meta info để UI
    hiển thị warning, giúp trader phân biệt SHORT thật vs false signal trong
    uptrend mạnh.

    Returns dict (None nếu direction != "SHORT"):
      - trust:    "HIGH" | "MEDIUM" | "LOW"
      - score:    0-100 (raw)
      - warnings: list[str] — context red flags
      - signals:  list[str] — context green flags (ủng hộ SHORT)

    Args:
      df_recent:  DataFrame có cols open, high, low, close, volume (theo nến)
      funding:    %-form (0.05 = 0.05%)
      atr_value:  ATR cùng timeframe với df_recent
    """
    if direction != "SHORT" or df_recent is None or len(df_recent) < lookback:
        return None

    score    = 60  # base score MEDIUM trust
    warnings = []
    signals  = []

    last_n = df_recent.tail(lookback)

    # 1. Volume context: nến gần đây xanh > đỏ → uptrend chưa hết → SHORT risky
    try:
        green = int((last_n["close"] > last_n["open"]).sum())
        if green >= lookback - 1:
            warnings.append(f"Volume: {green}/{lookback} nến gần xanh — uptrend chưa hết")
            score -= 25
        elif green <= 1:
            signals.append(f"Volume: {lookback - green}/{lookback} nến gần đỏ — momentum đang yếu")
            score += 15
    except Exception:
        pass

    # 2. Funding chưa crowded long → top chưa thật sự → SHORT yếu
    if funding is not None:
        if -0.005 < funding < 0.02:
            warnings.append(f"Funding {funding:+.4f}% chưa crowded long — top chưa rõ")
            score -= 20
        elif funding >= 0.05:
            signals.append(f"Funding {funding:+.4f}% extreme — long crowded, SHORT có lợi")
            score += 20
        elif funding >= 0.02:
            signals.append(f"Funding {funding:+.4f}% dương — long bắt đầu crowded")
            score += 10

    # 3. Rejection wick check trên cây gần nhất
    try:
        last = df_recent.iloc[-1]
        body_top = max(float(last["open"]), float(last["close"]))
        upper_wick = float(last["high"]) - body_top
        if atr_value is not None and atr_value > 0:
            wick_ratio = upper_wick / atr_value
            if wick_ratio >= 1.5:
                signals.append(f"Rejection wick mạnh {wick_ratio:.1f}× ATR — top confirmation")
                score += 20
            elif wick_ratio < 0.3:
                warnings.append("Không có rejection wick ở cây gần nhất")
                score -= 10
    except Exception:
        pass

    # 4. Volume divergence: 3 nến gần price up nhưng volume giảm dần → distribution
    try:
        last3 = df_recent.tail(3)
        all_green = bool((last3["close"] > last3["open"]).all())
        vol_descending = bool(last3["volume"].iloc[-1] < last3["volume"].iloc[-2] < last3["volume"].iloc[-3])
        if all_green and vol_descending:
            signals.append("Volume divergence: giá lên / vol giảm — distribution rõ")
            score += 15
    except Exception:
        pass

    score = max(0, min(100, score))
    if score >= 70:
        trust = "HIGH"
    elif score >= 40:
        trust = "MEDIUM"
    else:
        trust = "LOW"

    return {"trust": trust, "score": score, "warnings": warnings, "signals": signals}
