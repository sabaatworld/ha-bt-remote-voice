"""Recording test clips from the real transports (fakes here, no Bluetooth)."""

import asyncio

import pytest

from custom_components.bt_remote_voice import clip_recorder
from custom_components.bt_remote_voice import session as session_module
from custom_components.bt_remote_voice.clip_recorder import record_test
from custom_components.bt_remote_voice.fire import HOLD_START, HOLD_STOP
from custom_components.bt_remote_voice.protocol import Capabilities
from tests.fire_helpers import synthetic_packets

FIRE_CAPS = Capabilities(frame_size=80, version=5, codec=3, model=0, family="fire")
ATVV_CAPS = Capabilities(frame_size=134, version=256, codec=2, model=3)
SEARCH = bytes([8])
START_TAP = bytes([4, 0, 2, 1])  # reason 0 (after the host opened), codec 2, stream 1
START_HOLD = bytes([4, 3, 2, 1])  # reason 3 (hold), codec 2, stream 1
STOP = bytes([0, 0])
PACKETS = synthetic_packets(30)


class Fake:
    def __init__(self, caps, failures=0):
        self.caps, self.failures = caps, failures
        self.writes, self.closed, self.opens = [], 0, 0
        self.ready = asyncio.Event()

    async def open(self, address, on_control, on_audio):
        self.opens += 1
        if self.opens <= self.failures:
            raise ConnectionError("wake_remote")
        self.control, self.audio = on_control, on_audio
        self.ready.set()
        return self.caps

    async def write(self, data):
        self.writes.append(bytes(data))

    async def close(self):
        self.closed += 1


async def run(fake, script, *, family="fire", seconds=2.0, **kwargs):
    factory = {"fire_factory": lambda: fake} if family == "fire" else {"factory": lambda: fake}
    task = asyncio.create_task(record_test("AA:BB", family, seconds, **factory, **kwargs))
    await asyncio.wait_for(fake.ready.wait(), 5)
    await script(fake)
    return task


async def press_fire(fake, frames=12):
    fake.control(HOLD_START)
    for packet in PACKETS[:frames]:
        fake.audio(packet)
        await asyncio.sleep(0)
    fake.control(HOLD_STOP)
    await asyncio.sleep(0.3)


async def test_fire_press_becomes_one_clip():
    fake = Fake(FIRE_CAPS)
    task = await run(fake, press_fire, seconds=0.6)
    clips = await task
    assert len(clips) == 1
    assert abs(len(clips[0].pcm) - 12 * 640) < 640  # 12 frames of 20 ms, 16 kHz mono 16-bit
    assert clips[0].started is not None
    assert fake.closed == 1


async def test_two_presses_two_clips_with_own_start_times():
    async def script(fake):
        await press_fire(fake, 6)
        await asyncio.sleep(1.1)
        await press_fire(fake, 8)

    fake = Fake(FIRE_CAPS)
    clips = await (await run(fake, script, seconds=2.2))
    assert [round(len(c.pcm) / 640) for c in clips] == [6, 8]
    assert clips[1].started > clips[0].started


async def test_press_without_frames_gives_no_clip():
    async def script(fake):
        fake.control(HOLD_START)
        fake.control(HOLD_STOP)
        await asyncio.sleep(0.3)

    fake = Fake(FIRE_CAPS)
    assert await (await run(fake, script, seconds=0.6)) == []


async def test_fire_hold_press_has_no_vad(monkeypatch):
    seen = []
    real = clip_recorder.VoiceSession

    def spy(*args, **kwargs):
        seen.append(kwargs["caps"])
        return real(*args, **kwargs)

    monkeypatch.setattr(clip_recorder, "VoiceSession", spy)
    fake = Fake(FIRE_CAPS)
    await (await run(fake, press_fire, seconds=0.6))
    assert seen[0].model == 3 and seen[0].codec == 3  # reason 3 disables the VAD, like live


async def test_atvv_search_opens_mic_and_records_then_closes():
    async def script(fake):
        fake.control(SEARCH)
        await asyncio.sleep(0.3)
        fake.control(START_TAP)
        for _ in range(5):
            fake.audio(b"\x00" * 134)
            await asyncio.sleep(0)
        fake.control(STOP)
        await asyncio.sleep(0.3)

    fake = Fake(ATVV_CAPS)
    clips = await (await run(fake, script, family="atvv", seconds=0.9))
    assert bytes([12, 0]) in fake.writes  # the host opened the microphone
    assert bytes([13, 1]) in fake.writes  # and closed it for stream 1
    assert len(clips) == 1 and clips[0].pcm


async def test_atvv_start_reason_0_without_search_is_ignored():
    async def script(fake):
        fake.control(START_TAP)
        fake.audio(b"\x00" * 134)
        await asyncio.sleep(0.2)

    fake = Fake(ATVV_CAPS)
    assert await (await run(fake, script, family="atvv", seconds=0.5)) == []


async def test_atvv_keepalive_sent_while_open(monkeypatch):
    monkeypatch.setattr(clip_recorder, "KEEPALIVE_SECONDS", 0.05)

    async def script(fake):
        fake.control(START_HOLD)
        fake.audio(b"\x00" * 134)
        await asyncio.sleep(0.4)
        fake.control(STOP)
        await asyncio.sleep(0.2)

    fake = Fake(ATVV_CAPS)
    await (await run(fake, script, family="atvv", seconds=0.8))
    assert bytes([14, 1]) in fake.writes


async def test_wake_retry_until_connected(monkeypatch):
    monkeypatch.setattr(clip_recorder, "RETRY_SECONDS", 0.01)
    fake = Fake(FIRE_CAPS, failures=2)
    clips = await (await run(fake, press_fire, seconds=0.8))
    assert fake.opens == 3 and len(clips) == 1


async def test_cancel_mid_press_writes_close_and_closes_transport():
    async def script(fake):
        fake.control(HOLD_START)
        fake.audio(PACKETS[0])
        await asyncio.sleep(0.05)

    fake = Fake(FIRE_CAPS)
    task = await run(fake, script, seconds=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bytes([13, 1]) in fake.writes  # Fire turns this into the microphone disable
    assert fake.closed == 1


async def test_deadline_cuts_open_press_and_keeps_audio():
    async def script(fake):
        fake.control(HOLD_START)
        for packet in PACKETS[:5]:
            fake.audio(packet)

    fake = Fake(FIRE_CAPS)
    clips = await (await run(fake, script, seconds=0.3))
    assert len(clips) == 1 and clips[0].pcm
    assert bytes([13, 1]) in fake.writes and fake.closed == 1


async def test_open_error_never_raises():
    class Broken(Fake):
        async def open(self, *args):
            self.ready.set()
            raise ValueError("fire_voice_unsupported")

    fake = Broken(FIRE_CAPS)
    assert await (await run(fake, lambda f: asyncio.sleep(0), seconds=0.3)) == []
    assert fake.closed == 1


async def test_plain_key_remote_gives_zero_clips():
    fake = Fake(FIRE_CAPS)
    assert await (await run(fake, lambda f: asyncio.sleep(0.1), seconds=0.3)) == []


async def test_default_fire_transport_uses_the_entrys_short_press_filter(monkeypatch):
    made = []

    class Spy(Fake):
        def __init__(self, min_press_seconds):
            super().__init__(FIRE_CAPS)
            made.append(min_press_seconds)

    monkeypatch.setattr(clip_recorder, "FireTransport", Spy)
    await record_test("AA:BB", "fire", 0.1, min_press_seconds=0.25)
    assert made == [0.25]


async def test_each_clip_says_how_it_ended_and_how_many_frames():
    fake = Fake(FIRE_CAPS)
    clips = await (await run(fake, press_fire, seconds=0.6))
    assert clips[0].frames == 12 and clips[0].ended == "released" and clips[0].reason == 3


async def test_search_and_start_events_are_reported_for_the_test_result():
    events = []

    async def script(fake):
        fake.control(SEARCH)
        await asyncio.sleep(0.3)
        fake.control(START_HOLD)
        fake.audio(b"\x00" * 134)
        fake.control(STOP)
        await asyncio.sleep(0.3)

    fake = Fake(ATVV_CAPS)
    await (await run(fake, script, family="atvv", seconds=0.9, events=events))
    assert [(kind, reason, codec) for _t, kind, reason, codec in events] == [
        ("search", None, 2),
        ("start", 3, 2),
    ]


async def test_open_failure_is_reported_as_a_stable_error_code():
    class Broken(Fake):
        async def open(self, *args):
            self.ready.set()
            raise ValueError("voice_service_unavailable")

    errors = []
    fake = Broken(FIRE_CAPS)
    await (await run(fake, lambda f: asyncio.sleep(0), seconds=0.1, errors=errors))
    assert errors == ["voice_service_unavailable"]

    class Odd(Fake):
        async def open(self, *args):
            self.ready.set()
            raise RuntimeError("secret detail")

    errors = []
    await (await run(Odd(FIRE_CAPS), lambda f: asyncio.sleep(0), seconds=0.1, errors=errors))
    assert errors == ["bluetooth_unavailable"]  # never the raw message


async def early_stop_script(fake):  # the BT19K: START, 5 frames, STOP, then more audio, 2nd STOP
    fake.control(START_HOLD)
    for _ in range(5):
        fake.audio(b"\x00" * 134)
    fake.control(STOP)
    for _ in range(40):  # the stream goes on for 0.6 s after the first STOP
        fake.audio(b"\x00" * 134)
        await asyncio.sleep(0.015)
    await asyncio.sleep(0.4)


async def test_early_stop_is_ignored_when_the_setting_is_on(monkeypatch):
    monkeypatch.setattr(session_module, "STOP_QUIET_SECONDS", 0.15)
    summary = {}
    fake = Fake(ATVV_CAPS)
    clips = await (
        await run(
            fake,
            early_stop_script,
            family="atvv",
            seconds=2.5,
            ignore_early_stop=True,
            summary=summary,
        )
    )
    assert len(clips) == 1 and clips[0].frames == 45 and clips[0].ended == "released"
    assert summary["early_stops"] == 1 and summary["early_stops_ignored"] == 1  # handled


async def test_early_stop_cuts_the_clip_when_the_setting_is_off_but_is_still_reported():
    summary = {}
    fake = Fake(ATVV_CAPS)
    clips = await (await run(fake, early_stop_script, family="atvv", seconds=2.5, summary=summary))
    # What a real request gets: the frames up to the STOP plus the 0.1 s the session waits for.
    assert len(clips) == 1 and clips[0].frames < 45
    assert summary["early_stops"] == 1 and summary["early_stops_ignored"] == 0
    assert summary["gaps"] == 0


async def test_a_second_stop_ends_the_press_without_waiting(monkeypatch):
    monkeypatch.setattr(session_module, "STOP_QUIET_SECONDS", 5)  # would outlast the test

    async def script(fake):
        fake.control(START_HOLD)
        for _ in range(5):
            fake.audio(b"\x00" * 134)
        fake.control(STOP)
        fake.audio(b"\x00" * 134)
        fake.control(STOP)
        await asyncio.sleep(0.4)

    fake = Fake(ATVV_CAPS)
    clips = await (await run(fake, script, family="atvv", seconds=1.0, ignore_early_stop=True))
    assert len(clips) == 1 and clips[0].frames == 6 and clips[0].ended == "released"


async def test_lost_frames_are_filled_with_silence_when_the_setting_allows():
    from custom_components.bt_remote_voice.protocol import Control

    async def script(fake):
        fake.control(START_HOLD)
        for _ in range(5):
            fake.audio(b"\x00" * 134)
        # the remote says frame 8 comes next: three frames were lost
        fake.control(bytes([10, 2, 0, 8, 0, 0, 0]))
        fake.audio(b"\x00" * 134)
        fake.control(STOP)
        await asyncio.sleep(0.3)

    summary = {}
    fake = Fake(ATVV_CAPS)
    clips = await (
        await run(fake, script, family="atvv", seconds=0.8, max_gap_ms=500, summary=summary)
    )
    assert len(clips) == 1 and clips[0].ended == "released"
    assert summary["gaps"] == 1 and summary["gaps_covered"] == 1 and summary["lost_frames"] == 3
    assert Control  # (the sync frame above is the ATVV control 0x0A)


async def test_lost_frames_end_the_clip_when_the_setting_is_off():
    async def script(fake):
        fake.control(START_HOLD)
        for _ in range(5):
            fake.audio(b"\x00" * 134)
        fake.control(bytes([10, 2, 0, 8, 0, 0, 0]))
        await asyncio.sleep(0.3)

    summary = {}
    fake = Fake(ATVV_CAPS)
    clips = await (await run(fake, script, family="atvv", seconds=0.8, summary=summary))
    assert clips[0].ended == "audio_sequence_gap" and summary["gaps"] == 1
    assert summary["gaps_covered"] == 0


async def test_a_quick_release_is_not_reported_as_an_early_stop():
    async def script(fake):  # START, STOP 7 ms later, a few frames, the real STOP right after
        fake.control(START_HOLD)
        fake.control(STOP)
        for _ in range(3):
            fake.audio(b"\x00" * 134)
        fake.control(STOP)
        await asyncio.sleep(0.3)

    summary = {}
    fake = Fake(ATVV_CAPS)
    clips = await (
        await run(fake, script, family="atvv", seconds=0.8, ignore_early_stop=True, summary=summary)
    )
    assert len(clips) == 1 and summary["early_stops"] == 0


async def test_early_stops_are_not_counted_where_the_setting_cannot_apply():
    summary = {}
    fake = Fake(ATVV_CAPS)
    await (
        await run(
            fake,
            early_stop_script,
            family="atvv",
            seconds=2.5,
            early_stop_applies=False,
            summary=summary,
        )
    )
    assert summary["early_stops"] == 0
