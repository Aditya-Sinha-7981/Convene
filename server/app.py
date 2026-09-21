"""The Convene server: one FastAPI + aiortc process (ADR-01). Run with ``python -m server.app --cert C --key K``.

REST and pages over HTTPS, WebSocket signaling for phones, a read-only dashboard feed. The DT-17 aiohttp
prototype this replaces is recoverable from Git history (commit d44ba68).
"""
import argparse
import ipaddress
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import network
from .config import ConfigError, Settings, load_settings
from .pipeline.mlx_whisper_adapter import build_adapter
from .routes import CLIENT_DIR, install_error_handlers, router
from .runtime import Runtime, TransportConfig
from .transport.audio import AudioSink

log = logging.getLogger("convene")


def create_app(settings: Settings | None = None, *, sink: AudioSink | None = None, host: str | None = None,
               port: int = 8443, transport: TransportConfig | None = None, stt_adapter=None,
               stt_loaded: bool = False) -> FastAPI:
    """Build the application. ``host`` and ``port`` are the address phones use (for join URLs and QR codes).

    Pass ``stt_adapter`` to run the transcription pipeline (``stt_loaded=True`` if it is already loaded);
    without one the server does transport only and audio goes to the counting sink. Register hooks on
    ``app.state.runtime`` (``on_meeting_ended``, ``on_transcribed_window``) before the server starts. The
    interactive API documentation pages are disabled because they load assets from a CDN.
    """
    runtime = Runtime(settings or load_settings(), sink=sink, host=host, port=port, transport=transport,
                      stt_adapter=stt_adapter, stt_loaded=stt_loaded)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Convene", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
    install_error_handlers(app)
    return app


def uvicorn_options(config: TransportConfig) -> dict:
    """uvicorn settings shared by ``main`` and the tests: frame cap and the WebSocket heartbeat."""
    return {"ws_max_size": config.ws_max_size, "ws_ping_interval": config.ws_ping_interval_s,
            "ws_ping_timeout": config.ws_ping_timeout_s}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m server.app", description="Convene laptop server")
    parser.add_argument("--cert", required=True, help="TLS certificate (PEM), for example from mkcert")
    parser.add_argument("--key", required=True, help="TLS private key (PEM)")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--advertise-ip", help="the laptop's Wi-Fi IPv4 address for join URLs and QR codes "
                                               "(detected when omitted)")
    parser.add_argument("--config", help="path to a convene.toml (default: config/convene.toml)")
    parser.add_argument("--no-stt", action="store_true",
                        help="transport only: do not load the STT model or transcribe (for checking phones and Wi-Fi)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    try:
        settings = load_settings(args.config)
        address = network.detect_lan_address(args.advertise_ip)
    except (ConfigError, ipaddress.AddressValueError) as exc:
        sys.exit(f"startup failed: {exc}")
    if address is None:
        print("No LAN address detected. Pass --advertise-ip with this machine's Wi-Fi address; "
              "join URLs and QR codes are unavailable until then.", file=sys.stderr, flush=True)
    else:
        print(f"LAN address: {address}", flush=True)
    try:
        network.check_certificate(args.cert, args.key, address)
    except network.CertificateError as exc:
        sys.exit(f"startup failed: {exc}")
    print("Certificate OK", flush=True)

    adapter = None
    if args.no_stt:
        print("STT disabled (--no-stt): audio is received and counted, not transcribed.", flush=True)
    else:
        try:
            adapter = build_adapter(settings.stt)
            print(f"Loading STT model {settings.stt.model} from the local cache ...", flush=True)
            adapter.load()  # fails loudly here, not mid-meeting; never downloads
        except Exception as exc:
            sys.exit(f"startup failed: {exc}")
        print(f"STT model ready ({adapter.load_seconds:.1f} s)", flush=True)

    transport = TransportConfig()
    app = create_app(settings, host=address, port=args.port, transport=transport, stt_adapter=adapter,
                     stt_loaded=adapter is not None)
    where = address or "<this machine's address>"
    print(f"Open https://{where}:{args.port}/ on this laptop to start a meeting; phones join from the QR code it shows.",
          flush=True)
    uvicorn.run(app, host="0.0.0.0", port=args.port, ssl_certfile=args.cert, ssl_keyfile=args.key,
                log_level="info", **uvicorn_options(transport))


if __name__ == "__main__":
    main()
