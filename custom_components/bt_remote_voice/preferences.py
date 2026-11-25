"""Provider-neutral preferences; missing options retain existing behavior."""

from homeassistant.components.assist_pipeline import OPTION_PREFERRED
from homeassistant.helpers.translation import async_get_cached_translations

from .remote_types import DEFAULT_REMOTE_TYPE, REMOTE_TYPES

DOMAIN = "bt_remote_voice"

# Stored as pipeline_id to follow whichever Assist pipeline Home Assistant prefers (its own
# select option value); resolved at request time, so later changes apply to the next request.
PIPELINE_PREFERRED = OPTION_PREFERRED

SPEAKER_SKIP_LIMIT = 3  # consecutive lost replies before the Repairs hint appears
MAX_HEAD_START = 10  # seconds
REQUEST_RANGE = (5, 120, 5)  # seconds: (min, max, step)
CONVERSATION_RANGE = (30, 600, 10)
PRESS_RANGE = (0, 0.5, 0.05)  # seconds; 0 turns the short-press filter off
GAP_RANGE = (0, 500, 20)  # milliseconds of lost audio covered with silence; 0 turns it off

# Order of the options form and of the README configuration table (spec: option order).
FORM_ORDER = (
    "pipeline_id",
    "mode",
    "speaker",
    "reply_condition",
    "output_mode",
    "stream_head_start",
    "busy_processing",
    "busy_playback",
    "context_mode",
    "input_mode",
    "remote_type",
    "min_press_seconds",
    "ignore_early_stop",
    "gap_tolerance_ms",
    "max_request_seconds",
    "max_conversation_seconds",
)

# Allowed values per choice option. Labels live in translations/en.json under "selector"
# (translation_key == option name), never in code.
CHOICES = {
    "output_mode": ("auto", "buffered"),
    "busy_processing": ("reject", "queue", "replace"),
    "busy_playback": ("reject", "queue", "replace"),
    "context_mode": ("reuse", "fresh"),
    "input_mode": ("single", "conversation"),
    "remote_type": REMOTE_TYPES,
}
DEFAULTS = {
    "output_mode": "auto",
    "busy_processing": "reject",
    "busy_playback": "reject",
    "context_mode": "reuse",
    "input_mode": "single",
    "remote_type": DEFAULT_REMOTE_TYPE,
    "stream_head_start": 0,
    "min_press_seconds": 0,
    "ignore_early_stop": False,
    "gap_tolerance_ms": 0,
    "max_request_seconds": 30,
    "max_conversation_seconds": 120,
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
    for key, (low, high, _step) in (
        ("min_press_seconds", PRESS_RANGE),
        ("max_request_seconds", REQUEST_RANGE),
        ("max_conversation_seconds", CONVERSATION_RANGE),
    ):
        value = result[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not low <= value <= high
        ):
            raise ValueError("invalid_preferences")
    if result["max_conversation_seconds"] < result["max_request_seconds"]:
        raise ValueError("invalid_preferences")
    if not isinstance(result["ignore_early_stop"], bool):
        raise ValueError("invalid_preferences")
    gap = result["gap_tolerance_ms"]
    if (
        isinstance(gap, bool)
        or not isinstance(gap, (int, float))
        or not GAP_RANGE[0] <= gap <= GAP_RANGE[1]
    ):
        raise ValueError("invalid_preferences")
    return result


def capture_settings(options, family=None):
    """Early-stop and gap handling for a capture, from stored options (bad values mean off).

    The single place that decides when the settings apply, for real requests and for the test:
    ``early_stop_applies`` is False for Fire remotes (their STOP is the real key release) and in
    Alternating conversation (a held conversation manages its own turns); then the setting is off.
    """
    early = options.get("ignore_early_stop", DEFAULTS["ignore_early_stop"])
    gap = options.get("gap_tolerance_ms", DEFAULTS["gap_tolerance_ms"])
    usable_gap = (
        isinstance(gap, (int, float))
        and not isinstance(gap, bool)
        and GAP_RANGE[0] <= gap <= GAP_RANGE[1]
    )
    applies = family != "fire" and options.get("input_mode", "single") != "conversation"
    return {
        "ignore_early_stop": early is True and applies,
        "early_stop_applies": applies,
        "max_gap_ms": gap if usable_gap else DEFAULTS["gap_tolerance_ms"],
    }


def capture_buffer_bytes(seconds):
    """Audio buffer cap for a capture limit: 16 kHz 16-bit mono plus a second of headroom
    (frames arrive in bursts, so a capture must hit its time limit before its byte cap);
    never below today's cap."""
    return max(960000, int((seconds + 1) * 32000))


def capability_status():
    return {
        "streaming": "pipeline_advertised",
        "alternating_held_turns": True,
        "full_duplex": False,
        "release_before_provider_action": "provider_dependent_unverified",
        "full_duplex_reason": "No verified pipeline interruption, speaker flush and echo-reference contract",
    }


# Stable failure codes. Each has a translated state under entity.sensor.last_error.state;
# keep this tuple and translations/en.json in step (a test enforces it).
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
    "no_preferred_pipeline",
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
    "hidraw_unavailable",
    "hidraw_permission_denied",
    "opus_decoder_unavailable",
    "fire_voice_unsupported",
    "invalid_opus_packet",
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
