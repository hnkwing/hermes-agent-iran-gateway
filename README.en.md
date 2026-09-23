# Iranian Messengers for Hermes (Bale + Rubika)

A **Hermes Agent** gateway plugin that adds two Iranian messengers — **Bale** and **Rubika** — as gateway platforms, exactly the way Telegram works: create a bot once, hand Hermes its token, then talk to your Hermes agent from inside Bale/Rubika (text, photos, files, voice, video) and approve risky shell commands with a button tap.

No extra dependencies: only `httpx`, which ships with Hermes.

> نسخهٔ فارسی: [README.md](README.md)

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Getting a bot token](#getting-a-bot-token)
- [Configuration](#configuration)
- [Environment variables](#environment-variables)
- [Running and using it](#running-and-using-it)
- [Security and access control](#security-and-access-control)
- [Cron and outbound sends](#cron-and-outbound-sends)
- [Buttons: approvals and questions](#buttons-approvals-and-questions)
- [Architecture and wire details](#architecture-and-wire-details)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Development and tests](#development-and-tests)
- [Project layout](#project-layout)
- [License](#license)

---

## Features

| Capability | Bale | Rubika |
|---|---|---|
| Two-way text | ✅ | ✅ |
| Inbound via long polling (no HTTPS webhook needed) | ✅ | ✅ |
| Send photo / document / voice / video | ✅ | ✅ |
| Receive photo / document / voice / video (auto-download + cache) | ✅ | ✅ |
| Buttons for exec approval and clarify questions | ✅ (inline keyboard) | ✅ (chat keypad) |
| Typed fallback for every button (`1`, `2`, …) | ✅ | ✅ |
| Automatic chunking of long answers | ✅ | ✅ |
| Allowlist + DM pairing | ✅ | ✅ |
| Home channel for cron delivery (`deliver=bale/rubika`) | ✅ | ✅ |
| Message edit / delete | ✅ (`editMessageText`) | ✅ (`editMessageText`, `deleteMessage`) |
| Typing indicator | ❌ (no API) | ❌ (no API) |

Both platforms expose a Telegram-shaped HTTP bot API (`getUpdates` / `sendMessage` / `getFile`, JSON bodies), so this plugin mirrors the behaviour of Hermes' built-in Telegram adapter: bidirectional media, allowlists, cron home channels, button prompts and message chunking.

---

## Requirements

- **Hermes Agent** installed and working (`hermes --version`).
- Python 3.10+ (the interpreter Hermes itself runs on).
- `httpx` — already a Hermes dependency, nothing to install.
- A bot in Bale and/or Rubika, with its token.

---

## Install

### Option 1 — install from GitHub (recommended)

```bash
hermes plugins install https://github.com/<username>/hermes_iran_messengers --enable
```

### Option 2 — manual copy

```bash
git clone https://github.com/<username>/hermes_iran_messengers
cp -r hermes_iran_messengers ~/.hermes/plugins/
hermes plugins enable iran-messengers
```

Verify:

```bash
hermes plugins list
hermes plugins show iran-messengers
```

> ⚠️ **Keep the folder name underscored (`_`).** The plugin directory is itself a Python package (it has `__init__.py`); with a hyphenated folder name `pytest` cannot collect the tests (Hermes itself is fine with hyphens — only the test runner cares). If you cloned it into a hyphenated directory and want to run the suite, rename it to `hermes_iran_messengers`.

> ℹ️ **For reviewers:** `hermes plugins validate .` passes on this plugin. The security scan reports one *caution* for Persian text: `invisible_unicode` from the zero-width non-joiner (`U+200C`), which correct Persian orthography requires. The code, `plugin.yaml` and `.env.example` contain no invisible characters, and a test enforces that (`tests/test_registration.py::test_plugin_yaml_has_no_invisible_unicode`).

### Uninstall

```bash
hermes plugins disable iran-messengers
hermes plugins remove iran-messengers
```

---

## Getting a bot token

### Bale

1. Open the **BotFather** bot inside Bale (see <https://docs.bale.ai/>).
2. Create a bot with `/newbot`, pick a name and username.
3. Copy the token it returns → `BALE_BOT_TOKEN`.

### Rubika

1. Open the **BotFather** bot inside Rubika (docs: <https://rubika.ir/botapi>).
2. Create a bot and copy its token → `RUBIKA_BOT_TOKEN`.
3. Enable group messaging in the bot's privacy settings if you need it.

Never paste a token into a chat, screenshot, commit or issue. Keep it in `~/.hermes/.env` or `config.yaml` only.

---

## Configuration

### 0) The `hermes gateway setup` wizard (easiest)

```bash
hermes gateway setup        # pick "Bale (بله)" or "Rubika (روبیکا)" from the platform menu
```

The wizard prompts for the token, **verifies it against the live API** (a bad token produces a warning
and lets you save it anyway), configures the allowed users and the home channel, and tells you to
restart the gateway. Everything it saves lands in `~/.hermes/.env`.

> ⚠️ If the plugin copy you installed has no `wizard.py` (revisions older than this repo), selecting
> Bale/Rubika in the wizard only prints one hint line and returns to the same platform menu — which
> looks like the page reloading. `git pull` and re-copy the folder into `~/.hermes/plugins/`; see
> `CHANGELOG.md`.

### a) Env file (without the wizard)

```bash
hermes config set BALE_BOT_TOKEN "123456:AAA..."
hermes config set RUBIKA_BOT_TOKEN "ABC..."
```

Or edit `~/.hermes/.env` directly:

```dotenv
BALE_BOT_TOKEN=123456:AAA...
RUBIKA_BOT_TOKEN=ABC...
BALE_ALLOWED_USERS=123456789
BALE_HOME_CHANNEL=123456789
RUBIKA_ALLOW_ALL_USERS=false
```

### b) `config.yaml`

Both shapes are supported — keys directly under the platform name, or under `extra`:

```yaml
platforms:
  bale:
    enabled: true
    extra:
      token: "123456:AAA..."
      api_base: "https://tapi.bale.ai"   # only for a proxy/mirror
      markdown: false                    # default: plain text
      poll_timeout: 25
  rubika:
    enabled: true
    extra:
      token: "ABC..."
      api_base: "https://botapi.rubika.ir/v3"
      poll_interval: 3
      interactive: true                  # keypad buttons for prompts
```

Environment variables always win over `config.yaml`.

### c) Wizard / dashboard

```bash
hermes gateway setup      # pick Bale or Rubika and paste the token
```

Or in the Hermes dashboard (<http://127.0.0.1:9119>) → **Messaging** tab.

---

## Environment variables

`<PREFIX>` is `BALE` or `RUBIKA`.

| Variable | Meaning | Default |
|---|---|---|
| `<PREFIX>_BOT_TOKEN` | Bot token (required) | — |
| `<PREFIX>_ALLOWED_USERS` | Comma-separated allowed user IDs | empty (access closed) |
| `<PREFIX>_ALLOW_ALL_USERS` | Allow anyone (dev only) | `false` |
| `<PREFIX>_HOME_CHANNEL` | Default chat for cron results / notifications | empty |
| `<PREFIX>_HOME_CHANNEL_NAME` | Display name for the home channel | `Home` |
| `<PREFIX>_API_BASE` | API base URL (proxy / self-hosted mirror) | platform default |
| `<PREFIX>_MAX_MESSAGE_LENGTH` | Chunking cap per message | `4096` |

Bale only:

| Variable | Meaning | Default |
|---|---|---|
| `BALE_MARKDOWN` | Send with Bale's `*bold*` Markdown (fragile) | `false` (plain) |
| `BALE_POLL_TIMEOUT` | Long-poll timeout in seconds | `25` |

Rubika only:

| Variable | Meaning | Default |
|---|---|---|
| `RUBIKA_POLL_INTERVAL` | Seconds between `getUpdates` calls | `3` |
| `RUBIKA_INTERACTIVE` | Render prompts as chat-keypad buttons | `true` |

---

## Running and using it

```bash
hermes gateway run        # foreground (recommended on WSL/Docker/Termux)
hermes gateway install    # install as a systemd/launchd service
hermes gateway start|stop|restart|status
hermes gateway setup      # configure platforms
```

Message the bot from inside Bale/Rubika; every message becomes a Hermes session (exactly like Telegram). Hermes slash commands (`/status`, `/model`, …) work as well.

Sending without a running gateway (bot-token platforms):

```bash
hermes send --to bale:123456789 "hello from a script"
hermes send --to rubika:u0ABC... "build finished: OK"
hermes send --list | grep -E 'bale|rubika'
```

---

## Security and access control

Secure by default: **until the allowlist is filled or a DM pair is approved, nobody can use the bot.**

- Allowlist: `BALE_ALLOWED_USERS=111,222`, `RUBIKA_ALLOWED_USERS=...`
- Open access (test environments only):

```bash
hermes config set BALE_ALLOW_ALL_USERS true     # ⚠️ anyone can ping the bot
```

- Manage access requests:

```bash
hermes pairing list
hermes pairing approve <platform> <request-id|code>
hermes pairing revoke <platform> <user-id>
```

- Approvals for dangerous commands (`rm -rf`, …) are button prompts, accepted only from authorised users.

---

## Cron and outbound sends

```bash
hermes config set BALE_HOME_CHANNEL <chat_id>
hermes config set RUBIKA_HOME_CHANNEL <chat_id>
hermes cron add --schedule "0 9 * * *" --deliver bale "send me today's summary"
```

Cron uses the plugin's `standalone_sender_fn`, so scheduled sends work even when no gateway process is co-resident.

---

## Buttons: approvals and questions

When Hermes needs approval before running a command, the prompt arrives with buttons:

- **Bale:** an inline keyboard (`Approve once / Always / Deny`);
- **Rubika:** a one-time chat keypad under the composer.

In both cases **typing the answer works too** (option number or label), so a failed tap never blocks you. Approvals are only accepted from authorised users, and the prompt is edited/cleaned up afterwards.

---

## Architecture and wire details

```
inbound                agent / tools                 outbound
   │                        │                          ▲
   ▼                        ▼                          │
poll loop ──► MessageEvent ──► Hermes (session, LLM, tools) ──┴──► sendMessage/sendFile
```

### Bale

- Base: `https://tapi.bale.ai/bot<token>/<Method>`, JSON body, `{"ok": true, "result": ...}`.
- Inbound: `getUpdates` with `offset` and long polling (25 s default); each `update_id` is acked after handling.
- Media in: `getFile` → `file_path` → download `https://tapi.bale.ai/file/bot<token>/<file_path>` → local cache.
- Media out: `sendPhoto` / `sendDocument` / `sendVoice` / `sendVideo` (multipart upload).
- Buttons: `reply_markup.inline_keyboard`; presses arrive as `callback_query` and are acked with `answerCallbackQuery`.
- Text: plain by default. With `BALE_MARKDOWN=true` it sends `parse_mode=Markdown` and **retries the same chunk as plain text if the platform rejects the markup** (Bale's Markdown is spacing-sensitive and fragile).

### Rubika

- Base: `https://botapi.rubika.ir/v3/<token>/<Method>`, `{"status": "OK", "data": {...}}`.
- Inbound: `getUpdates` with `offset_id` (polling every 3 s by default — no long polling) and `next_offset_id`.
- Update types: `NewMessage`, `UpdatedMessage`, `RemovedMessage`, `StartedBot`, `StoppedBot`.
- Chat type (private/group/channel) is not in the update, so `getChat` is called once per chat and cached.
- Media in: `getFile(file_id)` → `download_url` → download → cache.
- Media out: `requestSendFile(type)` → `upload_url` → multipart upload → `sendFile(chat_id, file_id, text)`. Limits: image ≤ 10 MB, file/video ≤ 50 MB.
- Buttons: Rubika has no inline keyboard, so prompts use a **chat keypad** (`sendMessage` + `chat_keypad`, `one_time_keyboard`) with our own button ids (`hz:<token>`). A press returns as a `NewMessage` carrying `aux_data.button_id`; the meaning is looked up locally and forwarded to Hermes' `resolve_gateway_approval` / `resolve_gateway_clarify`, then the keypad is removed with `editChatKeypad(Remove)`.
- Text: Rubika renders plain text only (styling needs its metadata API), so Markdown is **stripped** before sending.

### Files

| File | Purpose |
|---|---|
| `plugin.yaml` | Manifest: name, required tokens, optional env, `python_dependencies` |
| `__init__.py` | Plugin entry point → `adapter.py` |
| `adapter.py` | `register(ctx)` — registers both platforms in the Hermes registry |
| `bale.py` | Full Bale adapter (`BaleAdapter`) |
| `rubika.py` | Full Rubika adapter (`RubikaAdapter`) |
| `common.py` | Shared bits: poll loop with backoff, chunking, Markdown helpers, media download |
| `tests/` | 53 tests against a mock API server (no real network) |

---

## Limitations

- **No typing indicator** — neither API has a chat-action method.
- **No threads/topics** — one session per chat.
- **No visible streaming** — long answers arrive complete (or as chunks).
- **Bale Markdown is fragile** — plain text by default; stick to `*bold*` and `` `code` `` if you enable it.
- **Rubika has no styling** — output is always plain text.
- **Edits** — Rubika delivers `UpdatedMessage` but Hermes does not process edits as new turns (they are ignored to avoid duplicate replies).
- **Media caps** — Rubika 10 MB images / 50 MB files; Bale voice notes must be OGG/Opus under 1 MB.
- If `tapi.bale.ai` or `botapi.rubika.ir` are unreachable on your network, point `*_API_BASE` at a proxy/mirror.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `getMe` rejected / `token rejected` | Wrong or revoked token — re-issue it from BotFather. |
| Gateway says the platform is not enabled | The token is missing from `~/.hermes/.env`; set it and restart the gateway. |
| Messages arrive but the bot never answers | The user is not allowlisted; check `hermes pairing list` / `*_ALLOWED_USERS`. |
| Bale: `can't parse entities` | Markdown was sent; set `BALE_MARKDOWN=false` (the adapter also retries plain automatically). |
| `429` / `TOO_REQUESTS` | Platform rate limit; the adapter backs off and retries. |
| Media is not downloaded | `getFile` failed or the file exceeds the size cap — check the gateway log. |
| Rubika sends no buttons | `RUBIKA_INTERACTIVE=false`; enable it or answer by typing the option number. |
| `attempted relative import with no known parent package` when running tests | The repo folder has a hyphen; rename it to `hermes_iran_messengers`. |

Logs:

```bash
hermes gateway status
journalctl --user -u hermes-gateway -f     # when installed as a service
hermes gateway run                          # foreground, live logs
```

---

## FAQ

**1. Do I need a webhook or HTTPS?**
No. Both platforms are polled, which is exactly why this works on a home server with no public domain.

**2. Can I enable both at once?**
Yes — the plugin registers two independent platforms (`bale`, `rubika`). Enabling only one is fine too (leave the other token unset).

**3. How is this different from Telegram?**
From the user's point of view: almost identical — same commands, buttons, media, allowlists. The differences are the missing typing indicator/threads and Bale's weak Markdown.

**4. Where do I put secrets?**
Only `~/.hermes/.env` or `config.yaml`. Never in a chat, commit or screenshot.

**5. Does it work on older Hermes versions?**
It uses the `register_platform` plugin API. If `hermes plugins validate .` complains, update Hermes.

---

## Development and tests

```bash
cd hermes_iran_messengers
~/.hermes/hermes-agent/venv/bin/python -m pytest        # 53 tests
hermes plugins validate .                                # manifest + load check
hermes plugins doctor .                                  # real runtime contracts
```

### Smoke test (end-to-end through Hermes' own machinery)

`scripts/smoke_test.py` drives the plugin the way the gateway does — discovery from a throwaway `HERMES_HOME`, registry lookup, `create_adapter`, connect, outbound send, inbound delivery to the gateway message handler, and the cron/standalone sender — against a local mock API, with no real credentials:

```bash
export SMOKE=/tmp/hermes_smoke
mkdir -p $SMOKE/plugins && cp -r . $SMOKE/plugins/hermes_iran_messengers
HERMES_HOME=$SMOKE hermes plugins enable iran-messengers

HERMES_HOME=$SMOKE ~/.hermes/hermes-agent/venv/bin/python scripts/smoke_test.py
# → SMOKE OK — discovery + registry + adapter + wire + cron sender verified for: bale, rubika
```

The suite needs no network and no tokens: an `aiohttp` mock plays both platform APIs (inbound/outbound, file upload/download, buttons, errors) and records every request so tests can assert on the wire payload. `pytest-asyncio` is not required — async tests run through `asyncio.run`.

> The adapter tests import `gateway.*` from a Hermes install, so **the full suite requires Hermes Agent to be installed**. When Hermes is absent (e.g. a bare CI runner), the GitHub workflow runs the static checks and explicitly skips the suite with a notice instead of pretending it passed.

> Run the commands from the repo root with no arguments (`python -m pytest`): `pytest.ini` points `testpaths` at `tests/`.

To add a feature: keep shared logic in `common.py`, keep the adapters thin, and add a mock-server test per new behaviour.

---

## Project layout

```
hermes_iran_messengers/
├── plugin.yaml            # plugin manifest
├── __init__.py            # entry point → register
├── adapter.py             # platform registration (register(ctx))
├── bale.py                # Bale adapter
├── rubika.py              # Rubika adapter
├── common.py              # shared helpers
├── pytest.ini
├── tests/
│   ├── conftest.py        # temp HERMES_HOME, mock API server, async test support
│   ├── test_common.py
│   ├── test_bale.py
│   ├── test_rubika.py
│   └── test_registration.py
├── README.md              # Persian (primary)
└── README.en.md           # this file
```

---

## References

- Bale docs: <https://docs.bale.ai/> — API base `https://tapi.bale.ai` (swagger: `/swagger/swagger.json`)
- Rubika docs: <https://rubika.ir/botapi>, <https://rubika.ir/botapi/methods>, <https://rubika.ir/botapi/models>
- Hermes Agent: <https://hermes-agent.nousresearch.com/docs> — platform guide: `gateway/platforms/ADDING_A_PLATFORM.md` in the Hermes source
- Reference Rubika Python client: <https://github.com/rubika-bot-api/rubika_bot_api>

## License

MIT — see [LICENSE](LICENSE).
