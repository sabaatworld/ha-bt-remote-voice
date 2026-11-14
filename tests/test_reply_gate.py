"""The reply condition gates playback only; the pipeline always runs unchanged."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.assist_satellite.entity import AssistSatelliteState

from custom_components.bt_remote_voice.assist_satellite import RemoteSatellite
from custom_components.bt_remote_voice.coordinator import VoiceCoordinator
from custom_components.bt_remote_voice.protocol import Capabilities
from custom_components.bt_remote_voice.sensor import LastError
from custom_components.bt_remote_voice.session import VoiceSession
from tests.test_coordinator import FakeHass


class FakeCondition:
    def __init__(self, result=(True, None)):
        self.result = result
        self.calls = []

    def decide(self, variables):
        self.calls.append(dict(variables))
        return self.result

    def async_unload(self):
        pass


def _setup(condition, **options):
    opts = {"mode": "commands", "speaker": "media_player.test", **options}
    entry = SimpleNamespace(
        title="Test Remote",
        data={"pipeline_id": "p", "address": "02:00:00:00:00:01", "name": "Test Remote"},
        options=opts,
    )
    c = VoiceCoordinator(FakeHass(), entry)
    c.reply_condition = condition
    sat = RemoteSatellite(c)
    sat.hass = SimpleNamespace()
    return c, sat


def _session():
    u = VoiceSession(128, finish_delay=0)
    u.audio(bytes(128))
    u.release()
    return u


def _events(sat, streamed):
    out = {"url": "/api/tts_proxy/t.wav", "mime_type": "audio/wav", "stream_response": True}
    sat.on_pipeline_event(PipelineEvent(PipelineEventType.RUN_START, {"tts_output": out}))
    if streamed:
        sat.on_pipeline_event(
            PipelineEvent(PipelineEventType.INTENT_PROGRESS, {"tts_start_streaming": True})
        )
    sat.on_pipeline_event(
        PipelineEvent(
            PipelineEventType.TTS_END, {"tts_output": {"media_id": "media-source://tts/t"}}
        )
    )


async def _run(sat, *, streamed, generation_seconds=0.05):
    played = []
    u = _session()
    _ = [x async for x in u.stream()]

    async def playback(*args, **kwargs):
        played.append(True)

    async def accept(*args, **kwargs):
        await asyncio.sleep(0.01)
        _events(sat, streamed)
        await asyncio.sleep(generation_seconds)

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(
            "custom_components.bt_remote_voice.assist_satellite.play_reply", side_effect=playback
        ),
        patch.object(sat, "tts_response_finished"),
    ):
        await sat.run(u)
    return played


@pytest.mark.parametrize("streamed", [False, True])
async def test_false_condition_never_plays_the_reply(streamed):
    c, sat = _setup(FakeCondition((False, "condition_false")))
    started = asyncio.get_running_loop().time()
    played = await _run(sat, streamed=streamed)
    assert played == []
    assert asyncio.get_running_loop().time() - started < 1  # the request still completes
    assert c.last_outcome["outcome"] == "reply_skipped"
    assert c.last_outcome["code"] == "condition_false"
    assert c.last_outcome["message"] is None
    assert sat.playback_task is None and c.phase != "playback"


@pytest.mark.parametrize("head_start", [0, 0.2])
async def test_skipped_streamed_reply_records_no_head_start(head_start):
    c, sat = _setup(FakeCondition((False, "condition_false")), stream_head_start=head_start)
    assert await _run(sat, streamed=True) == []
    assert "head_start_ms" not in c.last_timings and sat.head_start_task is None


@pytest.mark.parametrize("streamed", [False, True])
async def test_true_or_empty_condition_is_unchanged(streamed):
    c, sat = _setup(FakeCondition((True, None)))
    assert await _run(sat, streamed=streamed) == [True]
    assert c.last_outcome is None


async def test_condition_error_skips_with_its_own_code():
    c, sat = _setup(FakeCondition((False, "condition_error")))
    assert await _run(sat, streamed=False) == []
    assert c.last_outcome["code"] == "condition_error"


async def test_skip_is_not_a_failure_and_leaves_the_error_sensor_alone():
    c, sat = _setup(FakeCondition((False, "condition_false")))
    await _run(sat, streamed=False)
    assert c.failures == 0 and c.last_failure is None
    assert LastError(c).native_value == "none"


async def test_condition_receives_exactly_speaker_remote_held():
    cond = FakeCondition()
    _c, sat = _setup(cond, input_mode="conversation")
    await _run(sat, streamed=False)
    assert cond.calls == [{"speaker": "media_player.test", "remote": "Test Remote", "held": True}]
    cond2 = FakeCondition()
    _c, sat2 = _setup(cond2)
    await _run(sat2, streamed=False)
    assert cond2.calls[0]["held"] is False


async def test_no_speaker_means_the_condition_is_not_consulted():
    cond = FakeCondition((False, "condition_false"))
    c, sat = _setup(cond, speaker="")
    await _run(sat, streamed=False)
    assert cond.calls == [] and c.last_outcome is None


async def test_skipped_reply_does_not_show_responding():
    """HA's base handler sets RESPONDING on TTS_START; a skipped reply must undo that."""
    _c, sat = _setup(FakeCondition())
    sat.entity_id = "assist_satellite.test"
    sat.current_session = object()
    states = []
    with patch.object(sat, "_set_state", side_effect=states.append):
        sat.reply_allowed = False
        sat.on_pipeline_event(PipelineEvent(PipelineEventType.TTS_START, {}))
        assert states == [AssistSatelliteState.PROCESSING]
        states.clear()
        sat.reply_allowed = True
        sat.on_pipeline_event(PipelineEvent(PipelineEventType.TTS_START, {}))
        assert states == []


async def test_skipped_held_turns_keep_listening_while_the_button_stays_down():
    cond = FakeCondition((False, "condition_false"))
    c, sat = _setup(cond, input_mode="conversation")
    c.caps = Capabilities(128)
    runs = []

    async def accept(*args, **kwargs):
        runs.append(sat.current_session)
        _events(sat, streamed=False)

    played = []

    async def playback(*args, **kwargs):
        played.append(True)

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(
            "custom_components.bt_remote_voice.assist_satellite.play_reply", side_effect=playback
        ),
        patch.object(sat, "tts_response_finished"),
    ):
        c.control(bytes.fromhex("04030201"))
        held = c.capture.held
        for _ in range(2):  # two turns without releasing
            held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
            for _ in range(30):
                c.audio(bytes(128))
            held.vad = SimpleNamespace(Process10ms=lambda pcm: 0)
            for _ in range(60):
                c.audio(bytes(128))
            await asyncio.sleep(0.25)
        assert len(runs) == 2 and played == [] and c.mic_active
        assert len(cond.calls) == 2  # evaluated per turn
        c.control(bytes.fromhex("0002"))
        await c.pipeline_task
        await c.stop()


async def test_queued_request_is_evaluated_when_it_starts_not_when_pressed():
    cond = FakeCondition((True, None))
    c, sat = _setup(cond, busy_processing="queue")
    c.caps = Capabilities(128)
    first_done = asyncio.Event()
    starts = []

    async def accept(*args, **kwargs):
        starts.append(len(cond.calls))
        if len(starts) == 1:
            await first_done.wait()
        _events(sat, streamed=False)

    async def playback(*args, **kwargs):
        pass

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(
            "custom_components.bt_remote_voice.assist_satellite.play_reply", side_effect=playback
        ),
        patch.object(sat, "tts_response_finished"),
    ):
        c.control(bytes.fromhex("04030201"))
        c.audio(bytes(128))
        c.control(bytes.fromhex("0002"))
        await asyncio.sleep(0.2)
        c.phase = "processing"
        c.control(bytes.fromhex("04030202"))
        c.audio(bytes(128))
        c.control(bytes.fromhex("0002"))
        await asyncio.sleep(0.2)
        assert len(cond.calls) == 1  # the queued press has not been evaluated yet
        cond.result = (False, "condition_false")  # changes while it waits
        first_done.set()
        await asyncio.sleep(0.4)
        assert len(cond.calls) == 2
        assert c.last_outcome["outcome"] == "reply_skipped"  # the later value applied
        await c.stop()


async def test_expired_queued_request_is_never_evaluated():
    cond = FakeCondition((True, None))
    c, sat = _setup(cond, busy_processing="queue")
    c.caps = Capabilities(128)
    c.queue_ttl = 0.1
    gate = asyncio.Event()

    async def accept(*args, **kwargs):
        await gate.wait()
        _events(sat, streamed=False)

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch("custom_components.bt_remote_voice.assist_satellite.play_reply"),
        patch.object(sat, "tts_response_finished"),
    ):
        c.control(bytes.fromhex("04030201"))
        c.audio(bytes(128))
        c.control(bytes.fromhex("0002"))
        await asyncio.sleep(0.2)
        c.phase = "processing"
        c.control(bytes.fromhex("04030202"))
        c.audio(bytes(128))
        c.control(bytes.fromhex("0002"))
        await asyncio.sleep(0.4)  # the queued request expires
        gate.set()
        await asyncio.sleep(0.3)
        assert len(cond.calls) == 1  # only the first request was ever evaluated
        await c.stop()


async def test_condition_is_unloaded_only_after_the_pipeline_is_cancelled():
    order = []

    class Spy(FakeCondition):
        def async_unload(self):
            order.append("unload")

    c, sat = _setup(Spy())
    started = asyncio.Event()

    async def pipeline():
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            order.append("pipeline cancelled")
            raise

    c.pipeline_task = asyncio.create_task(pipeline())
    await started.wait()
    await c.stop()
    assert order == ["pipeline cancelled", "unload"]
