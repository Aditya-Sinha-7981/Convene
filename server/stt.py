"""Optional local STT adapter using a command that accepts a WAV path.

Set STT_COMMAND to a shell-free command containing {wav}, for example:
  STT_COMMAND='whisper-cli -m /path/model.bin -f {wav} -nt'
"""
import asyncio
import os
import shlex
import tempfile
import wave
from pathlib import Path


async def transcribe(pcm: bytes, sample_rate: int) -> str | None:
    template = os.getenv("STT_COMMAND")
    if not template:
        return None
    with tempfile.TemporaryDirectory(prefix="dt17-stt-") as directory:
        path = Path(directory) / "window.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        args = [part.replace("{wav}", str(path)) for part in shlex.split(template)]
        if not any(str(path) in part for part in args):
            raise ValueError("STT_COMMAND must contain {wav}")
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("STT timed out")
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace")[-500:])
        return stdout.decode(errors="replace").strip()
