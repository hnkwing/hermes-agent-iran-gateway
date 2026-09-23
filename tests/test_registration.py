"""Registration tests: the manifest contract each platform hands to Hermes."""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest
import yaml

from gateway.config import Platform, PlatformConfig

pytestmark = pytest.mark.usefixtures("plugin_ctx")

PLUGIN_YAML = Path(__file__).resolve().parent.parent / "plugin.yaml"


def _entry(ctx, name):
    matches = [item for item in ctx.platforms if item["name"] == name]
    assert len(matches) == 1, f"expected exactly one '{name}' registration, got {len(matches)}"
    return matches[0]


def test_plugin_registers_both_platforms(plugin_ctx):
    assert sorted(item["name"] for item in plugin_ctx.platforms) == ["bale", "rubika"]


@pytest.mark.parametrize("name,label,env_prefix", [
    ("bale", "Bale", "BALE"),
    ("rubika", "Rubika", "RUBIKA"),
])
def test_registration_contract(plugin_ctx, name, label, env_prefix):
    entry = _entry(plugin_ctx, name)
    assert label in entry["label"]
    assert entry["required_env"] == [f"{env_prefix}_BOT_TOKEN"]
    assert entry["allowed_users_env"] == f"{env_prefix}_ALLOWED_USERS"
    assert entry["allow_all_env"] == f"{env_prefix}_ALLOW_ALL_USERS"
    assert entry["cron_deliver_env_var"] == f"{env_prefix}_HOME_CHANNEL"
    assert entry["max_message_length"] == 4096
    assert entry["pii_safe"] is True
    assert entry["install_hint"]
    assert name in entry["platform_hint"].lower()
    assert inspect.iscoroutinefunction(entry["standalone_sender_fn"]), "cron needs an async sender"
    assert callable(entry["validate_config"]) and callable(entry["is_connected"])
    assert entry["adapter_factory"].__name__.endswith("Adapter")


@pytest.mark.parametrize("name,env_var", [("bale", "BALE_BOT_TOKEN"), ("rubika", "RUBIKA_BOT_TOKEN")])
def test_check_fn_tracks_the_token_env(plugin_ctx, monkeypatch, name, env_var):
    check_fn = _entry(plugin_ctx, name)["check_fn"]
    monkeypatch.setenv(env_var, "TOKEN")
    assert check_fn() is True
    monkeypatch.delenv(env_var, raising=False)
    assert check_fn() is False


def test_platform_enum_resolves_after_registration(plugin_ctx):
    assert Platform("bale").value == "bale"
    assert Platform("rubika").value == "rubika"
    assert Platform("BALE") is Platform("bale")  # cached pseudo-member


@pytest.mark.parametrize("name", ["bale", "rubika"])
def test_adapter_factory_builds_the_adapter(plugin_ctx, name):
    entry = _entry(plugin_ctx, name)
    adapter = entry["adapter_factory"](PlatformConfig(enabled=True, extra={"token": "TOKEN"}))
    assert adapter.platform.value == name
    assert adapter.name  # display label, e.g. "Rubika"


@pytest.mark.parametrize("name,env_prefix", [("bale", "BALE"), ("rubika", "RUBIKA")])
def test_env_enablement_seeds_extra(plugin_ctx, monkeypatch, name, env_prefix):
    entry = _entry(plugin_ctx, name)
    enable = entry["env_enablement_fn"]
    monkeypatch.setenv(f"{env_prefix}_BOT_TOKEN", "ENVTOKEN")
    monkeypatch.setenv(f"{env_prefix}_HOME_CHANNEL", "chat-42")
    seed = enable()
    assert seed["token"] == "ENVTOKEN"
    assert seed["home_channel"]["chat_id"] == "chat-42"
    monkeypatch.delenv(f"{env_prefix}_BOT_TOKEN", raising=False)
    assert enable() is None


def test_yaml_bridge_writes_env_when_unset(plugin_ctx, monkeypatch):
    entry = _entry(plugin_ctx, "bale")
    monkeypatch.delenv("BALE_BOT_TOKEN", raising=False)
    seeded = entry["apply_yaml_config_fn"]({}, {"extra": {"token": "YAMLTOKEN", "api_base": "https://bale.example"}})
    assert os.environ["BALE_BOT_TOKEN"] == "YAMLTOKEN"
    assert os.environ["BALE_API_BASE"] == "https://bale.example"
    assert seeded["token"] == "YAMLTOKEN"
    # env wins over YAML
    monkeypatch.setenv("BALE_API_BASE", "https://env.example")
    seeded = entry["apply_yaml_config_fn"]({}, {"extra": {"api_base": "https://yaml.example"}})
    assert os.environ["BALE_API_BASE"] == "https://env.example"
    assert seeded["api_base"] == "https://yaml.example"


def test_plugin_yaml_manifest_is_complete():
    manifest = yaml.safe_load(PLUGIN_YAML.read_text(encoding="utf-8"))
    assert manifest["name"] == "iran-messengers"
    assert manifest["kind"] == "platform"
    assert manifest["version"] and manifest["description"]
    assert manifest["license"] == "MIT"
    declared = {item["name"] for item in manifest["requires_env"]}
    assert declared == {"BALE_BOT_TOKEN", "RUBIKA_BOT_TOKEN"}
    optional = {item["name"] for item in manifest["optional_env"]}
    assert {"BALE_ALLOWED_USERS", "RUBIKA_ALLOWED_USERS", "BALE_HOME_CHANNEL"} <= optional
    assert manifest["python_dependencies"] == ["httpx>=0.28,<1.0"]


def test_plugin_yaml_has_no_invisible_unicode():
    """Persian text in the manifest trips Hermes' security scan on ZWNJ/zero-width chars."""
    suspicious = {0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x2060}
    offenders = {f"U+{ord(ch):04X}" for ch in PLUGIN_YAML.read_text(encoding="utf-8") if ord(ch) in suspicious}
    assert not offenders, f"invisible characters in plugin.yaml: {sorted(offenders)}"
