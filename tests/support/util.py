import asyncio
import time


async def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05, message: str = "condition not met"):
    """Poll until ``predicate()`` is truthy; fail with ``message`` after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(message)
        await asyncio.sleep(interval)
