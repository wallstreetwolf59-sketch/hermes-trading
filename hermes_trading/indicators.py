"""Indicator primitives.

Every function takes the raw ccxt OHLCV shape - [ts, open, high, low, close,
volume] - or a plain list of closes, and returns either a full series or the
latest value. Series are returned where the strategy needs to compare the
current bar against the previous one (crossovers).
"""
from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    """Exponential moving average, seeded with an SMA of the first `period`."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Wilder RSI. Returns one value per bar from index `period` onward."""
    if len(closes) < period + 1:
        return []

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out = [_rsi(avg_gain, avg_loss)]
    # Wilder smoothing for the remainder.
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(_rsi(avg_gain, avg_loss))
    return out


def rsi(closes: list[float], period: int = 14) -> float:
    series = rsi_series(closes, period)
    return series[-1] if series else 50.0


def true_range(candles: list[list]) -> list[float]:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|)."""
    out = []
    for i in range(1, len(candles)):
        high, low = candles[i][2], candles[i][3]
        prev_close = candles[i - 1][4]
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr(candles: list[list], period: int = 14) -> float | None:
    """Wilder ATR in price units."""
    tr = true_range(candles)
    if len(tr) < period:
        return None
    value = sum(tr[:period]) / period
    for t in tr[period:]:
        value = (value * (period - 1) + t) / period
    return value


def volume_ratio(candles: list[list], period: int = 20) -> float | None:
    """Latest bar volume divided by the average of the prior `period` bars."""
    vols = [c[5] for c in candles]
    if len(vols) < period + 1:
        return None
    baseline = sum(vols[-(period + 1):-1]) / period
    if baseline <= 0:
        return None
    return vols[-1] / baseline
