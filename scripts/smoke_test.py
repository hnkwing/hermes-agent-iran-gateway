#!/usr/bin/env python3
"""End-to-end smoke test for the Bale and Rubika adapters.

Unlike ``tests/`` (which uses a recording double), this exercises the *real* Hermes plugin
machinery: discovery from a throwaway ``HERMES_HOME``, the platform registry, ``create_adapter``,
``connect``, an outbound send, inbound delivery to the gateway message handler, and the
cron/standalone sender — all against a local mock API, with no real credentials.

Usage (from the repo root, with Hermes' own interpreter so ``gateway``/``httpx`` are importable):

    HERMES_HOME=/tmp/hermes_smoke python scripts/smoke_test.py            # both platforms
    HERMES_HOME=/tmp/hermes_smoke python scripts/smoke_test.py bale rubika

The directory must already contain the plugin (``HERMES_HOME/plugins/hermes_iran_messengers``)
and have it enabled (``hermes plugins enable iran-messengers``); the script prints what it sees
and exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

PLATFORMS = ("bale", "rubika")
PREFIX = {"bale": "BALE", "rubika": "RUBIKA"}


def prepare_env(home: str) -> None:
    os.environ["HERMES_HOME"] = home
    for platform, prefix in PREFIX.items():
        os.environ.setdefault(f"{prefix}_BOT_TOKEN", "SMOKE_TOKEN")
        os.environ.setdefault(f"{prefix}_ALLOW_ALL_USERS", "true")


def make_handler(kind: str):
    """A mock of both platform APIs; records every call and serves one queued update."""
    from aiohttp import web

    calls: list[tuple[str, dict]] = []
    queue = [{
        "update_id": 1,
        "message": {
            "message_id": 5, "date": 1730000000,
            "from": {"id": 222, "is_bot": False, "first_name": "کاربر"},
            "chat": {"id": 111, "type": "private"},
            "text": "سلام از سمت کاربر",
        },
    }] if kind == "bale" else [{
        "type": "NewMessage", "chat_id": "111",
        "new_message": {"message_id": "5", "text": "سلام از سمت کاربر", "time": "1730000000",
                        "sender_type": "User", "sender_id": "222"},
    }]

    def envelope(result):
        if kind == "bale":
            return {"ok": True, "result": result}
        return {"status": "OK", "data": result}

    async def handle(request):
        method = [s for s in request.match_info["tail"].split("/") if s][-1]
        try:
            payload = await request.json() if request.can_read_body else {}
        except Exception:
            payload = {}
        calls.append((method, payload))
        if method == "getMe":
            if kind == "bale":
                return web.json_response(envelope({"id": 7001, "is_bot": True, "username": "smoke_bot"}))
            return web.json_response(envelope({"bot": {"bot_id": "B1", "bot_title": "Smoke Bot"}}))
        if method == "getUpdates":
            if kind == "bale":
                updates, queue[:] = list(queue), []
                return web.json_response(envelope(updates))
            updates, queue[:] = list(queue), []
            return web.json_response(envelope({"updates": updates, "next_offset_id": "2"}))
        if method == "getChat":
            return web.json_response(envelope({"chat_type": "User", "user_id": "222"}))
        if method == "sendMessage":
            return web.json_response(envelope({"message_id": f"9{len(calls):02d}"}))
        return web.json_response(envelope({}))

    return handle, calls


async def check(kind: str) -> None:
    from aiohttp import web

    from gateway.config import PlatformConfig
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import discover_plugins

    print(f"\n=== {kind} ===")
    discover_plugins()
    entry = platform_registry.get(kind)
    assert entry is not None, f"'{kind}' is not registered — is the plugin enabled in HERMES_HOME?"
    print(f"registry entry: {entry.label!r} required_env={entry.required_env} "
          f"cron_env={entry.cron_deliver_env_var} check_fn={entry.check_fn()}")

    handle, calls = make_handler(kind)
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    config = PlatformConfig(enabled=True, extra={
        "token": "SMOKE_TOKEN", "api_base": base, "poll_timeout": 1, "poll_interval": 0.2,
        "max_message_length": 4096})
    adapter = platform_registry.create_adapter(kind, config)
    assert adapter is not None, "create_adapter returned None (check_fn/validate_config failed)"
    print("adapter:", type(adapter).__name__)

    received: list = []

    async def on_message(event):
        received.append(event)

    adapter.set_message_handler(on_message)          # the gateway's own path
    assert await adapter.connect() is True
    print("connected:", adapter.is_connected)

    result = await adapter.send("111", "**پاسخ** از سمت دستیار")
    sent = [p for m, p in calls if m == "sendMessage"]
    assert result.success and sent, (result, sent)
    assert sent[-1]["text"] == "پاسخ از سمت دستیار", sent[-1]   # markdown stripped
    print("send ->", result.success, result.message_id, "| text on the wire:", sent[-1]["text"])

    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.1)
    assert received, "inbound update never reached the gateway message handler"
    event = received[0]
    print("inbound ->", json.dumps({
        "text": event.text, "user_id": event.user_id, "chat_id": event.source.chat_id,
        "chat_type": event.source.chat_type, "platform": event.source.platform.value,
    }, ensure_ascii=False))
    assert event.source.platform.value == kind and event.user_id == "222"

    cron = await entry.standalone_sender_fn(config, "111", "پیام کرون")
    assert cron.get("success") is True, cron
    print("cron/standalone send ->", cron)

    await adapter.disconnect()
    await runner.cleanup()
    print(f"{kind}: OK")


async def main(kinds) -> int:
    for kind in kinds:
        await asyncio.wait_for(check(kind), timeout=60)
    print("\nSMOKE OK — discovery + registry + adapter + wire + cron sender verified for:",
          ", ".join(kinds))
    return 0


if __name__ == "__main__":
    home = os.environ.get("HERMES_HOME") or sys.exit("set HERMES_HOME to a throwaway home first")
    prepare_env(home)
    requested = [a for a in sys.argv[1:] if a in PLATFORMS] or list(PLATFORMS)
    print("HERMES_HOME:", home, "| plugin dir:", Path(home) / "plugins")
    sys.exit(asyncio.run(main(requested)))
