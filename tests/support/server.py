"""Run the real Convene app under uvicorn inside the test's event loop.

``aiortc`` needs real sockets, so the tests serve the actual ASGI app on a loopback port and talk to it with
aiohttp and the synthetic phone (starlette's TestClient would need an extra package and cannot carry WebRTC).
"""
import asyncio

import uvicorn

from server.app import create_app, uvicorn_options
from server.config import Settings
from server.runtime import TransportConfig

FAST = TransportConfig(gauge_interval_s=0.2, metrics_log_interval_s=0)


class RunningServer:
    def __init__(self, app, server, task, port: int, scheme: str):
        self.app, self._server, self._task, self.port, self.scheme = app, server, task, port, scheme

    @property
    def runtime(self):
        return self.app.state.runtime

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def ws_url(self, path: str) -> str:
        return self.base_url.replace("http", "ws", 1) + path

    async def stop(self) -> None:
        self._server.should_exit = True
        await asyncio.wait_for(self._task, 20)


async def start_server(settings: Settings, *, sink=None, transport: TransportConfig | None = None,
                       host: str | None = "192.168.50.10", ssl_certfile=None, ssl_keyfile=None) -> RunningServer:
    transport = transport or FAST
    app = create_app(settings, sink=sink, host=host, port=0, transport=transport)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on",
                            ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile, **uvicorn_options(transport))
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        if task.done():
            task.result()
        await asyncio.sleep(0.02)
    else:
        raise TimeoutError("server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    app.state.runtime.port = port  # so join URLs carry the real port
    return RunningServer(app, server, task, port, "https" if ssl_certfile else "http")


def settings_in(tmp_path, name: str = "convene.db") -> Settings:
    return Settings(root=tmp_path, database_path=tmp_path / name, exports_dir=tmp_path / "exports")


__all__ = ["RunningServer", "start_server", "settings_in", "FAST"]
