"""Test fixtures: an isolated HERMES_HOME, a mock bot API, and async test support.

The plugin directory name (``hermes-iran-messengers``) is not a valid Python identifier, so
tests load it as a synthetic package ``iran_messengers`` with ``submodule_search_locations``
pointing at the repo root — exactly how the Hermes plugin loader imports a plugin directory.

Two pytest quirks are handled here on purpose:

* the repo root is itself a package (``__init__.py``, as the Hermes plugin loader requires), so
  ``pytest.ini`` roots collection at ``tests/`` and nothing adds ``tests/`` to ``sys.path``:
  either would make pytest collect the plugin's own ``__init__.py`` as a test module and choke
  on its relative imports. The mock server therefore lives in this file, not a sibling module.
* ``pytest-asyncio`` is not required — ``pytest_pyfunc_call`` below runs ``async def`` tests with
  ``asyncio.run``, so the suite needs only pytest plus the platform's own dependencies.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Isolated HERMES_HOME *before* any gateway module is imported: cache dirs are computed at
# import time, so a late assignment would still write into the real ~/.hermes cache.
_TEST_HOME = tempfile.mkdtemp(prefix="iran-msgr-test-home-")
os.environ["HERMES_HOME"] = _TEST_HOME
os.environ.setdefault("BALE_BOT_TOKEN", "TESTTOKEN")
os.environ.setdefault("RUBIKA_BOT_TOKEN", "TESTTOKEN")

PACKAGE_NAME = "iran_messengers"


def load_plugin_package():
    """Import the plugin directory as a package (mirrors the Hermes plugin loader)."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PACKAGE_NAME  # the plugin's ``from .adapter import ...`` needs this
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


class MockPlatform:
    """Minimal aiohttp stand-in for the Bale/Rubika bot APIs.

    Both platforms answer ``POST <base>/<prefix><token>/<method>`` with a JSON envelope; only the
    envelope and the file paths differ:

    * Bale — ``/bot<token>/<method>``, ``{"ok": true, "result": ...}``, downloads at
      ``/file/bot<token>/<file_path>`` (production base URL ``https://tapi.bale.ai``).
    * Rubika — ``/<token>/<method>``, ``{"status": "OK", "data": ...}``, uploads at
      ``/upload/<name>`` and downloads at ``/download/<path>``.

    Register per-method handlers with :meth:`on` (a handler returns either a full envelope or a
    plain payload that gets wrapped); every request is recorded in :attr:`calls`.
    """

    def __init__(self, kind: str) -> None:
        if kind not in {"bale", "rubika"}:
            raise ValueError(f"unknown mock kind: {kind}")
        self.kind = kind
        self.routes: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self.downloads: Dict[str, bytes] = {}
        self.uploads: List[Tuple[str, bytes]] = []
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.base_url: str = ""
        self._runner = None
        self._site = None

    # -- configuration ------------------------------------------------------

    def on(self, method: str, handler: Callable[[Dict[str, Any]], Any]) -> "MockPlatform":
        self.routes[method] = handler
        return self

    def calls_for(self, method: str) -> List[Dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def _wrap(self, body: Any) -> Dict[str, Any]:
        if isinstance(body, dict):
            if self.kind == "bale" and "ok" in body:
                return body
            if self.kind == "rubika" and "status" in body:
                return body
        if self.kind == "bale":
            return {"ok": True, "result": body}
        return {"status": "OK", "data": body}

    def error(self, code: Any, description: str = "boom") -> Dict[str, Any]:
        """A platform-shaped error, so tests don't hand-write envelopes."""
        if self.kind == "bale":
            return {"ok": False, "error_code": code, "description": description}
        return {"status": str(code)}

    # -- routing ------------------------------------------------------------

    @staticmethod
    async def _multipart_file_bytes(request) -> bytes:
        """Raw bytes of the ``file`` part (Bale/Rubika uploads are multipart/form-data)."""
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                return b""
            if part.name == "file":
                return await part.read()

    async def _handle(self, request) -> Any:
        from aiohttp import web

        path = request.match_info.get("tail", "")
        segments = [s for s in path.split("/") if s]
        method = segments[-1] if segments else ""
        if self.kind == "bale" and segments[:1] == ["file"]:
            blob = self.downloads.get("/".join(segments[2:]))
            if blob is None:
                return web.Response(status=404, text="not found")
            return web.Response(body=blob, content_type="application/octet-stream")
        if self.kind == "rubika" and segments[:1] == ["download"]:
            blob = self.downloads.get("/".join(segments[1:]))
            if blob is None:
                return web.Response(status=404, text="not found")
            return web.Response(body=blob, content_type="application/octet-stream")
        if self.kind == "rubika" and segments[:1] == ["upload"]:
            name = segments[-1] if len(segments) > 1 else "file"
            raw = await self._multipart_file_bytes(request)
            self.uploads.append((name, raw))
            self.calls.append(("upload", {"name": name, "size": len(raw)}))
            return web.json_response({"status": "OK", "data": {"file_id": f"FID-{name}"}})
        payload: Dict[str, Any] = {}
        try:
            if request.content_type.startswith("multipart/"):
                reader = await request.multipart()
                while True:
                    part = await reader.next()
                    if part is None:
                        break
                    if part.name == "file":
                        raw = await part.read()
                        payload["_file_name"] = part.filename
                        payload["_file_size"] = len(raw)
                    else:
                        payload[part.name] = await part.text()
            elif request.can_read_body:
                payload = await request.json()
        except Exception:  # a malformed body is still recorded, as an empty call
            payload = {}
        self.calls.append((method, payload))
        handler = self.routes.get(method)
        if handler is None:
            return web.json_response(self._wrap({}))
        body = handler(payload)
        if asyncio.iscoroutine(body):
            body = await body
        return web.json_response(self._wrap(body))

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "MockPlatform":
        from aiohttp import web

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        port = self._site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None


class RecordingPluginContext:
    """Captures ``ctx.register_platform(...)`` calls so tests can assert the manifest contract."""

    def __init__(self) -> None:
        self.platforms: List[Dict[str, Any]] = []

    def register_platform(self, name: str, label: str, adapter_factory: Any, check_fn: Any,
                          **kwargs: Any) -> None:
        self.platforms.append({
            "name": name, "label": label, "adapter_factory": adapter_factory,
            "check_fn": check_fn, **kwargs,
        })


def register_into_registry(recorded: List[Dict[str, Any]], plugin_name: str) -> None:
    """Mirror ``PluginContext.register_platform`` into the real platform registry.

    The registry is what makes ``Platform("bale")`` resolve, so the adapters can only be
    constructed once this has run — the session fixture below does it for every test.
    """
    from gateway.platform_registry import PlatformEntry, platform_registry

    for record in recorded:
        params = dict(record)
        entry = PlatformEntry(
            name=params.pop("name"), label=params.pop("label"),
            source="plugin", plugin_name=plugin_name, **params,
        )
        platform_registry.register(entry, scope=None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def plugin_ctx(plugin) -> RecordingPluginContext:
    """Runs the plugin's real ``register(ctx)`` once, against a recording context.

    Also mirrors the registration into the real platform registry, which is what makes
    ``Platform("bale")`` resolve — every adapter test needs this, so the gateway-dependent test
    modules opt in with ``pytestmark = pytest.mark.usefixtures("plugin_ctx")``.
    """
    ctx = RecordingPluginContext()
    plugin.register(ctx)
    register_into_registry(ctx.platforms, plugin_name="iran-messengers")
    return ctx


@pytest.fixture(scope="session")
def plugin():
    """The plugin package (imported as ``iran_messengers``)."""
    return load_plugin_package()


@pytest.fixture(scope="session")
def bale_module(plugin):
    return sys.modules[f"{PACKAGE_NAME}.bale"]


@pytest.fixture(scope="session")
def rubika_module(plugin):
    return sys.modules[f"{PACKAGE_NAME}.rubika"]


@pytest.fixture(scope="session")
def common_module():
    """``common.py`` loaded standalone — it has no Hermes imports, so these tests run anywhere."""
    name = "iran_messengers_common"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "common.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def hermes_home() -> str:
    """The throwaway HERMES_HOME every test writes into."""
    return _TEST_HOME


@pytest.fixture
def mock_platform():
    """Factory: ``async with mock_platform("bale") as mock: ...``."""
    return MockPlatform


@pytest.fixture
def active_adapter():
    """Collect adapters so a failing assertion still tears them down."""
    created: List[Any] = []
    yield created
    for adapter in created:
        with contextlib.suppress(Exception):
            asyncio.run(adapter.disconnect())


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run ``async def`` tests with ``asyncio.run`` (no pytest-asyncio dependency)."""
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(func(**kwargs))
    return True
