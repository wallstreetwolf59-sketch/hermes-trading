"""Scores a set of closed trades against goal.yaml. Output is in [-1, +1]."""
from __future__ import annotations

import math
from typing import Iterable

# How much each leg of the goal matters to the final score.
_W_RETURN, _W_DRAWDOWN, _W_SHARPE = 0.5, 0.3, 0.2

# Below the goal's `failure_below` the score collapses rather than degrading
# linearly - a blown account is not "a bit worse" than a flat one.
_FAILURE_FLOOR = -1.0


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def equity_curve(trades: list[dict], start: float = 1.0) -> list[float]:
    """Compounded equity after each closed trade."""
    curve, equity = [start], start
    for t in trades:
        equity *= 1.0 + (t.get("return_pct", 0.0) / 100.0) * t.get("size_r", 1.0)
        curve.append(equity)
    return curve


def realised_return(trades: list[dict]) -> float:
    """Total compounded return as a decimal (0.07 == +7%)."""
    curve = equity_curve(trades)
    return curve[-1] - 1.0


def max_drawdown(trades: list[dict]) -> float:
    """Deepest peak-to-trough fall on the equity curve, as a positive decimal."""
    peak, worst = -math.inf, 0.0
    for equity in equity_curve(trades):
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def elapsed_days(trades: list[dict]) -> float:
    stamps = [t["closed_at"] for t in trades if t.get("closed_at")]
    if len(stamps) < 2:
        return 0.0
    return (max(stamps) - min(stamps)) / 86400.0


def sharpe(trades: list[dict]) -> float:
    """Annualised Sharpe. Zero when there is not enough data.

    Annualisation uses the *observed* trade frequency, not a flat 365. An
    agent that closes two trades a week must not be scored as though each
    trade were a trading day - that inflates Sharpe by roughly 13x.
    """
    rets = [(t.get("return_pct", 0.0) / 100.0) * t.get("size_r", 1.0) for t in trades]
    if len(rets) < 3:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0

    days = elapsed_days(trades)
    if days <= 0:
        return 0.0
    trades_per_year = (len(rets) / days) * 365.0
    # Cap the annualisation factor: a burst of trades in one hour should not
    # be extrapolated into a five-figure Sharpe.
    trades_per_year = min(trades_per_year, 365.0 * 24.0)
    return (mean / sd) * math.sqrt(trades_per_year)


def score(trades: Iterable[dict], goal: dict) -> float:
    """Composite score in [-1, +1]. Positive means beating the goal."""
    trades = [t for t in trades if t.get("closed_at")]
    if not trades:
        return 0.0

    target = float(goal.get("target_return", 0.05))
    window = float(goal.get("target_window_days", 30))
    max_dd = float(goal.get("max_drawdown", 0.08))
    min_sh = float(goal.get("min_sharpe", 1.2))
    floor = float(goal.get("failure_below", -0.04))

    ret = realised_return(trades)
    if ret < floor:
        return _FAILURE_FLOOR

    # Pro-rate the target to how long we have actually been running, so a
    # 3-day-old agent is not marked down for missing a 25-day number. The
    # 1-day floor stops the very first trades from producing absurd ratios.
    days = max(elapsed_days(trades), 1.0)
    pro_rated_target = target * min(days / window, 1.0)
    ret_leg = _clamp(ret / pro_rated_target) if pro_rated_target > 0 else 0.0

    dd_leg = _clamp(1.0 - (max_drawdown(trades) / max_dd)) if max_dd > 0 else 0.0
    sharpe_leg = _clamp(sharpe(trades) / min_sh) if min_sh > 0 else 0.0

    return _clamp(_W_RETURN * ret_leg + _W_DRAWDOWN * dd_leg + _W_SHARPE * sharpe_leg)


def report(trades: list[dict], goal: dict) -> dict:
    """Everything the reflection cycle needs to reason about, in one dict."""
    closed = [t for t in trades if t.get("closed_at")]
    wins = [t for t in closed if t.get("return_pct", 0) > 0]
    return {
        "closed_trades": len(closed),
        "score": round(score(closed, goal), 4),
        "realised_return": round(realised_return(closed), 5),
        "target_return": goal.get("target_return"),
        "max_drawdown": round(max_drawdown(closed), 5),
        "max_drawdown_limit": goal.get("max_drawdown"),
        "sharpe": round(sharpe(closed), 3),
        "min_sharpe": goal.get("min_sharpe"),
        "win_rate": round(len(wins) / len(closed), 3) if closed else 0.0,
        "elapsed_days": round(elapsed_days(closed), 2),
    }
