"""Entrypoint.

Default mode is a single tick, which is what GitHub Actions invokes. The
--loop mode exists so you can run the same worker continuously on a VPS
later without changing any other code.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .loop import STATE, load_goal, tick


def _parse_args(argv=None):
    p = argparse.ArgumentParser(prog="hermes_trading.run")
    p.add_argument("--asset", help="Override the asset in goal.yaml, e.g. ETH/USDT")
    p.add_argument("--loop", action="store_true", help="Run continuously instead of one tick")
    p.add_argument("--interval", type=int, default=60, help="Seconds between ticks in --loop mode")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


async def _run_forever(interval: int, verbose: bool) -> None:
    while True:
        try:
            await tick(verbose=verbose)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive, log and retry
            print(f"[tick] FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        await asyncio.sleep(interval)


def main(argv=None) -> int:
    args = _parse_args(argv)

    if args.asset:
        # Override for this process only - goal.yaml on disk is the source of
        # truth and is never rewritten by a CLI flag.
        import yaml

        goal_path = STATE / "goal.yaml"
        goal = load_goal()
        goal["asset"] = args.asset
        goal_path.write_text(yaml.safe_dump(goal, sort_keys=False), encoding="utf-8")
        print(f"[run] asset overridden to {args.asset}")

    goal = load_goal()
    print(f"Booting hermes-trading worker | asset={goal.get('asset')} "
          f"target={goal.get('target_return')} over {goal.get('target_window_days')}d "
          f"| max_dd={goal.get('max_drawdown')} min_sharpe={goal.get('min_sharpe')}")

    verbose = not args.quiet
    try:
        if args.loop:
            asyncio.run(_run_forever(args.interval, verbose))
        else:
            asyncio.run(tick(verbose=verbose))
    except KeyboardInterrupt:
        print("[run] interrupted")
    except Exception as exc:  # noqa: BLE001 - surface the failure to the runner
        print(f"[run] FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
