"""``hermes gateway setup`` flow: the registry must expose a ``setup_fn`` and it must save settings.

Regression guard: with no ``setup_fn`` the wizard's ``_configure_platform`` falls through to the
static "set these env vars yourself" hint and redraws the platform menu, which users report as the
setup page reloading and showing the same menu again.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

pytestmark = pytest.mark.usefixtures("plugin_ctx")


class _CLIStub:
    """Stands in for ``hermes_cli.setup`` + ``hermes_cli.setup_platforms`` with recorded I/O."""

    def __init__(self, monkeypatch, *, prompts=(), yes_no=(), env=None):
        from hermes_cli import setup as cli_setup
        from hermes_cli import setup_platforms as cli_platforms

        self.saved = {}
        self.allowlists = []
        self.lines = []
        self._prompts = list(prompts)
        self._yes_no = list(yes_no)
        self._env = dict(env or {})
        self._monkeypatch = monkeypatch

        for name in ("print_header", "print_info", "print_warning", "print_success", "print_error"):
            monkeypatch.setattr(cli_setup, name, self._record(name))
        monkeypatch.setattr(cli_setup, "prompt", self._prompt)
        monkeypatch.setattr(cli_setup, "prompt_yes_no", self._yes_no_answer)
        monkeypatch.setattr(cli_setup, "save_env_value", lambda key, value: self.saved.__setitem__(key, value))
        monkeypatch.setattr(cli_setup, "get_env_value", lambda key: self._env.get(key))
        monkeypatch.setattr(cli_platforms, "declines_reconfigure", lambda *a, **k: False)
        monkeypatch.setattr(
            cli_platforms, "_save_allowlist",
            lambda env_var, users, success_msg: self.allowlists.append((env_var, users)),
        )
        monkeypatch.setattr(
            cli_platforms, "save_prompted",
            lambda env_var, question, **kw: self.saved.__setitem__(
                env_var, self._prompt(question, password=kw.get("password", False))),
        )

    def _record(self, name):
        def _printer(*lines, **kwargs):
            self.lines.extend(str(line) for line in lines if line is not None)

        return _printer

    def _prompt(self, question, default=None, password=False):
        if not self._prompts:
            raise AssertionError(f"unexpected prompt: {question!r}")
        answer = self._prompts.pop(0)
        self.lines.append(f"? {question} -> {answer}")
        return answer

    def _yes_no_answer(self, question, default=True):
        if not self._yes_no:
            raise AssertionError(f"unexpected yes/no prompt: {question!r}")
        answer = self._yes_no.pop(0)
        self.lines.append(f"? {question} -> {answer}")
        return answer

    @property
    def output(self):
        return "\n".join(self.lines)


@pytest.fixture
def wizard_module(plugin):
    """``wizard.py`` is imported lazily by the adapters, so import it explicitly here."""
    import importlib

    return importlib.import_module("iran_messengers.wizard")


def _registry_entry(name):
    from gateway.platform_registry import platform_registry

    return platform_registry.get(name)


def test_both_platforms_register_a_setup_fn(bale_module, rubika_module):
    """The wizard dispatches ``entry.setup_fn`` first; ``None`` means "bounce back to the menu"."""
    for name, module in (("bale", bale_module), ("rubika", rubika_module)):
        entry = _registry_entry(name)
        assert entry is not None, f"{name} is not registered"
        assert callable(entry.setup_fn), f"{name} has no setup_fn — 'gateway setup' would redraw the menu"
        assert entry.setup_fn is module.interactive_setup


def test_bale_setup_saves_token_allowlist_and_home(monkeypatch, bale_module, wizard_module):
    monkeypatch.setattr(bale_module, "probe_bot_token", lambda token: (True, f"connected as @{token}"))
    cli = _CLIStub(
        monkeypatch,
        prompts=["TOKEN123", "111, 222"],
        yes_no=[False, True, False],  # open access? no | use 111 as home? yes | custom api base? no
    )

    bale_module.interactive_setup()

    assert cli.saved["BALE_BOT_TOKEN"] == "TOKEN123"
    assert cli.saved["BALE_ALLOW_ALL_USERS"] == "false"
    assert ("BALE_ALLOWED_USERS", "111, 222") in cli.allowlists
    assert cli.saved["BALE_HOME_CHANNEL"] == "111"
    assert "BALE_API_BASE" not in cli.saved
    assert "restart" in cli.output.lower()


def test_bale_setup_open_access_clears_allowlist(monkeypatch, bale_module):
    monkeypatch.setattr(bale_module, "probe_bot_token", lambda token: (True, "connected"))
    cli = _CLIStub(
        monkeypatch,
        prompts=["TOKEN123", ""],  # token, then "home channel" left empty
        yes_no=[True, False],  # open access? yes | custom api base? no
        env={"BALE_ALLOWED_USERS": "999"},
    )

    bale_module.interactive_setup()

    assert cli.saved["BALE_ALLOW_ALL_USERS"] == "true"
    assert cli.saved["BALE_ALLOWED_USERS"] == ""
    assert cli.saved["BALE_BOT_TOKEN"] == "TOKEN123"
    assert cli.saved.get("BALE_HOME_CHANNEL", "") == ""
    assert "open access" in cli.output.lower()


def test_setup_skips_when_token_cannot_be_verified(monkeypatch, bale_module):
    monkeypatch.setattr(bale_module, "probe_bot_token", lambda token: (False, "invalid token"))
    cli = _CLIStub(monkeypatch, prompts=["BOGUS", ""], yes_no=[False])

    bale_module.interactive_setup()

    assert cli.saved == {}
    assert cli.allowlists == []
    assert "could not verify" in cli.output.lower()


def test_rubika_setup_saves_token(monkeypatch, rubika_module):
    monkeypatch.setattr(rubika_module, "probe_bot_token", lambda token: (True, "connected as bot"))
    cli = _CLIStub(
        monkeypatch,
        prompts=["RTOKEN", "u0abc"],
        yes_no=[False, True, True],  # open access? no | home channel? yes | custom api base? yes
        env={"RUBIKA_API_BASE": "http://127.0.0.1:1/v3"},
    )
    cli._prompts.append("http://example.test/v3")  # the api-base prompt

    rubika_module.interactive_setup()

    assert cli.saved["RUBIKA_BOT_TOKEN"] == "RTOKEN"
    assert cli.saved["RUBIKA_HOME_CHANNEL"] == "u0abc"
    assert cli.saved["RUBIKA_API_BASE"] == "http://example.test/v3"


async def test_http_probe_reads_both_envelopes(mock_platform, wizard_module):
    """``http_probe`` talks to a live server: Bale answers ``{ok, result}``, Rubika ``{status, data}``."""
    async with mock_platform("bale") as bale:
        bale.on("getMe", lambda payload: {"ok": True, "result": {"id": 7, "username": "MyBot"}})
        ok, detail = await asyncio.to_thread(
            wizard_module.http_probe, f"{bale.base_url}/botTOKEN/getMe", style="bale")
        assert ok is True and "MyBot" in detail

        bale.on("getMe", lambda payload: {"ok": False, "description": "Unauthorized"})
        ok, detail = await asyncio.to_thread(
            wizard_module.http_probe, f"{bale.base_url}/botNOPE/getMe", style="bale")
        assert ok is False and "Unauthorized" in detail

    async with mock_platform("rubika") as rubika:
        rubika.on("getMe", lambda payload: {"status": "OK", "data": {"bot": {"bot_id": "b0", "username": "rbot"}}})
        ok, detail = await asyncio.to_thread(
            wizard_module.http_probe, f"{rubika.base_url}/TOKEN/getMe", style="rubika")
        assert ok is True and "rbot" in detail

        rubika.on("getMe", lambda payload: {"status": "INVALID_ACCESS", "data": {}})
        ok, detail = await asyncio.to_thread(
            wizard_module.http_probe, f"{rubika.base_url}/NOPE/getMe", style="rubika")
        assert ok is False


async def test_http_probe_survives_an_unreachable_host(wizard_module):
    ok, detail = await asyncio.to_thread(
        wizard_module.http_probe, "http://127.0.0.1:1/botX/getMe", style="bale")
    assert ok is False and detail
