"""One evaluation tick.

Each GitHub Actions run is a cold start: boot, evaluate once, commit state,
die. So all durable state lives in state/*.json(l) and is committed back to
the repo by the workflow - there is no in-memory carry-over between ticks.

Because ticks are five minutes apart, exits are checked against the highs
and lows of every candle since entry, not just the latest close. Otherwise
a stop that was breached and recovered inside the gap would be missed.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import yaml

from .adapters import macro, news, onchain, price
from .adapters.base import SchemaError, check_schema
from .indicators import atr, ema, rsi_series, volume_ratio

STATE = Path(__file__).resolve().parent.parent / "state"

MAX_RETRIES = 3
CIRCUIT_BREAK_AFTER = 5


# --------------------------------------------------------------------------- io

def _load_yaml(name: str) -> dict:
    return yaml.safe_load((STATE / name).read_text(encoding="utf-8")) or {}


def load_goal() -> dict:
    return _load_yaml("goal.yaml")


def load_strategy() -> dict:
    return _load_yaml("strategy.yaml")


def read_trades() -> list[dict]:
    path = STATE / "trades.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append_trade(trade: dict) -> None:
    with (STATE / "trades.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(trade) + "\n")


def read_position() -> dict:
    path = STATE / "position.json"
    if not path.exists():
        return {"positions": []}
    return json.loads(path.read_text(encoding="utf-8"))


def write_position(pos: dict) -> None:
    (STATE / "position.json").write_text(json.dumps(pos, indent=2), encoding="utf-8")


def read_heartbeat() -> dict:
    path = STATE / "heartbeat.json"
    if not path.exists():
        return {"consecutive_failures": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"consecutive_failures": 0}


def write_heartbeat(hb: dict) -> None:
    (STATE / "heartbeat.json").write_text(json.dumps(hb, indent=2), encoding="utf-8")


# --------------------------------------------------------------------- fetching

async def _with_retries(coro_factory, name: str):
    """Call an adapter up to MAX_RETRIES times with exponential backoff."""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last = exc
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
    raise ConnectionError(f"{name} failed after {MAX_RETRIES} attempts: {last}") from last


async def gather_context(asset: str, timeframe: str, limit: int) -> dict:
    """Price is load-bearing and retried. The rest is colour for Hermes."""
    px = await _with_retries(lambda: price.fetch(asset, timeframe, limit), "price")
    check_schema(px, price.SCHEMA_VERSION, "price")

    context = {}
    for name, mod in (("onchain", onchain), ("news", news), ("macro", macro)):
        try:
            payload = await mod.fetch(asset)
            check_schema(payload, mod.SCHEMA_VERSION, name)
            context[name] = payload
        except SchemaError:
            raise
        except Exception as exc:  # noqa: BLE001 - context is optional
            context[name] = {"available": False, "error": str(exc)}
    return {"price": px, **context}


# ------------------------------------------------------------------- signalling

def evaluate_signal(candles: list[list], strat: dict) -> dict:
    """Work out whether an entry fires this bar, and why or why not."""
    closes = [c[4] for c in candles]
    entry_cfg = strat["entry"]
    trend_cfg = strat.get("trend_filter", {})
    conf_cfg = strat.get("confirmation", {})
    risk_cfg = strat["risk"]

    period = int(entry_cfg.get("rsi_period", 14))
    threshold = float(entry_cfg["threshold"])
    direction = entry_cfg.get("direction", "long")

    rsis = rsi_series(closes, period)
    if len(rsis) < 2:
        return {"fire": False, "reason": "insufficient rsi history", "rsi": None}

    rsi_now, rsi_prev = rsis[-1], rsis[-2]
    last = closes[-1]

    diag = {
        "rsi": round(rsi_now, 2),
        "rsi_prev": round(rsi_prev, 2),
        "price": last,
    }

    # --- trend filter --------------------------------------------------
    if trend_cfg.get("enabled", True):
        fast = ema(closes, int(trend_cfg.get("fast_ema", 50)))
        slow = ema(closes, int(trend_cfg.get("slow_ema", 200)))
        if fast is None or slow is None:
            return {**diag, "fire": False, "reason": "not enough history for trend filter"}
        diag["ema_fast"] = round(fast, 2)
        diag["ema_slow"] = round(slow, 2)
        uptrend = fast > slow
        if direction == "long" and not uptrend:
            return {**diag, "fire": False, "reason": "trend filter: downtrend, no longs"}
        if direction == "short" and uptrend:
            return {**diag, "fire": False, "reason": "trend filter: uptrend, no shorts"}

    # --- volatility floor ----------------------------------------------
    atr_value = atr(candles, int(risk_cfg.get("atr_period", 14)))
    if atr_value is None:
        return {**diag, "fire": False, "reason": "not enough history for atr"}
    atr_pct = (atr_value / last) * 100.0
    diag["atr"] = round(atr_value, 2)
    diag["atr_pct"] = round(atr_pct, 4)
    if atr_pct < float(conf_cfg.get("min_atr_pct", 0.0)):
        return {**diag, "fire": False, "reason": f"tape too flat (atr {atr_pct:.3f}%)"}

    # --- entry trigger ---------------------------------------------------
    style = entry_cfg.get("indicator", "rsi_reversal")

    if style == "ema_pullback":
        # Dip to the pullback EMA on the prior bar, close back above it now.
        pb_len = int(entry_cfg.get("pullback_ema", 20))
        pb = ema(closes, pb_len)
        if pb is None:
            return {**diag, "fire": False, "reason": "not enough history for pullback ema"}
        diag["pullback_ema"] = round(pb, 2)
        prev_close = closes[-2]
        prev_low = candles[-2][3]
        if direction == "long":
            triggered = prev_low <= pb and prev_close <= pb and last > pb
            label = f"pullback to ema{pb_len} then close above"
        else:
            prev_high = candles[-2][2]
            triggered = prev_high >= pb and prev_close >= pb and last < pb
            label = f"pullback to ema{pb_len} then close below"

    elif direction == "long":
        if entry_cfg.get("require_cross_up", True):
            triggered = rsi_prev <= threshold < rsi_now
            label = f"rsi cross up through {threshold}"
        else:
            triggered = rsi_now < threshold
            label = f"rsi below {threshold}"
    else:
        if entry_cfg.get("require_cross_up", True):
            triggered = rsi_prev >= threshold > rsi_now
            label = f"rsi cross down through {threshold}"
        else:
            triggered = rsi_now > threshold
            label = f"rsi above {threshold}"

    if not triggered:
        return {**diag, "fire": False, "reason": f"no trigger ({label})"}

    # --- volume confirmation ---------------------------------------------
    required = float(conf_cfg.get("volume_mult", 0.0))
    if required > 0:
        ratio = volume_ratio(candles)
        diag["volume_ratio"] = round(ratio, 3) if ratio else None
        if ratio is None:
            return {**diag, "fire": False, "reason": "no volume data for confirmation"}
        if ratio < required:
            return {**diag, "fire": False,
                    "reason": f"volume {ratio:.2f}x below required {required}x"}

    return {**diag, "fire": True, "reason": label, "atr_value": atr_value}


def build_position(asset: str, signal: dict, strat: dict) -> dict:
    """Size the trade and pre-compute its stop and target from ATR."""
    risk_cfg = strat["risk"]
    direction = strat["entry"].get("direction", "long")
    entry_price = signal["price"]
    stop_distance = signal["atr_value"] * float(risk_cfg.get("stop_atr_mult", 1.5))
    rr = float(risk_cfg.get("reward_risk_ratio", 2.0))

    if direction == "long":
        stop_price = entry_price - stop_distance
        take_price = entry_price + stop_distance * rr
    else:
        stop_price = entry_price + stop_distance
        take_price = entry_price - stop_distance * rr

    return {
        "id": uuid.uuid4().hex[:12],
        "asset": asset,
        "side": direction,
        "opened_at": time.time(),
        "opened_ts_ms": int(time.time() * 1000),
        "entry": entry_price,
        "stop_price": stop_price,
        "take_price": take_price,
        "risk_per_unit": stop_distance,
        "size_r": float(risk_cfg.get("position_size_r", 0.5)),
        "rsi_at_entry": signal["rsi"],
        "atr_at_entry": round(signal["atr_value"], 2),
        "breakeven_moved": False,
        "strategy_version": str(strat.get("version", "01")),
    }


def check_exit(pos: dict, candles: list[list], strat: dict, now: float) -> tuple[str | None, float]:
    """Return (reason, exit_price). Scans every candle since entry.

    Where a single candle touches both the stop and the target we assume the
    stop filled first. That is the pessimistic read and it keeps backtest-
    flavoured optimism out of the trade log.
    """
    risk_cfg = strat["risk"]
    side = pos["side"]
    since = pos.get("opened_ts_ms", 0)
    recent = [c for c in candles if c[0] >= since] or candles[-1:]

    for candle in recent:
        high, low = candle[2], candle[3]
        if side == "long":
            if low <= pos["stop_price"]:
                return "stop_loss", pos["stop_price"]
            if high >= pos["take_price"]:
                return "take_profit", pos["take_price"]
        else:
            if high >= pos["stop_price"]:
                return "stop_loss", pos["stop_price"]
            if low <= pos["take_price"]:
                return "take_profit", pos["take_price"]

    held_minutes = (now - pos["opened_at"]) / 60.0
    max_hold = float(risk_cfg.get("max_hold_minutes", 240))
    if held_minutes >= max_hold:
        return "time_stop", candles[-1][4]

    return None, candles[-1][4]


def maybe_move_to_breakeven(pos: dict, last: float, strat: dict) -> bool:
    """Pull the stop to entry once the trade is far enough in profit."""
    if pos.get("breakeven_moved"):
        return False
    trigger_r = float(strat["risk"].get("breakeven_at_r", 0))
    if trigger_r <= 0:
        return False

    risk = pos["risk_per_unit"]
    gain = (last - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - last)
    if risk > 0 and gain >= trigger_r * risk:
        pos["stop_price"] = pos["entry"]
        pos["breakeven_moved"] = True
        return True
    return False


# ------------------------------------------------------------------------ tick

async def tick(verbose: bool = True) -> dict:
    goal, strat = load_goal(), load_strategy()
    asset = goal.get("asset", "BTC/USDT")
    timeframe = strat.get("timeframe", "5m")
    lookback = int(strat.get("lookback_candles", 300))
    hb = read_heartbeat()

    if hb.get("consecutive_failures", 0) >= CIRCUIT_BREAK_AFTER:
        raise RuntimeError(
            f"Circuit breaker open: {hb['consecutive_failures']} consecutive failed ticks. "
            "Fix the cause, then reset state/heartbeat.json to continue."
        )

    try:
        ctx = await gather_context(asset, timeframe, lookback)
    except Exception as exc:
        hb["consecutive_failures"] = hb.get("consecutive_failures", 0) + 1
        hb["last_error"] = f"{type(exc).__name__}: {exc}"
        hb["last_tick_at"] = time.time()
        write_heartbeat(hb)
        raise

    candles = ctx["price"]["candles"]
    last = ctx["price"]["last"]
    now = time.time()
    events = []

    # --- manage anything already open -------------------------------------
    still_open = []
    for pos in read_position().get("positions", []):
        reason, exit_price = check_exit(pos, candles, strat, now)
        if reason:
            move_pct = ((exit_price - pos["entry"]) / pos["entry"]) * 100.0
            if pos["side"] == "short":
                move_pct = -move_pct
            r_multiple = (move_pct / 100.0 * pos["entry"]) / pos["risk_per_unit"] \
                if pos["risk_per_unit"] else 0.0
            trade = {
                **pos,
                "closed_at": now,
                "exit": exit_price,
                "return_pct": round(move_pct, 4),
                "r_multiple": round(r_multiple, 3),
                "held_minutes": round((now - pos["opened_at"]) / 60.0, 1),
                "reason": reason,
            }
            append_trade(trade)
            events.append(f"CLOSE {reason} @ {exit_price:.2f} "
                          f"({move_pct:+.2f}%, {r_multiple:+.2f}R)")
        else:
            if maybe_move_to_breakeven(pos, last, strat):
                events.append(f"STOP -> breakeven @ {pos['entry']:.2f}")
            still_open.append(pos)

    # --- open a new one if flat and the signal fires ------------------------
    signal = evaluate_signal(candles, strat)
    if not still_open and signal.get("fire"):
        pos = build_position(asset, signal, strat)
        still_open.append(pos)
        events.append(f"OPEN {pos['side']} @ {pos['entry']:.2f} "
                      f"stop {pos['stop_price']:.2f} target {pos['take_price']:.2f} "
                      f"({signal['reason']})")

    write_position({"positions": still_open})

    closed = read_trades()
    hb = {
        "last_tick_at": now,
        "consecutive_failures": 0,
        "asset": asset,
        "price_source": ctx["price"]["source"],
        "last_price": last,
        "strategy_version": str(strat.get("version", "01")),
        "signal": {k: v for k, v in signal.items() if k != "atr_value"},
        "open_positions": len(still_open),
        "closed_trades": len(closed),
        "events": events,
    }
    write_heartbeat(hb)

    if verbose:
        print(f"[tick] {asset} @ {last:.2f} via {ctx['price']['source']} | "
              f"v{strat.get('version')} | open={len(still_open)} closed={len(closed)}")
        print(f"       signal: {signal['reason']} | rsi={signal.get('rsi')} "
              f"atr%={signal.get('atr_pct')}")
        for e in events:
            print(f"       {e}")
        if not events:
            print("       no action")

    return hb
