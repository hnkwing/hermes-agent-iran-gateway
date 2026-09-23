"""Bale (بله) gateway adapter for Hermes Agent — https://docs.bale.ai

Wire contract (checked against ``https://tapi.bale.ai/swagger/swagger.json``):

* ``POST https://tapi.bale.ai/bot<token>/<MethodName>`` — JSON body, or
  ``multipart/form-data`` for uploads. Response envelope:
  ``{"ok": true, "result": ...}`` or
  ``{"ok": false, "error_code": 400, "description": "..."}``.
* Inbound: ``getUpdates(offset, limit, timeout)`` (long polling) or ``setWebhook``.
  ``Update`` = ``{update_id, message | edited_message | channel_post | callback_query | ...}``.
* Media in: ``getFile(file_id) -> {file_path}`` then
  ``GET https://tapi.bale.ai/file/bot<token>/<file_path>``.
* Media out: ``sendPhoto`` / ``sendDocument`` / ``sendVoice`` / ``sendVideo`` with a
  ``file_id``, a public URL, or a multipart upload.
* Buttons: ``reply_markup={"inline_keyboard": [[{"text": ..., "callback_data": ...}]]}``;
  a press arrives as a ``callback_query`` update that must be acked with
  ``answerCallbackQuery``.
* Formatting: legacy Markdown (``*bold*``, ``_italic_``) — Bale wants spaces around the
  markers, so Markdown is opt-in (``markdown: true``) and a rejected send is retried as
  plain text.

Env vars: ``BALE_BOT_TOKEN`` (required), ``BALE_ALLOWED_USERS``, ``BALE_ALLOW_ALL_USERS``,
``BALE_HOME_CHANNEL``, ``BALE_HOME_CHANNEL_NAME``, ``BALE_API_BASE``, ``BALE_MARKDOWN``,
``BALE_POLL_TIMEOUT``, ``BALE_MAX_MESSAGE_LENGTH``.
config.yaml: ``platforms.bale.extra.{token,api_base,markdown,poll_timeout,max_message_length}``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import mimetypes
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
    md_to_bale,
    merge_platform_yaml,
    split_message,
    strip_markdown,
    to_int,
    truthy,
)

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://tapi.bale.ai"
MAX_MESSAGE_LENGTH = DEFAULT_MAX_MESSAGE_LENGTH
POLL_TIMEOUT_DEFAULT = 25
POLL_BATCH_LIMIT = 100
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 2000
UNAUTHORIZED = "⛔ You are not authorised to use this bot."
MEDIA_GROUP_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "video/mp4": ".mp4",
}

# Hermes' shared button-callback vocabulary, reused verbatim so the gateway's resolvers work
# unmodified. We keep Telegram's short prefixes and key them by a local id -> session_key map.
CB_APPROVAL = "ea"   # ea:<choice>:<approval_id>
CB_SLASH = "sc"      # sc:<choice>:<confirm_id>
CB_CLARIFY = "cl"    # cl:<clarify_id>:<idx|other>


def _api_base(extra: Dict[str, Any]) -> str:
    """Resolve the API base (``extra.api_base`` > ``BALE_API_BASE`` > default), no trailing slash."""
    value = str(extra_or_secret(extra, "api_base", "BALE_API_BASE", DEFAULT_API_BASE) or "").strip()
    return (value or DEFAULT_API_BASE).rstrip("/")


def _max_message_length(extra: Dict[str, Any]) -> int:
    value = to_int(extra_or_secret(extra, "max_message_length", "BALE_MAX_MESSAGE_LENGTH", MAX_MESSAGE_LENGTH),
                   MAX_MESSAGE_LENGTH)
    return value if value > 0 else MAX_MESSAGE_LENGTH


def _media_kind_for(ext: str, mime: str = "") -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    suffix = (ext or "").lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return "image"
    if suffix in {".ogg", ".mp3", ".m4a", ".wav", ".opus", ".oga"}:
        return "audio"
    if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
        return "video"
    return "document"


@dataclass
class _ApiResult:
    """One Bale API round-trip: ``ok`` plus exactly one of ``result`` / ``error``."""

    ok: bool
    result: Any = None
    error: str = ""
    error_code: int = 0
    retryable: bool = False


# -- capability probes / config helpers -------------------------------------


def check_requirements() -> bool:
    """Passive probe: httpx importable and a token stored (never installs anything)."""
    return httpx is not None and bool(str(get_scoped_secret("BALE_BOT_TOKEN", "") or "").strip())


def validate_config(config: Any) -> bool:
    """True when a token is configured via env or ``platforms.bale.extra.token``."""
    return bool(str(extra_or_secret(getattr(config, "extra", {}) or {}, "token", "BALE_BOT_TOKEN") or "").strip())


def is_connected(config: Any) -> bool:
    """Configured (env or config.yaml) — drives ``hermes gateway status``."""
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env so env-only setups surface in status output."""
    token = str(get_scoped_secret("BALE_BOT_TOKEN", "") or "").strip()
    if not token:
        return None
    seed = seed_extra_from_env(
        (
            ("BALE_API_BASE", "api_base", lambda v: v.rstrip("/")),
            ("BALE_MARKDOWN", "markdown", lambda v: truthy(v)),
            ("BALE_POLL_TIMEOUT", "poll_timeout", to_int),
            ("BALE_MAX_MESSAGE_LENGTH", "max_message_length", to_int),
        ),
        home_env="BALE_HOME_CHANNEL",
    )
    return {"token": token, **seed}


def _apply_yaml_config(cfg: dict, platform_cfg: dict) -> Optional[dict]:
    """Bridge ``platforms.bale`` YAML keys to env vars (env wins).

    Accepts both shapes people write in ``config.yaml`` — keys directly under ``bale:`` and
    keys under ``bale.extra:`` (``extra`` wins when both carry the same key).
    """
    return apply_yaml_bridge(merge_platform_yaml(platform_cfg), (
        ("api_base", "BALE_API_BASE", "str"),
        ("token", "BALE_BOT_TOKEN", "str"),
        ("markdown", "BALE_MARKDOWN", "lower"),
        ("poll_timeout", "BALE_POLL_TIMEOUT", "str"),
        ("max_message_length", "BALE_MAX_MESSAGE_LENGTH", "str"),
    ))


class BaleAdapter(BasePlatformAdapter):
    """Long-polling Bale adapter with native media and inline-button prompts."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("bale"))
        extra = config.extra or {}
        self._token: str = str(extra_or_secret(extra, "token", "BALE_BOT_TOKEN") or "").strip()
        self._api_base: str = _api_base(extra)
        self._markdown: bool = truthy(extra_or_secret(extra, "markdown", "BALE_MARKDOWN", False))
        self._poll_timeout: int = max(
            0, to_int(extra_or_secret(extra, "poll_timeout", "BALE_POLL_TIMEOUT", POLL_TIMEOUT_DEFAULT),
                      POLL_TIMEOUT_DEFAULT))
        self._max_len: int = _max_message_length(extra)
        self._client: Optional[Any] = None
        self._poll: Optional[PollingTask] = None
        self._offset: int = 0
        self._fatal: Optional[str] = None
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE, ttl_seconds=DEDUP_WINDOW_SECONDS)
        self._bot_id: Optional[str] = None
        self._bot_username: str = ""
        # Prompt state: the button payload stays short, the mapping lives here.
        self._approval_state: Dict[int, str] = {}
        self._approval_counter = itertools.count(1)
        self._slash_confirm_state: Dict[str, str] = {}
        self._clarify_state: Dict[str, str] = {}
        # (clarify_id, option index) -> the option label a button press resolves to.
        self._clarify_options: Dict[Tuple[str, int], str] = {}

    # -- transport ----------------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{self._api_base}/bot{self._token}/{method}"

    def _file_url(self, file_path: str) -> str:
        return f"{self._api_base}/file/bot{self._token}/{file_path.lstrip('/')}"

    async def _call(
        self,
        method: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        files: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> _ApiResult:
        """One API call; never raises for a platform-level failure."""
        if self._client is None:
            return _ApiResult(ok=False, error="Bale client is not connected")
        body = {k: v for k, v in (payload or {}).items() if v is not None}
        try:
            if files:
                resp = await self._client.post(self._url(method), data=body or None, files=files, timeout=timeout)
            else:
                resp = await self._client.post(self._url(method), json=body or {}, timeout=timeout)
        except Exception as exc:  # httpx timeouts / connection errors
            retryable = isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) if httpx else False
            logger.warning("[%s] %s transport error: %s", self.name, method, exc)
            return _ApiResult(ok=False, error=f"{type(exc).__name__}: {exc}", retryable=retryable)
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("ok") is True:
            return _ApiResult(ok=True, result=data.get("result"))
        code = int(resp.status_code or 0)
        description = ""
        if isinstance(data, dict):
            description = str(data.get("description") or "")
            code = to_int(data.get("error_code"), code)
        if not description:
            description = (resp.text or "").strip()[:300] or f"HTTP {resp.status_code}"
        retryable = code in {429, 500, 502, 503, 504} or code == 0
        if code == 401 or code == 403:
            self._fatal = f"Bale rejected the bot token ({code}: {description})"
        logger.warning("[%s] %s failed (code=%s retryable=%s): %s", self.name, method, code, retryable, description)
        return _ApiResult(ok=False, error=description, error_code=code, retryable=retryable)

    # -- lifecycle ----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Verify the token with ``getMe`` and start the long-poll loop."""
        if httpx is None:
            logger.error("[%s] httpx is not installed", self.name)
            return False
        if not self._token:
            logger.error("[%s] BALE_BOT_TOKEN is not configured", self.name)
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=90.0, write=60.0, pool=15.0))
        me = await self._call("getMe", {}, timeout=20.0)
        if not me.ok:
            self._set_fatal_error("bale_token_rejected", me.error or "getMe failed", retryable=me.retryable)
            return False
        info = me.result if isinstance(me.result, dict) else {}
        self._bot_id = clean_id(info.get("id")) or None
        self._bot_username = str(info.get("username") or "")
        self._mark_connected()
        self._poll = PollingTask(name=self.name, step=self._poll_step, should_stop=lambda _exc: bool(self._fatal))
        self._poll.start()
        self._wire_plugin_handlers(None)
        logger.info("[%s] Connected as @%s (%s)", self.name, self._bot_username or "?", self._api_base)
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
        """One ``getUpdates`` round; returns seconds to wait before the next one."""
        offset = self._offset + 1 if self._offset else 0
        result = await self._call(
            "getUpdates",
            {"offset": offset, "limit": POLL_BATCH_LIMIT, "timeout": self._poll_timeout},
            timeout=float(self._poll_timeout) + 30.0,
        )
        if self._fatal:
            raise RuntimeError(self._fatal)
        if not result.ok:
            if result.retryable:
                raise RuntimeError(result.error or "getUpdates failed")
            # A non-retryable protocol error (bad parameter, deleted bot): surface but keep polling
            # slowly — the operator sees it in the log and can fix the token/redeploy.
            logger.warning("[%s] getUpdates rejected: %s", self.name, result.error)
            return 5.0
        updates = result.result if isinstance(result.result, list) else []
        for update in updates:
            if isinstance(update, dict):
                self._offset = max(self._offset, to_int(update.get("update_id"), self._offset))
                try:
                    await self._dispatch_update(update)
                except Exception as exc:
                    logger.error("[%s] failed to handle update %s: %s", self.name, update.get("update_id"), exc,
                                 exc_info=True)
        return 0.2 if updates else 0.1

    async def _dispatch_update(self, update: Dict[str, Any]) -> None:
        """Route one ``Update`` object to its handler."""
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback_query(callback)
            return
        message = update.get("message") or update.get("channel_post")
        if isinstance(message, dict):
            await self._handle_message(message)
            return
        # edited_message / service updates are intentionally ignored (the gateway answers the
        # turn in progress; replaying an edit as a new prompt would double-run the agent).

    def _media_kind_and_id(self, message: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """``(kind, file_id, file_name)`` for the first attachment in *message*."""
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            best = max(photos, key=lambda p: to_int((p or {}).get("file_size"), 0) if isinstance(p, dict) else 0)
            if isinstance(best, dict) and best.get("file_id"):
                return "image", str(best["file_id"]), None
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            return "document", str(document["file_id"]), str(document.get("file_name") or "")
        voice = message.get("voice")
        if isinstance(voice, dict) and voice.get("file_id"):
            return "audio", str(voice["file_id"]), "voice.ogg"
        audio = message.get("audio")
        if isinstance(audio, dict) and audio.get("file_id"):
            return "audio", str(audio["file_id"]), str(audio.get("file_name") or "")
        video = message.get("video")
        if isinstance(video, dict) and video.get("file_id"):
            return "video", str(video["file_id"]), str(video.get("file_name") or "")
        animation = message.get("animation")
        if isinstance(animation, dict) and animation.get("file_id"):
            return "video", str(animation["file_id"]), str(animation.get("file_name") or "")
        sticker = message.get("sticker")
        if isinstance(sticker, dict) and sticker.get("file_id"):
            return "document", str(sticker["file_id"]), "sticker.webp"
        return None, None, None

    async def _download_to_cache(self, file_id: str, kind: str, file_name: Optional[str]) -> Optional[str]:
        """Download an inbound attachment and cache it for the agent's tools."""
        if self._client is None:
            return None
        info = await self._call("getFile", {"file_id": file_id}, timeout=30.0)
        if not info.ok or not isinstance(info.result, dict):
            logger.warning("[%s] getFile failed for %s: %s", self.name, file_id, info.error)
            return None
        file_path = str(info.result.get("file_path") or "")
        if not file_path:
            logger.warning("[%s] getFile returned no file_path for %s", self.name, file_id)
            return None
        try:
            blob = await download_bytes(self._client, self._file_url(file_path), max_bytes=DEFAULT_MEDIA_MAX_BYTES)
        except Exception as exc:
            logger.warning("[%s] media download failed (%s): %s", self.name, file_id, exc)
            return None
        try:
            validate_inbound_media_size(len(blob), media_type=kind)
        except ValueError as exc:
            logger.warning("[%s] %s", self.name, exc)
            return None
        name = Path(file_name or file_path).name or f"{kind}.bin"
        ext = Path(name).suffix or MEDIA_GROUP_EXT.get(mimetypes.guess_type(name)[0] or "", "")
        try:
            if kind == "image":
                return cache_image_from_bytes(blob, ext or ".jpg")
            if kind == "audio":
                return cache_audio_from_bytes(blob, ext or ".ogg")
            if kind == "video":
                return cache_video_from_bytes(blob, ext or ".mp4")
            return cache_document_from_bytes(blob, name)
        except ValueError as exc:
            logger.warning("[%s] refusing to cache %s: %s", self.name, name, exc)
            return None

    @staticmethod
    def _chat_type(chat: Dict[str, Any]) -> str:
        kind = str(chat.get("type") or "private").lower()
        if kind == "private":
            return "dm"
        if kind in {"group", "supergroup"}:
            return "group"
        return "channel"

    @staticmethod
    def _display_name(user: Dict[str, Any]) -> str:
        parts = [str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()]
        name = " ".join(p for p in parts if p).strip()
        return name or (f"@{user.get('username')}" if user.get("username") else "User")

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Normalise one Bale ``Message`` into a Hermes ``MessageEvent``."""
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id = clean_id(chat.get("id"))
        if not chat_id:
            return
        message_id = clean_id(message.get("message_id"))
        sender_id = clean_id(sender.get("id")) or None
        if sender_id and self._bot_id and sender_id == self._bot_id:
            # Checked before the dedup ring: our own posts must not poison it for the
            # user's later message carrying the same id (mirrors of our own sends).
            logger.debug("[%s] ignoring own message", self.name)
            return
        if self._dedup.is_duplicate(f"{chat_id}:{message_id}"):
            logger.debug("[%s] duplicate message %s in %s, skipping", self.name, message_id, chat_id)
            return
        chat_type = self._chat_type(chat)
        text = str(message.get("text") or message.get("caption") or "")
        media_paths: List[str] = []
        media_types: List[str] = []
        message_type = MessageType.TEXT
        kind, file_id, file_name = self._media_kind_and_id(message)
        if kind and file_id:
            path = await self._download_to_cache(file_id, kind, file_name)
            if path:
                media_paths.append(path)
                media_types.append(mimetypes.guess_type(path)[0] or f"{kind}/unknown")
                message_type = {
                    "image": MessageType.PHOTO, "audio": MessageType.VOICE,
                    "video": MessageType.VIDEO, "document": MessageType.DOCUMENT,
                }.get(kind, MessageType.DOCUMENT)
        if not text and not media_paths:
            if isinstance(message.get("location"), dict):
                loc = message["location"]
                text = f"[location] {loc.get('latitude')}, {loc.get('longitude')}"
                message_type = MessageType.LOCATION
            elif isinstance(message.get("contact"), dict):
                contact = message["contact"]
                text = f"[contact] {contact.get('first_name', '')} {contact.get('phone_number', '')}".strip()
            elif isinstance(message.get("sticker"), dict):
                text = "[sticker]"
                message_type = MessageType.STICKER
            else:
                text = "[unsupported message]"
        reply_to_message = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else None
        reply_author = (reply_to_message or {}).get("from") or {}
        source = self.build_source(
            chat_id=chat_id,
            chat_name=str(chat.get("title") or self._display_name(chat) or chat_id),
            chat_type=chat_type,
            user_id=sender_id,
            user_name=self._display_name(sender) if sender else None,
            message_id=message_id,
            is_bot=bool(sender.get("is_bot")),
        )
        timestamp = datetime.now(tz=timezone.utc)
        date = message.get("date")
        if date:
            try:
                timestamp = datetime.fromtimestamp(int(date), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                pass
        event = MessageEvent(
            text=text,
            message_type=message_type,
            user_id=sender_id,
            user_name=source.user_name,
            source=source,
            raw_message=message,
            message_id=message_id,
            media_urls=media_paths,
            media_types=media_types,
            reply_to_message_id=clean_id(reply_to_message.get("message_id")) if reply_to_message else None,
            reply_to_text=str(reply_to_message.get("text") or reply_to_message.get("caption") or "") or None
            if reply_to_message else None,
            reply_to_author_id=clean_id(reply_author.get("id")) or None if reply_to_message else None,
            reply_to_author_name=self._display_name(reply_author) if reply_to_message and reply_author else None,
            reply_to_is_own_message=bool(reply_to_message) and clean_id(reply_author.get("id")) == self._bot_id,
            platform_update_id=to_int(message.get("update_id"), 0) or None,
            timestamp=timestamp,
        )
        logger.debug("[%s] inbound %s/%s (%s): %s", self.name, chat_id, message_id, chat_type, text[:80])
        await self.handle_message(event)

    async def _handle_callback_query(self, callback: Dict[str, Any]) -> None:
        """Resolve a button press (exec approval / slash confirm / clarify)."""
        callback_id = clean_id(callback.get("id"))
        data = str(callback.get("data") or "")
        user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = clean_id(chat.get("id"))
        user_id = clean_id(user.get("id")) or None
        if not data:
            await self._answer_callback(callback_id)
            return
        if self._is_sender_authorized(user_id, self._chat_type(chat) if chat else "dm", chat_id) is False:
            await self._answer_callback(callback_id, UNAUTHORIZED, alert=True)
            return
        parts = data.split(":", 2)
        prefix = parts[0]
        if prefix == CB_APPROVAL and len(parts) == 3:
            await self._resolve_approval(
                callback_id, parts[1], parts[2], chat_id, user, clean_id(message.get("message_id")))
            return
        if prefix == CB_SLASH and len(parts) == 3:
            await self._resolve_slash_confirm(callback_id, parts[1], parts[2], chat_id)
            return
        if prefix == CB_CLARIFY and len(parts) == 3:
            await self._resolve_clarify(callback_id, parts[1], parts[2], chat_id)
            return
        logger.debug("[%s] unrecognised callback data: %s", self.name, data[:60])
        await self._answer_callback(callback_id)

    async def _answer_callback(self, callback_id: str, text: str = "", *, alert: bool = False) -> None:
        if not callback_id:
            return
        payload: Dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
            payload["show_alert"] = bool(alert)
        await self._call("answerCallbackQuery", payload, timeout=15.0)

    async def _resolve_approval(
        self, callback_id: str, choice: str, raw_id: str, chat_id: str, user: Dict[str, Any],
        prompt_message_id: str = "",
    ) -> None:
        """``ea:<choice>:<approval_id>`` — resolve a pending exec approval, then stamp the card."""
        approval_id = to_int(raw_id, 0)
        session_key = self._approval_state.pop(approval_id, None)
        if not session_key:
            await self._answer_callback(callback_id, "This approval was already resolved.", alert=True)
            return
        count = 0
        try:
            from tools.approval import resolve_gateway_approval

            count = resolve_gateway_approval(session_key, choice)
        except Exception as exc:
            logger.error("[%s] resolve_gateway_approval failed: %s", self.name, exc)
        if not count:
            # A tap that lands after the approval wait timed out must not claim success.
            await self._answer_callback(callback_id, "⌛ Approval expired — nothing was waiting.", alert=True)
            return
        label = {
            "once": "✅ Approved once", "session": "✅ Approved for session",
            "always": "✅ Approved permanently", "deny": "❌ Denied",
        }.get(choice, "Resolved")
        await self._answer_callback(callback_id, label)
        if prompt_message_id:
            await self._edit_message(chat_id, prompt_message_id, f"{label} by {self._display_name(user)}")
        if chat_id:
            self.resume_typing_for_chat(str(chat_id))

    async def _resolve_slash_confirm(self, callback_id: str, choice: str, confirm_id: str, chat_id: str) -> None:
        session_key = self._slash_confirm_state.pop(confirm_id, None)
        if not session_key:
            await self._answer_callback(callback_id, "This confirmation was already resolved.", alert=True)
            return
        try:
            from tools import slash_confirm as slash_confirm_mod

            await slash_confirm_mod.resolve(session_key, confirm_id, choice)
        except Exception as exc:
            logger.error("[%s] slash confirm resolve failed: %s", self.name, exc)
        await self._answer_callback(callback_id, {"once": "✅ Approved", "always": "🔒 Always",
                                                  "cancel": "❌ Cancelled"}.get(choice, "Resolved"))

    async def _resolve_clarify(self, callback_id: str, clarify_id: str, token: str, chat_id: str) -> None:
        session_key = self._clarify_state.get(clarify_id)
        if not session_key:
            await self._answer_callback(callback_id, "This question was already answered.", alert=True)
            return
        if token == "other":
            try:
                from tools.clarify_gateway import mark_awaiting_text

                mark_awaiting_text(clarify_id)
            except Exception as exc:
                logger.warning("[%s] mark_awaiting_text failed: %s", self.name, exc)
            await self._answer_callback(callback_id, "Type your answer as the next message.")
            return
        answer = self._clarify_options.get((clarify_id, to_int(token, -1)))
        if answer is None:
            await self._answer_callback(callback_id, "Unknown option.")
            return
        try:
            from tools.clarify_gateway import resolve_gateway_clarify

            resolved = resolve_gateway_clarify(clarify_id, answer)
        except Exception as exc:
            logger.error("[%s] resolve_gateway_clarify failed: %s", self.name, exc)
            resolved = False
        self._forget_clarify(clarify_id)
        await self._answer_callback(
            callback_id,
            f"✅ {answer}" if resolved else "This question was already answered.")

    def _forget_clarify(self, clarify_id: str) -> None:
        """Drop the per-prompt state for an ended clarify (cards, labels, text-capture)."""
        self._clarify_state.pop(clarify_id, None)
        for key in [k for k in self._clarify_options if k[0] == clarify_id]:
            self._clarify_options.pop(key, None)

    # -- outbound -----------------------------------------------------------

    def format_message(self, content: str) -> str:
        """Bale Markdown when enabled (opt-in), otherwise plain text."""
        return md_to_bale(content) if self._markdown else strip_markdown(content)

    def _text_payload(self, text: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"text": text}
        if self._markdown:
            payload["parse_mode"] = "Markdown"
        return payload

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None, **kwargs: Any,
    ) -> SendResult:
        """Send text, chunked to the platform cap; a rejected markup send is retried plain."""
        text = self.format_message(content or "")
        if not text.strip():
            return SendResult(success=True, message_id=None)
        chunks = split_message(text, self._max_len)
        last: Optional[SendResult] = None
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, **self._text_payload(chunk)}
            if index == 0 and reply_to:
                payload["reply_to_message_id"] = to_int(reply_to, 0) or reply_to
            result = await self._call("sendMessage", payload)
            if not result.ok and self._markdown and _looks_like_markup_error(result):
                logger.info("[%s] Markdown rejected, retrying as plain text", self.name)
                payload = {"chat_id": chat_id, "text": strip_markdown(chunk)}
                if index == 0 and reply_to:
                    payload["reply_to_message_id"] = to_int(reply_to, 0) or reply_to
                result = await self._call("sendMessage", payload)
            if not result.ok:
                return SendResult(
                    success=False,
                    error=result.error,
                    retryable=result.retryable,
                    raw_response=result,
                    error_kind=_error_kind(result),
                )
            last = SendResult(success=True, message_id=clean_id((result.result or {}).get("message_id"))
                              if isinstance(result.result, dict) else None, raw_response=result)
        return last or SendResult(success=True)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Bale's bot API has no chat-action method; nothing to show."""
        return None

    async def _upload(
        self, method: str, field: str, chat_id: str, file_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file with the method's own field name."""
        path = Path(file_path)
        if not path.is_file():
            return SendResult(success=False, error=f"file not found: {file_path}", error_kind="not_found")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload: Dict[str, Any] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = strip_markdown(caption)[:1000]
        if reply_to:
            payload["reply_to_message_id"] = to_int(reply_to, 0) or reply_to
        try:
            blob = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            return SendResult(success=False, error=f"cannot read {file_path}: {exc}")
        result = await self._call(
            method, payload, files={field: (path.name, blob, mime)}, timeout=180.0)
        if not result.ok:
            return SendResult(success=False, error=result.error, retryable=result.retryable,
                              error_kind=_error_kind(result))
        return SendResult(
            success=True,
            message_id=clean_id((result.result or {}).get("message_id")) if isinstance(result.result, dict) else None,
            raw_response=result,
        )

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                              **kwargs: Any) -> SendResult:
        return await self._upload("sendPhoto", "photo", chat_id, image_path, caption, reply_to, metadata)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Bale accepts a public URL for ``photo`` (``Sending by URL`` in the docs)."""
        if str(image_url).startswith(("http://", "https://")):
            payload: Dict[str, Any] = {"chat_id": chat_id, "photo": image_url}
            if caption:
                payload["caption"] = strip_markdown(caption)[:1000]
            if reply_to:
                payload["reply_to_message_id"] = to_int(reply_to, 0) or reply_to
            result = await self._call("sendPhoto", payload, timeout=60.0)
            if result.ok:
                return SendResult(success=True, raw_response=result)
            logger.debug("[%s] URL image rejected (%s), falling back to a link", self.name, result.error)
        return await super().send_image(chat_id, image_url, caption=caption, reply_to=reply_to, metadata=metadata)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> SendResult:
        return await self._upload("sendDocument", "document", chat_id, file_path, caption, reply_to, metadata)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Bale voices must be OGG/Opus and <= 1 MB (a larger file goes out as a document)."""
        try:
            size = Path(audio_path).stat().st_size
        except OSError:
            size = 0
        ext = Path(audio_path).suffix.lower()
        if ext not in {".ogg", ".oga", ".opus"} or size > 1024 * 1024:
            logger.info("[%s] sending %s as a document (voice needs ogg/opus <= 1MB)", self.name, Path(audio_path).name)
            return await self.send_document(chat_id, audio_path, caption=caption, reply_to=reply_to, metadata=metadata)
        return await self._upload("sendVoice", "voice", chat_id, audio_path, caption, reply_to, metadata)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        return await self._upload("sendVideo", "video", chat_id, video_path, caption, reply_to, metadata)

    async def _edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        """Edit a previously sent message (used to stamp a resolved prompt)."""
        if not chat_id or not message_id:
            return False
        result = await self._call("editMessageText", {
            "chat_id": chat_id, "message_id": to_int(message_id, 0) or message_id, "text": text,
        })
        return result.ok

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        result = await self._call("deleteMessage", {
            "chat_id": chat_id, "message_id": to_int(message_id, 0) or message_id,
        })
        return result.ok

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        result = await self._call("getChat", {"chat_id": chat_id}, timeout=20.0)
        if not result.ok or not isinstance(result.result, dict):
            return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}
        chat = result.result
        return {
            "name": str(chat.get("title") or self._display_name(chat) or chat_id),
            "type": self._chat_type(chat),
            "chat_id": clean_id(chat.get("id")) or str(chat_id),
        }

    # -- interactive prompts (native inline buttons) ------------------------

    def _approval_keyboard(self, prompt: ExecApprovalPrompt) -> Dict[str, Any]:
        approval_id = next(self._approval_counter)
        rows: List[List[Dict[str, str]]] = []
        row: List[Dict[str, str]] = []
        for label, choice, _style in prompt.actions:
            row.append({"text": label, "callback_data": f"{CB_APPROVAL}:{choice}:{approval_id}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        self._approval_state[approval_id] = prompt.session_key
        return {"inline_keyboard": rows}

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Inline-keyboard approval; buttons resolve via ``tools.approval``."""
        keyboard = self._approval_keyboard(prompt)
        payload: Dict[str, Any] = {"chat_id": prompt.chat_id, "text": strip_markdown(prompt.text),
                                   "reply_markup": keyboard}
        result = await self._call("sendMessage", payload)
        if not result.ok:
            # Drop the entry we optimistically stored, then let the gateway fall back to text.
            for key, value in list(self._approval_state.items()):
                if value == prompt.session_key:
                    self._approval_state.pop(key, None)
            return SendResult(success=False, error=result.error, retryable=result.retryable)
        return SendResult(
            success=True,
            message_id=clean_id((result.result or {}).get("message_id")) if isinstance(result.result, dict) else None)

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str,
                                 confirm_id: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Three-button slash-command confirmation."""
        text = f"{title}\n\n{strip_markdown(message)}" if title else strip_markdown(message)
        keyboard = {"inline_keyboard": [
            [{"text": "✅ Approve Once", "callback_data": f"{CB_SLASH}:once:{confirm_id}"},
             {"text": "🔒 Always Approve", "callback_data": f"{CB_SLASH}:always:{confirm_id}"}],
            [{"text": "❌ Cancel", "callback_data": f"{CB_SLASH}:cancel:{confirm_id}"}],
        ]}
        result = await self._call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": keyboard})
        if not result.ok:
            return SendResult(success=False, error=result.error, retryable=result.retryable)
        self._slash_confirm_state[confirm_id] = session_key
        return SendResult(success=True)

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Clarify prompt: one button per choice plus ``Other``; the text fallback still works."""
        text = f"❓ {strip_markdown(question)}"
        keyboard: Optional[Dict[str, Any]] = None
        if choices:
            self._clarify_state[clarify_id] = session_key
            if len(self._clarify_options) > 500:  # bound the label map
                self._clarify_options.clear()
            rows: List[List[Dict[str, str]]] = []
            for index, choice in enumerate(choices):
                label = str(choice)
                text += f"\n{index + 1}. {label}"
                self._clarify_options[(clarify_id, index)] = label
                rows.append([{"text": label[:60], "callback_data": f"{CB_CLARIFY}:{clarify_id}:{index}"}])
            rows.append([{"text": "✏️ Other (type answer)", "callback_data": f"{CB_CLARIFY}:{clarify_id}:other"}])
            keyboard = {"inline_keyboard": rows}
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:self._max_len]}
        if keyboard:
            payload["reply_markup"] = keyboard
        result = await self._call("sendMessage", payload)
        if not result.ok:
            return SendResult(success=False, error=result.error, retryable=result.retryable)
        if choices:
            # Let the gateway intercept a typed answer as well as a button press.
            try:
                from tools.clarify_gateway import mark_awaiting_text

                mark_awaiting_text(clarify_id)
            except Exception as exc:
                logger.debug("[%s] mark_awaiting_text unavailable: %s", self.name, exc)
        return SendResult(success=True)

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        """Forget a clarify prompt the gateway ended without a press."""
        self._forget_clarify(clarify_id)


# ``choices`` labels are kept next to their clarify id so a button press resolves to text.
def _looks_like_markup_error(result: _ApiResult) -> bool:
    """True when Bale rejected our Markdown rendering (retry as plain text)."""
    text = (result.error or "").lower()
    return "entit" in text or "parse" in text or "markdown" in text or result.error_code == 400


def _error_kind(result: _ApiResult) -> str:
    """Map an API failure onto Hermes' platform-neutral ``SEND_ERROR_KINDS``."""
    text = (result.error or "").lower()
    if result.error_code == 429 or "flood" in text or "too many" in text:
        return "rate_limited"
    if result.error_code == 403 or "forbidden" in text or "blocked" in text:
        return "forbidden"
    if result.error_code == 404 or "not found" in text:
        return "not_found"
    if result.retryable:
        return "transient"
    if "too long" in text:
        return "too_long"
    return "unknown"


async def _standalone_send(
    pconfig: Any, chat_id: str, message: str, *, thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None, force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process send for cron / ``hermes send`` when no gateway is running."""
    if httpx is None:
        return send_error("bale standalone send: httpx is not installed")
    extra = getattr(pconfig, "extra", {}) or {}
    token = str(extra_or_secret(extra, "token", "BALE_BOT_TOKEN") or "").strip()
    if not token:
        return send_error("bale standalone send: BALE_BOT_TOKEN is not configured")
    api_base = _api_base(extra)
    target = str(chat_id or "").strip()
    if not target:
        return send_error("bale standalone send: no chat_id")
    markdown = truthy(extra_or_secret(extra, "markdown", "BALE_MARKDOWN", False))
    payload: Dict[str, Any] = {"chat_id": to_int(target, 0) or target, "text": (message or "")[:MAX_MESSAGE_LENGTH]}
    if markdown:
        payload["text"] = md_to_bale(payload["text"])
        payload["parse_mode"] = "Markdown"
    else:
        payload["text"] = strip_markdown(payload["text"])
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{api_base}/bot{token}/sendMessage", json=payload)
            data = resp.json() if resp.content else {}
        if not isinstance(data, dict) or not data.get("ok"):
            detail = (data or {}).get("description") if isinstance(data, dict) else resp.text[:200]
            return send_error(f"bale HTTP {resp.status_code}: {detail}")
        result = data.get("result") or {}
        return {
            "success": True, "platform": "bale", "chat_id": str(payload["chat_id"]),
            "message_id": str(result.get("message_id", "")) if isinstance(result, dict) else None,
        }
    except Exception as exc:
        return send_error(f"bale standalone send failed: {exc}")


def probe_bot_token(token: str) -> tuple:
    """Verify a Bale bot token against ``getMe`` (best effort; never raises)."""
    from .wizard import http_probe

    base = str(extra_or_secret({}, "api_base", "BALE_API_BASE", DEFAULT_API_BASE) or "").strip()
    return http_probe(f"{(base or DEFAULT_API_BASE).rstrip('/')}/bot{token.strip()}/getMe", style="bale")


def interactive_setup() -> None:
    """``hermes gateway setup`` flow for Bale (registered as ``setup_fn``).

    Without a ``setup_fn`` the wizard falls through to a static env-var hint and redraws the platform
    menu, so selecting Bale looks like "the page reloaded and showed the same menu".
    """
    from .wizard import interactive_setup as _setup

    _setup(
        label="Bale (بله)",
        token_env="BALE_BOT_TOKEN",
        token_question="Bale bot token",
        how_to_get="Create a bot in Bale: open @BotFather, send /newbot and copy the token.",
        docs_url="https://docs.bale.ai/",
        probe=probe_bot_token,
        allowed_env="BALE_ALLOWED_USERS",
        allow_all_env="BALE_ALLOW_ALL_USERS",
        home_env="BALE_HOME_CHANNEL",
        allowed_question="Allowed Bale user IDs (comma-separated, leave empty to deny everyone)",
        allowed_example="Bale user IDs are numeric, e.g. 123456789 (ask the bot, or check getUpdates).",
        home_question="Home channel (chat/user ID, leave empty to set later)",
        api_base_env="BALE_API_BASE",
        api_base_question="Bale API base URL (default https://tapi.bale.ai)",
    )


def register_bale(ctx: Any) -> None:
    """Register the ``bale`` platform with the Hermes plugin context."""
    ctx.register_platform(
        name="bale",
        label="Bale (بله)",
        adapter_factory=BaleAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["BALE_BOT_TOKEN"],
        install_hint="No extra packages needed (httpx ships with Hermes)",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="BALE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="BALE_ALLOWED_USERS",
        allow_all_env="BALE_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🟢",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting on Bale (بله), an Iranian messenger whose bot API mirrors "
            "Telegram's. Messages are plain text by default (Markdown is opt-in and fragile), "
            "so avoid heavy formatting: no tables, keep code blocks short. There are no threads "
            "and no typing indicator. Images, documents, voice and video work in both "
            "directions; voice notes must be OGG/Opus under 1 MB. Long answers are chunked "
            f"around {MAX_MESSAGE_LENGTH} characters. Inline buttons are used for approvals and "
            "clarify questions; users can always answer by typing."
        ),
    )
