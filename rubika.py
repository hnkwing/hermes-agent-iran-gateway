"""Rubika (روبیکا) gateway adapter for Hermes Agent — https://rubika.ir/botapi

Wire contract (from the official bot API docs at https://rubika.ir/botapi/methods
and https://rubika.ir/botapi/models):

* ``POST https://botapi.rubika.ir/v3/<token>/<method>`` — JSON body. Envelope:
  ``{"status": "OK", "data": {...}}``; any other ``status`` is an error.
* Inbound: ``getUpdates(offset_id, limit)`` → ``data.updates[]`` +
  ``data.next_offset_id`` (or a webhook via ``updateBotEndpoints``).
  ``Update`` = ``{type, chat_id, new_message | updated_message | removed_message_id}``
  with ``type`` in ``NewMessage`` / ``UpdatedMessage`` / ``RemovedMessage`` /
  ``StartedBot`` / ``StoppedBot``.
* Outbound text: ``sendMessage(chat_id, text, reply_to_message_id, chat_keypad,
  inline_keypad)``; edit with ``editMessageText``; delete with ``deleteMessage``.
* Media in: ``getFile(file_id) → data.download_url``, then ``GET`` that URL.
* Media out: ``requestSendFile(type) → data.upload_url``, upload the bytes as
  ``multipart/form-data`` field ``file`` → ``{file_id}``, then
  ``sendFile(chat_id, file_id, text)``. Types: ``File`` (50MB), ``Image`` (10MB),
  ``Voice`` (mp3), ``Video`` (mp4, 50MB), ``Music`` (mp3), ``Gif`` (muted mp4).
* Buttons: ``Keypad`` = ``{"rows": [{"buttons": [{"id", "type", "button_text"}]}]}``.
  ``inline_keypad`` sits under the message (its presses are webhook-only), while
  ``chat_keypad`` is the keyboard under the composer and its presses DO arrive over
  long polling as a ``NewMessage`` whose ``aux_data.button_id`` is the button id.
  This adapter therefore renders approval/clarify prompts as a one-time chat keypad
  (plus a numbered text fallback), so buttons work with polling.

Env vars: ``RUBIKA_BOT_TOKEN`` (required), ``RUBIKA_ALLOWED_USERS``,
``RUBIKA_ALLOW_ALL_USERS``, ``RUBIKA_HOME_CHANNEL``, ``RUBIKA_HOME_CHANNEL_NAME``,
``RUBIKA_API_BASE``, ``RUBIKA_POLL_INTERVAL``, ``RUBIKA_INTERACTIVE``.
config.yaml: ``platforms.rubika.extra.{token,api_base,poll_interval,interactive}``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # httpx ships with every Hermes install
    import httpx
except ImportError:  # pragma: no cover - only on a broken install
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import (
    apply_yaml_bridge,
    extra_or_secret,
    get_scoped_secret,
    seed_extra_from_env,
    send_error,
)
from gateway.platforms.base import (
    BasePlatformAdapter,
    ExecApprovalPrompt,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
    validate_inbound_media_size,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

from .common import (
    DEFAULT_MEDIA_MAX_BYTES,
    DEFAULT_MAX_MESSAGE_LENGTH,
    PollingTask,
    clean_id,
    download_bytes,
    human_bytes,
    merge_platform_yaml,
    split_message,
    strip_markdown,
    to_int,
    truthy,
)

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://botapi.rubika.ir/v3"
MAX_MESSAGE_LENGTH = DEFAULT_MAX_MESSAGE_LENGTH
POLL_INTERVAL_DEFAULT = 3.0
POLL_BATCH_LIMIT = 50
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 2000
KEYPAD_BUTTON_PREFIX = "hz"  # our button ids: hz:<token>
UNAUTHORIZED = "⛔ You are not authorised to use this bot."

# Rubika's FileTypeEnum → the upload lane requestSendFile expects, chosen by extension.
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_GIF_EXT = {".gif"}
_VIDEO_EXT = {".mp4"}
_AUDIO_EXT = {".mp3", ".ogg", ".oga", ".opus", ".m4a", ".wav"}
FILE_TYPE_FOR_EXT = {**{e: "Image" for e in _IMAGE_EXT}, **{e: "Gif" for e in _GIF_EXT},
                     **{e: "Video" for e in _VIDEO_EXT}}
IMAGE_UPLOAD_MAX = 10 * 1024 * 1024
GENERIC_UPLOAD_MAX = 50 * 1024 * 1024


def _api_base(extra: Dict[str, Any]) -> str:
    value = str(extra_or_secret(extra, "api_base", "RUBIKA_API_BASE", DEFAULT_API_BASE) or "").strip()
    base = (value or DEFAULT_API_BASE).rstrip("/")
    return base


def _poll_interval(extra: Dict[str, Any]) -> float:
    raw = extra_or_secret(extra, "poll_interval", "RUBIKA_POLL_INTERVAL", POLL_INTERVAL_DEFAULT)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = POLL_INTERVAL_DEFAULT
    return value if value > 0 else POLL_INTERVAL_DEFAULT


def _max_message_length(extra: Dict[str, Any]) -> int:
    value = to_int(extra_or_secret(extra, "max_message_length", "RUBIKA_MAX_MESSAGE_LENGTH",
                                   MAX_MESSAGE_LENGTH), MAX_MESSAGE_LENGTH)
    return value if value > 0 else MAX_MESSAGE_LENGTH


def file_type_for(path: str, *, prefer_voice: bool = False) -> str:
    """Rubika's ``FileTypeEnum`` value for a local path (``File`` when nothing matches)."""
    ext = Path(path).suffix.lower()
    if prefer_voice and ext in _AUDIO_EXT:
        return "Voice"
    if ext in FILE_TYPE_FOR_EXT:
        return FILE_TYPE_FOR_EXT[ext]
    if ext in _AUDIO_EXT:
        return "Music"
    return "File"


@dataclass
class _ApiResult:
    """One Rubika API round-trip."""

    ok: bool
    data: Any = None
    error: str = ""
    status: str = ""
    retryable: bool = False


# -- capability probes / config helpers -------------------------------------


def check_requirements() -> bool:
    """Passive probe: httpx importable and a token stored (never installs anything)."""
    return httpx is not None and bool(str(get_scoped_secret("RUBIKA_BOT_TOKEN", "") or "").strip())


def validate_config(config: Any) -> bool:
    """True when a token is configured via env or ``platforms.rubika.extra.token``."""
    return bool(str(extra_or_secret(getattr(config, "extra", {}) or {}, "token", "RUBIKA_BOT_TOKEN") or "").strip())


def is_connected(config: Any) -> bool:
    """Configured (env or config.yaml) — drives ``hermes gateway status``."""
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env so env-only setups surface in status output."""
    token = str(get_scoped_secret("RUBIKA_BOT_TOKEN", "") or "").strip()
    if not token:
        return None
    seed = seed_extra_from_env(
        (
            ("RUBIKA_API_BASE", "api_base", lambda v: v.rstrip("/")),
            ("RUBIKA_POLL_INTERVAL", "poll_interval", float),
            ("RUBIKA_MAX_MESSAGE_LENGTH", "max_message_length", to_int),
            ("RUBIKA_INTERACTIVE", "interactive", lambda v: truthy(v, default=True)),
        ),
        home_env="RUBIKA_HOME_CHANNEL",
    )
    return {"token": token, **seed}


def _apply_yaml_config(cfg: dict, platform_cfg: dict) -> Optional[dict]:
    """Bridge ``platforms.rubika`` YAML keys to env vars (env wins).

    Accepts both shapes people write in ``config.yaml`` — keys directly under ``rubika:`` and
    keys under ``rubika.extra:`` (``extra`` wins when both carry the same key).
    """
    return apply_yaml_bridge(merge_platform_yaml(platform_cfg), (
        ("api_base", "RUBIKA_API_BASE", "str"),
        ("token", "RUBIKA_BOT_TOKEN", "str"),
        ("poll_interval", "RUBIKA_POLL_INTERVAL", "str"),
        ("interactive", "RUBIKA_INTERACTIVE", "lower"),
        ("max_message_length", "RUBIKA_MAX_MESSAGE_LENGTH", "str"),
    ))


class RubikaAdapter(BasePlatformAdapter):
    """Polling Rubika adapter with native media and chat-keypad prompt buttons."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("rubika"))
        extra = config.extra or {}
        self._token: str = str(extra_or_secret(extra, "token", "RUBIKA_BOT_TOKEN") or "").strip()
        self._api_base: str = _api_base(extra)
        self._poll_interval: float = _poll_interval(extra)
        self._max_len: int = _max_message_length(extra)
        self._interactive: bool = truthy(
            extra_or_secret(extra, "interactive", "RUBIKA_INTERACTIVE", True), default=True)
        self._client: Optional[Any] = None
        self._poll: Optional[PollingTask] = None
        self._offset: str = ""
        self._fatal: Optional[str] = None
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE, ttl_seconds=DEDUP_WINDOW_SECONDS)
        self._bot_id: str = ""
        self._bot_title: str = ""
        self._chat_info: Dict[str, Dict[str, str]] = {}  # chat_id -> {"type", "name"}
        # Button taps: short opaque ids map back to what the tap means.
        self._button_counter = itertools.count(1)
        self._buttons: Dict[str, Dict[str, Any]] = {}

    # -- transport ----------------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{self._api_base}/{self._token}/{method}"

    async def _call(self, method: str, payload: Optional[Dict[str, Any]] = None,
                    *, timeout: float = 60.0) -> _ApiResult:
        """One API call; never raises for a platform-level failure."""
        if self._client is None:
            return _ApiResult(ok=False, error="Rubika client is not connected")
        body = {k: v for k, v in (payload or {}).items() if v is not None}
        try:
            resp = await self._client.post(self._url(method), json=body, timeout=timeout)
        except Exception as exc:
            retryable = isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) if httpx else False
            logger.warning("[%s] %s transport error: %s", self.name, method, exc)
            return _ApiResult(ok=False, error=f"{type(exc).__name__}: {exc}", retryable=retryable)
        try:
            envelope = resp.json()
        except Exception:
            envelope = None
        if not isinstance(envelope, dict):
            return _ApiResult(ok=False, error=f"HTTP {resp.status_code}: {(resp.text or '')[:200]}",
                              retryable=resp.status_code >= 500)
        status = str(envelope.get("status") or "")
        if status.upper() == "OK":
            return _ApiResult(ok=True, data=envelope.get("data") or {}, status=status)
        detail = str(envelope.get("status") or envelope.get("description") or "unknown error")
        retryable = any(word in status.upper()
                        for word in ("FLOOD", "TOO_MANY", "TOO_REQUESTS", "SERVER", "TIMEOUT"))
        if any(word in status.upper() for word in ("INVALID_ACCESS", "TOKEN", "UNAUTHORIZED")):
            self._fatal = f"Rubika rejected the bot token ({status})"
        logger.warning("[%s] %s failed: %s", self.name, method, detail)
        return _ApiResult(ok=False, error=detail, status=status, retryable=retryable)

    @staticmethod
    def _message_id(data: Any) -> Optional[str]:
        """Message id from either documented response shape (``data.message_id``)."""
        if not isinstance(data, dict):
            return None
        for key in ("message_id", "id"):
            if data.get(key):
                return clean_id(data[key])
        nested = data.get("message_update")
        if isinstance(nested, dict) and nested.get("message_id"):
            return clean_id(nested["message_id"])
        return None

    # -- lifecycle ----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Verify the token with ``getMe`` and start the poll loop."""
        if httpx is None:
            logger.error("[%s] httpx is not installed", self.name)
            return False
        if not self._token:
            logger.error("[%s] RUBIKA_BOT_TOKEN is not configured", self.name)
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=60.0, write=120.0, pool=15.0))
        me = await self._call("getMe", {}, timeout=25.0)
        if not me.ok:
            self._set_fatal_error("rubika_token_rejected", me.error or "getMe failed", retryable=me.retryable)
            return False
        bot = (me.data or {}).get("bot") if isinstance(me.data, dict) else {}
        bot = bot if isinstance(bot, dict) else {}
        self._bot_id = clean_id(bot.get("bot_id")) or ""
        self._bot_title = str(bot.get("bot_title") or bot.get("username") or "")
        self._mark_connected()
        self._poll = PollingTask(name=self.name, step=self._poll_step,
                                 should_stop=lambda _exc: bool(self._fatal),
                                 healthy_after=120.0)
        self._poll.start()
        self._wire_plugin_handlers(None)
        logger.info("[%s] Connected as %s (%s)", self.name, self._bot_title or "?", self._api_base)
        return True

    async def disconnect(self) -> None:
        """Stop polling and close the HTTP client."""
        self._running = False
        if self._poll is not None:
            await self._poll.stop()
            self._poll = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("[%s] client close error: %s", self.name, exc)
            self._client = None
        self._mark_disconnected()
        logger.info("[%s] Disconnected", self.name)

    # -- inbound ------------------------------------------------------------

    async def _poll_step(self) -> float:
        """One ``getUpdates`` round; returns the configured poll interval."""
        payload: Dict[str, Any] = {"limit": POLL_BATCH_LIMIT}
        if self._offset:
            payload["offset_id"] = self._offset
        result = await self._call("getUpdates", payload, timeout=45.0)
        if self._fatal:
            raise RuntimeError(self._fatal)
        if not result.ok:
            if result.retryable:
                raise RuntimeError(result.error or "getUpdates failed")
            logger.warning("[%s] getUpdates rejected: %s", self.name, result.error)
            return max(self._poll_interval, 10.0)
        data = result.data if isinstance(result.data, dict) else {}
        updates = data.get("updates") if isinstance(data.get("updates"), list) else []
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                await self._dispatch_update(update)
            except Exception as exc:
                logger.error("[%s] failed to handle update: %s", self.name, exc, exc_info=True)
        next_offset = clean_id(data.get("next_offset_id"))
        if next_offset:
            self._offset = next_offset
        return self._poll_interval

    async def _dispatch_update(self, update: Dict[str, Any]) -> None:
        """Route one Rubika ``Update`` object."""
        event_type = str(update.get("type") or "")
        chat_id = clean_id(update.get("chat_id") or update.get("object_guid"))
        if event_type in {"NewMessage", "UpdatedMessage"}:
            message = update.get("new_message") or update.get("updated_message")
            if isinstance(message, dict):
                await self._handle_message(chat_id, message, event_type)
            return
        if event_type == "RemovedMessage":
            logger.debug("[%s] message %s removed in %s", self.name, update.get("removed_message_id"), chat_id)
            return
        if event_type in {"StartedBot", "StoppedBot"}:
            # A conversation opened/closed: nothing to answer, the agent replies when the user writes.
            logger.info("[%s] %s in chat %s", self.name, event_type, chat_id)

    async def _chat_info_for(self, chat_id: str) -> Tuple[str, str]:
        """``(chat_type, display name)`` for a chat, resolved once via ``getChat`` and cached.

        Rubika updates carry neither a chat type nor a title, but ``SessionSource`` needs both
        (chat type drives group gating; the name shows up in session listings).
        """
        cached = self._chat_info.get(chat_id)
        if cached:
            return cached.get("type", "dm"), cached.get("name", "")
        chat_type, name = "dm", ""
        info = await self._call("getChat", {"chat_id": chat_id}, timeout=20.0)
        payload = info.data if isinstance(info.data, dict) else {}
        raw = str(payload.get("chat_type") or "").strip().lower()
        if raw in {"group", "channel"}:
            chat_type = raw
        elif raw in {"user", "bot"}:
            chat_type = "dm"
        elif payload.get("title"):
            chat_type = "group"
        name = (str(payload.get("title") or "").strip()
                or " ".join(p for p in (str(payload.get("first_name") or "").strip(),
                                        str(payload.get("last_name") or "").strip()) if p).strip()
                or str(payload.get("username") or "").strip())
        self._chat_info[chat_id] = {"type": chat_type, "name": name}
        return chat_type, name

    async def _download_to_cache(self, file_id: str, file_name: str = "") -> Optional[str]:
        """Download an inbound attachment through ``getFile`` and cache it locally."""
        if self._client is None or not file_id:
            return None
        info = await self._call("getFile", {"file_id": file_id}, timeout=30.0)
        payload = info.data if isinstance(info.data, dict) else {}
        url = str(payload.get("download_url") or "")
        if not info.ok or not url:
            logger.warning("[%s] getFile failed for %s: %s", self.name, file_id, info.error or "no download_url")
            return None
        try:
            blob = await download_bytes(self._client, url, max_bytes=DEFAULT_MEDIA_MAX_BYTES)
        except Exception as exc:
            logger.warning("[%s] media download failed (%s): %s", self.name, file_id, exc)
            return None
        name = Path(file_name or f"{file_id}").name or "attachment.bin"
        ext = Path(name).suffix or (mimetypes.guess_extension(mimetypes.guess_type(name)[0] or "") or "")
        try:
            validate_inbound_media_size(len(blob), media_type="attachment")
        except ValueError as exc:
            logger.warning("[%s] %s", self.name, exc)
            return None
        try:
            if ext.lower() in _IMAGE_EXT or ext.lower() == ".gif":
                return cache_image_from_bytes(blob, ext or ".jpg")
            if ext.lower() in _VIDEO_EXT:
                return cache_video_from_bytes(blob, ext or ".mp4")
            if ext.lower() in _AUDIO_EXT:
                return cache_audio_from_bytes(blob, ext or ".mp3")
            return cache_document_from_bytes(blob, name)
        except ValueError as exc:
            logger.warning("[%s] refusing to cache %s: %s", self.name, name, exc)
            return None

    async def _handle_message(self, chat_id: str, message: Dict[str, Any], event_type: str) -> None:
        """Normalise one Rubika ``Message`` into a Hermes ``MessageEvent``."""
        if not chat_id:
            return
        message_id = clean_id(message.get("message_id"))
        sender_id = clean_id(message.get("sender_id")) or None
        sender_type = str(message.get("sender_type") or "").lower()
        if sender_type == "bot" and sender_id and sender_id == self._bot_id:
            # Before the dedup ring, so our own posts can't shadow a user message with the same id.
            logger.debug("[%s] ignoring own message", self.name)
            return
        if self._dedup.is_duplicate(f"{chat_id}:{message_id}:{event_type}"):
            logger.debug("[%s] duplicate message %s in %s, skipping", self.name, message_id, chat_id)
            return
        aux = message.get("aux_data") if isinstance(message.get("aux_data"), dict) else {}
        button_id = clean_id(aux.get("button_id"))
        if button_id.startswith(f"{KEYPAD_BUTTON_PREFIX}:"):
            await self._handle_button_tap(button_id, chat_id, message_id, sender_id)
            return
        text = str(message.get("text") or "")
        media_paths: List[str] = []
        media_types: List[str] = []
        message_type = MessageType.TEXT
        file_info = message.get("file") if isinstance(message.get("file"), dict) else None
        if file_info and file_info.get("file_id"):
            path = await self._download_to_cache(str(file_info["file_id"]), str(file_info.get("file_name") or ""))
            if path:
                media_paths.append(path)
                mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
                media_types.append(mime)
                if mime.startswith("image/"):
                    message_type = MessageType.PHOTO
                elif mime.startswith("audio/"):
                    message_type = MessageType.VOICE
                elif mime.startswith("video/"):
                    message_type = MessageType.VIDEO
                else:
                    message_type = MessageType.DOCUMENT
        if not text and not media_paths:
            if isinstance(message.get("location"), dict):
                loc = message["location"]
                text = f"[location] {loc.get('latitude')}, {loc.get('longitude')}"
                message_type = MessageType.LOCATION
            elif isinstance(message.get("contact_message"), dict):
                contact = message["contact_message"]
                text = f"[contact] {contact.get('first_name', '')} {contact.get('phone_number', '')}".strip()
            elif isinstance(message.get("sticker"), dict):
                text = "[sticker]"
                message_type = MessageType.STICKER
            elif isinstance(message.get("poll"), dict):
                poll = message["poll"]
                options = ", ".join(str(o) for o in (poll.get("options") or []))
                text = f"[poll] {poll.get('question', '')}: {options}"
            elif button_id:
                # A chat-keypad tap from someone else's keypad: its label arrives as the text.
                text = str(aux.get("button_text") or "[button]")
            else:
                text = "[unsupported message]"
        chat_type, chat_name = await self._chat_info_for(chat_id)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name or None,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=None,
            message_id=message_id,
            is_bot=sender_type == "bot",
        )
        timestamp = datetime.now(tz=timezone.utc)
        raw_time = message.get("time")
        if raw_time:
            try:
                timestamp = datetime.fromtimestamp(int(raw_time), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                pass
        reply_to = clean_id(message.get("reply_to_message_id")) or None
        event = MessageEvent(
            text=text,
            message_type=message_type,
            user_id=sender_id,
            source=source,
            raw_message=message,
            message_id=message_id,
            media_urls=media_paths,
            media_types=media_types,
            reply_to_message_id=reply_to,
            timestamp=timestamp,
        )
        logger.debug("[%s] inbound %s/%s (%s): %s", self.name, chat_id, message_id, chat_type, text[:80])
        await self.handle_message(event)

    # -- button taps (chat keypad) ------------------------------------------

    def _register_button(self, payload: Dict[str, Any]) -> str:
        """Store a button meaning and return the short id that goes on the wire."""
        token = base36(next(self._button_counter))
        if len(self._buttons) > 500:  # bound the map; a stale keypad resolves to "unknown"
            self._buttons.clear()
        self._buttons[token] = payload
        return f"{KEYPAD_BUTTON_PREFIX}:{token}"

    async def _handle_button_tap(self, button_id: str, chat_id: str, message_id: str,
                                 sender_id: Optional[str]) -> None:
        """Resolve a chat-keypad press the way Bale/Telegram resolve callback queries."""
        token = button_id.split(":", 1)[1]
        state = self._buttons.pop(token, None)
        if not state:
            logger.debug("[%s] unknown keypad tap %s in %s", self.name, button_id, chat_id)
            return
        chat_type, _chat_name = await self._chat_info_for(chat_id)
        if self._is_sender_authorized(sender_id, chat_type, chat_id) is False:
            await self._send_text(chat_id, UNAUTHORIZED)
            return
        kind = state.get("kind")
        if kind == "ea":
            await self._resolve_approval(chat_id, str(state.get("choice") or ""), state.get("session_key"))
        elif kind == "sc":
            await self._resolve_slash_confirm(chat_id, str(state.get("choice") or ""),
                                              str(state.get("confirm_id") or ""), state.get("session_key"))
        elif kind == "cl":
            await self._resolve_clarify(chat_id, str(state.get("clarify_id") or ""),
                                        to_int(state.get("index"), -1), str(state.get("label") or ""))
        await self._clear_keypad(chat_id)

    async def _clear_keypad(self, chat_id: str) -> None:
        """Remove the chat keypad a resolved prompt left behind (best effort)."""
        try:
            await self._call("editChatKeypad", {"chat_id": chat_id, "chat_keypad_type": "Remove"}, timeout=20.0)
        except Exception as exc:  # pragma: no cover - cosmetic
            logger.debug("[%s] keypad removal failed for %s: %s", self.name, chat_id, exc)

    async def _resolve_approval(self, chat_id: str, choice: str, session_key: Any) -> None:
        if not session_key or not choice:
            return
        count = 0
        try:
            from tools.approval import resolve_gateway_approval

            count = resolve_gateway_approval(str(session_key), choice)
        except Exception as exc:
            logger.error("[%s] resolve_gateway_approval failed: %s", self.name, exc)
        label = {
            "once": "✅ Approved once", "session": "✅ Approved for session",
            "always": "✅ Approved permanently", "deny": "❌ Denied",
        }.get(choice, "Resolved")
        await self._send_text(chat_id, label if count else "⌛ Approval expired — nothing was waiting.")
        if count:
            self.resume_typing_for_chat(chat_id)

    async def _resolve_slash_confirm(self, chat_id: str, choice: str, confirm_id: str, session_key: Any) -> None:
        if not confirm_id:
            return
        try:
            from tools import slash_confirm as slash_confirm_mod

            await slash_confirm_mod.resolve(str(session_key or ""), confirm_id, choice)
        except Exception as exc:
            logger.error("[%s] slash confirm resolve failed: %s", self.name, exc)
        await self._send_text(chat_id, {"once": "✅ Approved", "always": "🔒 Always approved",
                                        "cancel": "❌ Cancelled"}.get(choice, "Resolved"))

    async def _resolve_clarify(self, chat_id: str, clarify_id: str, index: int, label: str) -> None:
        if not clarify_id or index < 0:
            return
        try:
            from tools.clarify_gateway import resolve_gateway_clarify

            resolved = resolve_gateway_clarify(clarify_id, label)
        except Exception as exc:
            logger.error("[%s] resolve_gateway_clarify failed: %s", self.name, exc)
            resolved = False
        await self._send_text(chat_id, f"✅ {label}" if resolved else "This question was already answered.")

    # -- outbound -----------------------------------------------------------

    def format_message(self, content: str) -> str:
        """Rubika text is plain: markdown is stripped (styling needs its ``Metadata`` API)."""
        return strip_markdown(content)

    def _keypad(self, rows: List[List[Dict[str, Any]]], *, one_time: bool = True) -> Dict[str, Any]:
        return {"rows": [{"buttons": row} for row in rows],
                "resize_keyboard": True, "one_time_keyboard": bool(one_time)}

    @staticmethod
    def _button(button_id: str, label: str) -> Dict[str, Any]:
        return {"id": button_id, "type": "Simple", "button_text": label[:64]}

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> SendResult:
        """Send text, chunked to the platform cap."""
        text = self.format_message(content or "")
        if not text.strip():
            return SendResult(success=True, message_id=None)
        chunks = split_message(text, self._max_len)
        last: Optional[SendResult] = None
        for index, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if index == 0 and reply_to:
                payload["reply_to_message_id"] = reply_to
            result = await self._call("sendMessage", payload)
            if not result.ok:
                return SendResult(success=False, error=result.error, retryable=result.retryable,
                                  raw_response=result, error_kind=_error_kind(result))
            last = SendResult(success=True, message_id=self._message_id(result.data), raw_response=result)
        return last or SendResult(success=True)

    async def _send_text(self, chat_id: str, text: str) -> SendResult:
        """Internal one-shot text send (prompt feedback), no chunking loop."""
        return await self.send(chat_id, text)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Rubika's bot API has no chat-action method; nothing to show."""
        return None

    async def _upload_and_send(self, chat_id: str, file_path: str, *, caption: Optional[str] = None,
                               reply_to: Optional[str] = None, prefer_voice: bool = False) -> SendResult:
        """``requestSendFile`` → multipart upload → ``sendFile``."""
        path = Path(file_path)
        if not path.is_file():
            return SendResult(success=False, error=f"file not found: {file_path}", error_kind="not_found")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        upload_type = file_type_for(path.name, prefer_voice=prefer_voice)
        cap = IMAGE_UPLOAD_MAX if upload_type == "Image" else GENERIC_UPLOAD_MAX
        if size > cap:
            return SendResult(success=False, error=f"{path.name} is {human_bytes(size)} — Rubika's {upload_type} "
                                                   f"limit is {human_bytes(cap)}", error_kind="too_long")
        requested = await self._call("requestSendFile", {"type": upload_type}, timeout=30.0)
        upload_url = ""
        if requested.ok and isinstance(requested.data, dict):
            upload_url = str(requested.data.get("upload_url") or "")
        if not upload_url:
            return SendResult(success=False, error=f"requestSendFile failed: {requested.error or 'no upload_url'}",
                              retryable=requested.retryable)
        try:
            blob = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            return SendResult(success=False, error=f"cannot read {file_path}: {exc}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            resp = await self._client.post(upload_url, files={"file": (path.name, blob, mime)}, timeout=180.0)
            uploaded = resp.json() if resp.content else {}
        except Exception as exc:
            return SendResult(success=False, error=f"upload failed: {exc}", retryable=True)
        file_id = _upload_file_id(uploaded)
        if not file_id:
            return SendResult(success=False, error=f"upload returned no file_id ({str(uploaded)[:160]})")
        payload: Dict[str, Any] = {"chat_id": chat_id, "file_id": file_id}
        if caption:
            payload["text"] = strip_markdown(caption)[:self._max_len]
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        sent = await self._call("sendFile", payload, timeout=90.0)
        if not sent.ok:
            return SendResult(success=False, error=sent.error, retryable=sent.retryable,
                              error_kind=_error_kind(sent))
        return SendResult(success=True, message_id=self._message_id(sent.data), raw_response=sent)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                              **kwargs: Any) -> SendResult:
        return await self._upload_and_send(chat_id, image_path, caption=caption, reply_to=reply_to)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Rubika cannot fetch a URL, so the image is downloaded and uploaded (size-capped)."""
        url = str(image_url or "")
        if not url.startswith(("http://", "https://")):
            return await super().send_image(chat_id, image_url, caption=caption, reply_to=reply_to, metadata=metadata)
        if self._client is None:
            return SendResult(success=False, error="Rubika client is not connected")
        try:
            blob = await download_bytes(self._client, url, max_bytes=IMAGE_UPLOAD_MAX)
        except Exception as exc:
            logger.debug("[%s] image download failed (%s), sending the link instead", self.name, exc)
            return await super().send_image(chat_id, image_url, caption=caption, reply_to=reply_to, metadata=metadata)
        tmp_dir = Path(get_cache_scratch_dir())
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(url.split("?")[0]).suffix.lower() or ".jpg"
        target = tmp_dir / f"out_{uuid.uuid4().hex[:12]}{ext}"
        try:
            await asyncio.to_thread(target.write_bytes, blob)
            return await self._upload_and_send(chat_id, str(target), caption=caption, reply_to=reply_to)
        except OSError as exc:
            return SendResult(success=False, error=f"cannot stage image: {exc}")
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort
                pass

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> SendResult:
        return await self._upload_and_send(chat_id, file_path, caption=caption, reply_to=reply_to)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self._upload_and_send(chat_id, audio_path, caption=caption, reply_to=reply_to,
                                           prefer_voice=True)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        return await self._upload_and_send(chat_id, video_path, caption=caption, reply_to=reply_to)

    async def edit_message(self, chat_id: str, message_id: str, content: str,
                           metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        result = await self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                                      "text": self.format_message(content)})
        return SendResult(success=result.ok, error=None if result.ok else result.error)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        result = await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        return result.ok

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        info = await self._call("getChat", {"chat_id": chat_id}, timeout=20.0)
        payload = info.data if isinstance(info.data, dict) else {}
        if not info.ok or not payload:
            return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}
        raw = str(payload.get("chat_type") or "").strip().lower()
        chat_type = "dm" if raw in {"user", "bot", ""} else ("group" if raw == "group" else "channel")
        name = (str(payload.get("title") or "").strip()
                or " ".join(p for p in (str(payload.get("first_name") or "").strip(),
                                        str(payload.get("last_name") or "").strip()) if p).strip()
                or str(payload.get("username") or "").strip()
                or str(chat_id))
        return {"name": name, "type": chat_type, "chat_id": clean_id(payload.get("chat_id")) or str(chat_id)}

    # -- interactive prompts (chat-keypad buttons) --------------------------

    async def _send_prompt(self, chat_id: str, text: str, choices: List[Tuple[str, Dict[str, Any]]],
                           *, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send a prompt with one-time keypad buttons (plus the numbered text fallback)."""
        rows: List[List[Dict[str, Any]]] = []
        row: List[Dict[str, Any]] = []
        for label, state in choices:
            row.append(self._button(self._register_button(state), label))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:self._max_len]}
        if rows and self._interactive:
            payload["chat_keypad"] = self._keypad(rows)
        result = await self._call("sendMessage", payload)
        if not result.ok:
            return SendResult(success=False, error=result.error, retryable=result.retryable)
        return SendResult(success=True, message_id=self._message_id(result.data), raw_response=result)

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Exec approval as tappable keypad buttons (numbers still work as typed text)."""
        text = strip_markdown(prompt.text)
        choices: List[Tuple[str, Dict[str, Any]]] = []
        for index, (label, choice, _style) in enumerate(prompt.actions, start=1):
            text = f"{text}\n{index}. {label}"
            choices.append((label, {"kind": "ea", "choice": choice, "session_key": prompt.session_key}))
        if not self._interactive:
            return await self.send(prompt.chat_id, text, metadata=prompt.metadata)
        return await self._send_prompt(prompt.chat_id, text, choices, metadata=prompt.metadata)

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str,
                                 confirm_id: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Three-button slash-command confirmation."""
        text = f"{title}\n\n{strip_markdown(message)}".strip() if title else strip_markdown(message)
        choices: List[Tuple[str, Dict[str, Any]]] = []
        for index, (label, choice) in enumerate(
                (("✅ Approve Once", "once"), ("🔒 Always Approve", "always"), ("❌ Cancel", "cancel")), start=1):
            text = f"{text}\n{index}. {label}"
            choices.append((label, {"kind": "sc", "choice": choice, "confirm_id": confirm_id,
                                    "session_key": session_key}))
        if not self._interactive:
            return await self.send(chat_id, text, metadata=metadata)
        return await self._send_prompt(chat_id, text, choices, metadata=metadata)

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Clarify prompt: keypad buttons per choice plus a typed-answer fallback."""
        text = f"❓ {strip_markdown(question)}"
        buttons: List[Tuple[str, Dict[str, Any]]] = []
        if choices:
            limit = 8  # keep the keypad compact; the numbered text covers the rest
            for index, choice in enumerate(choices):
                label = str(choice)
                text += f"\n{index + 1}. {label}"
                if index < limit:
                    buttons.append((label, {"kind": "cl", "clarify_id": clarify_id, "index": index, "label": label}))
            try:
                from tools.clarify_gateway import mark_awaiting_text

                mark_awaiting_text(clarify_id)
            except Exception as exc:
                logger.debug("[%s] mark_awaiting_text unavailable: %s", self.name, exc)
        if not buttons or not self._interactive:
            return await self.send(chat_id, text[:self._max_len], metadata=metadata)
        return await self._send_prompt(chat_id, text, buttons, metadata=metadata)

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        """Drop button state for a clarify the gateway ended without a tap."""
        for token in [t for t, state in self._buttons.items()
                      if state.get("kind") == "cl" and state.get("clarify_id") == clarify_id]:
            self._buttons.pop(token, None)


def _upload_file_id(uploaded: Any) -> str:
    """``file_id`` from an upload response (``{"file_id": ...}`` or ``{"data": {"file_id": ...}}``)."""
    if not isinstance(uploaded, dict):
        return ""
    if uploaded.get("file_id"):
        return clean_id(uploaded["file_id"])
    nested = uploaded.get("data")
    if isinstance(nested, dict) and nested.get("file_id"):
        return clean_id(nested["file_id"])
    return ""


def base36(number: int) -> str:
    """Short opaque token for a button id (no '-'/'_' url-encoding surprises)."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number <= 0:
        return "0"
    out = ""
    while number:
        number, rem = divmod(number, 36)
        out = digits[rem] + out
    return out


def get_cache_scratch_dir() -> str:
    """Hermes cache dir for transient outbound staging files (never the system temp dir)."""
    from hermes_constants import get_hermes_home

    return str(Path(get_hermes_home()) / "cache" / "rubika")


def _error_kind(result: _ApiResult) -> str:
    """Map a Rubika failure onto Hermes' platform-neutral ``SEND_ERROR_KINDS``."""
    blob = f"{result.status} {result.error}".lower()
    if "flood" in blob or "too_many" in blob or "too many" in blob or "too_requests" in blob:
        return "rate_limited"
    if result.status.upper() in {"INVALID_ACCESS", "INVALID_AUTH"} or "blocked" in blob:
        return "forbidden"
    if "not_found" in blob or "not found" in blob:
        return "not_found"
    if "size" in blob or "large" in blob or "limit" in blob:
        return "too_long"
    if result.retryable:
        return "transient"
    return "unknown"


async def _standalone_send(
    pconfig: Any, chat_id: str, message: str, *, thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None, force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process send for cron / ``hermes send`` when no gateway is running."""
    if httpx is None:
        return send_error("rubika standalone send: httpx is not installed")
    extra = getattr(pconfig, "extra", {}) or {}
    token = str(extra_or_secret(extra, "token", "RUBIKA_BOT_TOKEN") or "").strip()
    target = str(chat_id or "").strip()
    if not token:
        return send_error("rubika standalone send: RUBIKA_BOT_TOKEN is not configured")
    if not target:
        return send_error("rubika standalone send: no chat_id")
    payload = {"chat_id": target, "text": strip_markdown(message or "")[:MAX_MESSAGE_LENGTH]}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{_api_base(extra)}/{token}/sendMessage", json=payload)
            envelope = resp.json() if resp.content else {}
        if not isinstance(envelope, dict) or str(envelope.get("status", "")).upper() != "OK":
            detail = envelope.get("status") if isinstance(envelope, dict) else resp.text[:200]
            return send_error(f"rubika send failed: {detail or f'HTTP {resp.status_code}'}")
        data = envelope.get("data") or {}
        message_id = None
        if isinstance(data, dict):
            message_id = data.get("message_id") or (data.get("message_update") or {}).get("message_id")
        return {"success": True, "platform": "rubika", "chat_id": target,
                "message_id": clean_id(message_id) or None}
    except Exception as exc:
        return send_error(f"rubika standalone send failed: {exc}")


def register_rubika(ctx: Any) -> None:
    """Register the ``rubika`` platform with the Hermes plugin context."""
    ctx.register_platform(
        name="rubika",
        label="Rubika (روبیکا)",
        adapter_factory=RubikaAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["RUBIKA_BOT_TOKEN"],
        install_hint="No extra packages needed (httpx ships with Hermes)",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="RUBIKA_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="RUBIKA_ALLOWED_USERS",
        allow_all_env="RUBIKA_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🟣",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting on Rubika (روبیکا), an Iranian messenger with a Telegram-shaped "
            "bot API. Rubika renders plain text only — markdown is stripped before sending — so "
            "write plain text, avoid tables, and keep code short. There are no threads and no "
            "typing indicator. Images (<=10MB), files (<=50MB), video and voice messages work in "
            "both directions. Approval and clarify prompts arrive as tappable keypad buttons "
            "under the composer; users can also answer by typing the option number or text. Long "
            f"answers are chunked around {MAX_MESSAGE_LENGTH} characters."
        ),
    )
