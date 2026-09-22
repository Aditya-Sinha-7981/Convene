"""The single speaker-label and confidence policy used by every consumer."""
from ..repositories.models import Participant, Utterance


def speaker_label(utterance: Utterance, participant: Participant | None, device_ordinal: int) -> str:
    if participant is not None:
        return participant.display_name
    return f"Speaker on Phone {device_ordinal}"


def confidence_reasons(utterance: Utterance, threshold: float) -> list[str]:
    reasons = []
    if utterance.attribution_method == "generic_unresolved" or utterance.attribution_confidence < threshold:
        reasons.append("attribution")
    if utterance.stt_confidence < threshold:
        reasons.append("transcription")
    return reasons


def is_low_confidence(utterance: Utterance, threshold: float) -> bool:
    """The contract's label flag concerns attribution; STT uncertainty is reported separately."""
    return utterance.attribution_method == "generic_unresolved" or utterance.attribution_confidence < threshold
