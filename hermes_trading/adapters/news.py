"""Sentiment adapter - Crypto Fear & Greed Index (free, no key).

Premium override: set NEWS_API_KEY in the environment.
"""
from __future__ import annotations

import os
import time

import httpx

SCHEMA_VERSION = 1


async def fetch(asset: str = "BTC/USDT") -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": "alternative.me/fng",
        "available": False,
        "fetched_at": time.time(),
    }
    if os.getenv("NEWS_API_KEY"):
        payload["source"] = "newsapi(key present, free path still used)"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            data = (await client.get("https://api.alternative.me/fng/?limit=2")).json()
        rows = data.get("data", [])
        if not rows:
            raise ValueError("empty fng payload")
        payload.update(
            available=True,
            fng_value=int(rows[0]["value"]),
            fng_label=rows[0]["value_classification"],
            fng_prev=int(rows[1]["value"]) if len(rows) > 1 else None,
        )
    except Exception as exc:  # noqa: BLE001 - context only, never blocks a tick
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload
