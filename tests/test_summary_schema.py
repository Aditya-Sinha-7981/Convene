"""CON-10 output contract: tolerant unwrapping, strict validation, no semantic repair (docs/summarization.md)."""
import json

import pytest

from server.summary.schema import ActionItemOutput, InvalidOutput, parse_output

VALID = {"summary": "We agreed to ship on Friday.", "action_items": [{"text": "Write notes", "owner": "Sam"},
                                                                       {"text": "Book a room", "owner": None}]}
EXPECTED = (ActionItemOutput("Write notes", "Sam"), ActionItemOutput("Book a room", None))


def test_a_valid_object_is_accepted():
    output = parse_output(json.dumps(VALID))
    assert output.summary == "We agreed to ship on Friday." and output.action_items == EXPECTED


@pytest.mark.parametrize("wrapped", [
    "```json\n{}\n```", "```\n{}\n```", "  ```JSON\n{}\n```  ",
    "Here is the summary:\n{}", "{}\nI hope this helps.", "Sure! {} Let me know.",
])
def test_one_code_fence_or_surrounding_prose_is_removed(wrapped):
    output = parse_output(wrapped.replace("{}", json.dumps(VALID, indent=2)))
    assert output.action_items == EXPECTED


def test_no_action_items_is_a_normal_result():
    assert parse_output('{"summary": "Nothing to do.", "action_items": []}').action_items == ()


def test_extra_keys_are_ignored_and_never_stored():
    value = {**VALID, "title": "Sync", "action_items": [{"text": "Write notes", "owner": "Sam", "due": "Friday"}]}
    output = parse_output(json.dumps(value))
    assert output.action_items == (ActionItemOutput("Write notes", "Sam"),)
    assert not hasattr(output, "title")


def test_whitespace_is_normalized_and_a_blank_owner_means_no_owner():
    output = parse_output(json.dumps({"summary": "  Two\n\nparagraphs.  ",
                                      "action_items": [{"text": " Write\n notes ", "owner": "  "}]}))
    assert output.summary == "Two\n\nparagraphs."
    assert output.action_items == (ActionItemOutput("Write notes", None),)


@pytest.mark.parametrize("text, reason", [
    ("", "no JSON object"),
    ("The team met and agreed things.", "no JSON object"),
    ('{"summary": "Cut off', "does not parse"),
    ('{"summary": "a", "action_items": [],}', "does not parse"),
    ('[{"summary": "a", "action_items": []}]', "more than one JSON value"),
    ('{"summary": "a", "action_items": []} {"summary": "b", "action_items": []}', "more than one JSON value"),
    ('"just a string"', "no JSON object"),
    ('{"action_items": []}', "'summary'"),
    ('{"summary": "   ", "action_items": []}', "'summary'"),
    ('{"summary": 3, "action_items": []}', "'summary'"),
    ('{"summary": ["a", "b"], "action_items": []}', "'summary'"),
    ('{"summary": "a"}', "'action_items'"),
    ('{"summary": "a", "action_items": {"text": "x", "owner": null}}', "'action_items'"),
    ('{"summary": "a", "action_items": ["Write notes"]}', "not an object"),
    ('{"summary": "a", "action_items": [{"owner": "Sam"}]}', "no non-empty 'text'"),
    ('{"summary": "a", "action_items": [{"text": "", "owner": "Sam"}]}', "no non-empty 'text'"),
    ('{"summary": "a", "action_items": [{"text": 5, "owner": "Sam"}]}', "no non-empty 'text'"),
    ('{"summary": "a", "action_items": [{"text": "x"}]}', "no 'owner'"),
    ('{"summary": "a", "action_items": [{"text": "x", "owner": 7}]}', "not a string or null"),
    ('{"summary": "a", "action_items": [{"text": "x", "owner": ["Sam", "Ana"]}]}', "not a string or null"),
])
def test_invalid_shapes_are_rejected_without_repair(text, reason):
    with pytest.raises(InvalidOutput, match=reason.replace("(", r"\(").replace("[", r"\[")):
        parse_output(text)


def test_rejection_reasons_never_echo_the_reply():
    with pytest.raises(InvalidOutput) as caught:
        parse_output('{"summary": "SECRET budget figure", "action_items": [{"text": "SECRET task"}]}')
    assert "SECRET" not in str(caught.value)
