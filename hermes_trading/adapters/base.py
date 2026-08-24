"""Shared adapter contract.

Every adapter exposes `async def fetch(...) -> dict` and stamps a
`schema_version`. The loop halts on a mismatch rather than silently
trading on a payload it no longer understands.
"""
from __future__ import annotations


class SchemaError(RuntimeError):
    """Raised when an adapter returns a payload shape we do not expect."""


def check_schema(payload: dict, expected: int, name: str) -> dict:
    got = payload.get("schema_version")
    if got != expected:
        raise SchemaError(
            f"{name} adapter returned schema_version={got!r}, expected {expected}. "
            "Halting rather than trading on an unknown payload."
        )
    return payload
