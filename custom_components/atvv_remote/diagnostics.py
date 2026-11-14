from .preferences import capability_status

"""Diagnostics intentionally contain no identifiers, transcripts or audio."""


async def async_get_config_entry_diagnostics(hass, entry):
    c = entry.runtime_data
    return {
        "ready": c.ready,
        "microphone_active": c.mic_active,
        "sessions": c.sessions,
        "failures": c.failures,
        "last_error": c.last_error,
        "mode": entry.options.get("mode", entry.data.get("mode", "transcription")),
        "profile": c.caps.profile if c.caps else None,
        "connections": c.reconnects,
        "last_session": c.last_session,
        "last_timings": c.last_timings,
        "capture_history": c.history.snapshot(),
        "rejected": c.rejected,
        "queued": c.queued,
        "logical_turns": c.turns,
        "last_outcome": c.last_outcome,
        "capabilities": capability_status(),
        "last_failure": c.last_failure,
        "spoken_replies": bool(entry.options.get("speaker")),
    }
