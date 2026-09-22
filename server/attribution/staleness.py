from ..repositories import audit_events


def transcript_high_water(conn, meeting_id: str) -> int:
    return audit_events.transcript_high_water(conn, meeting_id)


def artifact_is_stale(conn, meeting_id: str, input_as_of_seq: int) -> bool:
    return transcript_high_water(conn, meeting_id) > input_as_of_seq
