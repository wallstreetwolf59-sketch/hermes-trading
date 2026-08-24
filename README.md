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

- The price adapter tries Kraken, then Coinbase, then Bitstamp. On GitHub's US
  runners Kraken serves BTC/USDT directly, which is what production uses. From
  some regions Kraken refuses the pair and the adapter falls through to
  Coinbase, which quotes **BTC/USD** — close enough for paper trading, but not
  literally the same instrument. Check `price_source` in
  [`state/heartbeat.json`](state/heartbeat.json) to see which one served a
  given tick.
- GitHub cron is best-effort. Ticks get delayed under load; five minutes is the
  floor, not a guarantee.
## Backtest results — read this before trusting anything

v01 was backtested on Binance BTCUSDT perpetuals, 5-minute bars, 2026-01-29 to
2026-07-31, intraday, 0.03% slippage. **It does not have an edge.**

| Variant | Trades | Win rate | Total P&L ($1k/trade) |
|---|---:|---:|---:|
| RSI cross-up 32, no filters | 358 | 37.4% | −$459 |
| ...restricted to EMA50>EMA200 uptrend | 131 | 39.7% | −$28 |
| Momentum: Supertrend flip + 1h trend up | 68 | 50.0% | −$84 |

The trend filter helps materially (−$1.90 → −$0.21 average per trade) but does
not turn a losing signal into a winning one.

**The win rate / reward-risk frontier.** Reconstructing outcomes from each
trade's maximum favourable and adverse excursion, holding the stop at 1%:

| Target | R:R | Win rate |
|---:|---:|---:|
| 0.25% | 0.25 | 49.6% |
| 1.00% | 1.00 | 32.8% |
| 2.00% | 2.00 | 17.6% |

Win rate and reward-risk trade off mechanically. For a 2R target against a 1R
stop, a random walk wins about 33% of the time — every signal tested came in at
or below that. A 60% win rate at 2R would be +0.8R expectancy per trade, which
is fund-grade, and nothing here is remotely close.

**A bug this caught.** The original `volume_mult: 1.15` demanded above-average
volume on the entry bar, but in mean reversion volume spikes on the selloff and
dries up on the bounce being bought. It took the strategy from 131 qualifying
trades to zero — the agent would have run indefinitely without ever trading. It
is disabled at `0.0`.

**Four families tested, none with an edge.** Same window, same slippage:

| Family | Trades | Win rate | Verdict |
|---|---:|---:|---|
| RSI mean reversion | 358 | 37.4% | negative |
| RSI + EMA trend filter | 131 | 39.7% | negative |
| Supertrend momentum + 1h filter | 68 | — | negative |
| Donchian breakout @ 1.5R, no EOD | 63 | 30.2% | worse than random |

For a 1.5R target a random walk wins ~40%; the breakout signal wins 30%, so
breakouts on BTC 15m fail *more* often than chance.

**Intraday is the wrong frame for crypto.** With `intraday=True`, forced 23:58
UTC square-offs dominated every exit mix (67 of 81, 178 of 223, 110 of 358) and
trades almost never reached their targets. With `intraday=False` the same
strategy resolved cleanly - 107 stops, 50 targets, zero EOD exits. Perpetuals
have no session; a daily flat is an equities habit that costs money here.

**Why no further tuning was done.** The breakout strategy scored 35.5% win rate
in the first half of the sample and 25.0% in the second - unstable, and losing
in both. Searching more symbols and parameters would eventually surface a
combination that looks good in-sample, but that split-sample instability is
exactly the signature that says such a result would be noise.

Treat this repo as a working harness for a self-improving agent, **not** as a
profitable trading system.
