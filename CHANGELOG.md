# Changelog

All notable changes to the Iranian messengers plugin (Bale + Rubika) for Hermes Agent.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: semver.

## [1.0.0] — 2026-09-23

First public release: two Hermes gateway platforms, `bale` and `rubika`.

### Added

- **Bale adapter** (`bale.py`) — long polling with `getUpdates`, `{ok, result}` envelope,
  `sendMessage` with chunking, bidirectional media (`getFile` + `/file/bot<token>/<path>`
  download; `sendPhoto`/`sendDocument`/`sendVoice`/`sendVideo` uploads), inline-keyboard prompts
  for exec approvals, `clarify` questions and slash-command confirmations answered through
  `callback_query`, and an automatic plain-text retry when Bale rejects Markdown markup.
- **Rubika adapter** (`rubika.py`) — polling with `offset_id`, `{status, data}` envelope,
  `requestSendFile` → multipart upload → `sendFile` media flow, inbound `getFile` downloads,
  chat-keypad button prompts (`hz:<token>` ids resolved locally) for approvals/clarify/slash
  confirmations, per-chat `getChat` type caching, markdown stripping (Rubika renders plain text).
- **Shared core** (`common.py`) — supervised polling loop with capped exponential backoff and
  jitter, length-aware chunking with truncation guard, Markdown conversion/stripping, size-capped
  media download, id/env helpers, platform-YAML flattening.
- **Plugin plumbing** — `plugin.yaml` manifest (required/optional env, `python_dependencies:
  httpx>=0.28`), `register(ctx)` entry point registering both platforms with allowlists,
  cron home channels, standalone senders (cron outside the gateway), platform hints and PII-safe
  session metadata.
- **Tests** — 53 tests, no network and no tokens: an `aiohttp` mock plays both platform APIs
  (inbound/outbound, uploads/downloads, buttons, errors) and records every request for wire-level
  assertions. `pytest-asyncio` is not required.
- **Docs** — Persian `README.md` (primary) and English `README.en.md` with install, token setup,
  configuration, env tables, security, cron, architecture, limitations, troubleshooting and FAQ;
  MIT `LICENSE`; `.env.example`; CI workflow with static checks and a Hermes-gated test job.
- **Smoke test** (`scripts/smoke_test.py`) — drives both adapters through Hermes' real machinery
  (plugin discovery from a throwaway `HERMES_HOME`, the platform registry, `create_adapter`,
  connect, outbound send, inbound delivery to the gateway message handler, cron sender) against a
  local mock API. Verified output: `SMOKE OK — discovery + registry + adapter + wire + cron sender
  verified for: bale, rubika`.

### Notes

- Default text mode is plain (Markdown is opt-in on Bale via `BALE_MARKDOWN`), because Bale's
  legacy Markdown is spacing-sensitive and Rubika ignores markup entirely.
- Media caps: Rubika images ≤ 10 MB, files/videos ≤ 50 MB; Bale voice notes must be OGG/Opus
  under 1 MB.
- No typing indicator or thread support: neither platform API exposes a chat-action or thread id.
- Keep the repository folder name underscored (`hermes_iran_messengers`); a hyphenated directory
  name breaks `pytest` collection because the plugin root is itself a Python package.
