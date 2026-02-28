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
