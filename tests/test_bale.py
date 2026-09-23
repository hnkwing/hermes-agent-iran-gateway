"""Bale adapter tests: transport, inbound normalisation, media, buttons, standalone send."""

from __future__ import annotations

import asyncio
import os

import pytest
from gateway.config import PlatformConfig

pytestmark = pytest.mark.usefixtures("plugin_ctx")  # registers the platform in the registry

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _config(mock, **extra):
    base = {"token": "TESTTOKEN", "api_base": mock.base_url, "poll_timeout": 1}
    base.update(extra)
    return PlatformConfig(enabled=True, extra=base)


def _getme(_payload):
    return {"id": 7001, "is_bot": True, "username": "hermes_test_bot", "first_name": "Hermes"}


async def _connected(bale_module, mock, **extra):
    """Adapter connected against the mock API, with inbound capture installed."""
    adapter = bale_module.BaleAdapter(_config(mock, **extra))
    mock.on("getMe", _getme)
    assert await adapter.connect() is True
    events = []

    async def capture(event):
        events.append(event)

    adapter.handle_message = capture  # type: ignore[assignment]
    return adapter, events


def _message_update(text="سلام", chat_id=111, chat_type="private", message_id=5, **extra):
    message = {
        "message_id": message_id, "date": 1730000000,
        "from": {"id": 222, "is_bot": False, "first_name": "کاربر", "last_name": "تست", "username": "user_test"},
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
    }
    message.update(extra)
    return {"update_id": 1, "message": message}


async def test_connect_registers_bot_and_starts_polling(bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        adapter, _events = await _connected(bale_module, mock)
        try:
            assert adapter.is_connected is True
            assert adapter._bot_id == "7001"
            assert adapter._bot_username == "hermes_test_bot"
            assert mock.calls_for("getMe") == [{}]
            assert str(mock.base_url) + "/botTESTTOKEN/getMe" not in str(mock.calls)  # no token in payloads
        finally:
            await adapter.disconnect()
        assert adapter.is_connected is False


async def test_connect_fails_without_token(bale_module, mock_platform, monkeypatch):
    monkeypatch.delenv("BALE_BOT_TOKEN", raising=False)
    async with mock_platform("bale") as mock:
        adapter = bale_module.BaleAdapter(PlatformConfig(enabled=True, extra={"token": "", "api_base": mock.base_url}))
        assert await adapter.connect() is False
        assert bale_module.check_requirements() is False


async def test_inbound_text_message_maps_to_event(bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        adapter, events = await _connected(bale_module, mock)
        try:
            await adapter._dispatch_update(_message_update())
            await asyncio.sleep(0.01)
            assert len(events) == 1
            event = events[0]
            assert event.text == "سلام"
            assert event.user_id == "222"
            assert event.message_id == "5"
            assert event.source.chat_id == "111"
            assert event.source.chat_type == "dm"
            assert event.source.platform.value == "bale"
            assert event.user_name == "کاربر تست"
        finally:
            await adapter.disconnect()


async def test_group_message_and_reply_context(bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        adapter, events = await _connected(bale_module, mock)
        try:
            reply = {"message_id": 99, "from": {"id": 7001, "is_bot": True, "first_name": "Hermes"},
                     "chat": {"id": -500, "type": "supergroup"}, "text": "previous answer"}
            await adapter._dispatch_update(_message_update(
                text="ادامه بده", chat_id=-500, chat_type="supergroup", reply_to_message=reply))
            await asyncio.sleep(0.01)
            event = events[0]
            assert event.source.chat_type == "group"
            assert event.reply_to_message_id == "99"
            assert event.reply_to_text == "previous answer"
            assert event.reply_to_is_own_message is True
        finally:
            await adapter.disconnect()


async def test_own_and_duplicate_messages_are_filtered(bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        adapter, events = await _connected(bale_module, mock)
        try:
            own = _message_update(message_id=4)
            own["message"]["from"] = {"id": 7001, "is_bot": True, "first_name": "Hermes"}
            await adapter._dispatch_update(own)
            await adapter._dispatch_update(_message_update())
            await adapter._dispatch_update(_message_update())  # duplicate id
            await asyncio.sleep(0.01)
            assert len(events) == 1
        finally:
            await adapter.disconnect()


async def test_inbound_photo_is_downloaded_and_cached(bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        mock.downloads["photos/cat.png"] = PNG
        mock.on("getFile", lambda payload: {"file_id": payload["file_id"], "file_size": len(PNG),
                                            "file_path": "photos/cat.png"})
        adapter, events = await _connected(bale_module, mock)
        try:
            update = _message_update(text="", photo=[{"file_id": "F1", "file_size": len(PNG)}])
            await adapter._dispatch_update(update)
            await asyncio.sleep(0.05)
            assert len(events) == 1
            event = events[0]
            assert event.media_urls and os.path.isfile(event.media_urls[0])
            assert event.media_types == ["image/png"]
            assert event.text == ""
        finally:
            await adapter.disconnect()


async def test_send_chunks_text_and_replies_to_first_chunk(bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        sent = []

        def _send(payload):
            sent.append(payload)
            return {"message_id": 100 + len(sent)}

        mock.on("sendMessage", _send)
        adapter, _events = await _connected(bale_module, mock, max_message_length=20)
        try:
            result = await adapter.send("111", "یک متن نسبتا طولانی برای تست تکه‌تکه شدن", reply_to="9")
            assert result.success is True
            assert len(sent) > 1
            assert all(len(p["text"]) <= 20 for p in sent)
            assert sent[0]["reply_to_message_id"] == 9
            assert "reply_to_message_id" not in sent[1]
        finally:
            await adapter.disconnect()


async def test_send_retries_plain_text_when_markdown_is_rejected(bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        sent = []

        def _send(payload):
            sent.append(payload)
            if payload.get("parse_mode"):
                return mock.error(400, "Bad Request: can't parse entities: unexpected token")
            return {"message_id": 42}

        mock.on("sendMessage", _send)
        adapter, _events = await _connected(bale_module, mock, markdown=True)
        try:
            result = await adapter.send("111", "**bold** text")
            assert result.success is True
            assert sent[0]["parse_mode"] == "Markdown"
            assert "parse_mode" not in sent[1]
            assert "bold" in sent[1]["text"]
        finally:
            await adapter.disconnect()


async def test_send_reports_rate_limit(active_adapter, bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        mock.on("sendMessage", lambda payload: mock.error(429, "Too Many Requests: retry after 30"))
        adapter, _events = await _connected(bale_module, mock)
        active_adapter.append(adapter)
        result = await adapter.send("111", "hi")
        assert result.success is False
        assert result.error_kind == "rate_limited"
        assert result.retryable is True  # a 429 is worth retrying, the gateway backs off
        assert "retry after 30" in result.error


async def test_clarify_buttons_resolve_to_the_option_label(bale_module, monkeypatch, mock_platform):
    resolved = []
    monkeypatch.setattr("tools.clarify_gateway.resolve_gateway_clarify",
                        lambda clarify_id, answer: resolved.append((clarify_id, answer)) or True)
    monkeypatch.setattr("tools.clarify_gateway.mark_awaiting_text", lambda clarify_id: None)
    async with mock_platform("bale") as mock:
        sent = []

        def _send(payload):
            sent.append(payload)
            return {"message_id": 7}

        mock.on("sendMessage", _send)
        mock.on("answerCallbackQuery", lambda payload: True)
        adapter, _events = await _connected(bale_module, mock)
        try:
            result = await adapter.send_clarify("111", "کدام مدل؟", ["A", "B"], "cl-1", "sess-1")
            assert result.success is True
            keyboard = sent[0]["reply_markup"]["inline_keyboard"]
            assert len(keyboard) == 3  # two choices + "Other"
            callback_data = keyboard[1][0]["callback_data"]
            await adapter._handle_callback_query({
                "id": "cb-1", "data": callback_data,
                "from": {"id": 222, "first_name": "کاربر"},
                "message": {"message_id": 7, "chat": {"id": 111, "type": "private"}},
            })
            assert resolved == [("cl-1", "B")]
            assert mock.calls_for("answerCallbackQuery")
        finally:
            await adapter.disconnect()


async def test_approval_button_resolves_through_tools_approval(bale_module, monkeypatch, mock_platform):
    calls = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda session_key, choice: calls.append((session_key, choice)) or 1)
    async with mock_platform("bale") as mock:
        sent = []

        def _send(payload):
            sent.append(payload)
            return {"message_id": 11}

        mock.on("sendMessage", _send)
        mock.on("answerCallbackQuery", lambda payload: True)
        mock.on("editMessageText", lambda payload: True)
        adapter, _events = await _connected(bale_module, mock)
        try:
            result = await adapter.send_exec_approval(
                chat_id="111", command="rm -rf /tmp/x", session_key="sess-9", description="destructive")
            assert result.success is True
            buttons = sent[0]["reply_markup"]["inline_keyboard"]
            callback_data = buttons[0][0]["callback_data"]
            await adapter._handle_callback_query({
                "id": "cb-2", "data": callback_data,
                "from": {"id": 222, "first_name": "کاربر"},
                "message": {"message_id": 11, "chat": {"id": 111, "type": "private"}},
            })
            assert calls == [("sess-9", "once")]
            assert mock.calls_for("editMessageText")
        finally:
            await adapter.disconnect()


async def test_unauthorised_button_press_is_refused(monkeypatch, bale_module, mock_platform):
    resolved = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda session_key, choice: resolved.append(choice) or 1)
    async with mock_platform("bale") as mock:
        mock.on("sendMessage", lambda payload: {"message_id": 1})
        mock.on("answerCallbackQuery", lambda payload: True)
        adapter, _events = await _connected(bale_module, mock)
        adapter.set_authorization_check(lambda user_id, chat_type, chat_id: False)
        try:
            await adapter.send_exec_approval(chat_id="111", command="rm -rf /", session_key="s1")
            await adapter._handle_callback_query({
                "id": "cb-3", "data": "ea:once:1",
                "from": {"id": 999, "first_name": "Stranger"},
                "message": {"message_id": 1, "chat": {"id": 111, "type": "private"}},
            })
            assert resolved == []
            assert mock.calls_for("answerCallbackQuery")[0]["show_alert"] is True
        finally:
            await adapter.disconnect()


async def test_standalone_send_uses_env_token(active_adapter, bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        mock.on("sendMessage", lambda payload: {"message_id": 3})
        config = _config(mock)
        result = await bale_module._standalone_send(config, "111", "**تست**")
        assert result["success"] is True
        assert result["platform"] == "bale"
        payload = mock.calls_for("sendMessage")[0]
        assert payload["chat_id"] == 111
        assert "**" not in payload["text"]


async def test_get_chat_info_maps_types(bale_module, mock_platform):
    async with mock_platform("bale") as mock:
        mock.on("getChat", lambda payload: {"id": payload["chat_id"], "type": "supergroup", "title": "تیم"})
        adapter, _events = await _connected(bale_module, mock)
        try:
            info = await adapter.get_chat_info("111")
            assert info == {"name": "تیم", "type": "group", "chat_id": "111"}
        finally:
            await adapter.disconnect()
