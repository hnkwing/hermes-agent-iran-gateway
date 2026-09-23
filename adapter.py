"""Plugin entry point — registers both Iranian messenger platforms.

Hermes calls ``register(ctx)`` once per plugin load. Each platform registers
independently, so an install with only ``BALE_BOT_TOKEN`` set gets the Bale
adapter, and one with only ``RUBIKA_BOT_TOKEN`` gets Rubika; setting neither
leaves the plugin loaded but idle (no platform enabled).
"""

from __future__ import annotations

from typing import Any

from .bale import register_bale
from .rubika import register_rubika

__all__ = ["register"]


def register(ctx: Any) -> None:
    """Register the ``bale`` and ``rubika`` gateway platforms."""
    register_bale(ctx)
    register_rubika(ctx)
