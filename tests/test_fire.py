import asyncio
from unittest.mock import patch

import pytest

from custom_components.bt_remote_voice.fire import (
    FireTransport,
    find_hidraw,
    parse_report,
)

ADDRESS = "02:00:00:00:00:AF"
VOICE_DOWN = bytes.fromhex("0221020000")
VOICE_UP = bytes.fromhex("0200000000")
VOLUME_UP = bytes.fromhex("02e9000000")
AUDIO = b"\xf0" + bytes([0xB8]) + bytes(79)


class FakeHid:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.writes = []
        self.closed = False

    async def read(self):
        item = await self.queue.get()
        if item is None:
            raise OSError(19, "No such device")
        return item

    def write(self, data):
        self.writes.append(bytes(data))

    def close(self):
        self.closed = True

    def feed(self, data):
        self.queue.put_nowait(data)

    def disconnect(self):
        self.queue.put_nowait(None)


class Sink:
    def __init__(self):
        self.control = []
        self.audio = []


async def _open(device=None, **kwargs):
    device = device or FakeHid()
    transport = FireTransport(
        opener=lambda path: device,
        find=lambda address: "/dev/hidraw9",
        **kwargs,
    )
    sink = Sink()
    caps = await transport.open(ADDRESS, sink.control.append, sink.audio.append)
    return transport, device, sink, caps


def test_find_hidraw_matches_address_case_insensitively(tmp_path):
    for name, uniq in (("hidraw0", "02:00:00:00:00:be"), ("hidraw1", "02:00:00:00:00:af")):
        node = tmp_path / name / "device"
        node.mkdir(parents=True)
        (node / "uevent").write_text(f"HID_NAME=X\nHID_UNIQ={uniq}\n")
    assert find_hidraw(ADDRESS, sysfs=str(tmp_path)) == "/dev/hidraw1"
    assert find_hidraw("02:00:00:00:00:Af", sysfs=str(tmp_path)) == "/dev/hidraw1"
    assert find_hidraw("AA:BB:CC:DD:EE:FF", sysfs=str(tmp_path)) is None


def test_parse_report_kinds():
    assert parse_report(VOICE_DOWN, False) == ("voice_down", b"")
    assert parse_report(VOICE_DOWN, True) == ("other", b"")  # still held: no second start
    assert parse_report(VOICE_UP, True) == ("voice_up", b"")
    assert parse_report(VOICE_UP, False) == ("other", b"")  # another key's release
    assert parse_report(VOLUME_UP, False) == ("other", b"")
    assert parse_report(VOICE_UP, False)[0] == "other"
    assert parse_report(AUDIO, True) == ("audio", AUDIO[1:])
    assert parse_report(bytes.fromhex("01510000"), False)[0] == "other"


def test_voice_in_second_usage_slot_is_detected():
    assert parse_report(bytes.fromhex("0200002102"), False)[0] == "voice_down"


async def test_press_enables_mic_once_and_forwards_audio():
    transport, device, sink, caps = await _open()
    assert (caps.codec, caps.frame_size, caps.family) == (3, 80, "fire")
    device.feed(VOICE_DOWN)
    for _ in range(3):
        device.feed(AUDIO)
    await asyncio.sleep(0.05)
    assert sink.control == [bytes([4, 3, 3, 1])]
    assert sink.audio == [AUDIO[1:]] * 3
    assert device.writes == [b"\xf2\x01"]  # one enable write per press
    await transport.close()


async def test_release_disables_mic_and_sends_stop():
    transport, device, sink, _ = await _open()
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    device.feed(VOICE_UP)
    await asyncio.sleep(0.05)
    assert sink.control == [bytes([4, 3, 3, 1]), bytes([0, 0])]
    assert device.writes == [b"\xf2\x01", b"\xf2\x00"]
    await transport.close()


async def test_other_consumer_key_release_is_not_a_voice_release():
    transport, device, sink, _ = await _open()
    device.feed(VOLUME_UP)
    device.feed(VOICE_UP)  # all-zero report after a non-voice key
    await asyncio.sleep(0.05)
    assert sink.control == [] and device.writes == []
    await transport.close()


async def test_rearms_when_frames_stop_while_key_down():
    transport, device, sink, _ = await _open(rearm_seconds=0.1)
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    await asyncio.sleep(0.45)
    enables = [w for w in device.writes if w == b"\xf2\x01"]
    assert 2 <= len(enables) <= 5  # re-armed, but not on every poll
    await transport.close()


async def test_audio_gaps_up_to_300ms_are_not_a_loss():
    transport, device, sink, _ = await _open()  # default re-arm after 0.5 s
    device.feed(VOICE_DOWN)
    for _ in range(3):
        device.feed(AUDIO)
        await asyncio.sleep(0.3)
    assert device.writes == [b"\xf2\x01"]
    assert len(sink.audio) == 3
    await transport.close()


async def test_close_command_switches_mic_off_and_blocks_rearm():
    transport, device, sink, _ = await _open(rearm_seconds=0.1)
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    await asyncio.sleep(0.05)
    await transport.write(bytes([13, 1]))  # the coordinator closing the microphone
    assert device.writes[-1] == b"\xf2\x00"
    before = len(device.writes)
    await asyncio.sleep(0.4)  # key still down, frames stopped
    assert len(device.writes) == before
    device.feed(AUDIO)
    await asyncio.sleep(0.05)
    assert len(sink.audio) == 1  # frames after the close are ignored
    await transport.write(bytes([12, 0]))  # open/keepalive commands are no-ops
    await transport.write(bytes([14, 1]))
    assert len(device.writes) == before
    await transport.close()


async def test_node_loss_sets_lost_and_closes_cleanly():
    transport, device, sink, _ = await _open()
    device.feed(VOICE_DOWN)
    device.disconnect()
    await asyncio.wait_for(transport.lost.wait(), 1)
    assert await transport.check_connected() is False
    await transport.close()
    assert device.closed and transport.task.done()


async def test_permission_error_maps_to_hidraw_permission_denied():
    def opener(path):
        raise PermissionError

    transport = FireTransport(opener=opener, find=lambda a: "/dev/hidraw9")
    with pytest.raises(ValueError, match="hidraw_permission_denied"):
        await transport.open(ADDRESS, lambda d: None, lambda d: None)


async def test_unopenable_device_maps_to_hidraw_unavailable():
    def opener(path):
        raise OSError(19, "gone")

    transport = FireTransport(opener=opener, find=lambda a: "/dev/hidraw9")
    with pytest.raises(ConnectionError, match="hidraw_unavailable"):
        await transport.open(ADDRESS, lambda d: None, lambda d: None)


async def test_missing_node_times_out_with_wake_remote():
    transport = FireTransport(opener=lambda p: FakeHid(), find=lambda a: None, wait_seconds=0.2)
    with pytest.raises(ConnectionError, match="wake_remote"):
        await transport.open(ADDRESS, lambda d: None, lambda d: None)


async def test_missing_opus_decoder_fails_in_open():
    transport = FireTransport(opener=lambda p: FakeHid(), find=lambda a: "/dev/hidraw9")
    with patch("custom_components.bt_remote_voice.fire.opus_available", return_value=False):
        with pytest.raises(ValueError, match="opus_decoder_unavailable"):
            await transport.open(ADDRESS, lambda d: None, lambda d: None)


# ---- A3: session, coordinator, error codes ----
from types import SimpleNamespace  # noqa: E402

from custom_components.bt_remote_voice.coordinator import VoiceCoordinator  # noqa: E402
from custom_components.bt_remote_voice.protocol import Capabilities  # noqa: E402
from custom_components.bt_remote_voice.session import VoiceSession  # noqa: E402
from tests.fire_helpers import synthetic_packets  # noqa: E402
from tests.test_coordinator import FakeHass  # noqa: E402

FIRE_CAPS = Capabilities(frame_size=80, version=5, codec=3, model=3, family="fire")
PACKETS = synthetic_packets(50)


def _report(packet):
    return b"\xf0" + packet


def _fire_coordinator(**options):
    entry = SimpleNamespace(
        title="Fire remote",
        data={"address": ADDRESS, "name": "Fire remote", "pipeline_id": "p", "family": "fire"},
        options=options,
    )
    return VoiceCoordinator(FakeHass(), entry)


async def _wired(**options):
    """A coordinator driven by a FireTransport over a fake hidraw device."""
    c = _fire_coordinator(**options)
    device = FakeHid()
    transport = FireTransport(opener=lambda p: device, find=lambda a: "/dev/hidraw9")
    c.transport = transport
    c.caps = await transport.open(ADDRESS, c.control, c.audio)
    return c, transport, device


def test_fire_capabilities_profile_and_rate():
    assert FIRE_CAPS.sample_rate == 16000
    assert FIRE_CAPS.profile == "fire_0005_16000_3"
    assert Capabilities(128).profile == "atvv_0100_16000_3"  # ATVV unchanged


async def test_session_decodes_opus_packets_to_pcm():
    session = VoiceSession(80, caps=FIRE_CAPS, finish_delay=0)
    for packet in PACKETS[:10]:
        session.audio(packet)
    session.release()
    chunks = [x async for x in session.stream()]
    assert session.error is None and session.frames == 10
    assert abs(sum(len(x) for x in chunks) - 10 * 640) <= 0.05 * 10 * 640


@pytest.mark.parametrize(
    "packet", [bytes(79), bytes(81), bytes([0x48]) + bytes(79)], ids=["short", "long", "bad_toc"]
)
async def test_invalid_opus_packet_aborts_the_session(packet):
    session = VoiceSession(80, caps=FIRE_CAPS, finish_delay=0)
    session.audio(packet)
    assert session.error == "invalid_opus_packet"


async def test_missing_decoder_aborts_with_a_translated_code():
    session = VoiceSession(80, caps=FIRE_CAPS, finish_delay=0)
    with patch(
        "custom_components.bt_remote_voice.session.OpusDecoder",
        side_effect=ValueError("opus_decoder_unavailable"),
    ):
        session.audio(PACKETS[0])
    assert session.error == "opus_decoder_unavailable"


async def test_fire_press_to_reply_through_the_coordinator():
    c, transport, device = await _wired()
    received = []

    async def run(s):
        received.extend([x async for x in s.stream()])

    c.run_pipeline = run
    device.feed(VOICE_DOWN)
    for packet in PACKETS[:25]:
        device.feed(_report(packet))
    device.feed(VOICE_UP)
    await asyncio.sleep(0.3)
    await c.pipeline_task
    assert c.failures == 0 and c.last_error is None
    assert abs(sum(len(x) for x in received) - 25 * 640) <= 0.05 * 25 * 640
    assert device.writes[0] == b"\xf2\x01" and device.writes[-1] == b"\xf2\x00"
    await transport.close()
    await c.stop()


async def test_fire_held_conversation_two_turns():
    from types import SimpleNamespace as NS

    c, transport, device = await _wired(input_mode="conversation")
    turns = []
    gate = asyncio.Event()

    async def run(s):
        turns.append([x async for x in s.stream()])
        if len(turns) == 1:
            await gate.wait()

    c.run_pipeline = run
    device.feed(VOICE_DOWN)
    await asyncio.sleep(0.05)
    held = c.capture.held
    held.vad = NS(Process10ms=lambda pcm: 1)
    for i in range(40):
        device.feed(_report(PACKETS[i % 50]))
    await asyncio.sleep(0.1)
    held.vad = NS(Process10ms=lambda pcm: 0)
    for i in range(60):
        device.feed(_report(PACKETS[i % 50]))
    await asyncio.sleep(0.2)
    assert len(turns) == 1 and c.mic_active
    gate.set()
    await asyncio.sleep(0.5)
    held.vad = NS(Process10ms=lambda pcm: 1)
    for i in range(40):
        device.feed(_report(PACKETS[i % 50]))
    await asyncio.sleep(0.2)  # let the reader consume the speech before swapping the VAD
    held.vad = NS(Process10ms=lambda pcm: 0)
    for i in range(60):
        device.feed(_report(PACKETS[i % 50]))
    await asyncio.sleep(0.4)
    assert len(turns) == 2
    device.feed(VOICE_UP)
    await c.pipeline_task
    assert c.failures == 0
    await transport.close()
    await c.stop()


async def test_hold_longer_than_request_limit_ends_with_capture_limit_and_mic_off():
    c, transport, device = await _wired(max_request_seconds=0.3)
    c.run_pipeline = lambda s: asyncio.sleep(5)
    device.feed(VOICE_DOWN)
    for i in range(10):
        device.feed(_report(PACKETS[i]))
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.4)
    assert c.session.error == "capture_limit"
    assert b"\xf2\x00" in device.writes  # the coordinator's close reached the remote
    await transport.close()
    await c.stop()


async def test_fire_mic_closed_on_a_rejected_press():
    c, transport, device = await _wired()
    gate = asyncio.Event()

    async def run(s):
        _ = [x async for x in s.stream()]
        await gate.wait()

    c.run_pipeline = run
    device.feed(VOICE_DOWN)
    device.feed(_report(PACKETS[0]))
    device.feed(VOICE_UP)
    await asyncio.sleep(0.3)
    c.phase = "processing"
    device.writes.clear()
    device.feed(VOICE_DOWN)  # busy: rejected
    await asyncio.sleep(0.1)
    assert c.rejected == 1
    assert b"\xf2\x00" in device.writes
    gate.set()
    await transport.close()
    await c.stop()


async def test_codec_is_accepted_only_for_its_own_family():
    fire = _fire_coordinator()
    fire.caps = FIRE_CAPS
    fire.run_pipeline = lambda s: asyncio.sleep(5)
    fire.control(bytes([4, 3, 1, 1]))  # an ATVV codec on a Fire remote
    assert fire.sessions == 0
    fire.control(bytes([4, 3, 3, 1]))
    assert fire.sessions == 1
    await fire.stop()
    atvv = VoiceCoordinator(
        FakeHass(),
        SimpleNamespace(data={"address": ADDRESS, "name": "A", "pipeline_id": "p"}, options={}),
    )
    atvv.caps = Capabilities(128)
    atvv.run_pipeline = lambda s: asyncio.sleep(5)
    atvv.control(bytes([4, 3, 3, 1]))  # the Fire codec on an ATVV remote
    assert atvv.sessions == 0
    await atvv.stop()


async def test_unplugged_node_mid_hold_aborts_with_disconnected_and_cleans_up():
    c = _fire_coordinator()
    device = FakeHid()
    transport = FireTransport(opener=lambda p: device, find=lambda a: "/dev/hidraw9")
    c.run_pipeline = lambda s: asyncio.sleep(5)
    with patch(
        "custom_components.bt_remote_voice.coordinator.FireTransport", return_value=transport
    ):
        c.connection_task = asyncio.create_task(c.connect_loop())
        await asyncio.sleep(0.1)
        device.feed(VOICE_DOWN)
        device.feed(_report(PACKETS[0]))
        await asyncio.sleep(0.1)
        assert c.session is not None and c.session.error is None
        device.disconnect()
        await asyncio.sleep(0.3)
        # connect_loop aborts the input with "disconnected", then cancels the pipeline,
        # which marks the session "cancelled"; either way it must be finished and cleaned up.
        assert c.session.finished and c.session.error in ("disconnected", "cancelled")
        assert device.closed and transport.task.done()
        await c.stop()


async def test_wake_remote_does_not_back_off():
    c = _fire_coordinator()
    attempts = []
    delays = []

    class Sleeper:
        def __init__(self):
            self.real = asyncio.sleep

        async def __call__(self, delay, *a):
            delays.append(delay)
            await self.real(0)

    class Waking:
        lost = asyncio.Event()

        async def open(self, *args):
            attempts.append(1)
            raise ConnectionError("wake_remote")

        async def close(self):
            pass

    with (
        patch("custom_components.bt_remote_voice.coordinator.FireTransport", return_value=Waking()),
        patch("custom_components.bt_remote_voice.coordinator.asyncio.sleep", Sleeper()),
    ):
        task = asyncio.create_task(c.connect_loop())
        for _ in range(100):
            await asyncio.sleep(0)
            if len(attempts) >= 5:
                break
        c.stopping = True
        await asyncio.sleep(0.01)
        task.cancel()
    assert len(attempts) >= 5 and max(delays[:5]) <= 2, delays


# ---- review fixes: loop safety and reader robustness ----
async def test_device_lookup_runs_off_the_event_loop():
    import threading

    seen = []

    def find(address):
        seen.append(threading.current_thread() is threading.main_thread())
        return "/dev/hidraw9"

    transport = FireTransport(opener=lambda p: FakeHid(), find=find)
    await transport.open(ADDRESS, lambda d: None, lambda d: None)
    assert seen == [False]  # sysfs glob/open must not block Home Assistant's event loop
    await transport.close()


async def test_callback_exception_does_not_kill_the_reader(caplog):
    calls = []

    def on_audio(data):
        calls.append(data)
        if len(calls) == 1:
            raise RuntimeError("boom")

    device = FakeHid()
    transport = FireTransport(opener=lambda p: device, find=lambda a: "/dev/hidraw9")
    await transport.open(ADDRESS, lambda d: None, on_audio)
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    device.feed(AUDIO)
    await asyncio.sleep(0.1)
    assert len(calls) == 2 and not transport.lost.is_set()
    assert any("boom" in r.getMessage() or r.exc_info for r in caplog.records)
    await transport.close()


async def test_reader_failure_sets_lost_and_close_never_raises():
    class Broken(FakeHid):
        async def read(self):
            raise RuntimeError("unexpected")

    device = Broken()
    transport = FireTransport(opener=lambda p: device, find=lambda a: "/dev/hidraw9")
    await transport.open(ADDRESS, lambda d: None, lambda d: None)
    await asyncio.wait_for(transport.lost.wait(), 1)
    assert await transport.check_connected() is False
    await transport.close()  # must not re-raise the reader's exception


# ---- Ignore short presses (min_press_seconds) ----
HOLD_START = bytes([4, 3, 3, 1])


async def test_a_press_shorter_than_the_minimum_is_ignored_silently():
    transport, device, sink, _ = await _open(min_press_seconds=0.1)
    device.feed(VOICE_DOWN)
    device.feed(VOICE_UP)  # the wake replay: instantaneous press and release
    await asyncio.sleep(0.25)
    assert sink.control == [] and device.writes == [] and sink.audio == []
    assert transport.ignored_presses == 1
    await transport.close()


async def test_a_press_held_past_the_minimum_starts_once_after_the_delay():
    transport, device, sink, _ = await _open(min_press_seconds=0.1)
    device.feed(VOICE_DOWN)
    await asyncio.sleep(0.03)
    assert sink.control == [] and device.writes == []  # still pending
    await asyncio.sleep(0.15)
    assert sink.control == [HOLD_START] and device.writes == [b"\xf2\x01"]
    device.feed(AUDIO)
    device.feed(VOICE_UP)
    await asyncio.sleep(0.05)
    assert sink.audio == [AUDIO[1:]]
    assert sink.control == [HOLD_START, bytes([0, 0])]
    assert device.writes == [b"\xf2\x01", b"\xf2\x00"]
    assert transport.ignored_presses == 0
    await transport.close()


async def test_default_of_zero_starts_immediately_as_before():
    transport, device, sink, _ = await _open()
    device.feed(VOICE_DOWN)
    await asyncio.sleep(0.03)
    assert sink.control == [HOLD_START] and device.writes == [b"\xf2\x01"]
    await transport.close()


async def test_audio_that_arrives_while_the_press_is_pending_is_discarded():
    transport, device, sink, _ = await _open(min_press_seconds=0.1)
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    await asyncio.sleep(0.03)
    assert sink.audio == []
    await asyncio.sleep(0.15)
    assert sink.audio == [] and sink.control == [HOLD_START]
    await transport.close()


async def test_closing_with_a_pending_press_cancels_the_start():
    transport, device, sink, _ = await _open(min_press_seconds=0.1)
    device.feed(VOICE_DOWN)
    await asyncio.sleep(0.03)
    await transport.close()
    await asyncio.sleep(0.2)
    assert sink.control == [] and device.writes == []


async def test_a_new_press_after_an_ignored_one_is_judged_on_its_own():
    transport, device, sink, _ = await _open(min_press_seconds=0.1)
    device.feed(VOICE_DOWN)
    device.feed(VOICE_UP)
    await asyncio.sleep(0.03)
    device.feed(VOICE_DOWN)
    await asyncio.sleep(0.2)
    assert transport.ignored_presses == 1
    assert sink.control == [HOLD_START] and device.writes == [b"\xf2\x01"]
    await transport.close()


async def _transport_built_for(**options):
    c = _fire_coordinator(**options)
    built = []

    class Stop:
        lost = asyncio.Event()

        async def open(self, *args):
            c.stopping = True
            raise ConnectionError("wake_remote")

        async def close(self):
            pass

    def make(**kwargs):
        built.append(kwargs)
        return Stop()

    with patch("custom_components.bt_remote_voice.coordinator.FireTransport", make):
        await asyncio.wait_for(c.connect_loop(), 2)
    return built


async def test_coordinator_passes_the_configured_minimum_to_the_fire_transport():
    assert await _transport_built_for(min_press_seconds=0.1) == [{"min_press_seconds": 0.1}]
    assert await _transport_built_for() == [{"min_press_seconds": 0}]


@pytest.mark.parametrize("bad", ["0.1", None, True, -1, float("nan")])
async def test_an_unusable_option_value_turns_the_filter_off_instead_of_killing_voice(bad):
    assert await _transport_built_for(min_press_seconds=bad) == [{"min_press_seconds": 0}]


async def test_closing_clears_the_pending_timer_itself():
    transport, device, sink, _ = await _open(min_press_seconds=0.5)
    device.feed(VOICE_DOWN)
    await asyncio.sleep(0.03)
    assert transport.pending is not None
    await transport.close()
    assert transport.pending is None


async def test_a_failing_start_callback_is_logged_and_does_not_leave_a_half_started_hold(caplog):
    transport, device, sink, _ = await _open(min_press_seconds=0.05)

    def boom(data):
        raise RuntimeError("callback failed")

    transport.on_control = boom
    with caplog.at_level("ERROR"):
        device.feed(VOICE_DOWN)
        await asyncio.sleep(0.15)
    assert "Fire remote hold start failed" in caplog.text
    assert transport.voice_down is False and device.writes == []
    await transport.close()


# --- descriptor validation (issue #1): opt-in, legacy-safe --------------------------------

from pathlib import Path  # noqa: E402

from custom_components.bt_remote_voice.fire import find_all_hidraw, read_descriptor  # noqa: E402

GOOD_DESCRIPTOR = bytes.fromhex(
    (Path(__file__).parent / "fixtures" / "fire_0413_report_descriptor.hex").read_text().strip()
)
# vendor audio report but only a 10-byte output report (no 1-byte 0xF2 enable)
NO_PAIR_DESCRIPTOR = bytes.fromhex("0600ff 85f0 9550 7508 8100 85f3 950a 9102".replace(" ", ""))


async def _open_validated(nodes, descriptors, device=None):
    device = device or FakeHid()
    opened = []

    def opener(path):
        opened.append(path)
        return device

    transport = FireTransport(
        opener=opener,
        find_all=lambda address: list(nodes),
        read_descriptor=lambda node: descriptors.get(node),
    )
    sink = Sink()
    caps = await transport.open(ADDRESS, sink.control.append, sink.audio.append)
    return transport, device, sink, caps, opened


def test_find_all_hidraw_returns_every_matching_node(tmp_path):
    for name, uniq in (
        ("hidraw0", "02:00:00:00:00:af"),
        ("hidraw1", "02:00:00:00:00:be"),
        ("hidraw2", "02:00:00:00:00:AF"),
    ):
        node = tmp_path / name / "device"
        node.mkdir(parents=True)
        (node / "uevent").write_text(f"HID_UNIQ={uniq}\n")
    assert find_all_hidraw(ADDRESS, sysfs=str(tmp_path)) == ["/dev/hidraw0", "/dev/hidraw2"]
    assert find_all_hidraw("AA:BB:CC:DD:EE:FF", sysfs=str(tmp_path)) == []


def test_read_descriptor_maps_dev_node_to_sysfs_and_returns_none_on_error(tmp_path):
    node = tmp_path / "hidraw3" / "device"
    node.mkdir(parents=True)
    (node / "report_descriptor").write_bytes(b"\x05\x01")
    assert read_descriptor("/dev/hidraw3", sysfs=str(tmp_path)) == b"\x05\x01"
    assert read_descriptor("/dev/hidraw9", sysfs=str(tmp_path)) is None


async def test_descriptor_without_the_voice_pair_refuses_and_never_opens_or_writes():
    device = FakeHid()
    opened = []

    def opener(path):
        opened.append(path)
        return device

    transport = FireTransport(
        opener=opener,
        find_all=lambda a: ["/dev/hidraw4"],
        read_descriptor=lambda node: NO_PAIR_DESCRIPTOR,
        refuse_after_seconds=0.05,
        poll_seconds=0.01,
    )
    with pytest.raises(ValueError, match="fire_voice_unsupported"):
        await transport.open(ADDRESS, lambda d: None, lambda d: None)
    assert opened == [] and device.writes == []
    assert transport.descriptor_reports  # kept so the report can show what was found


async def test_descriptor_with_the_pair_uses_the_exact_legacy_bytes():
    transport, device, sink, _, opened = await _open_validated(
        ["/dev/hidraw4"], {"/dev/hidraw4": GOOD_DESCRIPTOR}
    )
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    device.feed(VOICE_UP)
    await asyncio.sleep(0.05)
    assert opened == ["/dev/hidraw4"]
    assert device.writes == [b"\xf2\x01", b"\xf2\x00"]
    assert sink.audio == [AUDIO[1:]]
    await transport.close()


async def test_unreadable_descriptor_keeps_legacy_behavior():
    transport, device, _, _, opened = await _open_validated(["/dev/hidraw4", "/dev/hidraw5"], {})
    assert opened == ["/dev/hidraw4"]  # first node, constants unchanged
    assert transport.descriptor_reports is None
    await transport.close()


async def test_one_unreadable_node_keeps_legacy_behavior_instead_of_refusing():
    # A readable node without the pair next to one that cannot be read: the unreadable one might
    # be the voice node, so the original first-node behavior applies (0.5.0) rather than a refusal.
    transport, _, _, _, opened = await _open_validated(
        ["/dev/hidraw3", "/dev/hidraw4"], {"/dev/hidraw3": NO_PAIR_DESCRIPTOR}
    )
    assert opened == ["/dev/hidraw4"]
    await transport.close()


async def test_a_later_node_with_the_pair_is_chosen_over_an_earlier_one_without():
    transport, _, _, _, opened = await _open_validated(
        ["/dev/hidraw3", "/dev/hidraw4"],
        {"/dev/hidraw3": NO_PAIR_DESCRIPTOR, "/dev/hidraw4": GOOD_DESCRIPTOR},
    )
    assert opened == ["/dev/hidraw4"]
    await transport.close()


async def test_node_appearing_later_is_waited_for_with_the_validating_finder():
    calls = []

    def find_all(address):
        calls.append(1)
        return [] if len(calls) < 3 else ["/dev/hidraw4"]

    transport = FireTransport(
        opener=lambda p: FakeHid(),
        find_all=find_all,
        read_descriptor=lambda n: GOOD_DESCRIPTOR,
        wait_seconds=2,
        poll_seconds=0.01,
    )
    await transport.open(ADDRESS, lambda d: None, lambda d: None)
    assert len(calls) == 3
    await transport.close()


async def test_default_transport_without_a_reader_is_unchanged():
    transport, device, _, _ = await _open()
    assert transport.descriptor_reports is None
    device.feed(VOICE_DOWN)
    await asyncio.sleep(0.05)
    assert device.writes == [b"\xf2\x01"]
    await transport.close()


def test_fire_module_has_no_gatt_imports():
    # Fire remotes are HID-only: a vendor GATT characteristic can wipe or unpair the remote.
    source = (
        Path(__file__).parent.parent / "custom_components/bt_remote_voice/fire.py"
    ).read_text()
    assert "dbus_fast" not in source and "GattCharacteristic" not in source


async def test_a_sibling_node_that_appears_later_with_the_pair_is_still_found():
    polls = []

    def find_all(address):
        polls.append(1)
        return ["/dev/hidraw3"] if len(polls) < 3 else ["/dev/hidraw3", "/dev/hidraw4"]

    opened = []
    transport = FireTransport(
        opener=lambda path: opened.append(path) or FakeHid(),
        find_all=find_all,
        read_descriptor=lambda node: (
            NO_PAIR_DESCRIPTOR if node == "/dev/hidraw3" else GOOD_DESCRIPTOR
        ),
        poll_seconds=0.01,
        refuse_after_seconds=2,
    )
    await transport.open(ADDRESS, lambda d: None, lambda d: None)
    assert opened == ["/dev/hidraw4"] and transport.hidraw_nodes == 2
    await transport.close()


async def test_nodes_that_never_gain_the_pair_are_refused_after_the_grace_period():
    transport = FireTransport(
        opener=lambda p: FakeHid(),
        find_all=lambda a: ["/dev/hidraw3", "/dev/hidraw4"],
        read_descriptor=lambda node: NO_PAIR_DESCRIPTOR,
        poll_seconds=0.01,
        refuse_after_seconds=0.05,
    )
    with pytest.raises(ValueError, match="fire_voice_unsupported"):
        await transport.open(ADDRESS, lambda d: None, lambda d: None)
    assert transport.hidraw_nodes == 2


async def test_an_empty_descriptor_counts_as_unreadable_and_keeps_legacy_behavior():
    opened = []
    transport = FireTransport(
        opener=lambda path: opened.append(path) or FakeHid(),
        find_all=lambda a: ["/dev/hidraw4"],
        read_descriptor=lambda node: b"",
    )
    await transport.open(ADDRESS, lambda d: None, lambda d: None)
    assert opened == ["/dev/hidraw4"] and transport.descriptor_reports is None
    await transport.close()


# ---- runtime counters for support: what the remote actually does after a press ----------
async def test_stats_count_presses_enables_frames_toc_bytes_and_timing():
    transport, device, sink, _ = await _open()
    device.feed(VOICE_DOWN)
    for _ in range(3):
        device.feed(AUDIO)
    device.feed(VOICE_UP)
    await asyncio.sleep(0.1)
    stats = transport.stats
    assert (stats["presses"], stats["enables"], stats["frames"]) == (1, 1, 3)
    assert stats["toc"] == {"b8": 3}
    assert isinstance(stats["first_frame_ms"], int) and 0 <= stats["first_frame_ms"] < 1000
    assert isinstance(stats["max_gap_ms"], int) and 0 <= stats["max_gap_ms"] < 1000
    assert stats["rearms"] == 0
    await transport.close()


async def test_a_press_without_audio_leaves_the_frame_stats_empty():
    transport, device, sink, _ = await _open()
    device.feed(VOICE_DOWN)
    device.feed(VOICE_UP)
    await asyncio.sleep(0.05)
    assert transport.stats["presses"] == 1 and transport.stats["enables"] == 1
    assert transport.stats["frames"] == 0 and transport.stats["first_frame_ms"] is None
    await transport.close()


async def test_the_toc_histogram_is_bounded():
    transport, device, sink, _ = await _open()
    device.feed(VOICE_DOWN)
    for toc in range(40):
        device.feed(b"\xf0" + bytes([toc]) + bytes(79))
    await asyncio.sleep(0.2)
    assert len(transport.stats["toc"]) <= 8
    await transport.close()


async def test_debug_logs_follow_a_press_without_leaking_audio_or_addresses(caplog):
    with caplog.at_level("DEBUG", logger="custom_components.bt_remote_voice.fire"):
        transport, device, sink, _ = await _open()
        device.feed(VOICE_DOWN)
        device.feed(AUDIO)
        device.feed(VOICE_UP)
        await asyncio.sleep(0.1)
        await transport.close()
    text = " ".join(r.getMessage() for r in caplog.records)
    for phrase in (
        "voice key down",
        "microphone enable written",
        "first audio frame",
        "voice key up",
    ):
        assert phrase in text
    assert ADDRESS not in text and "b8" not in text.replace("first audio frame", "")


async def test_a_rearm_does_not_reset_the_first_frame_time_and_records_the_gap():
    transport, device, sink, _ = await _open(rearm_seconds=0.05)
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    device.feed(AUDIO)
    await asyncio.sleep(0.05)
    first = transport.stats["first_frame_ms"]
    assert first is not None
    await asyncio.sleep(
        0.35
    )  # frames stop while the key is held: the reader re-arms the microphone
    stats = transport.stats
    assert stats["rearms"] >= 1 and stats["enables"] == stats["rearms"] + 1  # 1 initial + re-arms
    assert transport.stats["first_frame_ms"] == first  # not overwritten by the re-arm
    device.feed(AUDIO)
    await asyncio.sleep(0.05)
    assert transport.stats["first_frame_ms"] == first
    assert transport.stats["max_gap_ms"] >= 250  # the silence that triggered the re-arm
    await transport.close()


async def test_stats_accumulate_across_presses_and_first_frame_is_per_press():
    transport, device, sink, _ = await _open()
    for _ in range(2):
        device.feed(VOICE_DOWN)
        device.feed(AUDIO)
        device.feed(VOICE_UP)
        await asyncio.sleep(0.05)
    stats = transport.stats
    assert (stats["presses"], stats["enables"], stats["frames"]) == (2, 2, 2)
    assert isinstance(stats["first_frame_ms"], int)
    await transport.close()


async def test_a_failed_enable_leaves_no_stale_first_frame_wait():
    class Refuses(FakeHid):
        def write(self, data):
            raise OSError(5, "io error")

    transport, device, sink, _ = await _open(Refuses())
    device.feed(VOICE_DOWN)
    await asyncio.wait_for(transport.lost.wait(), 1)
    assert transport._awaiting_first is False and transport.stats["enables"] == 0
    await transport.close()


async def test_a_failure_while_counting_never_drops_the_audio_frame():
    transport, device, sink, _ = await _open()

    def boom(payload, now):
        raise RuntimeError("stats bug")

    transport._count_frame = boom
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    await asyncio.sleep(0.05)
    assert sink.audio == [AUDIO[1:]] and not transport.lost.is_set()
    await transport.close()


FIXTURE_0414 = Path(__file__).parent / "fixtures" / "fire_0414_report_descriptor.hex"


async def test_the_reported_0414_descriptor_opens_and_streams_like_the_tested_remote():
    descriptor = bytes.fromhex(FIXTURE_0414.read_text().strip())
    transport, device, sink, _, opened = await _open_validated(
        ["/dev/hidraw6"], {"/dev/hidraw6": descriptor}
    )
    device.feed(VOICE_DOWN)
    device.feed(AUDIO)
    device.feed(VOICE_UP)
    await asyncio.sleep(0.05)
    assert opened == ["/dev/hidraw6"]
    assert device.writes == [b"\xf2\x01", b"\xf2\x00"] and sink.audio == [AUDIO[1:]]
    assert {r.id for r in transport.descriptor_reports} >= {0xEF, 0xF0, 0xF2}
    await transport.close()
