"""On-chain adapter - Bitcoin network health from mempool.space (free, no key).

Premium override: set GLASSNODE_API_KEY in the environment.
"""
from __future__ import annotations

import os
import time

import httpx

SCHEMA_VERSION = 1
_BASE = "https://mempool.space/api"


async def fetch(asset: str = "BTC/USDT") -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": "mempool.space",
        "available": False,
        "fetched_at": time.time(),
    }
    if os.getenv("GLASSNODE_API_KEY"):
        payload["source"] = "glassnode(key present, free path still used)"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            fees = (await client.get(f"{_BASE}/v1/fees/recommended")).json()
            diff = (await client.get(f"{_BASE}/v1/difficulty-adjustment")).json()
        payload.update(
            available=True,
            fastest_fee_sat_vb=fees.get("fastestFee"),
            hour_fee_sat_vb=fees.get("hourFee"),
            difficulty_change_pct=diff.get("difficultyChange"),
            blocks_to_retarget=diff.get("remainingBlocks"),
        )
    except Exception as exc:  # noqa: BLE001 - context only, never blocks a tick
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload
