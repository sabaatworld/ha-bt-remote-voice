"""Provider-neutral preferences; missing options retain existing behavior."""

MAX_HEAD_START = 10  # seconds

CHOICES = {
    "output_mode": {"auto": "Automatic streaming", "buffered": "Wait for complete reply"},
    "busy_processing": {
        "reject": "Reject new request",
        "queue": "Queue one request",
        "replace": "Replace current request",
    },
    "busy_playback": {
        "reject": "Reject new request",
        "queue": "Queue one request",
        "replace": "Replace current reply",
    },
    "context_mode": {"reuse": "Reuse conversation", "fresh": "Fresh conversation per request"},
    "input_mode": {
        "single": "One request per hold",
        "conversation": "Conversation while held (alternating)",
    },
}
DEFAULTS = {
    "output_mode": "auto",
    "busy_processing": "reject",
    "busy_playback": "reject",
    "context_mode": "reuse",
    "input_mode": "single",
    "stream_head_start": 0,
}


def preferences(options):
    result = {**DEFAULTS, **options}
    if any(result[key] not in values for key, values in CHOICES.items()):
        raise ValueError("invalid_preferences")
    head_start = result["stream_head_start"]
    if (
        isinstance(head_start, bool)
        or not isinstance(head_start, (int, float))
        or not 0 <= head_start <= MAX_HEAD_START
    ):
        raise ValueError("invalid_preferences")
    return result


def capability_status():
    return {
        "streaming": "pipeline_advertised",
        "alternating_held_turns": True,
        "full_duplex": False,
        "release_before_provider_action": "provider_dependent_unverified",
        "full_duplex_reason": "No verified pipeline interruption, speaker flush and echo-reference contract",
    }


ERROR_MESSAGES = {
    "busy": "Still answering the previous request.",
    "queue_full": "A request is already waiting. Please try after the reply.",
    "queue_expired": "The waiting request expired. Please press and speak again.",
    "capture_limit": "The microphone capture limit was reached. Please use a shorter request.",
    "audio_sequence_gap": "Audio synchronization failed. Please press and try again.",
    "incomplete_utterance": "Audio ended before the request was complete.",
    "audio_stalled": "Microphone audio stopped arriving. Please try again.",
    "playback_stop_unverified": "Could not verify that the previous reply stopped. Wait until the speaker is idle.",
    "generation_retirement_failed": "The previous request has not stopped yet. Please wait.",
}


def error_message(code):
    return ERROR_MESSAGES.get(code, code.replace("_", " ").capitalize() if code else None)
