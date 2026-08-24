"""The reflection cycle.

Two modes:

  --fallback   deterministic rules. Used before Hermes is installed, and as
               the safety net if Hermes is unreachable. Proves the mechanism.
  --hermes     calls the local `hermes` binary with the recent trade history
               and lets it choose the change.

Both modes obey the same hard constraint: exactly ONE variable in
strategy.yaml changes per cycle.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .loop import STATE, load_goal, load_strategy, read_trades
from .score import report

HISTORY = STATE / "history"

# Only these may ever be touched by a reflection. Anything else Hermes
# proposes is rejected rather than applied.
TUNABLE = {
    "entry.threshold": (10.0, 60.0),
    "entry.rsi_period": (5, 50),
    "risk.stop_atr_mult": (0.5, 4.0),
    "risk.reward_risk_ratio": (0.5, 5.0),
    "risk.position_size_r": (0.1, 2.0),
    "risk.max_hold_minutes": (30, 1440),
    "confirmation.volume_mult": (0.0, 3.0),
    "trend_filter.fast_ema": (10, 100),
}

# Variables that must stay whole numbers.
INTEGER_VARS = {"entry.rsi_period", "risk.max_hold_minutes", "trend_filter.fast_ema"}


def _get(strategy: dict, dotted: str):
    node = strategy
    for part in dotted.split("."):
        node = node[part]
    return node


def _set(strategy: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = strategy
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def _clamp_to_bounds(variable: str, value: float) -> float:
    lo, hi = TUNABLE[variable]
    return max(lo, min(hi, value))


def apply_change(strategy: dict, variable: str, new_value, rationale: str,
                 source: str, snapshot: dict) -> dict:
    """Bump the version, archive the prior strategy, log the hypothesis."""
    if variable not in TUNABLE:
        raise ValueError(f"{variable!r} is not a tunable variable. Allowed: {sorted(TUNABLE)}")

    old_value = _get(strategy, variable)
    new_value = _clamp_to_bounds(variable, float(new_value))
    if variable in INTEGER_VARS:
        new_value = int(round(new_value))

    prior_version = str(strategy.get("version", "01"))
    HISTORY.mkdir(parents=True, exist_ok=True)
    archive = HISTORY / f"v{prior_version.zfill(4)}.yaml"
    archive.write_text(yaml.safe_dump(strategy, sort_keys=False), encoding="utf-8")

    _set(strategy, variable, new_value)
    strategy["version"] = str(int(prior_version) + 1).zfill(2)
    (STATE / "strategy.yaml").write_text(
        yaml.safe_dump(strategy, sort_keys=False), encoding="utf-8"
    )

    hypothesis = {
        "at": time.time(),
        "source": source,
        "from_version": prior_version,
        "to_version": strategy["version"],
        "variable": variable,
        "old_value": old_value,
        "new_value": new_value,
        "rationale": rationale,
        "evidence": snapshot,
    }
    with (STATE / "hypotheses.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(hypothesis) + "\n")

    print(f"[reflect] v{prior_version} -> v{strategy['version']}  "
          f"{variable}: {old_value} -> {new_value}")
    print(f"[reflect] {rationale}")
    return hypothesis


# ------------------------------------------------------------------- fallback

def decide_fallback(snapshot: dict, goal: dict, strategy: dict, trades: list[dict]):
    """Deterministic rules, in priority order. Returns (variable, value, why).

    The ordering matters: survival first (drawdown), then diagnosing *how*
    trades are dying (shaken out vs timing out), then chasing the target.
    """
    realised = snapshot["realised_return"]
    drawdown = snapshot["max_drawdown"]
    win_rate = snapshot["win_rate"]
    n = max(len(trades), 1)

    reasons = [t.get("reason") for t in trades]
    stop_share = reasons.count("stop_loss") / n
    time_share = reasons.count("time_stop") / n

    # 1. Drawdown is the failure condition, so it outranks everything.
    #    The right lever is size, not the stop - tightening a stop while
    #    bleeding usually increases the number of losses.
    if drawdown > float(goal["max_drawdown"]):
        current = float(_get(strategy, "risk.position_size_r"))
        return ("risk.position_size_r", round(current - 0.1, 2),
                f"Drawdown {drawdown:.2%} breached the {goal['max_drawdown']:.2%} limit. "
                "Cutting position size by 0.1R - size is the direct lever on drawdown.")

    # 2. Being stopped out on most trades with a poor hit rate means the stop
    #    sits inside the noise, not that the edge is wrong.
    if stop_share > 0.6 and win_rate < 0.4:
        current = float(_get(strategy, "risk.stop_atr_mult"))
        return ("risk.stop_atr_mult", round(current + 0.25, 2),
                f"{stop_share:.0%} of trades stopped out at a {win_rate:.0%} win rate. "
                "Widening the stop by 0.25 ATR - it is being clipped by noise.")

    # 3. Trades expiring on the clock means the target is too far away for
    #    the holding window, not that entries are bad.
    if time_share > 0.4:
        current = float(_get(strategy, "risk.reward_risk_ratio"))
        return ("risk.reward_risk_ratio", round(current - 0.25, 2),
                f"{time_share:.0%} of trades hit the time stop. "
                "Pulling the target in by 0.25R so winners can actually close.")

    target = float(goal["target_return"])
    window = float(goal.get("target_window_days", 30))
    days = max(snapshot["elapsed_days"], 1.0)
    pro_rated = target * min(days / window, 1.0)

    # 4. Behind target and not enough trades - loosen the entry gate.
    if realised < pro_rated:
        current = float(_get(strategy, "entry.threshold"))
        return ("entry.threshold", round(current + 2.0, 2),
                f"Realised {realised:.2%} is behind the pro-rated {pro_rated:.2%} target "
                f"at day {days:.1f}. Loosening the entry threshold by 2 to take more trades.")

    # 5. Ahead of target - let winners run further.
    current = float(_get(strategy, "risk.reward_risk_ratio"))
    return ("risk.reward_risk_ratio", round(current + 0.25, 2),
            f"Realised {realised:.2%} is ahead of the pro-rated {pro_rated:.2%} target. "
            "Extending the target by 0.25R while the edge is working.")


# --------------------------------------------------------------------- hermes

_PROMPT = """You are the reflection step of a self-improving paper-trading agent.

GOAL (immutable):
{goal}

CURRENT STRATEGY:
{strategy}

PERFORMANCE OF THE LAST {n} CLOSED TRADES:
{snapshot}

RECENT TRADES (most recent last):
{trades}

Choose EXACTLY ONE variable to change. Allowed variables and bounds:
{tunable}

Respond with ONLY a JSON object, no prose, no code fence:
{{"variable": "<one of the allowed>", "new_value": <number>, "rationale": "<one sentence>"}}
"""


def decide_hermes(snapshot: dict, goal: dict, strategy: dict, trades: list[dict], timeout: int):
    if not shutil.which("hermes"):
        raise RuntimeError("hermes binary not on PATH")

    prompt = _PROMPT.format(
        goal=yaml.safe_dump(goal, sort_keys=False),
        strategy=yaml.safe_dump(strategy, sort_keys=False),
        n=len(trades),
        snapshot=json.dumps(snapshot, indent=2),
        trades=json.dumps(trades[-25:], indent=2),
        tunable=json.dumps({k: list(v) for k, v in TUNABLE.items()}, indent=2),
    )
    proc = subprocess.run(
        ["hermes", "-p", prompt],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"hermes exited {proc.returncode}: {proc.stderr.strip()[:400]}")

    raw = proc.stdout.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in hermes output: {raw[:400]}")
    decision = json.loads(raw[start:end + 1])

    variable = decision["variable"]
    if variable not in TUNABLE:
        raise ValueError(f"hermes proposed untunable variable {variable!r}")
    return variable, decision["new_value"], decision.get("rationale", "(none given)")


# ----------------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hermes_trading.reflect")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fallback", action="store_true", help="Deterministic rules")
    mode.add_argument("--hermes", action="store_true", help="Ask the local hermes binary")
    p.add_argument("--force", action="store_true", help="Reflect even if the cadence is not met")
    p.add_argument("--timeout", type=int, default=300, help="Seconds to wait for hermes")
    args = p.parse_args(argv)

    goal, strategy = load_goal(), load_strategy()
    trades = [t for t in read_trades() if t.get("closed_at")]

    if not trades:
        print("[reflect] no closed trades yet - nothing to reflect on")
        return 0

    cadence = int(goal.get("reflection_every", 5))
    hyp_path = STATE / "hypotheses.jsonl"
    done = sum(1 for line in hyp_path.read_text(encoding="utf-8").splitlines() if line.strip()) \
        if hyp_path.exists() else 0
    due_at = (done + 1) * cadence

    if len(trades) < due_at and not args.force:
        print(f"[reflect] {len(trades)} closed trades, next reflection at {due_at}. Skipping.")
        return 0

    snapshot = report(trades, goal)
    print(f"[reflect] scoring {len(trades)} closed trades: {json.dumps(snapshot)}")

    if args.hermes:
        try:
            variable, value, rationale = decide_hermes(snapshot, goal, strategy, trades, args.timeout)
            source = "hermes"
        except Exception as exc:  # noqa: BLE001 - degrade to the deterministic rule
            print(f"[reflect] hermes unavailable ({type(exc).__name__}: {exc}) - using fallback",
                  file=sys.stderr)
            variable, value, rationale = decide_fallback(snapshot, goal, strategy, trades)
            source = "fallback(hermes-failed)"
    else:
        variable, value, rationale = decide_fallback(snapshot, goal, strategy, trades)
        source = "fallback"

    apply_change(strategy, variable, value, rationale, source, snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
