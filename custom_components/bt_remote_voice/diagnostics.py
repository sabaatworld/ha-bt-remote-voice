from .preferences import capability_status
from .remote_types import transport_family
from .report import ReportInput, build_report, config_summary, hid_summary, versions

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
        "speaker_skips": c.speaker_skips,
        "spoken_replies": bool(entry.options.get("speaker")),
        "ignored_presses": getattr(c.transport, "ignored_presses", None),
        "limits": dict(
            zip(("max_request_seconds", "max_conversation_seconds"), c.limits(), strict=True)
        ),
        "remote_report": remote_report(entry, c),
        "fire_stats": stats
        if isinstance(stats := getattr(c.transport, "stats", None), dict)
        else None,
    }


def remote_report(entry, coordinator):
    """The shareable Remote Report from what the running entry already knows (masked)."""
    data, options = entry.data or {}, entry.options or {}
    detected = data.get("detected") or {}
    reports = getattr(coordinator.transport, "descriptor_reports", None)
    reports = reports if isinstance(reports, tuple) else None
    return build_report(
        ReportInput(
            **versions(),
            adapter={"name": detected.get("adapter")},
            remote={
                **detected,
                "name": data.get("name"),
                "address": data.get("address"),
                "family": transport_family(options, data),
                "remote_type": options.get("remote_type", "auto"),
            },
            hid=hid_summary(
                reports,
                getattr(coordinator.transport, "descriptor_hex", None),
                getattr(coordinator.transport, "hidraw_nodes", None),
            )
            if reports is not None
            else {},
            config=config_summary(options, data),
            outcome={"error": coordinator.last_error or "none", "stage": "running"},
        )
    )
