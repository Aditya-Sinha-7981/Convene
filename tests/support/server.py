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


class ServerStartupError(RuntimeError):
    """The app refused to start (for example its STT model failed to load)."""


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
                       host: str | None = "192.168.50.10", ssl_certfile=None, ssl_keyfile=None, stt_adapter=None,
                       stt_loaded: bool = False, embedding_adapter=None, reasoning_adapter=None) -> RunningServer:
    transport = transport or FAST
    app = create_app(settings, sink=sink, host=host, port=0, transport=transport, stt_adapter=stt_adapter,
                     stt_loaded=stt_loaded, embedding_adapter=embedding_adapter, reasoning_adapter=reasoning_adapter)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on",
                            ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile, **uvicorn_options(transport))
    server = uvicorn.Server(config)

    async def serve() -> int | None:
        try:
            await server.serve()
        except SystemExit as exit_request:  # uvicorn exits with code 3 when application startup fails
            return exit_request.code or 0
        return None

    task = asyncio.create_task(serve())
    for _ in range(300):
        if server.started:
            break
        if task.done():
            raise ServerStartupError(f"the server exited during startup (code {task.result()})")
        await asyncio.sleep(0.02)
    else:
        raise TimeoutError("server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    app.state.runtime.port = port  # so join URLs carry the real port
    return RunningServer(app, server, task, port, "https" if ssl_certfile else "http")


def settings_in(tmp_path, name: str = "convene.db") -> Settings:
    return Settings(root=tmp_path, database_path=tmp_path / name, exports_dir=tmp_path / "exports")


__all__ = ["RunningServer", "ServerStartupError", "start_server", "settings_in", "FAST"]
