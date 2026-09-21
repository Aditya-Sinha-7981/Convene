"""Documentation-consistency checks for the Convene API contract (CON-02).

Offline, no server. These tests read docs/api.md, docs/transport.md and docs/data-model.md and check that the
worked JSON examples agree with the data model and the documented catalogs. They do not show that any endpoint
exists or behaves as documented; that is the job of the tasks that implement the routes.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
API = (DOCS / "api.md").read_text()
TRANSPORT = (DOCS / "transport.md").read_text()
DATA_MODEL = (DOCS / "data-model.md").read_text()
DECISIONS = (DOCS / "decisions.md").read_text()
CONTRACTS_LOG = ROOT / "logs" / "contracts.md"

UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
CAMEL = re.compile(r"[a-z][A-Z]")
PIPE = "\x00"

# JSON key -> stored entity that the value (a dict, or a list of dicts) represents.
KEY_ENTITY = {
    "meeting": "Meeting", "meetings": "Meeting",
    "device": "Device", "devices": "Device",
    "participant": "Participant", "participants": "Participant",
    "utterance": "Utterance", "utterances": "Utterance",
    "event": "ConnectionEvent",
    "query": "QAQuery",
    "summary": "Summary", "latest_summary": "Summary", "latest_attempt": "Summary",
    "action_items": "ActionItem",
    "export": "Export", "latest_export": "Export",
    "enrollment": "SpeakerEnrollment",
}
DASHBOARD_ENVELOPE = {"type", "seq", "meeting_id", "at"}
ID_LIST_KEYS = {"participant_ids", "cited_chunk_ids", "meeting_ids"}


# --- parsing helpers ------------------------------------------------------------------------


def fenced_json_blocks(text):
    """Yield (line_number, annotation, body) for every ```json fence; annotation is the preceding
    ``<!-- example: ... -->`` comment, or None."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```json"):
            start = i
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            annotation = None
            j = start - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if j >= 0:
                m = re.match(r"<!--\s*example:\s*(.+?)\s*-->", lines[j].strip())
                annotation = m.group(1) if m else None
            yield start + 1, annotation, "\n".join(body)
        i += 1


def json_values(body):
    """All top-level JSON values in a block (a block may hold several, one after another)."""
    decoder = json.JSONDecoder()
    values, index = [], 0
    while True:
        while index < len(body) and body[index].isspace():
            index += 1
        if index >= len(body):
            return values
        value, index = decoder.raw_decode(body, index)
        values.append(value)


def all_examples(text):
    return [(line, note, json_values(body)) for line, note, body in fenced_json_blocks(text)]


def split_row(line):
    cells = line.strip().strip("|").replace("\\|", PIPE).split("|")
    return [c.strip().replace(PIPE, "|") for c in cells]


def tables(section):
    """Markdown tables in a section: list of (header_cells, rows)."""
    found, current = [], None
    for line in section.splitlines():
        if line.strip().startswith("|"):
            cells = split_row(line)
            if current is None:
                current = [cells, []]
            elif all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            else:
                current[1].append(cells)
        else:
            if current is not None:
                found.append(tuple(current))
                current = None
    if current is not None:
        found.append(tuple(current))
    return found


def heading_section(text, predicate):
    """Text from the first heading satisfying predicate(level, title) to the next heading of the same or higher level."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m and predicate(len(m.group(1)), m.group(2).strip()):
            level = len(m.group(1))
            end = len(lines)
            for j in range(i + 1, len(lines)):
                n = re.match(r"^(#{1,6})\s", lines[j])
                if n and len(n.group(1)) <= level:
                    end = j
                    break
            return "\n".join(lines[i:end])
    return None


def ticks(cell):
    return re.findall(r"`([^`]+)`", cell)


# --- derived contract data ------------------------------------------------------------------


def _entities():
    fields, enums = {}, {}
    for match in re.finditer(r"^### (\w+)\s*$", DATA_MODEL, re.M):
        name = match.group(1)
        section = heading_section(DATA_MODEL, lambda lvl, title, n=name: lvl == 3 and title == n)
        for header, rows in tables(section):
            if header and header[0] == "Field":
                fields[name] = {row[0] for row in rows}
                for row in rows:
                    notes = row[2]
                    if "|" in notes and ticks(notes):
                        enums[(name, row[0])] = set(ticks(notes))
                if name == "AuditEvent":
                    for row in rows:
                        if row[0] == "component":
                            enums[(name, "component")] = set(ticks(row[2]))
                    enums.pop((name, "event_type"), None)
                break
    return fields, enums


ENTITY_FIELDS, ENUMS = _entities()


def _derived():
    section = heading_section(API, lambda lvl, title: title == "Derived fields")
    result = {}
    for header, rows in tables(section):
        if header[:2] == ["Entity", "Derived field"]:
            for row in rows:
                result.setdefault(row[0], {})[ticks(row[1])[0]] = row[2]
    return result


DERIVED = _derived()


def _audit_catalog():
    section = heading_section(DATA_MODEL, lambda lvl, title: title == "Audit event catalog")
    catalog = {}
    for header, rows in tables(section):
        if header[:1] == ["event_type"]:
            for row in rows:
                catalog[ticks(row[0])[0]] = set(ticks(row[2]))
    return catalog


AUDIT = _audit_catalog()


def _signal_catalog():
    section = heading_section(TRANSPORT, lambda lvl, title: title == "Message catalog")
    client, server = {}, {}
    for header, rows in tables(section):
        if header[:1] == ["type"]:
            for row in rows:
                target = client if row[1].startswith("client") else server
                target[ticks(row[0])[0]] = set(ticks(row[2]))
    return client, server


SIGNAL_CLIENT, SIGNAL_SERVER = _signal_catalog()


def _signal_errors():
    section = heading_section(TRANSPORT, lambda lvl, title: title == "Errors and isolation")
    for header, rows in tables(section):
        if header[:1] == ["code"]:
            return {ticks(r[0])[0]: r[1].strip() == "yes" for r in rows}
    return {}


SIGNAL_ERRORS = _signal_errors()


def _dashboard_catalog():
    section = heading_section(API, lambda lvl, title: title.startswith("WebSocket: `/ws/dashboard"))
    for header, rows in tables(section):
        if header[:2] == ["type", "durable"]:
            return {ticks(r[0])[0]: (r[1].strip() == "yes", set(ticks(r[2]))) for r in rows}
    return {}


DASHBOARD = _dashboard_catalog()


def _error_catalog():
    section = heading_section(API, lambda lvl, title: title.startswith("Error shape"))
    for header, rows in tables(section):
        if header[:1] == ["code"]:
            return {ticks(r[0])[0]: int(r[1]) for r in rows}
    return {}


ERROR_CODES = _error_catalog()


def _routes():
    section = heading_section(API, lambda lvl, title: title == "Route index")
    for header, rows in tables(section):
        if header[:2] == ["Method", "Path"]:
            return [(r[0], ticks(r[1])[0], r[2]) for r in rows]
    return []


ROUTES = _routes()
GAUGE_KEYS = set(ticks(DERIVED["Device"]["gauges"])) - {"null"}


def route_section(method, path):
    def match(level, title):
        if method == "WS":
            return level == 2 and title.startswith("WebSocket:") and f"`{path}`" in title
        return level == 3 and title == f"{method} {path}"
    return heading_section(API, match)


def walk(value, entity, where, problems):
    """Check dicts under keys mapped to stored entities against the data model; recurse everywhere."""
    if isinstance(value, dict):
        if entity:
            allowed = ENTITY_FIELDS[entity] | set(DERIVED.get(entity, {}))
            for key in value:
                if key not in allowed:
                    problems.append(f"{where}: {entity} example has undocumented field {key!r}")
            for key, item in value.items():
                allowed_values = ENUMS.get((entity, key))
                if allowed_values and isinstance(item, str) and item not in allowed_values:
                    problems.append(f"{where}: {entity}.{key}={item!r} is not one of {sorted(allowed_values)}")
        for key, item in value.items():
            child = KEY_ENTITY.get(key) if isinstance(item, (dict, list)) else None
            walk(item, child, where, problems)
    elif isinstance(value, list):
        for item in value:
            walk(item, entity, where, problems)


DOC_EXAMPLES = {
    "api.md": all_examples(API),
    "transport.md": all_examples(TRANSPORT),
    "data-model.md": all_examples(DATA_MODEL),
}


def flat_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from flat_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from flat_dicts(item)


# --- tests ----------------------------------------------------------------------------------


def test_parsers_found_the_contract_tables():
    """Guard against a silent parse failure making every other test vacuous."""
    assert {"Meeting", "Device", "Participant", "Utterance", "QAQuery", "Summary", "ActionItem", "Export",
            "AuditEvent", "ConnectionEvent"} <= set(ENTITY_FIELDS)
    assert len(ROUTES) >= 15
    assert len(AUDIT) >= 20
    assert len(DASHBOARD) >= 10
    assert SIGNAL_CLIENT and SIGNAL_SERVER and SIGNAL_ERRORS
    assert len(ERROR_CODES) >= 15
    assert DERIVED["Utterance"] and GAUGE_KEYS


@pytest.mark.parametrize("doc", sorted(DOC_EXAMPLES))
def test_every_json_example_parses(doc):
    text = {"api.md": API, "transport.md": TRANSPORT, "data-model.md": DATA_MODEL}[doc]
    blocks = list(fenced_json_blocks(text))
    for line, _, body in blocks:
        try:
            values = json_values(body)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{doc}:{line}: example is not valid JSON: {exc}")
        assert values, f"{doc}:{line}: empty JSON block"
    if doc in ("api.md", "transport.md"):
        assert blocks


def test_examples_use_only_documented_entity_fields_and_enum_values():
    problems = []
    for doc, examples in DOC_EXAMPLES.items():
        for line, note, values in examples:
            for value in values:
                entity = "AuditEvent" if note == "audit" else None
                walk(value, entity, f"{doc}:{line}", problems)
    assert not problems, "\n".join(problems)


def test_ids_are_uuid_v4_and_timestamps_are_utc_milliseconds():
    problems = []
    time_keys = {"t_start", "t_end", "timestamp", "at"}
    for doc, examples in DOC_EXAMPLES.items():
        for line, _, values in examples:
            for value in values:
                for obj in flat_dicts(value):
                    for key, item in obj.items():
                        if (key.endswith("_id") and key != "window_id") or key in ID_LIST_KEYS:
                            for ident in (item if isinstance(item, list) else [item]):
                                if ident is not None and not (isinstance(ident, str) and UUID_V4.match(ident)):
                                    problems.append(f"{doc}:{line}: {key}={ident!r} is not a UUID v4")
                        if key.endswith("_at") or key in time_keys:
                            if item is not None and not (isinstance(item, str) and TIMESTAMP.match(item)):
                                problems.append(f"{doc}:{line}: {key}={item!r} is not ISO 8601 UTC with milliseconds and Z")
    assert not problems, "\n".join(problems)


def test_json_field_names_are_snake_case():
    """ADR-16: one casing across REST, WebSocket and the data model."""
    problems = []
    for doc, examples in DOC_EXAMPLES.items():
        for line, _, values in examples:
            for value in values:
                for obj in flat_dicts(value):
                    problems += [f"{doc}:{line}: camelCase key {k!r}" for k in obj if CAMEL.search(k)]
    assert not problems, "\n".join(problems)


def test_audit_examples_match_the_catalog():
    seen = 0
    for line, note, values in DOC_EXAMPLES["data-model.md"]:
        if note != "audit":
            continue
        for value in values:
            seen += 1
            assert value["event_type"] in AUDIT, f"data-model.md:{line}: {value['event_type']} not in catalog"
            assert set(value["payload"]) == AUDIT[value["event_type"]], (
                f"data-model.md:{line}: payload keys {sorted(value['payload'])} != catalog {sorted(AUDIT[value['event_type']])}")
    assert seen


def test_audit_catalog_is_complete_for_the_events_the_docs_reference():
    """Every audit event type named elsewhere in the contract must be in the catalog."""
    needed = {"meeting_created", "meeting_started", "meeting_ended", "device_registered", "device_connected",
              "device_reconnected", "device_disconnected", "device_audio_resumed", "device_left", "signaling_error",
              "stt_window_dropped", "utterance_created", "utterance_corrected", "enrollment_completed",
              "enrollment_failed", "qa_query", "summary_started", "summary_generated", "summary_failed",
              "export_created", "export_failed", "model_load", "model_error", "index_failed", "server_started"}
    assert needed <= set(AUDIT), sorted(needed - set(AUDIT))
    # ConnectionEvent types map onto audit events one to one.
    mapping = {"connected": "device_connected", "disconnected": "device_disconnected",
               "reconnected": "device_reconnected", "audio_resumed": "device_audio_resumed"}
    assert set(mapping) == ENUMS[("ConnectionEvent", "event_type")]
    assert set(mapping.values()) <= set(AUDIT)
    # Every audit event type the prose in api.md cites in backticks with an underscore exists.
    cited = set(re.findall(r"audit `([a-z_]+)`", API))
    assert cited <= set(AUDIT), sorted(cited - set(AUDIT))


def test_signaling_examples_match_the_catalog():
    problems, seen = [], {"signal-client": set(), "signal-server": set()}
    catalogs = {"signal-client": SIGNAL_CLIENT, "signal-server": SIGNAL_SERVER}
    for line, note, values in DOC_EXAMPLES["transport.md"]:
        if not note or not note.startswith("ws="):
            continue
        kind = note[3:]
        for value in values:
            kind_catalog = catalogs[kind]
            if value.get("type") not in kind_catalog:
                problems.append(f"transport.md:{line}: type {value.get('type')!r} not in the {kind} catalog")
                continue
            seen[kind].add(value["type"])
            extra = set(value) - {"type"} - kind_catalog[value["type"]]
            if extra:
                problems.append(f"transport.md:{line}: {value['type']} has undocumented fields {sorted(extra)}")
            if value["type"] == "error":
                if value["code"] not in SIGNAL_ERRORS:
                    problems.append(f"transport.md:{line}: error code {value['code']!r} not documented")
                elif value["fatal"] != SIGNAL_ERRORS[value["code"]]:
                    problems.append(f"transport.md:{line}: fatal flag disagrees with the error table")
    assert not problems, "\n".join(problems)
    assert seen["signal-client"] == set(SIGNAL_CLIENT), "every client message needs an example"
    assert seen["signal-server"] == set(SIGNAL_SERVER), "every server message needs an example"


def test_signaling_has_no_trickle_or_separate_reconnect_message():
    assert "ice-candidate" not in SIGNAL_CLIENT and "ice-candidate" not in SIGNAL_SERVER
    assert "reconnect" not in SIGNAL_CLIENT


def test_dashboard_examples_match_the_event_catalog():
    problems, seen = [], set()
    for line, note, values in DOC_EXAMPLES["api.md"]:
        if note != "ws=dashboard":
            continue
        for value in values:
            kind = value.get("type")
            if kind not in DASHBOARD:
                problems.append(f"api.md:{line}: type {kind!r} not in the dashboard catalog")
                continue
            seen.add(kind)
            durable, payload = DASHBOARD[kind]
            expected = DASHBOARD_ENVELOPE | payload
            if set(value) != expected:
                problems.append(f"api.md:{line}: {kind} keys {sorted(value)} != envelope+payload {sorted(expected)}")
            if durable and not isinstance(value.get("seq"), int):
                problems.append(f"api.md:{line}: durable event {kind} needs an integer seq")
            if not durable and value.get("seq") is not None:
                problems.append(f"api.md:{line}: ephemeral event {kind} must have seq null")
            if kind == "device_gauges":
                for gauge in value["gauges"]:
                    if set(gauge) != GAUGE_KEYS | {"device_id"}:
                        problems.append(f"api.md:{line}: gauge keys {sorted(gauge)}")
            if kind == "error" and value["code"] not in ERROR_CODES:
                problems.append(f"api.md:{line}: error code {value['code']!r} not in the error catalog")
    assert not problems, "\n".join(problems)
    assert seen == set(DASHBOARD), f"events without an example: {sorted(set(DASHBOARD) - seen)}"


def test_durable_seq_values_in_a_block_increase():
    """Durable dashboard examples appear in seq order (connection_event may share a device_status seq)."""
    for line, note, values in DOC_EXAMPLES["api.md"]:
        if note != "ws=dashboard":
            continue
        durable = [v["seq"] for v in values if DASHBOARD[v["type"]][0]]
        assert durable == sorted(durable)


def test_device_gauges_shape_in_meeting_detail_matches_the_derived_table():
    for line, _, values in DOC_EXAMPLES["api.md"]:
        for value in values:
            for obj in flat_dicts(value):
                if "gauges" in obj and isinstance(obj["gauges"], dict):
                    assert set(obj["gauges"]) == GAUGE_KEYS, f"api.md:{line}"


@pytest.mark.parametrize("method,path,tier", ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_every_route_has_request_response_and_status_codes(method, path, tier):
    section = route_section(method, path)
    assert section is not None, f"no section for {method} {path}"
    for label in ("**Request**", "**Response**", "**Status codes**"):
        assert label in section, f"{method} {path}: missing {label}"
    if tier.startswith("provisional"):
        assert "rovisional" in section, f"{method} {path}: provisional route must say so"
    if method == "WS":
        return
    assert "**Side effects**" in section, f"{method} {path}: missing **Side effects**"
    assert list(fenced_json_blocks(section)), f"{method} {path}: needs a worked JSON example"
    rows = None
    for header, table_rows in tables(section):
        if header[:2] == ["Status", "Code"]:
            rows = table_rows
            break
    assert rows, f"{method} {path}: no status-code table"
    for status, code, _ in rows:
        assert status.isdigit(), f"{method} {path}: bad status {status!r}"
        if code == "—":
            assert status[0] in "23", f"{method} {path}: {status} needs an error code"
        else:
            name = ticks(code)[0]
            assert name in ERROR_CODES, f"{method} {path}: undocumented error code {name}"
            assert ERROR_CODES[name] == int(status), f"{method} {path}: {name} is {ERROR_CODES[name]} in the catalog, {status} here"


def test_error_examples_use_catalogued_codes():
    for line, _, values in DOC_EXAMPLES["api.md"]:
        for value in values:
            if isinstance(value, dict) and set(value) == {"error"} and "code" in value["error"]:
                assert value["error"]["code"] in ERROR_CODES, f"api.md:{line}"


def test_derived_fields_do_not_shadow_stored_fields():
    for entity, derived in DERIVED.items():
        assert entity in ENTITY_FIELDS, f"derived-field table names unknown entity {entity}"
        clash = set(derived) & ENTITY_FIELDS[entity]
        assert not clash, f"{entity}: derived fields shadow stored fields {sorted(clash)}"


def test_schema_changes_recorded_by_con02_are_in_the_data_model():
    assert {"seq"} <= ENTITY_FIELDS["AuditEvent"]
    assert {"status", "error_message"} <= ENTITY_FIELDS["Summary"]
    assert {"status", "error_message"} <= ENTITY_FIELDS["Export"]
    assert ENUMS[("Summary", "status")] == {"pending", "ready", "failed"}
    assert ENUMS[("Export", "status")] == {"pending", "ready", "failed"}


def test_no_stored_entity_field_was_renamed():
    """Fields the earlier contract defined must all still exist (CON-02 adds fields, never renames)."""
    original = {
        "Meeting": {"meeting_id", "title", "status", "created_at", "started_at", "ended_at"},
        "Device": {"device_id", "meeting_id", "joined_at", "status", "is_shared", "declared_speaker_count",
                   "reconnect_count", "user_agent"},
        "Participant": {"participant_id", "meeting_id", "device_id", "display_name", "enrollment_status"},
        "Utterance": {"utterance_id", "meeting_id", "device_id", "participant_id", "text", "t_start", "t_end",
                      "stt_confidence", "attribution_method", "attribution_confidence", "corrected",
                      "original_participant_id", "created_at"},
        "ConnectionEvent": {"event_id", "device_id", "meeting_id", "event_type", "timestamp"},
        "QAQuery": {"query_id", "meeting_id", "mode", "question", "answer", "cited_chunk_ids", "status", "created_at"},
        "Summary": {"summary_id", "meeting_id", "summary_text", "model_identifier", "generated_at"},
        "ActionItem": {"action_item_id", "summary_id", "meeting_id", "text", "owner_participant_id", "status"},
        "Export": {"export_id", "meeting_id", "type", "storage_path", "created_at"},
        "AuditEvent": {"event_id", "meeting_id", "event_type", "component", "timestamp", "payload"},
    }
    for entity, fields in original.items():
        assert fields <= ENTITY_FIELDS[entity], f"{entity} lost {sorted(fields - ENTITY_FIELDS[entity])}"


def test_demo_traceability_covers_every_criterion_and_step():
    section = heading_section(API, lambda lvl, title: title == "Demo traceability")
    assert section
    rows = next(rows for header, rows in tables(section) if header[:1] == ["Source"])
    sources = " ".join(r[0] for r in rows)
    for number in range(1, 9):
        assert f"Criterion {number} " in sources, f"success criterion {number} is not mapped"
    for number in range(1, 8):
        assert f"Demo step {number} " in sources, f"demo step {number} is not mapped"
    for row in rows:
        assert row[2].strip(), f"traceability row {row[0]!r} has no serving endpoint or explanation"


def test_served_pages_are_listed():
    section = heading_section(API, lambda lvl, title: title == "Served pages")
    listed = {ticks(r[1])[0] for _, rows in tables(section) for r in rows}
    assert {"/", "/join/{meeting_id}", "/dashboard/{meeting_id}", "/meetings/{meeting_id}", "/static/{path}"} <= listed


def test_proposed_decisions_are_recorded_as_adrs():
    for number in (15, 16, 17, 18):
        section = heading_section(DECISIONS, lambda lvl, title, n=number: title.startswith(f"ADR-{n}:"))
        assert section, f"ADR-{number} missing"
        assert re.search(r"\*\*Status:\*\* (Proposed|Locked)", section), f"ADR-{number} has no status"
        for label in ("**Decision:**", "**Rationale:**", "**Alternatives considered:**", "**Tradeoffs:**"):
            assert label in section, f"ADR-{number} missing {label}"


def test_gap_ledger_accounts_for_every_gap():
    assert CONTRACTS_LOG.exists(), "logs/contracts.md is the gap ledger"
    text = CONTRACTS_LOG.read_text()
    section = heading_section(text, lambda lvl, title: title.startswith("Gap ledger"))
    assert section
    rows = next(rows for header, rows in tables(section) if header[:1] == ["#"])
    by_id = {r[0]: r for r in rows}
    for number in range(1, 21):
        assert f"G{number}" in by_id, f"G{number} missing from the gap ledger"
    for gap_id, row in by_id.items():
        assert row[2].split()[0].strip("*").lower() in {"resolved", "proposed", "deferred"}, f"{gap_id}: status {row[2]!r}"
        assert row[3].strip(), f"{gap_id}: no owning document"
