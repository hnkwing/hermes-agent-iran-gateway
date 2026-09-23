"""Unit tests for the platform-independent helpers in ``common.py``."""

from __future__ import annotations

import asyncio

import pytest


def test_truthy_and_ids(common_module):
    assert common_module.truthy("YES") is True
    assert common_module.truthy("0") is False
    assert common_module.truthy(None, default=True) is True
    assert common_module.truthy("", default=True) is True
    assert common_module.to_int("42") == 42
    assert common_module.to_int(None, 7) == 7
    assert common_module.to_int("abc", 3) == 3
    assert common_module.clean_id(12345) == "12345"
    assert common_module.clean_id(None) == ""


def test_chunk_text_prefers_boundaries(common_module):
    text = "alpha\nbeta\ngamma delta epsilon"
    chunks = common_module.chunk_text(text, 12)
    assert all(len(c) <= 12 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_text_hard_split_long_word(common_module):
    chunks = common_module.chunk_text("x" * 25, 10)
    assert [len(c) for c in chunks] == [10, 10, 5]


def test_split_message_truncates_absurd_payloads(common_module):
    chunks = common_module.split_message("y" * 1000, 10, max_chunks=3)
    assert len(chunks) == 3
    assert chunks[-1].endswith("…(truncated)")


def test_strip_markdown(common_module):
    raw = "**bold** and _italic_ and `code`\n\n# Heading\n\n[link](https://x.ir) and ~~gone~~"
    plain = common_module.strip_markdown(raw)
    assert "**" not in plain and "`" not in plain and "#" not in plain
    assert "bold" in plain and "italic" in plain and "link (https://x.ir)" in plain
    assert "~~" not in plain


def test_md_to_bale_pads_bold(common_module):
    out = common_module.md_to_bale("hello **bold** world")
    assert "*bold*" in out
    assert out.startswith("hello ") and out.endswith(" world")
    assert common_module.md_to_bale("__bold__") == " *bold* "


@pytest.mark.parametrize("value,expected", [("512", "512B"), (2048, "2.0KB")])
def test_human_bytes(common_module, value, expected):
    assert common_module.human_bytes(value) == expected


def test_polling_task_backoff_and_stop(common_module):
    """A failing step backs off instead of dying, and stop() cancels the loop."""

    async def scenario():
        attempts = {"count": 0}
        delays = []

        async def step() -> float:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("boom")
            return 0.01

        task = common_module.PollingTask(name="test", step=step)
        original_sleep = asyncio.sleep

        async def fast_sleep(delay: float) -> None:
            delays.append(delay)
            await original_sleep(0)

        asyncio.sleep = fast_sleep  # type: ignore[assignment]
        try:
            task.start()
            await original_sleep(0.05)
            assert attempts["count"] >= 3
            assert delays and delays[0] >= 1.0
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]
            await task.stop()
        assert not task.running

    asyncio.run(scenario())


def test_polling_task_fatal_error_stops(common_module):
    """``should_stop`` declares an error fatal: the loop exits instead of retrying."""

    async def scenario():
        attempts = {"count": 0}

        async def step() -> float:
            attempts["count"] += 1
            raise RuntimeError("invalid token")

        task = common_module.PollingTask(name="test", step=step, should_stop=lambda exc: "invalid" in str(exc))
        task.start()
        await asyncio.sleep(0.05)
        assert attempts["count"] == 1
        assert not task.running
        await task.stop()

    asyncio.run(scenario())
