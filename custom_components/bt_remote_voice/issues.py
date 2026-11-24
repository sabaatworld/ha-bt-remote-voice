"""Repairs for settings that depend on things outside this integration.

The reply speaker and the Assist pipeline can be removed or renamed after a remote
is configured. A Repairs notice tells the owner at startup, not only when a request
fails. These issues are not fixable in place: the text points to Configure.
"""

from homeassistant.core import CoreState, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event

from .config_flow import pipelines
from .preferences import DOMAIN, PIPELINE_PREFERRED, SPEAKER_SKIP_LIMIT


def _set(hass, entry, key, severity, missing, placeholders):
    issue_id = f"{key}_{entry.entry_id}"
    if missing:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=key,
            translation_placeholders={"remote": entry.title, **placeholders},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def sync_issues(hass, entry):
    """Create or clear this remote's issues from the current configuration."""
    pipeline = entry.options.get("pipeline_id", entry.data.get("pipeline_id"))
    _set(
        hass,
        entry,
        "pipeline_missing",
        ir.IssueSeverity.ERROR,
        bool(pipeline) and pipeline != PIPELINE_PREFERRED and pipeline not in pipelines(hass),
        {"pipeline": pipeline},
    )
    # Replies are only played in commands mode, so only then is the speaker relied on.
    mode = entry.options.get("mode", entry.data.get("mode", "transcription"))
    speaker = (entry.options.get("speaker") or "") if mode == "commands" else ""
    # Only a removed entity is a configuration problem; "unavailable" is temporary.
    _set(
        hass,
        entry,
        "speaker_missing",
        ir.IssueSeverity.WARNING,
        bool(speaker) and hass.states.get(speaker) is None,
        {"speaker": speaker},
    )
    # A reload starts the counter again, so a stale hint never outlives the entry's last run.
    sync_speaker_issue(
        hass, entry, getattr(getattr(entry, "runtime_data", None), "speaker_skips", 0)
    )


@callback
def sync_speaker_issue(hass, entry, skips):
    """Warn when replies keep getting lost because the speaker is not working.

    Only a run of lost replies raises it, never one blip; a spoken reply, the speaker coming
    back, or a reload of the entry clears it. ``speaker_missing`` covers a removed entity.
    """
    _set(
        hass,
        entry,
        "speaker_unavailable",
        ir.IssueSeverity.WARNING,
        skips >= SPEAKER_SKIP_LIMIT,
        {"speaker": entry.options.get("speaker") or ""},
    )


@callback
def clear_issues(hass, entry):
    for key in ("pipeline_missing", "speaker_missing", "speaker_unavailable"):
        ir.async_delete_issue(hass, DOMAIN, f"{key}_{entry.entry_id}")


@callback
def watch_speaker(hass, entry):
    """Re-check when the reply speaker appears or disappears; returns the unsubscribe."""
    speaker = entry.options.get("speaker") or ""
    if not speaker:
        return lambda: None

    @callback
    def changed(event):
        # While starting, the started check decides; other integrations may not be loaded.
        if hass.state is CoreState.running:
            new, old = event.data.get("new_state"), event.data.get("old_state")
            coordinator = getattr(entry, "runtime_data", None)
            gone = (None, "unknown", "unavailable")
            came_back = old is None or old.state in gone
            # Only a return from unavailable counts; a speaker that merely changes between idle,
            # buffering and playing while replies keep failing must not wipe the count.
            if new is not None and new.state not in gone and came_back and coordinator:
                coordinator.speaker_skips = 0
            sync_issues(hass, entry)

    return async_track_state_change_event(hass, [speaker], changed)
