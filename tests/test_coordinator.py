import asyncio

from custom_components.atvv_remote.coordinator import VoiceCoordinator
from custom_components.atvv_remote.protocol import Capabilities


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
