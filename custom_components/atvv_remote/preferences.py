"""Provider-neutral preferences; missing options retain existing behavior."""

from homeassistant.helpers.translation import async_get_cached_translations

DOMAIN = "atvv_remote"

MAX_HEAD_START = 10  # seconds

# Allowed values per choice option. Labels live in strings.json under "selector"
# (translation_key == option name), never in code.
CHOICES = {
    "output_mode": ("auto", "buffered"),
    "busy_processing": ("reject", "queue", "replace"),
    "busy_playback": ("reject", "queue", "replace"),
    "context_mode": ("reuse", "fresh"),
    "input_mode": ("single", "conversation"),
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


# Stable failure codes. Each has a translated state under entity.sensor.last_error.state;
# keep this tuple and strings.json in step (a test enforces it).
ERROR_CODES = (
    "busy",
    "queue_full",
    "queue_expired",
    "queue_discarded",
    "queue_failed",
    "capture_limit",
    "buffer_overflow",
    "audio_sequence_gap",
    "invalid_audio_sync",
    "invalid_audio_frame",
    "invalid_control",
    "unsupported_audio_start",
    "incomplete_utterance",
    "audio_stalled",
    "audio_failed",
    "no_audio",
    "no_speech",
    "held_mode_unsupported",
    "held_failed",
    "held_turn_failed",
    "remote_stopped",
    "disconnected",
    "cancelled",
    "pipeline_finished_early",
    "pipeline_timeout",
    "pipeline_failed",
    "pipeline_removed",
    "playback_stop_unverified",
    "playback_stop_failed",
    "playback_start_timeout",
    "playback_finish_timeout",
    "playback_interrupted",
    "speaker_unavailable",
    "speaker_busy",
    "speaker_cannot_play",
    "missing_tts_audio",
    "generation_retirement_failed",
    "replacement_retirement_failed",
    "microphone_open_failed",
    "microphone_open_rejected",
    "microphone_start_timeout",
    "microphone_close_failed",
    "microphone_keepalive_failed",
    "active_generation_cancelled",
    "wake_remote",
    "bluetooth_unavailable",
    "bluetooth_operation_failed",
    "not_paired",
    "multiple_adapters",
    "voice_service_unavailable",
    "unsupported_write",
    "unsupported_capabilities",
    "unsupported_codec",
    "invalid_frame_size",
)


def error_state(code):
    """Sensor state for a failure code: a known code, ``none`` or ``other``."""
    if not code:
        return "none"
    return code if code in ERROR_CODES else "other"


def error_message(hass, code):
    """Readable text for a code in HA's language, from the integration's translations."""
    if not code:
        return None
    language = getattr(getattr(hass, "config", None), "language", None)
    if language is None:
        return None
    translations = async_get_cached_translations(hass, language, "entity", DOMAIN)
    base = f"component.{DOMAIN}.entity.sensor.last_error.state."
    return translations.get(base + error_state(code))
