"""Interactive ``hermes gateway setup`` flow for the Bale and Rubika platforms.

Why this module exists: ``hermes_cli.gateway_setup_wizard._configure_platform`` dispatches, in order,
(1) the registry entry's ``setup_fn``, (2) a built-in setup function keyed by platform name, (3) the
static ``vars`` list, and (4) a "set these env vars yourself" hint. A plugin that registers no
``setup_fn`` therefore falls through to (4): selecting the platform prints one hint line and redraws
the platform menu — which looks like the wizard reloading and losing the selection. Registering
``setup_fn=interactive_setup`` is what makes the wizard actually walk the user through the token,
the allowlist, and the home channel.

All ``hermes_cli`` imports are lazy so the adapters stay importable outside the CLI (tests, gateway).
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

# ``probe(token) -> (ok, detail)``: verifies a bot token against the live API.
Probe = Callable[[str], Tuple[bool, str]]


def _cli():
    """Import the CLI prompt helpers; raises ``ImportError`` outside a Hermes CLI install."""
    from hermes_cli import setup as cli_setup
    from hermes_cli import setup_platforms as cli_platforms

    return cli_setup, cli_platforms


def http_probe(url: str, *, style: str) -> Tuple[bool, str]:
    """Ask the platform API who the token belongs to (never raises).

    ``style="bale"`` expects the Telegram-shaped ``{"ok": true, "result": {...}}`` envelope,
    ``style="rubika"`` expects ``{"status": "OK", "data": {"bot": {...}}}``.
    """
    try:
        import httpx

        response = httpx.post(url, json={}, timeout=15.0)
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — any transport/parse failure is just "unverified"
        return False, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    if style == "rubika":
        if not isinstance(payload, dict) or payload.get("status") != "OK":
            detail = ""
            if isinstance(payload, dict):
                detail = str((payload.get("data") or {}).get("message") or payload.get("status") or "")
            return False, detail or "unexpected response"
        bot = ((payload.get("data") or {}).get("bot") or {})
        name = bot.get("username") or bot.get("bot_id") or bot.get("title") or "bot"
        return True, f"connected as {name}"

    if isinstance(payload, dict) and payload.get("ok"):
        result = payload.get("result") or {}
        name = result.get("username") or result.get("first_name") or "bot"
        return True, f"connected as @{str(name).lstrip('@')} (id {result.get('id', '?')})"
    description = payload.get("description") if isinstance(payload, dict) else None
    return False, str(description or "unexpected response")


def interactive_setup(
    *,
    label: str,
    token_env: str,
    token_question: str,
    how_to_get: str,
    docs_url: str,
    probe: Probe,
    allowed_env: str,
    allow_all_env: str,
    home_env: str,
    allowed_question: str,
    allowed_example: str,
    home_question: str,
    api_base_env: str = "",
    api_base_question: str = "",
) -> None:
    """Walk the user through token, access control and home channel for one platform."""
    cli_setup, cli_platforms = _cli()

    print_header = cli_setup.print_header
    print_info = cli_setup.print_info
    print_warning = cli_setup.print_warning
    print_success = cli_setup.print_success
    print_error = cli_setup.print_error
    prompt = cli_setup.prompt
    prompt_yes_no = cli_setup.prompt_yes_no
    save_env_value = cli_setup.save_env_value
    get_env_value = cli_setup.get_env_value

    print_header(label)
    if cli_platforms.declines_reconfigure(label, f"Reconfigure {label}?", token_env):
        return

    print_info(how_to_get)
    if docs_url:
        print_info(f"   Docs: {docs_url}")
    print()

    token = ""
    for _ in range(3):
        answer = (prompt(token_question, password=True) or "").strip()
        if not answer:
            print_warning("A bot token is required — skipping setup")
            return
        ok, detail = probe(answer)
        if ok:
            print_success(f"Bot token verified — {detail}")
            token = answer
            break
        print_error(f"Could not verify that token: {detail}")
        if prompt_yes_no("Save it anyway (the API may be unreachable from here)?", False):
            token = answer
            break
    if not token:
        print_warning("No verified token — skipping setup")
        return

    save_env_value(token_env, token)
    print_success(f"{label} token saved")

    print()
    print_info("🔒 Access control: who may talk to the bot", f"   {allowed_example}")
    allowed = ""
    if prompt_yes_no("Allow everyone who can reach the bot to use it?", False):
        save_env_value(allow_all_env, "true")
        save_env_value(allowed_env, "")
        print_warning("Open access — anyone who finds your bot can use it.")
    else:
        save_env_value(allow_all_env, "false")
        allowed = (prompt(allowed_question, default=get_env_value(allowed_env) or "") or "").strip()
        if allowed:
            cli_platforms._save_allowlist(allowed_env, allowed, f"{label} allowlist configured")
        else:
            print_warning("No allowlist set — the bot will ignore every user until you allow one.")
            print_info("   Tip: send the bot a message, then run 'hermes pairing list' and approve yourself.")

    print()
    print_info("📬 Home channel: where cron results and notifications are delivered")
    first = allowed.replace(" ", "").split(",")[0] if allowed else ""
    if first and prompt_yes_no(f"Use {first} as the home channel?", True):
        save_env_value(home_env, first)
        print_success(f"{label} home channel set to {first}")
    else:
        cli_platforms.save_prompted(home_env, home_question, skip_msg="Home channel not set (you can set it later)")

    if api_base_env and api_base_question:
        print()
        if prompt_yes_no("Route through a custom API base URL (proxy, self-hosted mirror)?", False):
            cli_platforms.save_prompted(
                api_base_env, api_base_question, transform=lambda value: value.strip().rstrip("/"),
                skip_msg="No API base URL set — using the default",
            )

    print()
    print_success(f"{label} configured")
    print_info("   Restart the gateway to connect: hermes gateway restart")


__all__ = ["Probe", "http_probe", "interactive_setup"]
