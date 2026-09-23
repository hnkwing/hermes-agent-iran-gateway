"""Shared building blocks for the Bale and Rubika gateway adapters.

Both Iranian platforms expose a Telegram-shaped HTTP bot API: POST JSON to a method
URL and read JSON back. What differs is the response envelope (Bale:
``{"ok": true, "result": ...}``; Rubika: ``{"status": "OK", "data": ...}``) and the
message/update shape, so everything that is *not* platform specific lives here:

* :class:`PollingTask` - a supervised long-poll loop (capped exponential backoff + jitter,
  fatal-error exit, shutdown-safe cancellation)
* :func:`download_bytes` - size-capped media download
* :func:`md_to_bale` / :func:`strip_markdown` - markdown handling
* :func:`split_message` - platform-aware chunking
* small env/id helpers

This module deliberately imports **only** the stdlib so it can be unit-tested
without a Hermes runtime. The adapters import it as ``from .common import ...``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_MESSAGE_LENGTH",
    "DEFAULT_MEDIA_MAX_BYTES",
    "TRUTHY",
    "truthy",
    "to_int",
    "clean_id",
    "chunk_text",
    "split_message",
    "strip_markdown",
    "md_to_bale",
    "download_bytes",
    "PollingTask",
    "human_bytes",
]

#: Conservative default per-message cap. Neither platform documents a hard limit;
#: both reject pathologically long payloads, so the adapters chunk at this size and
#: let the operator override it through ``max_message_length``.
DEFAULT_MAX_MESSAGE_LENGTH = 4096

#: Hard ceiling for a single inbound/outbound attachment handled by these adapters.
DEFAULT_MEDIA_MAX_BYTES = 64 * 1024 * 1024

TRUTHY = frozenset({"1", "true", "yes", "on", "y", "بله"})


def truthy(value: Any, default: bool = False) -> bool:
    """Parse a config/env truthy flag; blanks fall back to *default*."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in TRUTHY


def to_int(value: Any, default: int = 0) -> int:
    """Best-effort int conversion (platform ids arrive as int or numeric string)."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def clean_id(value: Any) -> str:
    """Normalise a platform id to the string form used for env/config keys and session keys."""
    return str(value).strip() if value is not None else ""


def chunk_text(text: str, limit: int) -> List[str]:
    """Split *text* into ``<= limit`` pieces, preferring newline then space boundaries."""
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


def split_message(text: str, limit: int, *, max_chunks: int = 20) -> List[str]:
    """``chunk_text`` with a safety valve: absurd payloads are truncated, not fanned out."""
    chunks = chunk_text(text or "", limit)
    if len(chunks) <= max_chunks:
        return chunks
    kept = chunks[:max_chunks]
    kept[-1] = kept[-1] + "\n…(truncated)"
    return kept


_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+.-]*\n?")
_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_BOLD_UNDER_RE = re.compile(r"__(.+?)__", re.S)
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\s)([^*]+?)(?<!\s)\*(?!\*)", re.S)
_ITALIC_UNDER_RE = re.compile(r"(?<!_)_(?!\s)([^_]+?)(?<!\s)_(?!_)", re.S)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.S)
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.M)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.M)


def merge_platform_yaml(platform_cfg: Any) -> Dict[str, Any]:
    """Flatten a platform's ``config.yaml`` block for ``apply_yaml_bridge``.

    Hermes passes the whole platform section (``{"enabled": true, "extra": {...}}``), but people
    also write the keys directly under the platform name, so merge both with ``extra`` winning.
    """
    block = platform_cfg if isinstance(platform_cfg, dict) else {}
    extra = block.get("extra")
    if not isinstance(extra, dict):
        return {k: v for k, v in block.items() if k != "extra"}
    return {**{k: v for k, v in block.items() if k != "extra"}, **extra}


def strip_markdown(text: str) -> str:
    """Reduce markdown to the plain text both platforms render literally.

    Used for Rubika (styling needs its own UTF-16 ``Metadata`` API, which this adapter
    does not emit) and for Bale when ``markdown: false`` (its legacy Markdown dialect is
    strict about spaces around ``*``, so plain text is the safe default).
    """
    if not text:
        return text
    out = _CODE_FENCE_RE.sub("", text)
    out = out.replace("`", "")
    out = _IMAGE_RE.sub(lambda m: (f"{m.group(1)} ({m.group(2)})" if m.group(1) else m.group(2)), out)
    out = _LINK_RE.sub(r"\1 (\2)", out)
    out = _BOLD_STAR_RE.sub(r"\1", out)
    out = _BOLD_UNDER_RE.sub(r"\1", out)
    out = _STRIKE_RE.sub(r"\1", out)
    out = _ITALIC_STAR_RE.sub(r"\1", out)
    out = _ITALIC_UNDER_RE.sub(r"\1", out)
    out = _HEADING_RE.sub(r"\1", out)
    out = _HR_RE.sub("", out)
    return out


def md_to_bale(text: str) -> str:
    """Convert standard markdown to Bale's legacy Markdown dialect.

    Bale renders ``*bold*`` and ``_italic_`` only when the markers are surrounded by
    spaces (documented behaviour), so bold spans are padded. Send failures caused by a
    dialect mismatch are retried as plain text by the adapter.
    """
    if not text:
        return text
    out = _CODE_FENCE_RE.sub("", text)
    out = _BOLD_STAR_RE.sub(r"*\1*", out)
    out = _BOLD_UNDER_RE.sub(r"*\1*", out)
    out = _STRIKE_RE.sub(r"\1", out)
    out = _HEADING_RE.sub(r"*\1*", out)
    return _pad_emphasis(out)


_BOLD_SPAN_RE = re.compile(r"(?<!\s)(\*[^*\n]+\*)(?!\s)")


def _pad_emphasis(text: str) -> str:
    """Add the spaces Bale requires around ``*bold*`` spans (only when missing)."""
    return _BOLD_SPAN_RE.sub(lambda m: f" {m.group(1)} ", text)


async def download_bytes(
    client: Any,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    max_bytes: int = DEFAULT_MEDIA_MAX_BYTES,
    timeout: float = 60.0,
) -> bytes:
    """Stream *url* into memory, refusing payloads above *max_bytes*.

    Reads the body in chunks and re-checks the running total so a lying or missing
    ``Content-Length`` cannot smuggle an oversized file past the cap.
    """
    buf = bytearray()
    async with client.stream("GET", url, headers=headers or {}, timeout=timeout) as resp:
        resp.raise_for_status()
        declared = to_int(resp.headers.get("content-length"), 0)
        if max_bytes and declared and declared > max_bytes:
            raise ValueError(f"attachment too large ({declared} bytes > {max_bytes})")
        async for chunk in resp.aiter_bytes(65536):
            buf.extend(chunk)
            if max_bytes and len(buf) > max_bytes:
                raise ValueError(f"attachment too large (> {max_bytes} bytes)")
    return bytes(buf)


def human_bytes(size: Any) -> str:
    """Compact byte size for log lines (never raises)."""
    try:
        num = float(size)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}GB"


class PollingTask:
    """A supervised long-poll loop.

    ``step`` performs exactly one poll round and returns how many seconds to wait before
    the next one. Raises from ``step`` are logged and answered with capped exponential
    backoff plus jitter; ``should_stop(exc)`` (optional) can declare an error fatal, in
    which case the loop exits permanently instead of retrying. Cancellation from
    :meth:`stop` is propagated silently.
    """

    BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

    def __init__(
        self,
        *,
        name: str,
        step: Callable[[], Awaitable[float]],
        should_stop: Optional[Callable[[BaseException], bool]] = None,
        healthy_after: float = 60.0,
    ) -> None:
        self.name = name
        self._step = step
        self._should_stop = should_stop
        self._healthy_after = healthy_after
        self._task: Optional[asyncio.Task] = None
        self._stop_requested = False

    def start(self) -> None:
        """Spawn the loop (idempotent while a task is alive)."""
        if self._task is not None and not self._task.done():
            return
        self._stop_requested = False
        self._task = asyncio.create_task(self._run(), name=f"{self.name}-poll")

    async def stop(self) -> None:
        """Cancel and await the loop; safe to call when it never started."""
        self._stop_requested = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[%s] poll task ended with %s", self.name, exc)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        backoff_idx = 0
        while not self._stop_requested:
            started = asyncio.get_event_loop().time()
            try:
                delay = await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._should_stop is not None and self._should_stop(exc):
                    logger.error("[%s] polling stopped: %s", self.name, exc)
                    return
                delay = self.BACKOFF[min(backoff_idx, len(self.BACKOFF) - 1)]
                backoff_idx += 1
                logger.warning("[%s] poll error: %s — retrying in %.1fs", self.name, exc, delay)
            else:
                if asyncio.get_event_loop().time() - started >= self._healthy_after:
                    backoff_idx = 0
            if self._stop_requested:
                return
            await asyncio.sleep(max(0.0, float(delay) * (1.0 + random.random() * 0.25)))
