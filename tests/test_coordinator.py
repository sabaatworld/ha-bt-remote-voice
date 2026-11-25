import asyncio

from custom_components.bt_remote_voice.coordinator import VoiceCoordinator
from custom_components.bt_remote_voice.protocol import Capabilities


class Entry:
    options = {"mode": "transcription", "pipeline_id": "test-pipeline"}
    data = {"address": "02:00:00:00:00:01", "name": "Test Remote"}


class FakeHass:
    def async_create_task(self, coro, *args, **kwargs):
        return asyncio.create_task(coro)


async def test_duplicate_start_search_does_not_cancel_and_busy_hold_is_not_replayed():
    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128)
    streams = []
    done = asyncio.Event()

    async def consume(session):
        streams.append([v async for v in session.stream()])
        await done.wait()

    c.run_pipeline = consume
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0800"))
    c.control(bytes.fromhex("04030201"))
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.15)
    assert len(streams) == 1 and len(streams[0]) == 1
    c.control(bytes.fromhex("04030202"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    assert c.last_error == "busy"
    done.set()
    await c.pipeline_task
    assert len(streams) == 1


async def test_disconnect_aborts_pipeline_and_clears_mic_state():
    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128)

    async def consume(session):
        async for _ in session.stream():
            pass

    c.run_pipeline = consume
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    await c.stop()
    assert c.mic_active is False
    assert c.pipeline_task is None or c.pipeline_task.done()


async def test_input_failure_closes_remote_microphone():
    from unittest.mock import AsyncMock

    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128)
    c.transport = type("Transport", (), {"write": AsyncMock()})()

    async def consume(session):
        async for _ in session.stream():
            pass

    c.run_pipeline = consume
    c.control(bytes.fromhex("04030207"))
    c.audio(b"x")
    await c.pipeline_task
    c.transport.write.assert_awaited_once_with(bytes.fromhex("0d07"))


async def test_pipeline_hang_does_not_leave_remote_permanently_busy():
    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128)
    c.pipeline_timeout = 0.01
    c.run_pipeline = lambda session: asyncio.Event().wait()
    c.control(bytes.fromhex("04030201"))
    c.control(bytes.fromhex("0002"))
    await asyncio.wait_for(c.pipeline_task, 0.2)
    assert c.last_error == "pipeline_timeout"


async def test_ignored_hold_cannot_be_accepted_after_previous_pipeline_completes():
    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128)
    done = asyncio.Event()

    async def consume(session):
        async for _ in session.stream():
            pass
        await done.wait()

    c.run_pipeline = consume
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    await asyncio.sleep(0.15)
    c.control(bytes.fromhex("04030202"))
    done.set()
    await c.pipeline_task
    c.control(bytes.fromhex("04030202"))
    assert c.sessions == 1
    c.control(bytes.fromhex("0002"))


async def test_legacy_voice_search_requests_mic_and_stop_drains():
    from unittest.mock import AsyncMock

    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(8, version=4, codec=1, model=0)
    c.transport = type("Transport", (), {"write": AsyncMock(), "close": AsyncMock()})()
    results = []

    async def consume(session):
        results.extend([x async for x in session.stream()])

    c.run_pipeline = consume
    c.control(b"\x08")
    await asyncio.sleep(0.01)
    c.transport.write.assert_awaited_with(bytes.fromhex("0c0001"))
    c.control(b"\x04")
    c.audio(bytes.fromhex("0000000000001717"))
    c.control(b"\x00")
    await c.pipeline_task
    assert len(results) == 1 and c.sessions == 1
    await c.stop()


async def test_tap_voice_closes_mic_on_vad_eof_and_duplicates_are_ignored():
    from unittest.mock import AsyncMock

    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128, model=1)
    c.transport = type("Transport", (), {"write": AsyncMock(), "close": AsyncMock()})()
    c.keepalive_interval = 0.01

    async def consume(session):
        async for _ in session.stream():
            pass

    c.run_pipeline = consume
    c.control(bytes.fromhex("04010207"))
    c.audio(bytes(128))
    c.control(b"\x08")
    assert c.sessions == 1
    c.session.release()
    await c.pipeline_task
    assert c.mic_active is False
    assert bytes.fromhex("0d07") in [call.args[0] for call in c.transport.write.await_args_list]
    await c.stop()


async def test_disconnect_resets_ignored_hold_and_aborts_even_released_input():
    from unittest.mock import AsyncMock, patch

    c = VoiceCoordinator(FakeHass(), Entry())
    c.ignored_press = True
    c.stream_id = 7
    c.pending_open = True
    t = type(
        "Transport",
        (),
        {"open": AsyncMock(side_effect=ConnectionError("wake_remote")), "close": AsyncMock()},
    )()
    with patch("custom_components.bt_remote_voice.coordinator.BlueZTransport", return_value=t):
        c.connection_task = asyncio.create_task(c.connect_loop())
        await asyncio.sleep(0.01)
        assert not c.ignored_press and not c.pending_open and c.stream_id is None
        await c.stop()


async def test_mic_open_error_and_missing_start_recover():
    from unittest.mock import AsyncMock

    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(8, version=4, codec=1, model=0)
    c.transport = type("Transport", (), {"write": AsyncMock(), "close": AsyncMock()})()
    c.run_pipeline = lambda s: asyncio.Event().wait()
    c.control(b"\x08")
    await asyncio.sleep(0)
    c.control(bytes.fromhex("0c0f02"))
    assert c.last_error == "microphone_open_rejected" and not c.pending_open
    await c.stop()


async def test_search_only_voice_control_can_request_audio_without_toggling_auto_start():
    from unittest.mock import AsyncMock

    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128, model=3)
    c.transport = type("Transport", (), {"write": AsyncMock(), "close": AsyncMock()})()
    c.open_grace = 0.01
    c.control(b"\x08")
    await asyncio.sleep(0.02)
    c.transport.write.assert_awaited_with(bytes.fromhex("0c00"))
    await c.stop()
    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128)
    c.transport = type("Transport", (), {"write": AsyncMock(), "close": AsyncMock()})()
    c.open_grace = 0.01
    c.run_pipeline = lambda s: asyncio.Event().wait()
    c.control(b"\x08")
    c.control(bytes.fromhex("04030201"))
    await asyncio.sleep(0.02)
    c.transport.write.assert_not_awaited()
    await c.stop()


async def test_release_during_keepalive_sleep_never_reopens_legacy_microphone():
    from unittest.mock import AsyncMock

    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(8, version=4, codec=1, model=0)
    c.transport = type("Transport", (), {"write": AsyncMock(), "close": AsyncMock()})()
    c.keepalive_interval = 0
    c.run_pipeline = lambda s: asyncio.Event().wait()
    c.pending_open = True
    c.control(b"\x04")
    await asyncio.sleep(0.01)
    c.audio(bytes.fromhex("0000000000001717"))
    c.control(b"\x00")
    await asyncio.sleep(0.06)
    assert bytes.fromhex("0c0001") not in [
        call.args[0] for call in c.transport.write.await_args_list
    ]
    await c.stop()


async def test_busy_tap_stream_is_closed_using_its_own_id():
    from unittest.mock import AsyncMock

    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128, model=1)
    c.transport = type("Transport", (), {"write": AsyncMock(), "close": AsyncMock()})()
    done = asyncio.Event()

    async def consume(session):
        async for _ in session.stream():
            pass
        await done.wait()

    c.run_pipeline = consume
    c.control(bytes.fromhex("04010207"))
    c.audio(bytes(128))
    c.session.release()
    await asyncio.sleep(0.12)
    c.control(bytes.fromhex("04010208"))
    await asyncio.sleep(0.02)
    assert bytes.fromhex("0d08") in [call.args[0] for call in c.transport.write.await_args_list]
    done.set()
    await c.pipeline_task
    assert c.sessions == 1
    await c.stop()


async def test_stale_aborted_session_does_not_authorize_unsolicited_open_start():
    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128)
    c.run_pipeline = lambda s: asyncio.Event().wait()
    c.control(bytes.fromhex("04030207"))
    c.session.abort("disconnected")
    await c.pipeline_task
    c.control(bytes.fromhex("04000208"))
    assert c.sessions == 1
    await c.stop()


async def test_sleep_after_input_eof_does_not_cancel_pending_intent_or_reply():
    from unittest.mock import AsyncMock, patch

    c = VoiceCoordinator(FakeHass(), Entry())
    lost = asyncio.Event()
    caps = Capabilities(128)
    t = type(
        "Transport", (), {"open": AsyncMock(return_value=caps), "close": AsyncMock(), "lost": lost}
    )()
    done = asyncio.Event()
    input_drained = asyncio.Event()

    async def consume(session):
        async for _ in session.stream():
            pass
        input_drained.set()
        await done.wait()

    c.run_pipeline = consume
    with patch("custom_components.bt_remote_voice.coordinator.BlueZTransport", return_value=t):
        c.connection_task = asyncio.create_task(c.connect_loop())
        await asyncio.sleep(0.01)
        c.control(bytes.fromhex("04030207"))
        c.audio(bytes(128))
        c.control(bytes.fromhex("0002"))
        await input_drained.wait()
        lost.set()
        await asyncio.sleep(0.02)
        assert not c.pipeline_task.done()
        done.set()
        await c.pipeline_task
        assert c.failures == 0
        await c.stop()


async def test_an_early_stop_keeps_the_capture_open_when_the_setting_is_on():
    class EarlyEntry(Entry):
        options = {**Entry.options, "ignore_early_stop": True}

    c = VoiceCoordinator(FakeHass(), EarlyEntry())
    c.caps = Capabilities(128)

    async def consume(session):
        async for _ in session.stream():
            pass

    c.run_pipeline = consume
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))  # the remote's early STOP: the button is still held
    assert c.mic_active is True and c.capture.closed is False  # keepalives keep going
    assert c.session.stop_pending and not c.session.released
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))  # the real release
    assert c.mic_active is False and c.capture.closed is True and c.session.released
    await c.pipeline_task


async def test_an_early_stop_ends_the_capture_at_once_when_the_setting_is_off():
    c = VoiceCoordinator(FakeHass(), Entry())
    c.caps = Capabilities(128)

    async def consume(session):
        async for _ in session.stream():
            pass

    c.run_pipeline = consume
    c.control(bytes.fromhex("04030201"))
    c.audio(bytes(128))
    c.control(bytes.fromhex("0002"))
    assert c.mic_active is False and c.capture.closed is True and c.session.released
    await c.pipeline_task
