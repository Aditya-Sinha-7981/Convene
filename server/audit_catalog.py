"""The audit event catalog: the code copy of the table in docs/data-model.md ("Audit event catalog").

``tests/test_audit_emit.py`` asserts this equals the documented table, so the two cannot drift. Adding an
event type means adding a row to the documentation first.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class EventSpec:
    component: str
    payload_keys: frozenset
    meeting_required: bool = True  # False: the event can occur outside any single meeting


def _spec(component: str, *keys: str, meeting_required: bool = True) -> EventSpec:
    return EventSpec(component, frozenset(keys), meeting_required)


CATALOG: dict[str, EventSpec] = {
    "server_started": _spec("api", "reconciled_devices", "reconciled_meetings", meeting_required=False),
    "meeting_created": _spec("api", "title"),
    "meeting_started": _spec("transport", "first_device_id"),
    "meeting_ended": _spec("api", "utterance_count", "device_count"),
    "hook_failed": _spec("api", "hook", "error"),
    "device_registered": _spec("registry", "device_id", "is_shared", "declared_speaker_count", "user_agent"),
    "device_connected": _spec("transport", "device_id", "reconnect_count"),
    "device_reconnected": _spec("transport", "device_id", "reconnect_count", "via", "remote_addr", "user_agent"),
    "device_disconnected": _spec("transport", "device_id", "reason"),
    "device_audio_resumed": _spec("transport", "device_id", "gap_ms"),
    "device_left": _spec("transport", "device_id", "reason"),
    "signaling_error": _spec("transport", "device_id", "code", "remote_addr", meeting_required=False),
    "stt_window_dropped": _spec("stt", "device_id", "window_id", "reason"),
    "model_load": _spec("models", "resource_type", "model_identifier", "runtime", "duration_ms",
                        meeting_required=False),
    "model_error": _spec("models", "resource_type", "model_identifier", "device_id", "window_id", "related_id",
                         "error", meeting_required=False),
    "utterance_created": _spec("attribution", "utterance_id", "device_id", "participant_id", "attribution_method",
                               "attribution_confidence", "stt_confidence"),
    "utterance_corrected": _spec("attribution", "utterance_id", "from", "to", "changed", "created_participant_id"),
    "enrollment_completed": _spec("speaker", "device_id", "participant_id", "enrollment_id", "quality_flag",
                                  "sample_duration_s"),
    "enrollment_failed": _spec("speaker", "device_id", "participant_id", "reason"),
    "index_failed": _spec("rag", "utterance_id_start", "utterance_id_end", "error"),
    "qa_query": _spec("rag", "query_id", "mode", "status", "meeting_ids", "chunk_count", "duration_ms",
                      "error_code", "reason", "best_similarity", meeting_required=False),
    "summary_started": _spec("summary", "summary_id", "trigger"),
    "summary_generated": _spec("summary", "summary_id", "input_as_of_seq", "model_identifier", "action_item_count",
                               "attempts", "duration_ms", "drain_timed_out"),
    "summary_failed": _spec("summary", "summary_id", "error_code", "attempts", "duration_ms", "drain_timed_out"),
    "export_created": _spec("export", "export_id", "summary_id", "input_as_of_seq", "type"),
    "export_failed": _spec("export", "export_id", "error_code"),
}

# ConnectionEvent.event_type -> the AuditEvent type it projects (docs/data-model.md, ConnectionEvent).
CONNECTION_TO_AUDIT = {
    "connected": "device_connected",
    "disconnected": "device_disconnected",
    "reconnected": "device_reconnected",
    "audio_resumed": "device_audio_resumed",
}
