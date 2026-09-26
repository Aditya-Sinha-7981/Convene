"""Validation of the model's summary output (docs/summarization.md, "Output contract").

    { "summary": string, "action_items": [ { "text": string, "owner": string | null } ] }

Parsing is tolerant of packaging and strict about meaning. It removes one wrapping code fence, or prose around a
single JSON object, and nothing else: it never fills in a missing key, converts a wrong type, or joins pieces of
broken JSON. Keys outside the contract are ignored and never stored. An ``owner`` that is an empty or blank string
is read as null, since both mean "no owner"; any other non-string owner is rejected.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_FENCE = re.compile(r"^```[A-Za-z]*[ \t]*\n(.*)\n```$", re.DOTALL)


class InvalidOutput(ValueError):
    """The model's reply is not a usable summary. The message is a short reason with no transcript text."""


@dataclass(frozen=True)
class ActionItemOutput:
    text: str
    owner: str | None


@dataclass(frozen=True)
class SummaryOutput:
    summary: str
    action_items: tuple[ActionItemOutput, ...]


def extract_object(text: str) -> dict:
    """The single JSON object in ``text``, unwrapped from one code fence or surrounding prose."""
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    start = body.find("{")
    if start < 0:
        raise InvalidOutput("no JSON object in the reply")
    try:
        value, end = json.JSONDecoder().raw_decode(body, start)
    except json.JSONDecodeError as exc:
        raise InvalidOutput(f"the JSON object does not parse ({exc.msg})") from None
    if "{" in body[end:] or "[" in body[end:] or "}" in body[:start] or "[" in body[:start]:
        raise InvalidOutput("the reply holds more than one JSON value")
    if not isinstance(value, dict):
        raise InvalidOutput("the reply is not a JSON object")
    return value


def validate(value: dict) -> SummaryOutput:
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise InvalidOutput("'summary' is missing or not a non-empty string")
    items = value.get("action_items")
    if not isinstance(items, list):
        raise InvalidOutput("'action_items' is missing or not a list")
    parsed = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise InvalidOutput(f"action item {number} is not an object")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidOutput(f"action item {number} has no non-empty 'text'")
        if "owner" not in item:
            raise InvalidOutput(f"action item {number} has no 'owner' (use null for none)")
        owner = item["owner"]
        if owner is not None and not isinstance(owner, str):
            raise InvalidOutput(f"action item {number} 'owner' is not a string or null")
        parsed.append(ActionItemOutput(" ".join(text.split()), (owner.strip() or None) if owner is not None else None))
    return SummaryOutput(summary.strip(), tuple(parsed))


def parse_output(text: str) -> SummaryOutput:
    return validate(extract_object(text))


__all__ = ["InvalidOutput", "ActionItemOutput", "SummaryOutput", "extract_object", "validate", "parse_output"]
