from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType, PipelineStage

from custom_components.atvv_remote.assist_satellite import RemoteSatellite
from custom_components.atvv_remote.coordinator import VoiceCoordinator
from custom_components.atvv_remote.session import AudioFailure, VoiceSession


class Entry:
    options = {}
    data = {
        "address": "02:00:00:00:00:01",
        "name": "Test Remote",
        "pipeline_id": "stable-id",
        "mode": "transcription",
    }


async def test_satellite_streams_stt_only_and_validates_stable_pipeline_id():
    c = VoiceCoordinator(SimpleNamespace(), Entry())
    satellite = RemoteSatellite(c)
    from homeassistant.components.assist_pipeline.runtime import KEY_ASSIST_PIPELINE

    satellite.hass = SimpleNamespace(
        data={
            KEY_ASSIST_PIPELINE: SimpleNamespace(
                pipeline_store=SimpleNamespace(
                    data={"stable-id": SimpleNamespace(id="stable-id", name="Renamed")}
                )
            )
        }
    )
    with patch(
        "custom_components.atvv_remote.assist_satellite.async_get_pipelines",
        return_value=[SimpleNamespace(id="stable-id", name="Renamed")],
    ):
        assert satellite._resolve_pipeline() == "stable-id"
    with patch(
        "custom_components.atvv_remote.assist_satellite.async_get_pipelines", return_value=[]
    ):
        with pytest.raises(AudioFailure, match="pipeline_removed"):
            satellite._resolve_pipeline()
    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    s.release()
    with patch.object(satellite, "async_accept_pipeline_from_satellite", AsyncMock()) as accept:
        await satellite.run(s)
        assert accept.call_args.kwargs["end_stage"] == PipelineStage.STT
    c.entry = SimpleNamespace(data=Entry.data, options={"mode": "commands"})
    with patch.object(satellite, "async_accept_pipeline_from_satellite", AsyncMock()) as accept:
        await satellite.run(s)
        assert accept.call_args.kwargs["end_stage"] == PipelineStage.INTENT


async def test_intent_guard_rejects_early_stt_and_corrupted_audio():
    c = VoiceCoordinator(SimpleNamespace(), Entry())
    satellite = RemoteSatellite(c)
    s = VoiceSession(128)
    satellite.current_session = s
    with pytest.raises(AudioFailure, match="incomplete_utterance"):
        satellite.on_pipeline_event(PipelineEvent(PipelineEventType.STT_END))
    s.abort("disconnected")
    with pytest.raises(AudioFailure, match="incomplete_utterance"):
        satellite.on_pipeline_event(PipelineEvent(PipelineEventType.STT_END))


async def test_stt_cannot_execute_before_provider_has_drained_input():
    c = VoiceCoordinator(SimpleNamespace(), Entry())
    satellite = RemoteSatellite(c)
    session = VoiceSession(128, finish_delay=0)
    session.audio(bytes(128))
    session.release()
    satellite.current_session = session
    with pytest.raises(AudioFailure, match="incomplete_utterance"):
        satellite.on_pipeline_event(PipelineEvent(PipelineEventType.STT_END))
    assert [len(chunk) async for chunk in session.stream()] == [512]
    satellite.on_pipeline_event(PipelineEvent(PipelineEventType.STT_END))


async def test_configured_reply_runs_tts_and_acknowledges_only_after_playback():
    c = VoiceCoordinator(SimpleNamespace(), Entry())
    c.entry = SimpleNamespace(
        data=Entry.data, options={"mode": "commands", "speaker": "media_player.test_speaker"}
    )
    satellite = RemoteSatellite(c)
    satellite.hass = SimpleNamespace()
    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    s.release()

    async def accept(*args, **kwargs):
        assert kwargs["end_stage"] == PipelineStage.TTS
        satellite.on_pipeline_event(
            PipelineEvent(
                PipelineEventType.TTS_END, {"tts_output": {"media_id": "media-source://tts/test"}}
            )
        )

    order = []

    async def playback(*args):
        order.append("played")

    with (
        patch.object(satellite, "_resolve_pipeline", return_value="stable-id"),
        patch.object(satellite, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch("custom_components.atvv_remote.assist_satellite.play_reply", side_effect=playback),
        patch.object(
            satellite, "tts_response_finished", side_effect=lambda: order.append("finished")
        ),
    ):
        await satellite.run(s)
    assert order == ["played", "finished"]


async def test_pipeline_error_is_propagated_to_coordinator_without_a_speaker():
    c = VoiceCoordinator(SimpleNamespace(), Entry())
    satellite = RemoteSatellite(c)
    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    s.release()

    async def accept(*args, **kwargs):
        satellite.on_pipeline_event(PipelineEvent(PipelineEventType.ERROR, {"code": "stt-failed"}))

    with (
        patch.object(satellite, "_resolve_pipeline", return_value="stable-id"),
        patch.object(satellite, "async_accept_pipeline_from_satellite", side_effect=accept),
    ):
        with pytest.raises(AudioFailure, match="stt-failed"):
            await satellite.run(s)
