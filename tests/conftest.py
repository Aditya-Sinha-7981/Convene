import sys

import pytest
from aiohttp import web

from server import app as prototype

MEETING = "TEST-123"


class _NoSSLContext:
    """Stands in for ssl.SSLContext so main() runs without a certificate."""

    def __init__(self, *args, **kwargs):
        pass

    def load_cert_chain(self, *args, **kwargs):
        pass


def build_prototype_app(monkeypatch):
    """Return the aiohttp app that ``server.app.main()`` builds, without serving or touching disk.

    The prototype has no app factory (CON-04 replaces it), so this drives the real ``main()``
    with ``run_app``, TLS, QR generation and LAN detection patched out. The routes under test
    are therefore exactly the routes the prototype registers.
    """
    captured = {}
    with monkeypatch.context() as patch:
        patch.setattr(sys, "argv", ["server.app", "--cert", "unused.pem", "--key", "unused.pem"])
        patch.setattr(prototype.ssl, "SSLContext", _NoSSLContext)
        patch.setattr(prototype.qrcode, "make", lambda *a, **k: (_ for _ in ()).throw(AssertionError("QR written")))
        patch.setattr(prototype, "local_addresses", lambda: [])
        patch.setattr(prototype.web, "run_app", lambda app, **kwargs: captured.update(app=app, kwargs=kwargs))
        prototype.main()
    return captured["app"], captured["kwargs"]


@pytest.fixture(autouse=True)
def clean_participants():
    """The prototype keeps all state in a module-level dict; isolate every test."""
    prototype.participants.clear()
    yield
    prototype.participants.clear()


@pytest.fixture
def run_app_kwargs(monkeypatch):
    return build_prototype_app(monkeypatch)[1]


class ServedApp:
    def __init__(self, host, port):
        self.host, self.port = host, port

    def make_url(self, path):
        return f"http://{self.host}:{self.port}{path}"


@pytest.fixture
async def server(monkeypatch):
    """Serve the prototype app on a loopback port.

    Uses AppRunner with ``handler_cancellation=False``, which is what ``web.run_app`` uses.
    ``aiohttp.test_utils.TestServer`` forces it to True, which cancels the handler mid-cleanup
    when a client disconnects and would make these tests describe behavior the real server lacks.
    """
    app, _ = build_prototype_app(monkeypatch)
    runner = web.AppRunner(app, handler_cancellation=False)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    yield ServedApp(host, port)
    await runner.cleanup()


@pytest.fixture
def signaling_url(server):
    return f"ws://{server.host}:{server.port}/ws/{MEETING}"


@pytest.fixture
def meeting():
    return MEETING
