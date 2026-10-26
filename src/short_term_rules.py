"""Deterministic short-term observations; never executes trades."""
import math

import pandas as pd


def short_pullback(history):
    """MA5 > MA10 > MA20, support held, <70% of prior-five-session volume."""
    if not {'close', 'volume'}.issubset(history) or len(history) < 20:
        return False
    close = pd.to_numeric(history.close, errors='coerce')
    volume = pd.to_numeric(history.volume, errors='coerce')
    if close.tail(20).isna().any() or volume.tail(6).isna().any():
        return False
    ma5, ma10, ma20 = (float(close.tail(n).mean()) for n in (5, 10, 20))
    price, today_volume = float(close.iloc[-1]), float(volume.iloc[-1])
    average_volume = float(volume.iloc[-6:-1].mean())
    values = (ma5, ma10, ma20, price, today_volume, average_volume)
    if not all(math.isfinite(x) for x in values) or min(ma20, price, average_volume) <= 0 or today_volume <= 0:
        return False
    near_support = (ma5 <= price <= ma5*1.01) or (ma10 <= price <= ma10*1.02)
    return bool(ma5 > ma10 > ma20 and near_support and today_volume < average_volume*0.7)
