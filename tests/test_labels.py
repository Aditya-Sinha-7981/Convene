from server.attribution.labels import confidence_reasons, is_low_confidence, speaker_label
from server.repositories.models import Participant, Utterance


def row(**changes):
    values = dict(utterance_id="u", meeting_id="m", device_id="d", participant_id="p", text="hello",
                  t_start="2026-09-23T00:00:00.000Z", t_end="2026-09-23T00:00:01.000Z",
                  stt_confidence=.9, attribution_method="device", attribution_confidence=.95,
                  created_at="2026-09-23T00:00:01.000Z")
    values.update(changes)
    return Utterance(**values)


def test_named_and_generic_labels_are_stable():
    person = Participant("p", "m", "d", "Priya", "not_required")
    assert speaker_label(row(), person, 1) == "Priya"
    assert speaker_label(row(participant_id=None, attribution_method="generic_unresolved"), None, 2) \
        == "Speaker on Phone 2"


def test_attribution_low_confidence_uses_one_server_threshold():
    assert not is_low_confidence(row(attribution_confidence=.8), .8)
    assert is_low_confidence(row(attribution_confidence=.799), .8)
    assert is_low_confidence(row(participant_id=None, attribution_method="generic_unresolved",
                                 attribution_confidence=.9), .8)
    assert not is_low_confidence(row(attribution_method="manual_correction", attribution_confidence=1), .8)


def test_stt_and_attribution_uncertainty_remain_separately_identifiable():
    assert confidence_reasons(row(stt_confidence=.3), .8) == ["transcription"]
    assert confidence_reasons(row(stt_confidence=.3, attribution_confidence=.3), .8) \
        == ["attribution", "transcription"]
