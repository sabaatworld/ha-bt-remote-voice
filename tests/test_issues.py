from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.helpers import issue_registry as ir

from custom_components.bt_remote_voice.issues import clear_issues, sync_issues, watch_speaker

DOMAIN = "bt_remote_voice"


def _entry(options=None, data=None):
    return SimpleNamespace(
        entry_id="e1",
        title="Office Remote",
        data=data or {"pipeline_id": "p1"},
        options=options or {},
    )


def _ids(hass):
    return {i.issue_id: i for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN}


def _pipelines(found):
    return patch("custom_components.bt_remote_voice.issues.pipelines", return_value=found)


async def test_missing_pipeline_raises_a_repair_with_the_remote_and_pipeline_named(hass):
    with _pipelines({"other": "Other"}):
        sync_issues(hass, _entry())
    issue = _ids(hass)["pipeline_missing_e1"]
    assert issue.translation_key == "pipeline_missing"
    assert issue.translation_placeholders == {"remote": "Office Remote", "pipeline": "p1"}
    assert issue.severity == ir.IssueSeverity.ERROR and not issue.is_fixable


async def test_issue_clears_when_the_pipeline_exists_again(hass):
    with _pipelines({}):
        sync_issues(hass, _entry())
    assert "pipeline_missing_e1" in _ids(hass)
    with _pipelines({"p1": "Home Assistant"}):
        sync_issues(hass, _entry())
    assert _ids(hass) == {}


async def test_missing_speaker_raises_a_warning_and_none_or_present_does_not(hass):
    entry = _entry(options={"speaker": "media_player.office", "mode": "commands"})
    with _pipelines({"p1": "P"}):
        sync_issues(hass, entry)
        issue = _ids(hass)["speaker_missing_e1"]
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.translation_placeholders["speaker"] == "media_player.office"
        hass.states.async_set("media_player.office", "idle")
        sync_issues(hass, entry)
        assert _ids(hass) == {}
        sync_issues(hass, _entry(options={"speaker": ""}))
    assert _ids(hass) == {}


async def test_an_unavailable_speaker_is_not_a_configuration_problem(hass):
    hass.states.async_set("media_player.office", "unavailable")
    with _pipelines({"p1": "P"}):
        sync_issues(hass, _entry(options={"speaker": "media_player.office", "mode": "commands"}))
    assert _ids(hass) == {}


async def test_watching_the_speaker_raises_and_clears_the_issue(hass):
    from homeassistant.core import CoreState

    hass.set_state(CoreState.running)
    entry = _entry(options={"speaker": "media_player.office", "mode": "commands"})
    hass.states.async_set("media_player.office", "idle")
    with _pipelines({"p1": "P"}):
        stop = watch_speaker(hass, entry)
        hass.states.async_remove("media_player.office")
        await hass.async_block_till_done()
        assert "speaker_missing_e1" in _ids(hass)
        hass.states.async_set("media_player.office", "idle")
        await hass.async_block_till_done()
        assert _ids(hass) == {}
        stop()


async def test_clear_issues_removes_both(hass):
    with _pipelines({}):
        sync_issues(hass, _entry(options={"speaker": "media_player.gone", "mode": "commands"}))
    assert len(_ids(hass)) == 2
    clear_issues(hass, _entry())
    assert _ids(hass) == {}


async def test_speaker_is_not_flagged_when_the_voice_mode_never_plays_replies(hass):
    entry = _entry(options={"speaker": "media_player.gone", "mode": "transcription"})
    with _pipelines({"p1": "P"}):
        sync_issues(hass, entry)
    assert _ids(hass) == {}
    entry = _entry(options={"speaker": "media_player.gone", "mode": "commands"})
    with _pipelines({"p1": "P"}):
        sync_issues(hass, entry)
    assert "speaker_missing_e1" in _ids(hass)


async def test_options_pipeline_overrides_the_one_saved_at_setup(hass):
    entry = _entry(options={"pipeline_id": "p2"}, data={"pipeline_id": "p1"})
    with _pipelines({"p1": "P1"}):
        sync_issues(hass, entry)
    assert _ids(hass)["pipeline_missing_e1"].translation_placeholders["pipeline"] == "p2"


async def test_syncing_again_keeps_one_issue_and_the_users_dismissal(hass):
    with _pipelines({}):
        sync_issues(hass, _entry())
        ir.async_ignore_issue(hass, DOMAIN, "pipeline_missing_e1", True)
        sync_issues(hass, _entry())
    issues = [i for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN]
    assert len(issues) == 1 and issues[0].dismissed_version is not None


async def test_watcher_does_not_evaluate_before_home_assistant_has_started(hass):
    from homeassistant.core import CoreState

    hass.set_state(CoreState.not_running)
    entry = _entry(options={"speaker": "media_player.office", "mode": "commands"})
    hass.states.async_set("media_player.office", "idle")
    with _pipelines({}):
        stop = watch_speaker(hass, entry)
        hass.states.async_set("media_player.office", "playing")
        await hass.async_block_till_done()
        stop()
    assert _ids(hass) == {}
