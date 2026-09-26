"""Participant colours (ADR-25): a fixed palette of 12 keys, unique within a meeting while any is free.

The server stores and hands out only the key; the client maps each key to its hex value (``client/theme.css``).
The Convene brand colour is not in the palette: it belongs to the product, never to a participant.
"""
import random

from .errors import ColorTakenError, ValidationError

PALETTE = ("lime", "green", "teal", "cyan", "sky", "azure", "violet", "plum", "magenta", "pink", "slate", "charcoal")


def choose(taken: list[str | None], requested: str | None, rng: random.Random | None = None) -> str:
    """The colour for a new participant, given the colours already used in the meeting.

    A requested colour must be in the palette and not in use (``ColorTakenError`` otherwise). With no request, a
    random unused colour; once all 12 are used, a random one among the least used, so colours stay spread out.
    """
    if requested is not None:
        if requested not in PALETTE:
            raise ValidationError(f"color must be one of {', '.join(PALETTE)}")
        if requested in taken:
            raise ColorTakenError(f"the colour {requested} is already used in this meeting; choose another")
        return requested
    counts = {key: taken.count(key) for key in PALETTE}
    fewest = min(counts.values())
    return (rng or random).choice([key for key in PALETTE if counts[key] == fewest])
