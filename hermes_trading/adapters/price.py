"""Price adapter - public OHLCV candles, no API key required."""
from __future__ import annotations

import asyncio
import time

import ccxt

SCHEMA_VERSION = 1

# GitHub-hosted runners sit on US IPs, where Binance/Bybit/OKX answer HTTP 451.
# These three serve public market data from the US without credentials.
_EXCHANGE_CHAIN = ("kraken", "coinbase", "bitstamp")


def _symbol_for(exchange_id: str, asset: str) -> str:
    # Coinbase and Bitstamp quote in USD rather than USDT.
    if exchange_id in ("coinbase", "bitstamp"):
        return asset.replace("/USDT", "/USD")
    return asset


def _fetch_sync(asset: str, timeframe: str, limit: int):
    errors = []
    for ex_id in _EXCHANGE_CHAIN:
        try:
            ex = getattr(ccxt, ex_id)({"enableRateLimit": True, "timeout": 20000})
            candles = ex.fetch_ohlcv(_symbol_for(ex_id, asset), timeframe=timeframe, limit=limit)
            if not candles or len(candles) < 20:
                raise ValueError(f"thin candle set ({len(candles) if candles else 0})")
            return ex_id, candles
        except Exception as exc:  # noqa: BLE001 - we genuinely want the next source
            errors.append(f"{ex_id}: {type(exc).__name__}: {exc}")
    raise ConnectionError("all price sources failed -> " + " | ".join(errors))


async def fetch(asset: str = "BTC/USDT", timeframe: str = "5m", limit: int = 200) -> dict:
    source, candles = await asyncio.to_thread(_fetch_sync, asset, timeframe, limit)
    closes = [c[4] for c in candles]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "asset": asset,
        "timeframe": timeframe,
        "candles": candles,
        "closes": closes,
        "last": closes[-1],
        "fetched_at": time.time(),
    }
