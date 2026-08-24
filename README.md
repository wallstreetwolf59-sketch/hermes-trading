# hermes-trading

A self-improving **paper-trading** agent. A GitHub Actions cron runs the worker
every five minutes; the repo itself is the database. Every tick, every trade and
every change the agent makes to its own strategy is a commit.

> **Paper mode only.** No exchange keys, no orders, no money. The live execution
> path is not imported unless both flags in `.env` are flipped, and that is
> deliberately not a thing you can do by accident.

## The goal

Defined once in [`state/goal.yaml`](state/goal.yaml) and never edited by the agent:

| | |
|---|---|
| Asset | BTC/USDT |
| Success | +7% over 25 days, Sharpe ≥ 1.2 |
| Failure | 10% drawdown |
| Reflection cadence | every 5 closed trades |
| Guardrail | exactly ONE variable changes per cycle |

## The strategy

[`state/strategy.yaml`](state/strategy.yaml) is the only file the agent rewrites.
v01 is an intraday, trend-filtered RSI reversal:

- **Trend filter** — EMA(50) over EMA(200). Longs only in an uptrend, so the
  agent stops buying every dip in a downtrend.
- **Entry** — RSI(14) crossing back *up* through 32, not merely sitting below it.
  Waits for the turn rather than catching the fall.
- **Confirmation** — bar volume ≥ 1.15× its 20-bar average, and ATR ≥ 0.08% of
  price. No trades on thin or dead tape.
- **Risk** — stop at 1.5× ATR, target at 2R, so both scale with volatility
  instead of being fixed percentages.
- **Intraday** — force flat after 4 hours; stop pulls to breakeven at +1R.

Exits are checked against the high and low of every candle since entry, not just
the latest close — a stop breached and recovered inside the 5-minute gap still
counts. Where one candle touches both stop and target, the stop is assumed to
have filled first.

## How it improves itself

Every 5 closed trades, [`reflect.py`](hermes_trading/reflect.py) scores the
outcomes against the goal and changes exactly one variable. Rules are applied in
priority order — survival first, then diagnosing *how* trades are dying, then
chasing the target:

1. Drawdown breached → cut `position_size_r` (size is the direct lever on drawdown)
2. >60% stopped out at <40% win rate → widen `stop_atr_mult` (stop sits in the noise)
3. >40% timing out → pull in `reward_risk_ratio` (target unreachable in the window)
4. Behind the pro-rated target → loosen `entry.threshold`
5. Ahead of it → extend `reward_risk_ratio`

Only eight variables are tunable, each hard-bounded. Anything proposed outside
that set is rejected rather than applied.

## Layout

```
hermes_trading/
  run.py          entrypoint (one tick, or --loop for a VPS)
  loop.py         tick engine: signal, position management, exits
  indicators.py   RSI, EMA, ATR, volume ratio
  score.py        scores trades in [-1, +1] against the goal
  reflect.py      the reflection cycle (--fallback | --hermes)
  adapters/       price · onchain · news · macro, all free endpoints
state/
  goal.yaml       immutable target
  strategy.yaml   the file that evolves
  trades.jsonl    every closed trade
  hypotheses.jsonl  every change, with its reasoning and evidence
  history/        every prior strategy version
```

## Running it locally

```bash
uv sync
uv run python -m hermes_trading.run          # one tick
uv run python -m hermes_trading.reflect --fallback --force
```

## Caveats worth knowing

- Kraken does not serve BTC/USDT to all regions, so the price adapter falls
  through to Coinbase, which quotes **BTC/USD**. Close enough for paper trading,
  but it is a substitution, not the same instrument.
- GitHub cron is best-effort. Ticks get delayed under load; five minutes is the
  floor, not a guarantee.
- **This strategy has not been backtested.** It is a reasonable structure, not a
  demonstrated edge.
