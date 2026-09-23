"""Rubika adapter tests: envelope parsing, inbound normalisation, media, keypads, standalone send."""

from __future__ import annotations

import asyncio
import os

import pytest
from gateway.config import PlatformConfig

pytestmark = pytest.mark.usefixtures("plugin_ctx")  # registers the platform in the registry

PDF = b"%PDF-1.4\n% test fixture\n"
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _config(mock, **extra):
    base = {"token": "TESTTOKEN", "api_base": mock.base_url, "poll_interval": 0.05}
    base.update(extra)
    return PlatformConfig(enabled=True, extra=base)


def _getme(_payload):
    return {"bot": {"bot_id": "B1", "bot_title": "Hermes Test", "username": "hermes_test_bot"}}


def _user_chat(_payload):
    return {"chat_type": "User", "user_id": "U1", "first_name": "کاربر"}


async def _connected(rubika_module, mock, **extra):
    adapter = rubika_module.RubikaAdapter(_config(mock, **extra))
    mock.on("getMe", _getme)
    mock.routes.setdefault("getChat", _user_chat)  # a test may have installed its own
    assert await adapter.connect() is True
    events = []

    async def capture(event):
        events.append(event)

    adapter.handle_message = capture  # type: ignore[assignment]
    return adapter, events


def _new_message(text="سلام", chat_id="C1", message_id="7", **extra):
    message = {
        "message_id": message_id, "text": text, "time": "1730000000",
        "is_edited": False, "sender_type": "User", "sender_id": "U1",
    }
    message.update(extra)
    return {"type": "NewMessage", "chat_id": chat_id, "new_message": message}


async def test_connect_verifies_token_and_starts_polling(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        adapter, _events = await _connected(rubika_module, mock)
        try:
            assert adapter.is_connected is True
            assert adapter._bot_id == "B1"
            assert adapter._bot_title == "Hermes Test"
            assert mock.calls_for("getMe") == [{}]
        finally:
            await adapter.disconnect()
        assert adapter.is_connected is False


async def test_connect_fails_when_token_is_rejected(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        mock.on("getMe", lambda payload: mock.error("INVALID_ACCESS"))
        adapter = rubika_module.RubikaAdapter(_config(mock))
        assert await adapter.connect() is False
        assert adapter.is_connected is False


async def test_inbound_text_message_maps_to_event(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        adapter, events = await _connected(rubika_module, mock)
        try:
            await adapter._dispatch_update(_new_message())
            await asyncio.sleep(0.01)
            assert len(events) == 1
            event = events[0]
            assert event.text == "سلام"
            assert event.user_id == "U1"
            assert event.message_id == "7"
            assert event.source.chat_id == "C1"
            assert event.source.chat_type == "dm"
            assert event.source.platform.value == "rubika"
        finally:
            await adapter.disconnect()


async def test_group_chat_type_and_name_come_from_getchat_cache(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        mock.on("getChat", lambda payload: {"chat_type": "Group", "title": "تیم تست"})
        adapter, events = await _connected(rubika_module, mock)
        try:
            await adapter._dispatch_update(_new_message(message_id="7"))
            await adapter._dispatch_update(_new_message(text="دوباره", message_id="8"))
            await asyncio.sleep(0.01)
            assert [e.source.chat_type for e in events] == ["group", "group"]
            assert events[0].source.chat_name == "تیم تست"
            assert len(mock.calls_for("getChat")) == 1  # cached per chat
        finally:
            await adapter.disconnect()


async def test_own_messages_and_duplicates_are_filtered(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        adapter, events = await _connected(rubika_module, mock)
        try:
            await adapter._dispatch_update(_new_message(message_id="4", sender_type="Bot", sender_id="B1"))
            await adapter._dispatch_update(_new_message(message_id="7"))
            await adapter._dispatch_update(_new_message(message_id="7"))  # duplicate
            await asyncio.sleep(0.01)
            assert len(events) == 1
            assert events[0].message_id == "7"
        finally:
            await adapter.disconnect()


async def test_inbound_document_is_downloaded_and_cached(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        mock.downloads["files/report.pdf"] = PDF
        mock.on("getFile", lambda payload: {"download_url": f"{mock.base_url}/download/files/report.pdf"})
        adapter, events = await _connected(rubika_module, mock)
        try:
            await adapter._dispatch_update(_new_message(
                text="", file={"file_id": "F1", "file_name": "report.pdf", "size": len(PDF)}))
            await asyncio.sleep(0.05)
            assert len(events) == 1
            event = events[0]
            assert event.media_urls and os.path.isfile(event.media_urls[0])
            assert event.media_types == ["application/pdf"]
            assert event.message_type.value == "document"
        finally:
            await adapter.disconnect()


async def test_unsupported_payloads_become_placeholder_text(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        adapter, events = await _connected(rubika_module, mock)
        try:
            await adapter._dispatch_update(_new_message(
                text="", location={"latitude": 35.7, "longitude": 51.4}))
            await asyncio.sleep(0.01)
            assert events[0].text == "[location] 35.7, 51.4"
            assert events[0].message_type.value == "location"
        finally:
            await adapter.disconnect()


async def test_send_chunks_plain_text_and_replies_to_first_chunk(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        sent = []

        def _send(payload):
            sent.append(payload)
            return {"message_id": "100"}

        mock.on("sendMessage", _send)
        adapter, _events = await _connected(rubika_module, mock, max_message_length=24)
        try:
            result = await adapter.send("C1", "**bold** و یک متن طولانی‌تر برای تکه‌تکه شدن", reply_to="5")
            assert result.success is True
            assert len(sent) > 1
            assert all(len(p["text"]) <= 24 for p in sent)
            assert "**" not in sent[0]["text"]
            assert sent[0]["reply_to_message_id"] == "5"
            assert "reply_to_message_id" not in sent[1]
        finally:
            await adapter.disconnect()


async def test_send_reports_flood_as_rate_limited(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        mock.on("sendMessage", lambda payload: mock.error("TOO_REQUESTS"))
        adapter, _events = await _connected(rubika_module, mock)
        try:
            result = await adapter.send("C1", "hi")
            assert result.success is False
            assert result.error_kind == "rate_limited"
            assert result.retryable is True
        finally:
            await adapter.disconnect()


async def test_send_image_uploads_then_sends_file_id(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        mock.on("requestSendFile", lambda payload: {"upload_url": f"{mock.base_url}/upload/photo.jpg"})
        mock.on("sendFile", lambda payload: {"message_id": "300"})
        adapter, _events = await _connected(rubika_module, mock)
        tmp = os.path.join(os.environ["HERMES_HOME"], "out.jpg")
        with open(tmp, "wb") as handle:
            handle.write(JPG)
        try:
            result = await adapter.send_image_file("C1", tmp, caption="**نمودار**")
            assert result.success is True
            assert result.message_id == "300"
            assert mock.calls_for("requestSendFile")[0]["type"] == "Image"
            assert mock.uploads and mock.uploads[0][1] == JPG
            sent = mock.calls_for("sendFile")[0]
            assert sent["file_id"] == "FID-photo.jpg"
            assert sent["text"] == "نمودار"  # markdown stripped
        finally:
            await adapter.disconnect()


async def test_oversized_image_is_refused_before_uploading(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        mock.on("requestSendFile", lambda payload: {"upload_url": f"{mock.base_url}/upload/big.jpg"})
        adapter, _events = await _connected(rubika_module, mock)
        big = os.path.join(os.environ["HERMES_HOME"], "big.jpg")
        with open(big, "wb") as handle:
            handle.write(b"\xff\xd8\xff\xe0" + b"\x00" * (11 * 1024 * 1024))
        try:
            result = await adapter.send_image_file("C1", big)
            assert result.success is False
            assert result.error_kind == "too_long"
            assert not mock.uploads
        finally:
            await adapter.disconnect()


async def test_keypad_tap_resolves_clarify_and_clears_keypad(rubika_module, mock_platform, monkeypatch):
    resolved = []
    monkeypatch.setattr("tools.clarify_gateway.resolve_gateway_clarify",
                        lambda clarify_id, answer: resolved.append((clarify_id, answer)) or True)
    monkeypatch.setattr("tools.clarify_gateway.mark_awaiting_text", lambda clarify_id: None)
    async with mock_platform("rubika") as mock:
        sent = []

        def _send(payload):
            sent.append(payload)
            return {"message_id": "100"}

        mock.on("sendMessage", _send)
        mock.on("editChatKeypad", lambda payload: True)
        adapter, _events = await _connected(rubika_module, mock)
        try:
            result = await adapter.send_clarify("C1", "کدام مدل؟", ["A", "B"], "cl-1", "sess-1")
            assert result.success is True
            keypad = sent[0]["chat_keypad"]
            assert keypad["one_time_keyboard"] is True
            assert [b["button_text"] for b in keypad["rows"][0]["buttons"]] == ["A", "B"]
            button_id = keypad["rows"][0]["buttons"][1]["id"]
            await adapter._dispatch_update(_new_message(
                text="B", message_id="9", aux_data={"button_id": button_id, "button_text": "B"}))
            await asyncio.sleep(0.01)
            assert resolved == [("cl-1", "B")]
            assert mock.calls_for("editChatKeypad")[0]["chat_keypad_type"] == "Remove"
        finally:
            await adapter.disconnect()


async def test_keypad_tap_resolves_exec_approval(rubika_module, mock_platform, monkeypatch):
    calls = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda session_key, choice: calls.append((session_key, choice)) or 1)
    async with mock_platform("rubika") as mock:
        sent = []

        def _send(payload):
            sent.append(payload)
            return {"message_id": "100"}

        mock.on("sendMessage", _send)
        mock.on("editChatKeypad", lambda payload: True)
        adapter, _events = await _connected(rubika_module, mock)
        try:
            result = await adapter.send_exec_approval(
                chat_id="C1", command="rm -rf /tmp/x", session_key="sess-7", description="destructive")
            assert result.success is True
            keypad = sent[0]["chat_keypad"]
            button_id = keypad["rows"][0]["buttons"][0]["id"]
            await adapter._dispatch_update(_new_message(
                text="✅ Approve Once", message_id="9", aux_data={"button_id": button_id}))
            await asyncio.sleep(0.01)
            assert calls == [("sess-7", "once")]
        finally:
            await adapter.disconnect()


async def test_unauthorised_keypad_tap_is_refused(rubika_module, mock_platform, monkeypatch):
    resolved = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda session_key, choice: resolved.append(choice) or 1)
    async with mock_platform("rubika") as mock:
        mock.on("sendMessage", lambda payload: {"message_id": "100"})
        mock.on("editChatKeypad", lambda payload: True)
        adapter, _events = await _connected(rubika_module, mock)
        adapter.set_authorization_check(lambda user_id, chat_type, chat_id: False)
        try:
            await adapter.send_exec_approval(chat_id="C1", command="rm -rf /", session_key="s1")
            button_id = mock.calls_for("sendMessage")[0]["chat_keypad"]["rows"][0]["buttons"][0]["id"]
            await adapter._dispatch_update(_new_message(
                text="", message_id="9", sender_id="U9", aux_data={"button_id": button_id}))
            await asyncio.sleep(0.01)
            assert resolved == []
            assert "not authorised" in mock.calls_for("sendMessage")[-1]["text"]
        finally:
            await adapter.disconnect()


async def test_standalone_send_uses_env_token(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        mock.on("sendMessage", lambda payload: {"message_id": "42"})
        result = await rubika_module._standalone_send(_config(mock), "C1", "**تست**")
        assert result["success"] is True
        assert result["platform"] == "rubika"
        assert result["message_id"] == "42"
        assert "**" not in mock.calls_for("sendMessage")[0]["text"]


async def test_get_chat_info_maps_types(rubika_module, mock_platform):
    async with mock_platform("rubika") as mock:
        mock.on("getChat", lambda payload: {"chat_type": "Channel", "title": "کانال تست"})
        adapter, _events = await _connected(rubika_module, mock)
        try:
            info = await adapter.get_chat_info("C1")
            assert info["type"] == "channel"
            assert info["name"] == "کانال تست"
        finally:
            await adapter.disconnect()
