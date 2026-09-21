import argparse
import asyncio
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import qrcode
from qrcode.image.svg import SvgPathImage
from aiohttp import WSMsgType, web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from .stt import transcribe


ROOT = Path(__file__).resolve().parents[1]
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dt17")
participants = {}
stt_slots = defaultdict(lambda: asyncio.Semaphore(1))


def local_addresses():
    addresses = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            address = result[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


async def process_window(participant, number, pcm, rate, start, end):
    async with stt_slots[participant["id"]]:
        began = time.monotonic()
        try:
            result = await transcribe(pcm, rate)
            if result is not None:
                log.info("STT [%s][W%d] audio=%.3f..%.3f stt_ms=%.0f text=%r",
                         participant["id"], number, start, end,
                         (time.monotonic() - began) * 1000, result)
        except Exception as exc:
            log.warning("STT [%s][W%d] error=%s", participant["id"], number, exc)


async def receive_audio(participant, track):
    participant_id = participant["id"]
    buffers = []
    samples = 0
    window = 0
    rate = 48000
    stream_start = time.monotonic()
    try:
        while True:
            frame = await track.recv()
            raw = frame.to_ndarray()
            # PyAV audio frames are planar or packed; average channels to mono.
            channels = frame.layout.channels
            count = len(channels)
            if raw.ndim == 2 and count > 1:
                if raw.shape[0] == count:
                    mono = raw.astype(np.float32).mean(axis=0)
                else:
                    mono = raw.reshape(-1, count).astype(np.float32).mean(axis=1)
            else:
                mono = raw.reshape(-1).astype(np.float32)
            if np.issubdtype(raw.dtype, np.floating):
                mono *= 32767
            pcm = np.clip(mono, -32768, 32767).astype("<i2")
            if frame.sample_rate != rate:
                # WebRTC's received Opus audio is normally 48 kHz. Keep windows
                # in their actual rate; a negotiated change starts a new window.
                buffers.clear()
                samples = 0
                rate = frame.sample_rate
            participant["last_audio"] = time.monotonic()
            participant["samples"] += len(pcm)
            participant["rate"] = rate
            buffers.append(pcm.tobytes())
            samples += len(pcm)
            if samples >= rate:
                window += 1
                end = time.monotonic() - stream_start
                start = end - samples / rate
                payload = b"".join(buffers)
                buffers.clear()
                samples = 0
                log.info("AUDIO [%s] window=%d duration_ms=%.0f receive_age_ms=%.0f",
                         participant_id, window, len(payload) / 2 / rate * 1000,
                         (time.monotonic() - participant["last_audio"]) * 1000)
                if os.getenv("STT_COMMAND"):
                    # Never block the receiver on transcription.
                    if participant["pending_stt"] >= 2:
                        log.warning("STT [%s] backlog; dropping window %d", participant_id, window)
                    else:
                        participant["pending_stt"] += 1
                        async def run(data=payload, number=window, a=start, b=end, hz=rate):
                            try:
                                await process_window(participant, number, data, hz, a, b)
                            finally:
                                participant["pending_stt"] -= 1
                        asyncio.create_task(run())
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.info("AUDIO [%s] stopped: %s", participant_id, exc)


async def close_peer(participant):
    peer = participant.get("peer")
    participant["peer"] = None
    if peer:
        await peer.close()
    task = participant.get("audio_task")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        participant["audio_task"] = None


async def signaling(request):
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    participant = None
    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
                kind = data.get("type")
                if kind == "join":
                    meeting = request.match_info["meeting"]
                    token = data.get("token")
                    if not isinstance(token, str) or not 16 <= len(token) <= 128:
                        raise ValueError("invalid session token")
                    key = (meeting, token)
                    participant = participants.get(key)
                    if participant is None:
                        participant = {"id": "p" + secrets.token_hex(3), "meeting": meeting,
                                       "token": token, "reconnects": 0, "samples": 0,
                                       "rate": 48000, "last_audio": None, "pending_stt": 0,
                                       "peer": None, "audio_task": None}
                        participants[key] = participant
                    else:
                        participant["reconnects"] += 1
                        old_ws = participant.get("ws")
                        if old_ws and old_ws is not ws:
                            await old_ws.close()
                        await close_peer(participant)
                    participant["ws"] = ws
                    log.info("TRANSPORT [%s] joined meeting=%s reconnects=%d",
                             participant["id"], meeting, participant["reconnects"])
                    await ws.send_json({"type": "joined", "participantId": participant["id"],
                                        "reconnects": participant["reconnects"]})
                elif kind == "offer" and participant:
                    if not isinstance(data.get("sdp"), str) or len(data["sdp"]) > 100000:
                        raise ValueError("invalid offer")
                    await close_peer(participant)
                    peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
                    participant["peer"] = peer
                    @peer.on("connectionstatechange")
                    async def state_change():
                        log.info("TRANSPORT [%s] state=%s", participant["id"], peer.connectionState)
                    @peer.on("track")
                    def on_track(track):
                        if track.kind == "audio":
                            participant["audio_task"] = asyncio.create_task(receive_audio(participant, track))
                    await peer.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type="offer"))
                    answer = await peer.createAnswer()
                    await peer.setLocalDescription(answer)
                    await ws.send_json({"type": "answer", "sdp": peer.localDescription.sdp})
                else:
                    raise ValueError("unexpected message")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
            except Exception as exc:
                log.exception("Signaling failure")
                await ws.send_json({"type": "error", "message": str(exc)})
    finally:
        if participant and participant.get("ws") is ws:
            participant["ws"] = None
            await close_peer(participant)
            log.info("TRANSPORT [%s] signaling disconnected", participant["id"])
    return ws


async def page(request):
    return web.FileResponse(ROOT / "client" / "index.html")


async def script(request):
    return web.FileResponse(ROOT / "client" / "app.js")


async def metrics(request):
    now = time.monotonic()
    return web.json_response([{
        "participant": p["id"], "meeting": p["meeting"],
        "state": p["peer"].connectionState if p["peer"] else "disconnected",
        "audioReceived": p["last_audio"] is not None,
        "lastAudioAgeMs": round((now - p["last_audio"]) * 1000) if p["last_audio"] else None,
        "audioDurationSeconds": round(p["samples"] / p["rate"], 2),
        "reconnects": p["reconnects"]
    } for p in participants.values()])


async def summary(app):
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        for p in participants.values():
            age = round((now - p["last_audio"]) * 1000) if p["last_audio"] else None
            log.info("METRICS [%s] state=%s audio=%s last_audio_age_ms=%s duration_s=%.1f reconnects=%d",
                     p["id"], p["peer"].connectionState if p["peer"] else "disconnected",
                     "RECEIVING" if age is not None and age < 2000 else "MISSING",
                     age, p["samples"] / p["rate"], p["reconnects"])


async def background(app):
    task = asyncio.create_task(summary(app))
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for participant in participants.values():
        await close_peer(participant)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--meeting", default="TEST-123")
    parser.add_argument("--advertise-ip", help="Mac Wi-Fi IPv4 address to put in the join URL and QR")
    args = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    app = web.Application()
    app.router.add_get("/join/{meeting}", page)
    app.router.add_get("/app.js", script)
    app.router.add_get("/ws/{meeting}", signaling)
    app.router.add_get("/metrics", metrics)
    app.cleanup_ctx.append(background)
    if args.advertise_ip:
        address = str(ipaddress.IPv4Address(args.advertise_ip))
        addresses = [address]
    else:
        addresses = local_addresses()
    if not addresses:
        print("No LAN address detected. Pass --advertise-ip with the Mac's Wi-Fi address.", flush=True)
    for address in addresses:
        url = f"https://{address}:{args.port}/join/{args.meeting}"
        qr = qrcode.make(url, image_factory=SvgPathImage)
        output = ROOT / f"join-{address}.svg"
        qr.save(output)
        print(f"Join URL: {url}\nQR code: {output}", flush=True)
    web.run_app(app, host="0.0.0.0", port=args.port, ssl_context=context)


if __name__ == "__main__":
    main()
