import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.atvv_remote.protocol import Capabilities, Control
from custom_components.atvv_remote.session import AudioFailure, VoiceSession


async def test_routed_capture_validates_frames_without_retaining_pcm():
    chunks = []
    s = VoiceSession(128, on_pcm=chunks.append, finish_delay=0)
    for _ in range(200):
        s.audio(bytes(128))
    assert s.frames == 200 and len(chunks) == 200 and s.queued_bytes == 0
    s.sync(Control("sync", sequence=201, codec=2))
    assert s.error == "audio_sequence_gap"


def test_history_keeps_failures_after_success_and_bounds_metadata():
    from custom_components.atvv_remote.history import CaptureHistory

    h = CaptureHistory()
    for i in range(25):
        item = h.begin(i, model=3)
        for n in range(100):
            h.record(item, "rx", count=n, payload=b"private", address="private")
        h.finish(item, error="audio_sequence_gap" if i == 23 else None)
    records = h.snapshot()
    assert len(records) == 20
    assert records[-2]["error"] == "audio_sequence_gap"
    assert len(records[-1]["events"]) == 64
    assert "private" not in repr(records)
    h.clear()
    assert h.snapshot() == []


async def test_completed_input_rejects_overflow_and_drains_valid_pcm():
    from custom_components.atvv_remote.held import Utterance

    u = Utterance(max_bytes=4)
    u.feed(b"1234")
    u.release()
    assert [x async for x in u.stream()] == [b"1234"]
    assert u.input_complete
    bad = Utterance(max_bytes=4)
    bad.feed(b"12345")
    with pytest.raises(AudioFailure, match="buffer_overflow"):
        _ = [x async for x in bad.stream()]


async def test_held_mode_requires_speech_and_drops_response_audio():
    from custom_components.atvv_remote.held import HeldTurns

    turns = []
    h = HeldTurns(turns.append)
    for _ in range(100):
        h.feed(bytes(320), probability=0)
    assert turns == []
    for _ in range(40):
        h.feed(bytes(320), probability=1)
    for _ in range(90):
        h.feed(bytes(320), probability=0)
    assert len(turns) == 1 and turns[0].finished
    h.feed(bytes(320), probability=1)
    assert h.current is None  # reply still in progress
    h.resume()
    for _ in range(40):
        h.feed(bytes(320), probability=1)
    h.release()
    assert len(turns) == 2
    for u in turns:
        assert [x async for x in u.stream()]


async def test_no_vad_pipeline_retains_satellite_and_chat_identity():
    from custom_components.atvv_remote.pipeline import run_audio_pipeline

    sat = SimpleNamespace(
        hass=SimpleNamespace(),
        _conversation_id="old",
        registry_entry=None,
        entity_id="assist_satellite.test",
        tts_options=None,
        _resolve_pipeline=lambda: "test-pipeline",
        _internal_on_pipeline_event=lambda e: None,
        async_set_context=lambda c: None,
    )
    session = SimpleNamespace(stream=lambda: None)
    from contextlib import contextmanager

    @contextmanager
    def chat(hass, conversation_id, **kwargs):
        assert conversation_id == "old"
        # Omitting context is deprecated (breaks in HA 2027.5); a satellite acts for no user.
        assert "context" in kwargs and kwargs["context"] is None
        yield SimpleNamespace(conversation_id="reused")

    with (
        patch("custom_components.atvv_remote.pipeline.chat_session.async_get_chat_session", chat),
        patch(
            "custom_components.atvv_remote.pipeline.async_pipeline_from_audio_stream", AsyncMock()
        ) as run,
    ):
        await run_audio_pipeline(sat, session, "stt", "intent")
        assert run.call_args.kwargs["audio_settings"].is_vad_enabled is False
        assert run.call_args.kwargs["satellite_id"] == "assist_satellite.test"
        assert run.call_args.kwargs["conversation_id"] == "reused"
        assert sat._conversation_id == "reused"


async def test_capture_timeout_is_not_clean_eof():
    s = VoiceSession(128, max_duration=0.01)
    s.audio(bytes(128))
    await asyncio.sleep(0.02)
    with pytest.raises(AudioFailure, match="capture_limit"):
        _ = [x async for x in s.stream()]


async def test_reply_streams_before_generation_finishes_and_never_replays():
    from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType

    from custom_components.atvv_remote.assist_satellite import RemoteSatellite
    from custom_components.atvv_remote.coordinator import VoiceCoordinator

    c = VoiceCoordinator(
        SimpleNamespace(),
        SimpleNamespace(
            data={"pipeline_id": "p", "address": "02:00:00:00:00:01", "name": "Test"},
            options={"mode": "commands", "speaker": "media_player.test"},
        ),
    )
    sat = RemoteSatellite(c)
    sat.hass = SimpleNamespace()
    u = VoiceSession(128, finish_delay=0)
    u.audio(bytes(128))
    u.release()
    _ = [x async for x in u.stream()]
    started = asyncio.Event()
    finish = asyncio.Event()

    async def playback(*args, **kwargs):
        started.set()
        await finish.wait()

    async def accept(*args, **kwargs):
        sat.on_pipeline_event(
            PipelineEvent(
                PipelineEventType.RUN_START,
                {
                    "tts_output": {
                        "url": "/api/tts_proxy/test.wav",
                        "mime_type": "audio/wav",
                        "stream_response": True,
                    }
                },
            )
        )
        sat.on_pipeline_event(
            PipelineEvent(PipelineEventType.INTENT_PROGRESS, {"tts_start_streaming": True})
        )
        await asyncio.wait_for(started.wait(), 0.1)
        sat.on_pipeline_event(
            PipelineEvent(
                PipelineEventType.TTS_END, {"tts_output": {"media_id": "media-source://tts/test"}}
            )
        )
        finish.set()

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(
            "custom_components.atvv_remote.assist_satellite.play_reply", side_effect=playback
        ) as play,
        patch.object(sat, "tts_response_finished"),
    ):
        await sat.run(u)
        assert play.call_count == 1
        # Generation-side timings let diagnostics compare production with playback.
        assert {"output_ready_ms", "tts_end_ms", "generation_complete_ms"} <= set(c.last_timings)


async def test_cancelled_playback_stops_only_its_own_media(hass):
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    from custom_components.atvv_remote.playback import play_reply

    for replaced in (False, True):
        hass.states.async_set(
            "media_player.test",
            "idle",
            {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
        )
        calls = []

        async def call(domain, service, data, calls=calls, **kwargs):
            calls.append(service)
            if service == "media_stop":
                hass.states.async_set("media_player.test", "idle")
            if service == "play_media":
                hass.states.async_set(
                    "media_player.test", "playing", {"media_content_id": "/api/tts_proxy/ours.wav"}
                )

        with patch.object(type(hass.services), "async_call", side_effect=call):
            task = asyncio.create_task(
                play_reply(hass, "media_player.test", {"media_id": "/api/tts_proxy/ours.wav"})
            )
            await asyncio.sleep(0.01)
            if replaced:
                hass.states.async_set(
                    "media_player.test", "playing", {"media_content_id": "unrelated-music"}
                )
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(AudioFailure if replaced else asyncio.CancelledError):
                await task
            assert ("media_stop" in calls) is (not replaced)


@pytest.mark.parametrize("phase", ["processing", "playback"])
async def test_queue_one_waits_then_accepts_and_preserves_context(phase):
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={f"busy_{phase}": "queue"})
    )
    c.caps = Capabilities(128)
    first = asyncio.Event()
    results = []

    async def run(s):
        results.append([x async for x in s.stream()])
        if len(results) == 1:
            await first.wait()

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.12)
    c.phase = phase
    c.control(bytes.fromhex("04030202"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.12)
    assert c.sessions == 2 and len(results) == 1
    c.control(bytes.fromhex("04030203"))
    c.control(bytes.fromhex("0002"))
    assert c.last_error == "queue_full"
    first.set()
    await asyncio.sleep(0.15)
    assert len(results) == 2
    await c.stop()


async def test_replace_old_cleanup_cannot_close_new_capture():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={"busy_processing": "replace"})
    )
    c.caps = Capabilities(128)
    c.transport = SimpleNamespace(write=AsyncMock(), close=AsyncMock())
    started = asyncio.Event()
    ids = []

    async def run(s):
        ids.append(s)
        if len(ids) == 1:
            _ = [x async for x in s.stream()]
            started.set()
            await asyncio.Event().wait()
        else:
            _ = [x async for x in s.stream()]

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await started.wait()
    c.control(bytes.fromhex("04030202"))
    c.audio(bytes(128))
    await asyncio.sleep(0.1)
    assert c.sessions == 2 and c.mic_active
    assert bytes.fromhex("0d02") not in [x.args[0] for x in c.transport.write.await_args_list]
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.15)
    assert len(ids) == 2
    await c.stop()


async def test_queue_expiration_is_reported_and_never_runs():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={"busy_processing": "queue"})
    )
    c.caps = Capabilities(128)
    done = asyncio.Event()
    results = []

    async def run(s):
        results.append([x async for x in s.stream()])
        await done.wait()

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.12)
    c.control(bytes.fromhex("04030202"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    c.pending.released_at -= 31
    done.set()
    await asyncio.sleep(0.15)
    assert len(results) == 1 and c.last_error == "queue_expired"
    await c.stop()


def test_all_outcomes_are_advertised_without_breaking_existing_events():
    from custom_components.atvv_remote.event import VoiceButton

    entry = SimpleNamespace(data={"address": "02:00:00:00:00:01", "name": "Test"})
    button = VoiceButton(SimpleNamespace(entry=entry))
    assert {
        "pressed",
        "released",
        "accepted",
        "rejected",
        "queued",
        "failed",
        "context_reset",
    } <= set(button.event_types)


def test_preferences_are_validated_and_defaults_preserve_behavior():
    from custom_components.atvv_remote.preferences import preferences

    opts = preferences({})
    assert opts["busy_processing"] == opts["busy_playback"] == "reject"
    assert opts["input_mode"] == "single" and opts["context_mode"] == "reuse"
    assert opts["output_mode"] == "auto" and "audible_feedback" not in opts
    with pytest.raises(ValueError):
        preferences({"input_mode": "full_duplex"})
    with pytest.raises(ValueError):
        preferences({"busy_processing": "infinite_queue"})


async def test_held_coordinator_runs_two_turns_and_suppresses_response_input():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={"input_mode": "conversation"})
    )
    c.caps = Capabilities(128)
    results = []
    allow = asyncio.Event()

    async def run(s):
        results.append([x async for x in s.stream()])
        if len(results) == 1:
            await allow.wait()

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    held = c.capture.held
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
    for _ in range(30):
        c.audio(bytes(128))
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 0)
    for _ in range(60):
        c.audio(bytes(128))
    await asyncio.sleep(0.02)
    assert len(results) == 1 and c.mic_active
    for _ in range(100):
        c.audio(bytes(128))
    assert held.current is None
    allow.set()
    await asyncio.sleep(0.02)
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
    for _ in range(30):
        c.audio(bytes(128))
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 0)
    for _ in range(60):
        c.audio(bytes(128))
    await asyncio.sleep(0.02)
    assert len(results) == 2 and c.mic_active
    c.control(bytes.fromhex("0002"))
    await c.pipeline_task
    assert not c.mic_active and c.failures == 0
    await c.stop()


async def test_replacement_is_blocked_when_previous_stop_unverified():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(FakeHass(), SimpleNamespace(data=Entry.data, options={}))
    c.caps = Capabilities(128)
    owner = c.accept_capture(Control("start", stream_id=2, reason=3), pending=True)
    c.pending = owner
    old = asyncio.create_task(asyncio.sleep(0))
    await old
    c.recovery_blocked = True
    c.run_pipeline = AsyncMock()
    await c.promote(owner, old, False)
    assert c.pending is None and owner.session.error == "playback_stop_unverified"
    c.run_pipeline.assert_not_awaited()
    await c.stop()


async def test_fresh_context_does_not_reuse_previous_chat_id():
    from contextlib import contextmanager

    from custom_components.atvv_remote.pipeline import run_audio_pipeline

    sat = SimpleNamespace(
        hass=SimpleNamespace(),
        _conversation_id="old",
        registry_entry=None,
        entity_id="assist_satellite.test",
        tts_options=None,
        _resolve_pipeline=lambda: "p",
        _internal_on_pipeline_event=lambda e: None,
        async_set_context=lambda c: None,
    )

    @contextmanager
    def chat(hass, cid, **kwargs):
        assert cid is None
        yield SimpleNamespace(conversation_id="new")

    with (
        patch("custom_components.atvv_remote.pipeline.chat_session.async_get_chat_session", chat),
        patch(
            "custom_components.atvv_remote.pipeline.async_pipeline_from_audio_stream", AsyncMock()
        ),
    ):
        await run_audio_pipeline(
            sat, SimpleNamespace(stream=lambda: None), "stt", "intent", fresh=True
        )
    assert sat._conversation_id == "new"


async def test_long_logical_capture_drains_25_seconds_without_truncation():
    from custom_components.atvv_remote.held import Utterance

    u = Utterance()
    for _ in range(2500):
        u.feed(bytes(320))
    u.release()
    assert sum(len(x) for x in [x async for x in u.stream()]) == 800000
    assert u.input_complete and not u.error


async def test_provider_stream_error_is_raised_instead_of_clean_eof():
    from contextlib import contextmanager

    from custom_components.atvv_remote.pipeline import run_audio_pipeline

    sat = SimpleNamespace(
        hass=SimpleNamespace(),
        _conversation_id=None,
        registry_entry=None,
        entity_id="assist_satellite.test",
        tts_options=None,
        _resolve_pipeline=lambda: "p",
        _internal_on_pipeline_event=lambda e: None,
        async_set_context=lambda c: None,
    )

    @contextmanager
    def chat(hass, cid, **kwargs):
        yield SimpleNamespace(conversation_id="new")

    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    s.abort("audio_sequence_gap")
    effects = []

    async def provider(*args, **kwargs):
        async for _ in kwargs["stt_stream"]:
            pass
        effects.append("clean_eof")

    with (
        patch("custom_components.atvv_remote.pipeline.chat_session.async_get_chat_session", chat),
        patch("custom_components.atvv_remote.pipeline.async_pipeline_from_audio_stream", provider),
    ):
        with pytest.raises(AudioFailure, match="audio_sequence_gap"):
            await run_audio_pipeline(sat, s, "stt", "intent")
    assert effects == []


async def test_playback_timeout_stops_owned_audio(hass):
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    from custom_components.atvv_remote.playback import play_reply

    hass.states.async_set(
        "media_player.test",
        "idle",
        {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
    )
    calls = []

    async def call(domain, service, data, **kwargs):
        calls.append(service)
        if service == "media_stop":
            hass.states.async_set("media_player.test", "idle")
        if service == "play_media":
            hass.states.async_set("media_player.test", "playing", {"media_content_id": "owned"})

    with patch.object(type(hass.services), "async_call", side_effect=call):
        with pytest.raises(AudioFailure, match="playback_finish_timeout"):
            await play_reply(hass, "media_player.test", {"media_id": "owned"}, finish_timeout=0.01)
    assert "media_stop" in calls


async def test_requested_start_remote_can_queue_while_busy():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={"busy_processing": "queue"})
    )
    c.caps = Capabilities(8, version=4, codec=1, model=0)
    c.transport = SimpleNamespace(write=AsyncMock(), close=AsyncMock())
    c.pipeline_task = asyncio.create_task(asyncio.Event().wait())
    c.run_pipeline = AsyncMock()
    c.phase = "processing"
    c.control(b"\x08")
    await asyncio.sleep(0.01)
    c.transport.write.assert_awaited_with(bytes.fromhex("0c0001"))
    c.control(b"\x04")
    assert c.pending and c.sessions == 1
    await c.stop()


async def test_abnormal_held_stop_never_publishes_completed_turn():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={"input_mode": "conversation"})
    )
    c.caps = Capabilities(128)
    c.run_pipeline = AsyncMock()
    c.control(bytes.fromhex("04030201"))
    c.capture.held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
    for _ in range(30):
        c.audio(bytes(128))
    await asyncio.sleep(0.01)
    c.control(bytes.fromhex("0001"))
    await c.pipeline_task
    c.run_pipeline.assert_not_awaited()
    assert c.last_error == "remote_stopped"
    await c.stop()


async def test_pending_request_expires_while_original_still_running():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={"busy_processing": "queue"})
    )
    c.queue_ttl = 0.02
    c.caps = Capabilities(128)
    done = asyncio.Event()

    async def run(s):
        _ = [x async for x in s.stream()]
        await done.wait()

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.12)
    c.control(bytes.fromhex("04030202"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.05)
    assert c.pending is None and c.last_error == "queue_expired" and not c.pipeline_task.done()
    done.set()
    await c.pipeline_task
    await c.stop()


async def test_early_playback_error_cancels_generation_promptly():
    from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType

    from custom_components.atvv_remote.assist_satellite import RemoteSatellite
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(),
        SimpleNamespace(
            data={**Entry.data, "pipeline_id": "p"},
            options={"mode": "commands", "speaker": "media_player.test"},
        ),
    )
    sat = RemoteSatellite(c)
    sat.hass = SimpleNamespace()
    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    s.release()
    _ = [x async for x in s.stream()]
    cancelled = asyncio.Event()

    async def accept(*args, **kwargs):
        try:
            sat.on_pipeline_event(
                PipelineEvent(
                    PipelineEventType.RUN_START,
                    {"tts_output": {"url": "/ours", "stream_response": True}},
                )
            )
            sat.on_pipeline_event(
                PipelineEvent(PipelineEventType.INTENT_PROGRESS, {"tts_start_streaming": True})
            )
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch(
            "custom_components.atvv_remote.assist_satellite.play_reply",
            AsyncMock(side_effect=AudioFailure("speaker_busy")),
        ),
    ):
        with pytest.raises(AudioFailure, match="speaker_busy"):
            await asyncio.wait_for(sat.run(s), 0.2)
    assert cancelled.is_set()


async def test_held_failure_closes_capture_instead_of_resuming():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={"input_mode": "conversation"})
    )
    c.caps = Capabilities(128)
    c.run_pipeline = AsyncMock(side_effect=AudioFailure("playback_stop_unverified"))
    c.control(bytes.fromhex("04030201"))
    held = c.capture.held
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
    for _ in range(30):
        c.audio(bytes(128))
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 0)
    for _ in range(60):
        c.audio(bytes(128))
    await asyncio.sleep(0.03)
    assert c.pipeline_task.done() and not c.mic_active
    assert c.run_pipeline.await_count == 1
    await c.stop()


async def test_requested_start_recovery_after_speaker_becomes_idle():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    hass = FakeHass()
    hass.states = SimpleNamespace(get=lambda eid: SimpleNamespace(state="idle"))
    c = VoiceCoordinator(
        hass, SimpleNamespace(data=Entry.data, options={"speaker": "media_player.test"})
    )
    c.caps = Capabilities(8, version=4, codec=1, model=0)
    c.transport = SimpleNamespace(write=AsyncMock(), close=AsyncMock())
    c.recovery_blocked = True
    c.control(b"\x08")
    await asyncio.sleep(0.01)
    c.transport.write.assert_awaited_with(bytes.fromhex("0c0001"))
    await c.stop()


async def test_physical_capture_through_core_stream_exceeds_old_15_second_cutoff():
    from contextlib import contextmanager

    from homeassistant.components.assist_pipeline.audio_enhancer import EnhancedAudioChunk
    from homeassistant.components.assist_pipeline.default_pipeline import _DefaultPipelineProcessor
    from homeassistant.components.assist_pipeline.models import AudioSettings
    from homeassistant.components.assist_pipeline.vad import VoiceCommandSegmenter

    from custom_components.atvv_remote.pipeline import run_audio_pipeline

    host = SimpleNamespace(
        audio_settings=AudioSettings(is_vad_enabled=False),
        capture_audio=lambda b: None,
        process_event=lambda e: None,
    )
    core = _DefaultPipelineProcessor(host)

    async def original_audio():
        for i in range(2500):
            yield EnhancedAudioChunk(bytes(320), i * 10, 0.9)

    old = [
        x
        async for x in core._speech_to_text_stream(
            original_audio(), VoiceCommandSegmenter(silence_seconds=31)
        )
    ]
    assert 450000 < sum(map(len, old)) < 500000
    s = VoiceSession(128, finish_delay=0)
    sat = SimpleNamespace(
        hass=SimpleNamespace(),
        _conversation_id=None,
        registry_entry=None,
        entity_id="assist_satellite.test",
        tts_options=None,
        _resolve_pipeline=lambda: "p",
        _internal_on_pipeline_event=lambda e: None,
        async_set_context=lambda c: None,
    )

    @contextmanager
    def chat(hass, cid, **kwargs):
        yield SimpleNamespace(conversation_id="new")

    consumed = []

    async def provider(*args, **kwargs):
        assert kwargs["audio_settings"].is_vad_enabled is False

        async def enhanced():
            async for chunk in kwargs["stt_stream"]:
                yield EnhancedAudioChunk(chunk, 0, 0.9)

        consumed.extend([x async for x in core._speech_to_text_stream(enhanced(), None)])

    async def produce():
        for _ in range(1600):
            s.audio(bytes(128))
            await asyncio.sleep(0)
        s.release()

    with (
        patch("custom_components.atvv_remote.pipeline.chat_session.async_get_chat_session", chat),
        patch("custom_components.atvv_remote.pipeline.async_pipeline_from_audio_stream", provider),
    ):
        await asyncio.gather(run_audio_pipeline(sat, s, "stt", "intent"), produce())
    assert sum(map(len, consumed)) == 819200 and s.input_complete and not s.error


async def test_live_tool_can_precede_stt_failure_and_is_not_retried():
    from contextlib import contextmanager

    from custom_components.atvv_remote.pipeline import run_audio_pipeline

    sat = SimpleNamespace(
        hass=SimpleNamespace(),
        _conversation_id=None,
        registry_entry=None,
        entity_id="assist_satellite.test",
        tts_options=None,
        _resolve_pipeline=lambda: "p",
        _internal_on_pipeline_event=lambda e: None,
        async_set_context=lambda c: None,
    )

    @contextmanager
    def chat(hass, cid, **kwargs):
        yield SimpleNamespace(conversation_id="new")

    s = VoiceSession(128, finish_delay=0)
    s.audio(bytes(128))
    effects = []
    calls = []

    async def provider(*args, **kwargs):
        calls.append(True)
        effects.append("provider_tool_during_stt")
        s.abort("audio_sequence_gap")
        _ = [x async for x in kwargs["stt_stream"]]

    with (
        patch("custom_components.atvv_remote.pipeline.chat_session.async_get_chat_session", chat),
        patch("custom_components.atvv_remote.pipeline.async_pipeline_from_audio_stream", provider),
    ):
        with pytest.raises(AudioFailure):
            await run_audio_pipeline(sat, s, "stt", "intent")
    assert len(calls) == 1 and effects == ["provider_tool_during_stt"]


async def test_replacement_wait_is_bounded_for_cancellation_resistant_generation():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(FakeHass(), SimpleNamespace(data=Entry.data, options={}))
    c.retirement_timeout = 0.01
    c.caps = Capabilities(128)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def old_generation():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    old = asyncio.create_task(old_generation())
    await entered.wait()
    owner = c.accept_capture(Control("start", reason=3, stream_id=2), pending=True)
    c.pending = owner
    c.run_pipeline = AsyncMock()
    await asyncio.wait_for(c.promote(owner, old, True), 0.1)
    assert owner.session.error == "replacement_retirement_failed" and not old.done()
    c.run_pipeline.assert_not_awaited()
    release.set()
    await old
    await c.stop()


async def test_owned_stop_must_be_observed_before_retirement(hass):
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    from custom_components.atvv_remote.playback import play_reply

    hass.states.async_set(
        "media_player.test",
        "idle",
        {"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)},
    )

    async def call(domain, service, data, **kwargs):
        if service == "play_media":
            hass.states.async_set("media_player.test", "playing", {"media_content_id": "ours"})

    with patch.object(type(hass.services), "async_call", side_effect=call):
        with pytest.raises(AudioFailure, match="playback_stop_failed"):
            await play_reply(
                hass,
                "media_player.test",
                {"media_id": "ours"},
                finish_timeout=0.01,
                stop_timeout=0.01,
            )


async def _disconnect_harness(options, run):
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(FakeHass(), SimpleNamespace(data=Entry.data, options=options))
    lost = asyncio.Event()
    transport = SimpleNamespace(
        open=AsyncMock(return_value=Capabilities(128)),
        close=AsyncMock(),
        lost=lost,
        write=AsyncMock(),
        check_connected=AsyncMock(return_value=True),
    )
    c.run_pipeline = run
    patcher = patch(
        "custom_components.atvv_remote.coordinator.BlueZTransport", return_value=transport
    )
    patcher.start()
    c.connection_task = asyncio.create_task(c.connect_loop())
    await asyncio.sleep(0.01)
    return c, lost, patcher


def _speak(held, c, frames=30, silence=60):
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
    for _ in range(frames):
        c.audio(bytes(128))
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 0)
    for _ in range(silence):
        c.audio(bytes(128))


async def test_disconnect_with_pending_capture_lets_drained_original_finish():
    done, drained, finished = asyncio.Event(), asyncio.Event(), []

    async def run(s):
        if not drained.is_set():
            async for _ in s.stream():
                pass
            drained.set()
            await done.wait()
            finished.append(True)
        else:
            async for _ in s.stream():
                pass

    c, lost, patcher = await _disconnect_harness({"busy_processing": "queue"}, run)
    try:
        c.control(bytes.fromhex("04030201"))
        c.audio(bytes(128))
        c.control(bytes.fromhex("0002"))
        await drained.wait()
        first = c.pipeline_task
        c.phase = "processing"
        c.control(bytes.fromhex("04030202"))
        c.audio(bytes(128))
        await asyncio.sleep(0.02)
        assert c.pending is not None and c.session is c.pending.session
        lost.set()
        await asyncio.sleep(0.05)
        assert not first.done(), "drained original request was cancelled by pending capture"
        assert c.pending is None
        done.set()
        await first
        assert finished == [True]
    finally:
        patcher.stop()
        await c.stop()


async def test_disconnect_after_held_turn_drained_lets_reply_finish_without_listening():
    done, drained, finished, turns = asyncio.Event(), asyncio.Event(), [], []

    async def run(s):
        turns.append(1)
        async for _ in s.stream():
            pass
        drained.set()
        await done.wait()
        finished.append(True)

    c, lost, patcher = await _disconnect_harness({"input_mode": "conversation"}, run)
    try:
        c.control(bytes.fromhex("04030201"))
        _speak(c.capture.held, c)
        await drained.wait()
        c.control(bytes.fromhex("0002"))
        task = c.pipeline_task
        lost.set()
        await asyncio.sleep(0.05)
        assert not task.done(), "fully drained held reply was cancelled on disconnect"
        done.set()
        await task
        assert finished == [True] and turns == [1]
        assert c.failures == 0, c.last_error
    finally:
        patcher.stop()
        await c.stop()


@pytest.mark.parametrize("held", [False, True])
async def test_disconnect_with_incomplete_active_input_cancels_everything(held):
    gate = asyncio.Event()
    cancelled = []

    async def run(s):
        try:
            await gate.wait()
            async for _ in s.stream():
                pass
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    options = {"input_mode": "conversation"} if held else {"busy_processing": "queue"}
    c, lost, patcher = await _disconnect_harness(options, run)
    try:
        c.control(bytes.fromhex("04030201"))
        if held:
            _speak(c.capture.held, c)
            await asyncio.sleep(0.02)
        else:
            c.audio(bytes(128))
            c.control(bytes.fromhex("0002"))
            await asyncio.sleep(0.02)
            c.phase = "processing"
            c.control(bytes.fromhex("04030202"))
            c.audio(bytes(128))
        task = c.pipeline_task
        lost.set()
        await asyncio.sleep(0.1)
        assert task.done() and cancelled == [True]
        assert c.pending is None and not c.mic_active
    finally:
        patcher.stop()
        await c.stop()


async def test_input_failure_with_resistant_generation_blocks_recovery():
    """Input abort while generation resists cancellation must block recovery."""
    from custom_components.atvv_remote.assist_satellite import RemoteSatellite
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(),
        SimpleNamespace(data={**Entry.data, "pipeline_id": "p"}, options={}),
    )
    c.caps = Capabilities(128)
    sat = RemoteSatellite(c)
    sat.hass = SimpleNamespace()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def accept(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()  # cancellation-resistant provider

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
    ):
        c.control(bytes.fromhex("04030201"))
        c.audio(bytes(128))
        await entered.wait()
        c.session.abort("audio_sequence_gap")
        await asyncio.wait_for(c.pipeline_task, 8)
        gen_alive = not release.is_set()
        assert gen_alive
        assert c.recovery_blocked, "old generation still running but new presses allowed"
        release.set()
        await asyncio.sleep(0.01)
        await c.stop()


async def test_disconnect_discarded_pending_is_recorded():
    done, drained = asyncio.Event(), asyncio.Event()

    async def run(s):
        async for _ in s.stream():
            pass
        drained.set()
        await done.wait()

    c, lost, patcher = await _disconnect_harness({"busy_processing": "queue"}, run)
    try:
        c.control(bytes.fromhex("04030201"))
        c.audio(bytes(128))
        c.control(bytes.fromhex("0002"))
        await drained.wait()
        c.phase = "processing"
        c.control(bytes.fromhex("04030202"))
        c.audio(bytes(128))
        await asyncio.sleep(0.02)
        lost.set()
        await asyncio.sleep(0.2)
        snap = c.history.snapshot()
        assert snap[-1]["error"] == "disconnected", "discarded pending has no recorded error"
    finally:
        done.set()
        patcher.stop()
        await c.stop()


async def test_held_trailing_audio_after_stop_reaches_turn():
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    c = VoiceCoordinator(
        FakeHass(), SimpleNamespace(data=Entry.data, options={"input_mode": "conversation"})
    )
    c.caps = Capabilities(128)
    got = []

    async def run(s):
        got.append(len([x async for x in s.stream()]))

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    held = c.capture.held
    held.vad = SimpleNamespace(Process10ms=lambda pcm: 1)
    total = []
    original = c.capture.session.on_pcm
    c.capture.session.on_pcm = lambda pcm: (total.append(len(pcm)), original(pcm))
    for _ in range(40):
        c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    for _ in range(4):  # frames arriving within finish_delay, accepted in single mode
        c.audio(bytes(128))
    await c.pipeline_task
    assert c.capture.session.frames == 44
    assert got and got[0] * 320 >= sum(total) - 320
    await c.stop()


def _coordinator(options=None):
    from custom_components.atvv_remote.coordinator import VoiceCoordinator
    from tests.test_coordinator import Entry, FakeHass

    return VoiceCoordinator(FakeHass(), SimpleNamespace(data=Entry.data, options=options or {}))


async def test_error_sensor_reports_root_cause_and_clears_on_success():
    from custom_components.atvv_remote.sensor import LastError

    c = _coordinator()
    c.caps = Capabilities(128)
    sensor = LastError(c)
    assert sensor.native_value == "none" and sensor.available
    assert sensor.entity_category == "diagnostic"
    assert sensor.extra_state_attributes == {
        "message": None,
        "stage": None,
        "since": None,
        "session": None,
    }

    async def fail(s):
        raise AudioFailure("audio_stalled")

    c.run_pipeline = fail
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await c.pipeline_task
    assert sensor.native_value == "audio_stalled"
    attrs = sensor.extra_state_attributes
    assert attrs["message"] and attrs["stage"] and attrs["session"] == 1 and attrs["since"]

    async def ok(s):
        async for _ in s.stream():
            pass

    c.run_pipeline = ok
    c.control(bytes.fromhex("04030202"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await c.pipeline_task
    assert sensor.native_value == "none"
    assert sensor.extra_state_attributes["message"] is None
    await c.stop()


async def test_busy_rejection_is_not_a_latched_error():
    from custom_components.atvv_remote.sensor import LastError

    c = _coordinator()
    c.caps = Capabilities(128)
    done = asyncio.Event()

    async def run(s):
        async for _ in s.stream():
            pass
        await done.wait()

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.12)
    c.control(bytes.fromhex("04030202"))
    assert c.last_outcome["outcome"] == "rejected"
    assert LastError(c).native_value == "none"
    done.set()
    await c.pipeline_task
    await c.stop()


async def test_error_sensor_survives_disconnect_and_keeps_failure_details():
    from custom_components.atvv_remote.sensor import LastError

    c = _coordinator()
    c.ready = False
    c.outcome("failed", "capture_limit")
    sensor = LastError(c)
    assert sensor.available
    assert sensor.native_value == "capture_limit"
    assert "shorter" in sensor.extra_state_attributes["message"]


def test_audible_feedback_is_removed():
    from importlib.util import find_spec

    from custom_components.atvv_remote import preferences

    assert find_spec("custom_components.atvv_remote.feedback") is None
    assert "audible_feedback" not in preferences.DEFAULTS


async def test_each_request_keeps_its_own_timings_in_history():
    c = _coordinator()
    c.caps = Capabilities(128)
    calls = []

    async def run(s):
        async for _ in s.stream():
            pass
        calls.append(1)
        c.last_timings = {"output_ready_ms": 100 * len(calls), "playback_complete_ms": 999}

    c.run_pipeline = run
    for stream_id in ("01", "02"):
        c.control(bytes.fromhex(f"040302{stream_id}"))
        c.audio(bytes(128))
        c.control(bytes.fromhex("0002"))
        await c.pipeline_task
    snapshot = c.history.snapshot()
    assert [item["timings"][0]["output_ready_ms"] for item in snapshot] == [100, 200]
    # Only numeric stage timings are retained; nothing else can leak in.
    c.last_timings = {"x_ms": 1, "note": "text", "bad": object()}
    c.history.timings(c.capture.record, c.last_timings)
    assert c.history.snapshot()[-1]["timings"][-1] == {"x_ms": 1}
    await c.stop()


def _streaming_satellite(head_start):
    from custom_components.atvv_remote.assist_satellite import RemoteSatellite
    from custom_components.atvv_remote.coordinator import VoiceCoordinator

    options = {"mode": "commands", "speaker": "media_player.test"}
    if head_start is not None:
        options["stream_head_start"] = head_start
    c = VoiceCoordinator(
        SimpleNamespace(),
        SimpleNamespace(
            data={"pipeline_id": "p", "address": "02:00:00:00:00:01", "name": "Test"},
            options=options,
        ),
    )
    sat = RemoteSatellite(c)
    sat.hass = SimpleNamespace()
    return c, sat


async def _run_streamed(sat, generation_seconds):
    """Run one streamed reply; return seconds from readiness to play_media request."""
    from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType

    u = VoiceSession(128, finish_delay=0)
    u.audio(bytes(128))
    u.release()
    _ = [x async for x in u.stream()]
    ready_at, played_at = [], []

    async def playback(*args, **kwargs):
        played_at.append(asyncio.get_running_loop().time())

    async def accept(*args, **kwargs):
        sat.on_pipeline_event(
            PipelineEvent(
                PipelineEventType.RUN_START,
                {
                    "tts_output": {
                        "url": "/api/tts_proxy/test.wav",
                        "mime_type": "audio/wav",
                        "stream_response": True,
                    }
                },
            )
        )
        ready_at.append(asyncio.get_running_loop().time())
        sat.on_pipeline_event(
            PipelineEvent(PipelineEventType.INTENT_PROGRESS, {"tts_start_streaming": True})
        )
        await asyncio.sleep(generation_seconds)
        sat.on_pipeline_event(
            PipelineEvent(
                PipelineEventType.TTS_END, {"tts_output": {"media_id": "media-source://tts/test"}}
            )
        )

    with (
        patch.object(sat, "_resolve_pipeline", return_value="p"),
        patch.object(sat, "async_accept_pipeline_from_satellite", side_effect=accept),
        patch("custom_components.atvv_remote.assist_satellite.play_reply", side_effect=playback),
        patch.object(sat, "tts_response_finished"),
    ):
        await sat.run(u)
    return ready_at[0], played_at


async def test_head_start_delays_streamed_playback_for_a_cushion():
    c, sat = _streaming_satellite(0.2)
    ready, played = await _run_streamed(sat, generation_seconds=0.5)
    assert len(played) == 1
    assert 0.18 <= played[0] - ready < 0.4
    assert c.last_timings["head_start_ms"] == 200


async def test_reply_that_finishes_inside_head_start_plays_immediately_once():
    c, sat = _streaming_satellite(5)
    ready, played = await _run_streamed(sat, generation_seconds=0.05)
    assert len(played) == 1
    assert played[0] - ready < 1


async def test_default_has_no_head_start():
    c, sat = _streaming_satellite(None)
    ready, played = await _run_streamed(sat, generation_seconds=0.3)
    assert len(played) == 1 and played[0] - ready < 0.05
    assert "head_start_ms" not in c.last_timings


async def test_cancelled_run_leaves_no_pending_head_start_timer():
    c, sat = _streaming_satellite(5)
    task = asyncio.create_task(_run_streamed(sat, generation_seconds=10))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert sat.head_start_task is None or sat.head_start_task.done()


def test_head_start_preference_is_validated():
    from custom_components.atvv_remote.preferences import DEFAULTS, preferences

    assert DEFAULTS["stream_head_start"] == 0
    assert preferences({"stream_head_start": 3.5})["stream_head_start"] == 3.5
    for bad in (-1, 11, "3", True, None):
        with pytest.raises(ValueError):
            preferences({"stream_head_start": bad})


async def test_late_starting_consumer_does_not_overflow_a_single_hold():
    """A provider that connects slowly must still receive the audio spoken meanwhile."""
    c = _coordinator()
    c.caps = Capabilities(128)
    start = asyncio.Event()
    received = []

    async def run(s):
        await start.wait()  # e.g. a live provider still opening its connection
        received.extend([x async for x in s.stream()])

    c.run_pipeline = run
    c.control(bytes.fromhex("04030201"))
    for _ in range(150):  # about 3 s of speech arrives before anything reads
        c.audio(bytes(128))
    assert c.session.error is None
    c.control(bytes.fromhex("0002"))
    start.set()
    await c.pipeline_task
    assert c.failures == 0 and len(received) == 150
    await c.stop()
