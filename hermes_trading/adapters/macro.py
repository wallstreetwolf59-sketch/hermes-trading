"""Macro adapter - US 10y yield and dollar index via yfinance (free, no key)."""
from __future__ import annotations

import asyncio
import time

SCHEMA_VERSION = 1
_TICKERS = {"us10y": "^TNX", "dxy": "DX-Y.NYB"}


def _fetch_sync() -> dict:
    import yfinance as yf

    out = {}
    for name, ticker in _TICKERS.items():
        hist = yf.Ticker(ticker).history(period="5d", interval="1d")
        if hist.empty:
            continue
        closes = hist["Close"].dropna()
        out[name] = round(float(closes.iloc[-1]), 4)
        if len(closes) > 1:
            out[f"{name}_chg_5d"] = round(float(closes.iloc[-1] - closes.iloc[0]), 4)
    return out


async def fetch(asset: str = "BTC/USDT") -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": "yfinance",
        "available": False,
        "fetched_at": time.time(),
    }
    try:
        payload.update(await asyncio.to_thread(_fetch_sync), available=True)
    except Exception as exc:  # noqa: BLE001 - context only, never blocks a tick
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload
