"""A reply speaker that cannot play loses the spoken reply, never the command."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.helpers import issue_registry as ir

from custom_components.bt_remote_voice.assist_satellite import RemoteSatellite
from custom_components.bt_remote_voice.coordinator import VoiceCoordinator
from custom_components.bt_remote_voice.issues import (
    SPEAKER_SKIP_LIMIT,
    clear_issues,
    sync_issues,
    watch_speaker,
)
from custom_components.bt_remote_voice.session import AudioFailure, VoiceSession
from tests.test_coordinator import Entry, FakeHass

PLAY = "custom_components.bt_remote_voice.assist_satellite.play_reply"
DOMAIN = "bt_remote_voice"


def _satellite(speaker_state=None, options=None):
    c = VoiceCoordinator(
        FakeHass(),
        SimpleNamespace(
            data={**Entry.data, "pipeline_id": "p"},
            options={"mode": "commands", "speaker": "media_player.test", **(options or {})},
        ),
    )
    sat = RemoteSatellite(c)
    states = SimpleNamespace(
        get=lambda _e: None if speaker_state is None else SimpleNamespace(state=speaker_state)
    )
    sat.hass = SimpleNamespace(states=states) if speaker_state is not False else SimpleNamespace()
    return c, sat


async def _session():
    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    s.release()
    _ = [x async for x in s.stream()]
    return s


def _stream_events(sat):
    sat.on_pipeline_event(
        PipelineEvent(
            PipelineEventType.RUN_START, {"tts_output": {"url": "/ours", "stream_response": True}}
        )
    )
    sat.on_pipeline_event(
        PipelineEvent(PipelineEventType.INTENT_PROGRESS, {"tts_start_streaming": True})
    )


@pytest.mark.parametrize("code", ["speaker_unavailable", "speaker_busy", "speaker_cannot_play"])
async def test_mid_stream_speaker_problem_does_not_cancel_the_command(code):
    c, sat = _satellite(speaker_state="idle")
    s = await _session()
    finished = asyncio.Event()
    seen = {}

    async def accept(*args, **kwargs):
        _stream_events(sat)
        await asyncio.sleep(0.05)  # playback has failed by now; the intent is still running
        seen["phase"] = c.phase
        seen["outcome"] = dict(c.last_outcome)
        finished.set()

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(PLAY, AsyncMock(side_effect=AudioFailure(code))),
    ):
        await asyncio.wait_for(sat.run(s), 1)  # returns normally: the request succeeded
    assert finished.is_set()
    assert seen["phase"] == "processing"  # busy_playback policy must not apply any more
    assert seen["outcome"]["outcome"] == "reply_skipped" and seen["outcome"]["code"] == code
    assert c.failures == 0 and c.last_failure is None


async def test_buffered_playback_failure_after_generation_is_a_skipped_reply():
    c, sat = _satellite(speaker_state="idle", options={"output_mode": "buffered"})
    s = await _session()

    async def accept(*args, **kwargs):
        sat.on_pipeline_event(
            PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"media_id": "m"}})
        )

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(PLAY, AsyncMock(side_effect=AudioFailure("speaker_unavailable"))),
    ):
        await asyncio.wait_for(sat.run(s), 1)
    assert c.last_outcome["outcome"] == "reply_skipped"
    assert c.last_outcome["code"] == "speaker_unavailable"
    assert c.failures == 0


@pytest.mark.parametrize(
    "code", ["playback_interrupted", "playback_stop_failed", "missing_tts_audio"]
)
async def test_other_playback_failures_stay_failures(code):
    c, sat = _satellite(speaker_state="idle", options={"output_mode": "buffered"})
    s = await _session()

    async def accept(*args, **kwargs):
        sat.on_pipeline_event(
            PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"media_id": "m"}})
        )

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(PLAY, AsyncMock(side_effect=AudioFailure(code))),
        pytest.raises(AudioFailure, match=code),
    ):
        await asyncio.wait_for(sat.run(s), 1)


@pytest.mark.parametrize("state", ["unavailable", "unknown", None])
async def test_unavailable_speaker_at_start_runs_the_command_without_tts(state):
    c, sat = _satellite(speaker_state=state)
    if state is None:  # entity missing altogether
        sat.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: None))
    s = await _session()
    stages = []

    async def accept(*args, **kwargs):
        stages.append(kwargs["end_stage"])

    play = AsyncMock()
    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(PLAY, play),
    ):
        await asyncio.wait_for(sat.run(s), 1)
    assert [str(x) for x in stages] == ["intent"] or stages[0].value == "intent"
    play.assert_not_called()
    assert c.last_outcome["outcome"] == "reply_skipped"
    assert c.last_outcome["code"] == "speaker_unavailable"
    # a removed entity is a Repairs issue of its own and is not counted
    assert c.speaker_skips == (0 if state is None else 1) and c.failures == 0


async def test_condition_skip_is_the_only_outcome_and_speaker_not_consulted():
    c, sat = _satellite(speaker_state="unavailable")
    c.reply_condition = SimpleNamespace(decide=lambda _ctx: (False, "condition_false"))
    s = await _session()

    async def accept(*args, **kwargs):
        return None

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(PLAY, AsyncMock()),
    ):
        await asyncio.wait_for(sat.run(s), 1)
    assert c.last_outcome["code"] == "condition_false"
    assert c.speaker_skips == 0


async def test_busy_speaker_neither_counts_nor_resets_and_spoken_reply_resets():
    c, _ = _satellite()
    with patch("custom_components.bt_remote_voice.issues.sync_speaker_issue") as sync:
        c.note_reply("speaker_unavailable")
        c.note_reply("speaker_busy")
        assert c.speaker_skips == 1
        c.note_reply("speaker_cannot_play")
        c.note_reply("speaker_unavailable")
        assert c.speaker_skips == SPEAKER_SKIP_LIMIT and sync.call_count == 1
        c.note_reply()  # a spoken reply
        assert c.speaker_skips == 0 and sync.call_count == 2


def _entry(skips=0, speaker="media_player.office"):
    return SimpleNamespace(
        entry_id="e1",
        title="Office Remote",
        data={"pipeline_id": "p1"},
        options={"speaker": speaker, "mode": "commands"},
        runtime_data=SimpleNamespace(speaker_skips=skips),
    )


def _ids(hass):
    return {i.issue_id: i for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN}


async def test_repair_after_three_lost_replies_and_clears_when_speaker_returns(hass):
    from homeassistant.core import CoreState

    hass.set_state(CoreState.running)
    hass.states.async_set("media_player.office", "unavailable")
    entry = _entry()
    pipelines = patch(
        "custom_components.bt_remote_voice.issues.pipelines", return_value={"p1": "P"}
    )
    with pipelines:
        sync_issues(hass, entry)
        assert "speaker_unavailable_e1" not in _ids(hass)
        entry.runtime_data.speaker_skips = SPEAKER_SKIP_LIMIT
        sync_issues(hass, entry)
        issue = _ids(hass)["speaker_unavailable_e1"]
        assert issue.translation_key == "speaker_unavailable"
        assert issue.translation_placeholders == {
            "remote": "Office Remote",
            "speaker": "media_player.office",
        }
        assert issue.severity == ir.IssueSeverity.WARNING and not issue.is_fixable
        unsub = watch_speaker(hass, entry)
        hass.states.async_set("media_player.office", "idle")  # unavailable -> idle: it is back
        await hass.async_block_till_done()
        unsub()
    assert entry.runtime_data.speaker_skips == 0
    assert "speaker_unavailable_e1" not in _ids(hass)


async def test_repair_survives_speaker_unavailable_state_changes_and_is_removed_with_entry(hass):
    from homeassistant.core import CoreState

    hass.set_state(CoreState.running)
    entry = _entry(skips=SPEAKER_SKIP_LIMIT)
    with patch("custom_components.bt_remote_voice.issues.pipelines", return_value={"p1": "P"}):
        sync_issues(hass, entry)
        hass.states.async_set("media_player.office", "unavailable")
        await hass.async_block_till_done()
        unsub = watch_speaker(hass, entry)
        hass.states.async_set("media_player.office", "unknown")
        await hass.async_block_till_done()
        sync_issues(hass, entry)  # level-triggered: counter still at the limit
        unsub()
    assert "speaker_unavailable_e1" in _ids(hass)
    clear_issues(hass, entry)
    assert "speaker_unavailable_e1" not in _ids(hass)


async def test_speaker_lost_after_playback_started_stays_a_failure():
    err = AudioFailure("speaker_unavailable")
    err.after_start = True  # play_reply marks a speaker that went offline mid-playback
    c, sat = _satellite(speaker_state="idle", options={"output_mode": "buffered"})
    s = await _session()

    async def accept(*args, **kwargs):
        sat.on_pipeline_event(
            PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"media_id": "m"}})
        )

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(PLAY, AsyncMock(side_effect=err)),
        pytest.raises(AudioFailure, match="speaker_unavailable"),
    ):
        await asyncio.wait_for(sat.run(s), 1)
    assert c.speaker_skips == 0


async def test_play_reply_marks_unavailability_before_and_after_start(hass):
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    from custom_components.bt_remote_voice.playback import play_reply

    features = {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)}
    # never started: the speaker is already unavailable
    hass.states.async_set("media_player.test_speaker", "unavailable")
    with pytest.raises(AudioFailure) as before:
        await play_reply(hass, "media_player.test_speaker", {"media_id": "m"})
    assert str(before.value) == "speaker_unavailable"
    assert not getattr(before.value, "after_start", False)

    # started, then lost
    hass.states.async_set("media_player.test_speaker", "idle", features)

    async def call(*args, **kwargs):
        if args[1] == "media_stop":
            return
        hass.states.async_set(
            "media_player.test_speaker", "playing", {**features, "media_content_id": "m"}
        )
        hass.states.async_set("media_player.test_speaker", "unavailable")

    with patch.object(type(hass.services), "async_call", side_effect=call):
        with pytest.raises(AudioFailure) as after:
            await play_reply(hass, "media_player.test_speaker", {"media_id": "m"})
    assert str(after.value) == "speaker_unavailable" and after.value.after_start is True


async def test_next_request_decides_again_and_a_spoken_reply_resets_the_count():
    c, sat = _satellite(speaker_state="unavailable")
    s = await _session()

    async def accept(*args, **kwargs):
        return None

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(PLAY, AsyncMock()),
    ):
        await asyncio.wait_for(sat.run(s), 1)
        assert c.speaker_skips == 1 and sat.speaker_skip == "speaker_unavailable"
        # the speaker is back for the second request (as in a held conversation's next turn)
        sat.hass.states = SimpleNamespace(get=lambda _e: SimpleNamespace(state="idle"))
        c.speaker_skips = 2

        async def accept_tts(*args, **kwargs):
            sat.on_pipeline_event(
                PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"media_id": "m"}})
            )

        sat.async_accept_pipeline_from_satellite = accept_tts  # type: ignore[method-assign]
        s2 = await _session()
        with patch.object(sat, "tts_response_finished"):
            await asyncio.wait_for(sat.run(s2), 1)
    assert sat.speaker_skip is None and sat.reply_allowed is True
    assert c.speaker_skips == 0


async def test_speaker_in_transcription_mode_is_not_consulted():
    c, sat = _satellite(speaker_state="unavailable", options={"mode": "transcription"})
    s = await _session()

    async def accept(*args, **kwargs):
        return None

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
    ):
        await asyncio.wait_for(sat.run(s), 1)
    assert c.speaker_skips == 0 and sat.speaker_skip is None


async def test_removed_speaker_skips_the_reply_but_is_not_counted():
    c, sat = _satellite(speaker_state=None)
    sat.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: None))
    s = await _session()

    async def accept(*args, **kwargs):
        return None

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(PLAY, AsyncMock()),
    ):
        await asyncio.wait_for(sat.run(s), 1)
    assert c.last_outcome["outcome"] == "reply_skipped" and c.speaker_skips == 0


async def test_state_flapping_while_replies_fail_does_not_reset_the_count(hass):
    from homeassistant.core import CoreState

    hass.set_state(CoreState.running)
    entry = _entry(skips=2)
    hass.states.async_set("media_player.office", "idle")
    with patch("custom_components.bt_remote_voice.issues.pipelines", return_value={"p1": "P"}):
        unsub = watch_speaker(hass, entry)
        hass.states.async_set("media_player.office", "buffering")
        hass.states.async_set("media_player.office", "idle")
        await hass.async_block_till_done()
        assert entry.runtime_data.speaker_skips == 2  # idle <-> buffering is not a return
        hass.states.async_set("media_player.office", "unavailable")
        hass.states.async_set("media_player.office", "idle")
        await hass.async_block_till_done()
        unsub()
    assert entry.runtime_data.speaker_skips == 0
