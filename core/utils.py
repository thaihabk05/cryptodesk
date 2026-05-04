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
